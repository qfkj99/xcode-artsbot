"""Xcode 分组监控插件。

群内有人发送配置的状态关键词（完全匹配、无需 @）时，请求 Xcode 站点的
分组监控快照接口，回复监控截图和可选的文字摘要。
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

DEFAULT_API_URL = "https://xcodebest.com/api/group-monitor/snapshot/details"
DEFAULT_FAIL_MESSAGE = "状态查询失败，请稍后再试。"
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
    """只有命中关键词且群允许时才激活处理器，其他群消息完全不受影响。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        plugin = _plugin_instance
        return plugin is not None and plugin.should_handle(event)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class XcodePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 群 ID -> 上次触发时间（time.monotonic）
        self._last_trigger: dict[str, float] = {}

        global _plugin_instance
        _plugin_instance = self

    # ------------------------------------------------------------------
    # 触发判断
    # ------------------------------------------------------------------

    def should_handle(self, event: AstrMessageEvent) -> bool:
        """每条消息都会调用，所以先做最便宜的判断。"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        if event.get_message_str().strip() not in self._keywords():
            return False

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return False
        if self._is_group_allowed(group_id):
            return True

        logger.info(
            f"[Xcode] 群 {group_id} 发送了状态关键词，但该群不在白名单中，已忽略。"
            "如需启用，请把这个群 ID 加到插件配置的「群配置」里。"
        )
        return False

    def _keywords(self) -> set[str]:
        keywords = self.config.get("keywords") or []
        return {str(k).strip() for k in keywords if str(k).strip()}

    def _group_entry(self, group_id: str) -> dict | None:
        for item in self.config.get("groups") or []:
            if (
                isinstance(item, dict)
                and str(item.get("group_id", "")).strip() == group_id
            ):
                return item
        return None

    def _is_group_allowed(self, group_id: str) -> bool:
        entry = self._group_entry(group_id)
        if entry is not None and not entry.get("enabled", True):
            return False
        if not self.config.get("only_whitelisted_groups", True):
            return True
        return entry is not None

    def _cooldown_seconds(self, group_id: str) -> int:
        entry = self._group_entry(group_id)
        if entry is not None:
            return max(0, _as_int(entry.get("cooldown_seconds"), 5))
        return max(0, _as_int(self.config.get("default_cooldown_seconds"), 5))

    def _in_cooldown(self, group_id: str) -> bool:
        """检查冷却；不在冷却中则立即记录本次触发，防止并发重复请求。"""
        now = time.monotonic()
        last = self._last_trigger.get(group_id)
        if last is not None and now - last < self._cooldown_seconds(group_id):
            return True
        self._last_trigger[group_id] = now
        return False

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    @filter.custom_filter(StatusKeywordFilter)
    async def on_status_keyword(self, event: AstrMessageEvent):
        """群内发送状态关键词时，回复分组监控快照截图。"""
        group_id = str(event.get_group_id() or "").strip()
        if self._in_cooldown(group_id):
            logger.debug(f"[Xcode] 群 {group_id} 处于冷却中，忽略本次触发。")
            event.stop_event()
            return

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

    async def terminate(self):
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None
