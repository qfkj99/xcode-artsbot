"""群发通知：给「群配置」里的群批量发送文字 / 图片，可选 @全体成员。

QQ 官方机器人的限制（见 README）：
- 群里主动发消息：群主需开启「机器人主动在群聊内发言」，且机器人要有群主动消息权限；
- 频控：未认证机器人 30 条/分钟，所以按「条」控制发送间隔；
- @全体成员：机器人必须是群管理员，每个群每天只能 1 次，且只能用于纯文本消息，
  所以「@全体 + 图片」会拆成两条发送。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import MessageType
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.platform.message_session import MessageSession

AT_ALL_TAG = "<qqbot-at-everyone />"
MAX_TEXT_LENGTH = 2000
MAX_TARGETS = 200
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_HISTORY = 100
ORPHAN_IMAGE_TTL_SECONDS = 24 * 3600
BEIJING_TZ = timezone(timedelta(hours=8))

IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif)$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif"}

# 一个群的待发送内容由 1~2 个「部分」组成：
#   text        文字（可带 @全体成员）
#   image       图片
#   text+image  文字和图片在同一条消息里（不 @全体成员时）
PART_TEXT = "text"
PART_IMAGE = "image"
PART_TEXT_IMAGE = "text+image"

TargetResolver = Callable[[], Awaitable[list[dict]]]


class BroadcastError(Exception):
    """发送单条消息失败，消息即展示给管理员的原因。"""


def _today() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def _detect_image_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


def friendly_error(err: BaseException) -> str:
    """把 QQ 接口 / AstrBot 抛出的异常转成给管理员看的说明。"""
    if isinstance(err, BroadcastError):
        return str(err)
    message = str(getattr(err, "msgs", "") or err).strip() or err.__class__.__name__
    hint = ""
    if "无权限" in message and "主动" in message:
        hint = "（请确认群主已开启「机器人主动在群聊内发言」，并且机器人在 QQ 开放平台有群主动消息权限）"
    elif "全体" in message or "everyone" in message.lower():
        hint = "（@全体成员 需要机器人是群管理员，并且每个群每天只能 @全体成员 1 次）"
    return f"{message}{hint}"[:300]


def _target_state(target: dict) -> str:
    statuses = [p["status"] for p in target["parts"]]
    if statuses and all(s == "ok" for s in statuses):
        return "ok"
    if "sending" in statuses:
        return "sending"
    if "pending" in statuses:
        return "pending"
    if "ok" in statuses:
        return "partial"
    if "cancelled" in statuses and "failed" not in statuses:
        return "cancelled"
    return "failed"


def _job_summary(job: dict) -> dict:
    states = [_target_state(t) for t in job["targets"]]
    return {
        "id": job["id"],
        "created_at": job["created_at"],
        "finished_at": job.get("finished_at"),
        "operator": job.get("operator") or "",
        "is_test": job.get("is_test", False),
        "status": job["status"],
        "text": job["text"][:80],
        "has_image": bool(job.get("image")),
        "at_all": job.get("at_all", False),
        "total": len(states),
        "ok": states.count("ok"),
        "failed": sum(s in ("failed", "partial") for s in states),
        "cancelled": states.count("cancelled"),
        "pending": sum(s in ("pending", "sending") for s in states),
    }


def _job_view(job: dict) -> dict:
    view = dict(job)
    view["summary"] = _job_summary(job)
    view["targets"] = [{**t, "state": _target_state(t)} for t in job["targets"]]
    return view


class BroadcastService:
    def __init__(
        self,
        context: Any,
        config: Any,
        data_dir: Path,
        resolve_targets: TargetResolver,
    ) -> None:
        self._context = context
        self._config = config
        self._dir = data_dir
        self._images_dir = data_dir / "broadcast_images"
        self._history_path = data_dir / "broadcast_history.json"
        self._at_all_path = data_dir / "at_all_usage.json"
        self._resolve_targets = resolve_targets

        self._history: list[dict] = self._load_history()
        self._at_all_usage: dict[str, str] = self._load_json(self._at_all_path, {})
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        self._last_send_at = 0.0
        self._cleanup_images()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            logger.warning(f"[Xcode] 读取 {path.name} 失败：{e}")
            return default
        return data if isinstance(data, type(default)) else default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning(f"[Xcode] 保存 {path.name} 失败：{e}")

    def _load_history(self) -> list[dict]:
        history = [
            j
            for j in self._load_json(self._history_path, [])
            if isinstance(j, dict) and JOB_ID_RE.match(str(j.get("id", "")))
        ]
        changed = False
        for job in history:
            if job.get("status") == "running":
                # AstrBot 重启或插件重载时任务被打断；不自动续发，避免意外重复发送
                job["status"] = "interrupted"
                job["finished_at"] = job.get("finished_at") or int(time.time())
                for target in job.get("targets", []):
                    for part in target.get("parts", []):
                        if part.get("status") in ("pending", "sending"):
                            part["status"] = "cancelled"
                            part["error"] = "任务中断（AstrBot 重启或插件重载），未发送"
                changed = True
        if changed:
            self._write_json(self._history_path, history)
        return history

    def _save_history(self) -> None:
        del self._history[MAX_HISTORY:]
        self._write_json(self._history_path, self._history)

    def _cleanup_images(self) -> None:
        """删除不再被任何群发记录引用、且超过一天的图片。"""
        if not self._images_dir.is_dir():
            return
        referenced = {
            (j.get("image") or {}).get("id") for j in self._history if j.get("image")
        }
        now = time.time()
        for path in self._images_dir.iterdir():
            try:
                if (
                    path.name not in referenced
                    and now - path.stat().st_mtime > ORPHAN_IMAGE_TTL_SECONDS
                ):
                    path.unlink()
            except OSError:
                continue

    # ------------------------------------------------------------------
    # @全体成员 每日额度（每个群每天 1 次）
    # ------------------------------------------------------------------

    @staticmethod
    def _group_key(target: dict) -> str:
        return f"{target['platform_id']}:{target['group_id']}"

    def _at_all_used_today(self, target: dict) -> bool:
        if not target.get("platform_id") or not target.get("group_id"):
            return False
        return self._at_all_usage.get(self._group_key(target)) == _today()

    def _mark_at_all_used(self, target: dict) -> None:
        today = _today()
        self._at_all_usage = {k: v for k, v in self._at_all_usage.items() if v == today}
        self._at_all_usage[self._group_key(target)] = today
        self._write_json(self._at_all_path, self._at_all_usage)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _interval(self) -> float:
        try:
            value = float(self._config.get("broadcast_interval_seconds", 2.5))
        except (TypeError, ValueError):
            value = 2.5
        return min(60.0, max(1.0, value))

    def _is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _find_job(self, job_id: str) -> dict | None:
        return next((j for j in self._history if j["id"] == job_id), None)

    async def _target_views(self) -> list[dict]:
        targets = await self._resolve_targets()
        return [
            {
                "session": t["session"],
                "name": t["name"],
                "enabled": t["enabled"],
                "sendable": bool(t.get("platform_id") and t.get("group_id")),
                "at_all_used_today": self._at_all_used_today(t),
            }
            for t in targets
        ]

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------

    async def api_state(self):
        latest = self._history[0] if self._history else None
        return json_response(
            {
                "targets": await self._target_views(),
                "job": _job_view(latest) if latest else None,
                "running": self._is_running(),
                "history": [_job_summary(j) for j in self._history],
                "interval": self._interval(),
                "max_text_length": MAX_TEXT_LENGTH,
                "max_image_bytes": MAX_IMAGE_BYTES,
            }
        )

    async def api_job(self, job_id: str):
        job = self._find_job(job_id) if JOB_ID_RE.match(job_id or "") else None
        if job is None:
            return error_response("找不到这条群发记录", status_code=404)
        return json_response({"job": _job_view(job), "running": self._is_running()})

    async def api_upload(self):
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("没有收到图片文件")
        data = await upload.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return error_response(f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
        ext = _detect_image_type(data[:16])
        if ext is None:
            return error_response("只支持 PNG、JPG、GIF 格式的图片")
        image_id = f"{uuid.uuid4().hex}.{ext}"
        path = self._images_dir / image_id
        try:
            self._images_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:
            logger.error(f"[Xcode] 保存群发图片失败：{e}")
            return error_response("保存图片失败", status_code=500)
        name = Path(str(upload.filename or "image")).name[:100]
        return json_response(
            {"image": {"id": image_id, "name": name, "size": len(data)}}
        )

    async def api_image(self, image_id: str):
        """返回图片的 data URL，用于「复用」历史内容时预览。"""
        if not IMAGE_ID_RE.match(image_id or ""):
            return error_response("图片 ID 无效")
        path = self._images_dir / image_id
        try:
            data = path.read_bytes()
        except OSError:
            return error_response("图片已被清理，请重新上传", status_code=404)
        mime = IMAGE_MIME[image_id.rsplit(".", 1)[1]]
        encoded = base64.b64encode(data).decode()
        return json_response({"data_url": f"data:{mime};base64,{encoded}"})

    async def api_send(self):
        if self._is_running():
            return error_response(
                "已有群发任务正在进行，请等它结束或先停止", status_code=409
            )
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式错误")

        text = str(payload.get("text") or "").strip()
        if len(text) > MAX_TEXT_LENGTH:
            return error_response(f"文字不能超过 {MAX_TEXT_LENGTH} 个字")
        at_all = payload.get("at_all", False)
        is_test = payload.get("test", False)
        if not isinstance(at_all, bool) or not isinstance(is_test, bool):
            return error_response("请求格式错误：at_all / test 必须是布尔值")

        image = None
        image_id = payload.get("image_id")
        if image_id:
            if not isinstance(image_id, str) or not IMAGE_ID_RE.match(image_id):
                return error_response("图片 ID 无效")
            if not (self._images_dir / image_id).is_file():
                return error_response("图片已失效，请重新上传")
            image = {"id": image_id, "name": str(payload.get("image_name") or "")[:100]}
        if not text and not image:
            return error_response("请填写通知内容或添加图片")

        sessions = payload.get("targets")
        if not isinstance(sessions, list) or not sessions:
            return error_response("请至少选择一个群")
        if len(sessions) > MAX_TARGETS:
            return error_response(f"一次最多发送 {MAX_TARGETS} 个群")
        if is_test and len(sessions) != 1:
            return error_response("测试发送只能选择一个群")
        known = {t["session"]: t for t in await self._resolve_targets()}
        targets: list[dict] = []
        for session in dict.fromkeys(sessions):
            target = known.get(session) if isinstance(session, str) else None
            if target is None:
                return error_response("只能发送给「群配置」里的群，请刷新页面后重试")
            targets.append(target)

        job = self._new_job(text, image, at_all, targets, is_test)
        self._history.insert(0, job)
        self._save_history()
        self._start(job)
        logger.info(
            f"[Xcode] {job['operator'] or '管理员'} 发起{'测试' if is_test else ''}群发："
            f"{len(targets)} 个群，@全体成员={at_all}，图片={'有' if image else '无'}"
        )
        return json_response({"job": _job_view(job), "running": True})

    async def api_retry(self):
        if self._is_running():
            return error_response(
                "已有群发任务正在进行，请等它结束或先停止", status_code=409
            )
        payload = await request.json(default={})
        job_id = str(payload.get("job_id") or "") if isinstance(payload, dict) else ""
        source = self._find_job(job_id) if JOB_ID_RE.match(job_id) else None
        if source is None:
            return error_response("找不到这条群发记录", status_code=404)
        if (
            source.get("image")
            and not (self._images_dir / source["image"]["id"]).is_file()
        ):
            return error_response("原图片已被清理，无法重试，请重新发送")

        targets = []
        for target in source["targets"]:
            parts = [
                {**p, "status": "pending", "error": ""}
                for p in target["parts"]
                if p["status"] in ("failed", "cancelled")
            ]
            if parts:
                targets.append({**target, "parts": parts})
        if not targets:
            return error_response("这次群发没有失败的群，不需要重试")

        job = self._job_shell(
            source["text"], source.get("image"), source.get("at_all", False)
        )
        job["retry_of"] = source["id"]
        job["targets"] = targets
        self._history.insert(0, job)
        self._save_history()
        self._start(job)
        logger.info(f"[Xcode] 重试群发 {source['id']} 中失败的 {len(targets)} 个群")
        return json_response({"job": _job_view(job), "running": True})

    async def api_cancel(self):
        if not self._is_running():
            return error_response("当前没有正在进行的群发任务")
        self._cancel_requested = True
        return json_response({"cancelling": True})

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    def _job_shell(self, text: str, image: dict | None, at_all: bool) -> dict:
        return {
            "id": uuid.uuid4().hex[:12],
            "created_at": int(time.time()),
            "finished_at": None,
            "operator": str(getattr(request, "username", "") or ""),
            "is_test": False,
            "retry_of": None,
            "status": "running",
            "text": text,
            "image": image,
            "at_all": at_all,
            "interval": self._interval(),
            "targets": [],
        }

    def _new_job(
        self,
        text: str,
        image: dict | None,
        at_all: bool,
        targets: list[dict],
        is_test: bool,
    ) -> dict:
        job = self._job_shell(text, image, at_all)
        job["is_test"] = is_test
        for target in targets:
            sendable = bool(target.get("platform_id") and target.get("group_id"))
            use_at_all = at_all and sendable and not self._at_all_used_today(target)
            if use_at_all:
                # @全体成员只能用在纯文本消息里，图片单独发一条
                parts = [{"kind": PART_TEXT, "at_all": True}]
                if image:
                    parts.append({"kind": PART_IMAGE, "at_all": False})
            elif text and image:
                parts = [{"kind": PART_TEXT_IMAGE, "at_all": False}]
            else:
                parts = [{"kind": PART_TEXT if text else PART_IMAGE, "at_all": False}]
            for part in parts:
                part.update(status="pending", error="")
            if not sendable:
                for part in parts:
                    part.update(
                        status="failed",
                        error="无法确定这个群属于哪个机器人，请在「群配置」里移除后重新添加",
                    )
            job["targets"].append(
                {
                    "session": target["session"],
                    "name": target["name"],
                    "platform_id": target.get("platform_id") or "",
                    "group_id": target.get("group_id") or "",
                    "at_all_skipped": at_all and sendable and not use_at_all,
                    "parts": parts,
                }
            )
        return job

    def _start(self, job: dict) -> None:
        self._cancel_requested = False
        self._task = asyncio.get_running_loop().create_task(self._run(job))

    async def _run(self, job: dict) -> None:
        try:
            for target in job["targets"]:
                for part in target["parts"]:
                    if part["status"] != "pending":
                        continue
                    if self._cancel_requested:
                        part.update(status="cancelled", error="管理员停止了发送")
                        continue
                    await self._pace()
                    if self._cancel_requested:
                        part.update(status="cancelled", error="管理员停止了发送")
                        continue
                    part["status"] = "sending"
                    try:
                        await self._send_part(job, target, part)
                    except Exception as e:  # 每个群单独记录结果，继续发下一个
                        part.update(status="failed", error=friendly_error(e))
                        logger.warning(
                            f"[Xcode] 群发到「{target['name'] or target['group_id']}」失败："
                            f"{part['error']}"
                        )
                    else:
                        part.update(status="ok", error="")
                        if part.get("at_all"):
                            self._mark_at_all_used(target)
                    self._save_history()
            job["status"] = "cancelled" if self._cancel_requested else "done"
        except asyncio.CancelledError:
            job["status"] = "interrupted"
            for target in job["targets"]:
                for part in target["parts"]:
                    if part["status"] in ("pending", "sending"):
                        part.update(
                            status="cancelled", error="任务中断（插件重载），未发送"
                        )
            raise
        finally:
            job["finished_at"] = int(time.time())
            self._save_history()
            summary = _job_summary(job)
            logger.info(
                f"[Xcode] 群发 {job['id']} 结束：成功 {summary['ok']}，"
                f"失败 {summary['failed']}，取消 {summary['cancelled']}"
            )

    async def _pace(self) -> None:
        """按「条」控制发送间隔，避开 QQ 主动消息的频控。"""
        wait = self._interval() - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send_at = time.monotonic()

    async def _send_part(self, job: dict, target: dict, part: dict) -> None:
        platform = self._context.get_platform_inst(target["platform_id"])
        if platform is None:
            raise BroadcastError("找不到对应的机器人（可能已删除或未启用）")
        # AstrBot 重启后会忘记哪些会话是群，不告诉它的话主动消息会被静默跳过
        remember = getattr(platform, "remember_session_scene", None)
        if callable(remember):
            remember(target["group_id"], "group")

        chain: list = []
        if part["kind"] in (PART_TEXT, PART_TEXT_IMAGE):
            content = job["text"]
            if part.get("at_all"):
                content = f"{AT_ALL_TAG}\n{content}" if content else AT_ALL_TAG
            if content:
                chain.append(Comp.Plain(content))
        if part["kind"] in (PART_IMAGE, PART_TEXT_IMAGE):
            image_path = self._images_dir / job["image"]["id"]
            if not image_path.is_file():
                raise BroadcastError("图片已被清理，请重新上传后发送")
            chain.append(Comp.Image.fromFileSystem(str(image_path)))

        session = MessageSession(
            target["platform_id"], MessageType.GROUP_MESSAGE, target["group_id"]
        )
        await platform.send_by_session(
            session, MessageChain(chain=chain).use_markdown(False)
        )

    async def terminate(self) -> None:
        if self._is_running():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
