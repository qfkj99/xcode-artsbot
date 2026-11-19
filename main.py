"""Xcode 分组监控插件。

群内有人发送配置的状态关键词（完全匹配、无需 @）时，请求 Xcode 站点的
分组监控快照接口，回复监控截图和可选的文字摘要。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .broadcast import BroadcastService

# 与 metadata.yaml 的 name 一致。WebUI 调用插件接口时用的是这个名字，与安装目录名无关。
PLUGIN_NAME = "astrbot_plugin_xcode"
GROUP_TEMPLATE_KEY = "group"
MAX_GROUPS = 200
MAX_COOLDOWN_SECONDS = 86400
MAX_SEEN_GROUPS = 2000
SEEN_SAVE_DELAY_SECONDS = 3

DEFAULT_API_URL = "https://xcodebest.com/api/group-monitor/snapshot/details"
DEFAULT_FAIL_MESSAGE = "状态查询失败，请稍后再试。"
# 处理器优先级：必须高于 AstrBot 内置「群聊上下文感知 / 主动回复」（sys.maxsize - 2）
# 和「空 @ 等待」（sys.maxsize - 1），这样关键词消息会先由本插件处理并 stop_event，
# 不会再被主动回复拿去调用大模型。过滤器只匹配完全等于关键词的群消息，不影响其他消息。
HANDLER_PRIORITY = sys.maxsize
BEIJING_TZ = timezone(timedelta(hours=8))

# 状态 -> (图标, 名称)。计数行按这个顺序展示。
STATUS_META: dict[str, tuple[str, str]] = {
    "up": ("✅", "正常"),
    "degraded": ("⚠️", "降级"),
    "down": ("❌", "异常"),
    "pending": ("⏳", "暂无数据"),
}
# 需要列出具体分组名的状态，按严重程度排序。
LISTED_STATUSES = ("down", "degraded", "pending")

# 关键词过滤器在类定义阶段就被实例化，拿不到插件配置，
# 所以通过这个引用读取当前插件实例上的配置。
_plugin_instance: XcodePlugin | None = None


class SnapshotError(Exception):
    """快照接口请求或解析失败。"""


class StatusKeywordFilter(filter.CustomFilter):
    """群消息与关键词完全一致时才激活处理器，其他消息完全不受影响。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        plugin = _plugin_instance
        return plugin is not None and plugin.is_status_keyword(event)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_group_session(value: Any) -> tuple[str, str] | None:
    """解析 ``<平台ID>:GroupMessage:<会话ID>``；不是群聊会话时返回 None。"""
    platform_id, sep1, rest = str(value or "").strip().partition(":")
    message_type, sep2, session_id = rest.partition(":")
    if not (sep1 and sep2 and platform_id and session_id):
        return None
    if message_type != MessageType.GROUP_MESSAGE.value:
        return None
    return platform_id, session_id


def _session_points_to_group(value: Any, platform_id: str, group_id: str) -> bool:
    """判断群配置里保存的会话是否指向指定机器人上的指定群。

    支持三种写法：
    - 群会话：``<平台ID>:GroupMessage:<群ID>``
    - 开启会话隔离时的会话：``<平台ID>:GroupMessage:<用户ID>_<群ID>``，
      选中群里任意一个人的会话都视为选中这个群
    - 直接填写的群 ID（兼容手动填写）
    """
    value = str(value or "").strip()
    if not value or not group_id:
        return False
    if ":" not in value:
        return value == group_id
    parsed = _parse_group_session(value)
    if parsed is None or parsed[0] != platform_id:
        return False
    session_id = parsed[1]
    return session_id == group_id or session_id.endswith(f"_{group_id}")


def _session_matches(value: Any, event: AstrMessageEvent) -> bool:
    """判断群配置里保存的会话是否就是当前消息所在的群。"""
    return _session_points_to_group(
        value,
        str(event.get_platform_id() or ""),
        str(event.get_group_id() or "").strip(),
    )


class XcodePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # "<平台ID>:<群ID>" -> 上次触发时间（time.monotonic）
        self._last_trigger: dict[str, float] = {}
        # 插件见过的群："<平台ID>:GroupMessage:<群ID>" -> {"name": 群名}。
        # 供「群配置」页面按群名选择，持久化在 data/plugin_data/<插件名>/ 下。
        self._seen_groups: dict[str, dict[str, str]] = self._load_seen_groups()
        self._seen_save_task: asyncio.Task | None = None
        # 「群发通知」页面（views/broadcast）的后端
        self._broadcast = BroadcastService(
            context,
            config,
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME,
            self._broadcast_targets,
        )

        # 「群配置」「群发通知」页面调用的接口。WebUI 需要登录，只有管理员能调用。
        bc = self._broadcast
        for route, handler, methods, desc in (
            ("groups", self.api_get_groups, ["GET"], "获取 Xcode 群配置"),
            ("groups", self.api_save_groups, ["POST"], "保存 Xcode 群配置"),
            ("group-candidates", self.api_group_candidates, ["GET"], "可添加的群"),
            ("broadcast/state", bc.api_state, ["GET"], "群发通知：状态"),
            ("broadcast/jobs/<job_id>", bc.api_job, ["GET"], "群发通知：任务进度"),
            ("broadcast/upload", bc.api_upload, ["POST"], "群发通知：上传图片"),
            ("broadcast/images/<image_id>", bc.api_image, ["GET"], "群发通知：图片"),
            ("broadcast/send", bc.api_send, ["POST"], "群发通知：发送"),
            ("broadcast/retry", bc.api_retry, ["POST"], "群发通知：重试失败的群"),
            ("broadcast/cancel", bc.api_cancel, ["POST"], "群发通知：停止发送"),
        ):
            context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)

        global _plugin_instance
        _plugin_instance = self

    # ------------------------------------------------------------------
    # 触发判断
    # ------------------------------------------------------------------

    def is_status_keyword(self, event: AstrMessageEvent) -> bool:
        """每条消息都会调用，所以只做最便宜的判断：群消息 + 关键词完全一致。

        顺带记下见过的群和群名（只在新群或群名变化时写盘），供群配置页面选择。
        """
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return False
        try:
            self._remember_group(event, group_id)
        except Exception as e:  # 记录失败不能影响正常触发
            logger.debug(f"[Xcode] 记录群信息失败：{e}")
        return event.get_message_str().strip() in self._keywords()

    def _keywords(self) -> set[str]:
        keywords = self.config.get("keywords") or []
        return {str(k).strip() for k in keywords if str(k).strip()}

    def _group_entry(self, event: AstrMessageEvent) -> dict | None:
        for item in self.config.get("groups") or []:
            if isinstance(item, dict) and _session_matches(item.get("session"), event):
                return item
        return None

    def _is_group_allowed(self, entry: dict | None) -> bool:
        if entry is not None:
            return bool(entry.get("enabled", True))
        return not self.config.get("only_whitelisted_groups", True)

    def _cooldown_seconds(self, entry: dict | None) -> int:
        if entry is not None:
            return max(0, _as_int(entry.get("cooldown_seconds"), 5))
        return max(0, _as_int(self.config.get("default_cooldown_seconds"), 5))

    def _in_cooldown(self, group_key: str, cooldown: int) -> bool:
        """检查冷却；不在冷却中则立即记录本次触发，防止并发重复请求。"""
        now = time.monotonic()
        last = self._last_trigger.get(group_key)
        if last is not None and now - last < cooldown:
            return True
        self._last_trigger[group_key] = now
        return False

    @staticmethod
    def _log_not_allowed(event: AstrMessageEvent, entry: dict | None) -> None:
        group = getattr(event.message_obj, "group", None)
        group_name = getattr(group, "group_name", None)
        label = f"「{group_name}」" if group_name else f" {event.get_group_id()}"
        if entry is not None:
            logger.debug(f"[Xcode] 群{label} 已在群配置中停用，忽略本次触发。")
            return
        logger.info(
            f"[Xcode] 群{label} 发送了状态关键词，但不在白名单中，已忽略。"
            "在插件详情页的「群配置」页面点「添加群」即可选到这个群；"
            f"也可以手动输入会话 ID：{event.unified_msg_origin}"
        )

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    @filter.custom_filter(StatusKeywordFilter, priority=HANDLER_PRIORITY)
    async def on_status_keyword(self, event: AstrMessageEvent):
        """群内发送状态关键词时，回复分组监控快照截图。"""
        entry = self._group_entry(event)
        if not self._is_group_allowed(entry):
            self._log_not_allowed(event, entry)
            # 不回复也不拦截。处理器被激活后 AstrBot 会记录这个群会话，
            # 之后在插件配置的会话选择器里就能直接选到它。
            return

        group_key = f"{event.get_platform_id()}:{event.get_group_id()}"
        if self._in_cooldown(group_key, self._cooldown_seconds(entry)):
            logger.debug(f"[Xcode] 群 {group_key} 处于冷却中，忽略本次触发。")
            event.stop_event()
            return

        # 先回一句提示。每次 yield 都会被 AstrBot 立即发送，再继续往下执行。
        pending_text = str(self.config.get("pending_text") or "").strip()
        if pending_text:
            yield event.plain_result(pending_text)

        try:
            data = await self._fetch_snapshot()
        except SnapshotError as e:
            logger.error(f"[Xcode] 获取分组监控快照失败：{e}")
            yield event.plain_result(self._fail_message())
            event.stop_event()
            return

        text = self._build_text(data)
        image_b64 = self._extract_image(data)

        chain: list = []
        if image_b64 is None:
            logger.warning("[Xcode] 接口未返回有效截图，仅发送文字内容。")
            text = "\n".join(t for t in (text, "⚠️ 监控截图暂时获取失败。") if t)
        if text:
            chain.append(Comp.Plain(text))
        if image_b64 is not None:
            chain.append(Comp.Image.fromBase64(image_b64))

        yield event.chain_result(chain)
        event.stop_event()

    # ------------------------------------------------------------------
    # 接口请求
    # ------------------------------------------------------------------

    async def _fetch_snapshot(self) -> dict:
        token = str(self.config.get("api_token") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[len("bearer ") :].strip()
        if not token:
            raise SnapshotError("未配置 API Token，请在插件配置中填写 api_token")

        url = str(self.config.get("api_url") or DEFAULT_API_URL).strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "New-Api-User": str(self.config.get("api_user") or "").strip(),
        }
        params = {"range": self._range()}
        timeout_s = max(1, _as_int(self.config.get("request_timeout"), 15))

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    body = await resp.text()
                    status = resp.status
        except TimeoutError as e:
            raise SnapshotError(f"请求超时（{timeout_s}s）") from e
        except aiohttp.ClientError as e:
            raise SnapshotError(f"网络错误：{e!r}") from e

        if status != 200:
            raise SnapshotError(f"HTTP {status}：{body[:200]}")

        try:
            payload = json.loads(body)
        except ValueError as e:
            raise SnapshotError(f"返回内容不是合法 JSON：{body[:200]}") from e

        if not isinstance(payload, dict):
            raise SnapshotError(f"返回格式异常：{body[:200]}")
        if not payload.get("success"):
            raise SnapshotError(
                f"接口返回失败：{payload.get('message') or '无错误信息'}"
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise SnapshotError("返回内容缺少 data 字段")
        return data

    @staticmethod
    def _extract_image(data: dict) -> str | None:
        raw = data.get("image_base64")
        if not isinstance(raw, str):
            return None
        raw = "".join(raw.split())  # 去掉可能夹带的换行/空格
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        if not raw:
            return None
        try:
            base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("[Xcode] image_base64 不是合法的 Base64 数据。")
            return None
        return raw

    # ------------------------------------------------------------------
    # 文字内容
    # ------------------------------------------------------------------

    def _range(self) -> str:
        return str(self.config.get("range") or "24h").strip() or "24h"

    def _fail_message(self) -> str:
        return (
            str(self.config.get("fail_message") or "").strip() or DEFAULT_FAIL_MESSAGE
        )

    def _build_text(self, data: dict) -> str:
        parts: list[str] = []
        header = str(self.config.get("header_text") or "").strip()
        if header:
            parts.append(header)
        if self.config.get("show_summary", True):
            parts.append(self._build_summary(data))
        footer = str(self.config.get("footer_text") or "").strip()
        if footer:
            parts.append(footer)
        return "\n".join(parts)

    def _build_summary(self, data: dict) -> str:
        lines = ["📊 分组状态"]

        groups = [g for g in data.get("groups") or [] if isinstance(g, dict)]
        if not groups:
            lines.append("暂无分组数据")
        else:
            names_by_status: dict[str, list[str]] = {}
            unknown_labels: dict[str, str] = {}
            for g in groups:
                status = str(g.get("status") or "unknown").strip().lower()
                names_by_status.setdefault(status, []).append(
                    str(g.get("group") or "未命名分组")
                )
                if status not in STATUS_META:
                    unknown_labels.setdefault(
                        status, str(g.get("status_text") or status)
                    )

            counts = [
                f"{icon} {label} {len(names_by_status.get(status, []))}"
                for status, (icon, label) in STATUS_META.items()
            ]
            counts += [
                f"❔ {label} {len(names_by_status[status])}"
                for status, label in unknown_labels.items()
            ]
            lines.append(" ｜ ".join(counts))

            problem_lines = [
                f"{STATUS_META[status][0]} {STATUS_META[status][1]}：{'、'.join(names_by_status[status])}"
                for status in LISTED_STATUSES
                if names_by_status.get(status)
            ]
            problem_lines += [
                f"❔ {label}：{'、'.join(names_by_status[status])}"
                for status, label in unknown_labels.items()
            ]
            lines += problem_lines or ["🎉 所有分组运行正常"]

        time_line = self._time_line(data)
        if time_line:
            lines.append(time_line)

        return "\n".join(lines)

    @staticmethod
    def _time_line(data: dict) -> str:
        """用接口实际返回的统计窗口展示时间（北京时间）。"""

        def to_dt(value: Any) -> datetime | None:
            if isinstance(value, (int, float)) and value > 0:
                return datetime.fromtimestamp(value, BEIJING_TZ)
            return None

        start, end = to_dt(data.get("window_start")), to_dt(data.get("window_end"))
        if start and end:
            end_fmt = "%H:%M" if start.date() == end.date() else "%m-%d %H:%M"
            return f"🕒 统计时段：{start:%m-%d %H:%M} ~ {end:{end_fmt}}"
        if end:
            return f"🕒 更新时间：{end:%Y-%m-%d %H:%M}"
        return ""

    # ------------------------------------------------------------------
    # 见过的群（群配置页面的候选列表）
    # ------------------------------------------------------------------

    @staticmethod
    def _seen_groups_path() -> Path:
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "seen_groups.json"

    def _load_seen_groups(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._seen_groups_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"[Xcode] 读取已记录的群失败，将重新记录：{e}")
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(session): {"name": str(record.get("name") or "")[:100]}
            for session, record in raw.items()
            if isinstance(record, dict) and _parse_group_session(session)
        }

    def _write_seen_groups(self) -> None:
        path = self._seen_groups_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(self._seen_groups, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning(f"[Xcode] 保存已记录的群失败：{e}")

    def _remember_group(self, event: AstrMessageEvent, group_id: str) -> None:
        session = (
            f"{event.get_platform_id()}:{MessageType.GROUP_MESSAGE.value}:{group_id}"
        )
        group = getattr(event.message_obj, "group", None)
        name = str(getattr(group, "group_name", None) or "").strip()[:100]
        record = self._seen_groups.get(session)
        if record is not None and (not name or record.get("name") == name):
            return
        if record is None and len(self._seen_groups) >= MAX_SEEN_GROUPS:
            return
        self._seen_groups[session] = {"name": name or (record or {}).get("name", "")}
        self._schedule_seen_save()

    def _schedule_seen_save(self) -> None:
        """合并短时间内的多次变化，延迟几秒再写一次盘。"""
        if self._seen_save_task is not None and not self._seen_save_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_seen_groups()
            return
        self._seen_save_task = loop.create_task(self._save_seen_later())

    async def _save_seen_later(self) -> None:
        await asyncio.sleep(SEEN_SAVE_DELAY_SECONDS)
        self._write_seen_groups()

    # ------------------------------------------------------------------
    # 群配置页面（views/groups）使用的 Web API
    # ------------------------------------------------------------------

    def _config_groups(self) -> list[dict]:
        return [
            item for item in self.config.get("groups") or [] if isinstance(item, dict)
        ]

    def _default_cooldown(self) -> int:
        return max(0, _as_int(self.config.get("default_cooldown_seconds"), 5))

    async def _alias_names(self) -> tuple[dict[str, str], dict[str, str]]:
        """读取 AstrBot 记录的会话名称：(用户设置的别名, 自动记录的群名)。"""
        try:
            aliases = await self.context.get_db().get_umo_aliases()
        except Exception as e:
            logger.warning(f"[Xcode] 读取 AstrBot 会话名称失败：{e}")
            return {}, {}
        user_aliases: dict[str, str] = {}
        auto_names: dict[str, str] = {}
        for alias in aliases or []:
            umo = str(getattr(alias, "umo", "") or "")
            if not umo:
                continue
            if user_alias := str(getattr(alias, "user_alias", "") or "").strip():
                user_aliases[umo] = user_alias
            if auto_name := str(getattr(alias, "auto_name", "") or "").strip():
                auto_names[umo] = auto_name
        return user_aliases, auto_names

    def _resolve_group_name(
        self,
        session: str,
        user_aliases: dict[str, str],
        auto_names: dict[str, str],
        fallback: str = "",
    ) -> str:
        """群名优先级：AstrBot 里设置的别名 > 插件记录的群名 > AstrBot 自动记录的群名 > 保存时的群名。"""
        if session in user_aliases:
            return user_aliases[session]
        for seen_session, record in self._seen_groups.items():
            parsed = _parse_group_session(seen_session)
            if parsed and _session_points_to_group(session, *parsed):
                name = user_aliases.get(seen_session) or record.get("name")
                if name:
                    return name
        return auto_names.get(session) or fallback

    async def _groups_payload(self) -> dict:
        user_aliases, auto_names = await self._alias_names()
        default_cooldown = self._default_cooldown()
        groups = []
        for item in self._config_groups():
            session = str(item.get("session") or "").strip()
            if not session:
                continue
            groups.append(
                {
                    "session": session,
                    "name": self._resolve_group_name(
                        session,
                        user_aliases,
                        auto_names,
                        str(item.get("remark") or "").strip(),
                    ),
                    "enabled": bool(item.get("enabled", True)),
                    "cooldown_seconds": max(
                        0, _as_int(item.get("cooldown_seconds"), default_cooldown)
                    ),
                }
            )
        return {
            "groups": groups,
            "only_whitelisted": bool(self.config.get("only_whitelisted_groups", True)),
            "default_cooldown": default_cooldown,
        }

    async def api_get_groups(self):
        return json_response(await self._groups_payload())

    def _locate_group(self, session: str) -> tuple[str, str]:
        """群配置里的会话 -> (机器人ID, 群ID)；无法确定时返回空字符串。"""
        parsed = _parse_group_session(session)
        if parsed is not None:
            platform_id, session_id = parsed
            if f"{platform_id}:{MessageType.GROUP_MESSAGE.value}:{session_id}" in (
                self._seen_groups
            ):
                return platform_id, session_id
            # 开启会话隔离时会话 ID 是「用户ID_群ID」，与 AstrBot 自身的处理一致
            return platform_id, session_id.rsplit("_", 1)[-1]
        # 直接填写的群 ID：看哪个机器人见过这个群
        for seen_session in self._seen_groups:
            seen = _parse_group_session(seen_session)
            if seen is not None and seen[1] == session:
                return seen
        return "", ""

    async def _broadcast_targets(self) -> list[dict]:
        """群发可选的群：只能是「群配置」里的群。"""
        user_aliases, auto_names = await self._alias_names()
        targets = []
        for item in self._config_groups():
            session = str(item.get("session") or "").strip()
            if not session:
                continue
            platform_id, group_id = self._locate_group(session)
            targets.append(
                {
                    "session": session,
                    "name": self._resolve_group_name(
                        session,
                        user_aliases,
                        auto_names,
                        str(item.get("remark") or "").strip(),
                    ),
                    "enabled": bool(item.get("enabled", True)),
                    "platform_id": platform_id,
                    "group_id": group_id,
                }
            )
        return targets

    async def api_group_candidates(self):
        """可添加的群：插件见过的群 + AstrBot 记录过名称的群会话。"""
        user_aliases, auto_names = await self._alias_names()
        candidates: dict[str, str] = {
            session: user_aliases.get(session)
            or record.get("name")
            or auto_names.get(session)
            or ""
            for session, record in self._seen_groups.items()
        }
        known_groups = [p for p in map(_parse_group_session, candidates) if p]
        for umo in sorted({*user_aliases, *auto_names}):
            if umo in candidates or _parse_group_session(umo) is None:
                continue
            # 开启会话隔离时产生的「用户ID_群ID」会话，和已记录的群算同一个群
            if any(_session_points_to_group(umo, *group) for group in known_groups):
                continue
            candidates[umo] = user_aliases.get(umo) or auto_names.get(umo) or ""

        configured = [
            str(i.get("session") or "").strip() for i in self._config_groups()
        ]
        items = []
        for session, name in candidates.items():
            parsed = _parse_group_session(session)
            added = any(
                value == session
                or (parsed is not None and _session_points_to_group(value, *parsed))
                for value in configured
            )
            items.append({"session": session, "name": name, "added": added})
        items.sort(key=lambda x: (x["added"], not x["name"], x["name"], x["session"]))
        return json_response({"groups": items})

    async def api_save_groups(self):
        payload = await request.json(default={})
        raw_groups = payload.get("groups") if isinstance(payload, dict) else None
        if not isinstance(raw_groups, list):
            return error_response("请求格式错误：groups 必须是列表")
        if len(raw_groups) > MAX_GROUPS:
            return error_response(f"最多只能添加 {MAX_GROUPS} 个群")

        old_remarks = {
            str(i.get("session") or "").strip(): str(i.get("remark") or "")
            for i in self._config_groups()
        }
        default_cooldown = self._default_cooldown()
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in raw_groups:
            if not isinstance(item, dict):
                return error_response("请求格式错误：群配置必须是对象")
            session = str(item.get("session") or "").strip()
            if not session or len(session) > 512 or any(c.isspace() for c in session):
                return error_response(f"会话 ID 无效：{session[:60] or '（空）'}")
            if ":" in session and _parse_group_session(session) is None:
                return error_response(f"只能添加群聊会话：{session[:60]}")
            if session in seen:
                continue
            seen.add(session)

            raw_cooldown = item.get("cooldown_seconds", default_cooldown)
            if isinstance(raw_cooldown, bool) or _as_int(raw_cooldown, -1) < 0:
                return error_response("冷却时间必须是不小于 0 的整数")
            cooldown = _as_int(raw_cooldown, default_cooldown)
            if cooldown > MAX_COOLDOWN_SECONDS:
                return error_response(
                    f"冷却时间需要在 0 ~ {MAX_COOLDOWN_SECONDS} 秒之间"
                )
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                return error_response("请求格式错误：enabled 必须是布尔值")
            name = str(item.get("name") or "").strip()
            cleaned.append(
                {
                    "__template_key": GROUP_TEMPLATE_KEY,
                    "session": session,
                    # 保存当时的群名，作为以后显示群名的兜底
                    "remark": (name or old_remarks.get(session, ""))[:100],
                    "enabled": enabled,
                    "cooldown_seconds": cooldown,
                }
            )

        self.config["groups"] = cleaned
        await self.config.save_config_async()
        logger.info(f"[Xcode] 群配置已保存，共 {len(cleaned)} 个群。")
        return json_response(await self._groups_payload())

    async def terminate(self):
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None
        if self._seen_save_task is not None and not self._seen_save_task.done():
            self._seen_save_task.cancel()
            self._write_seen_groups()
        await self._broadcast.terminate()
