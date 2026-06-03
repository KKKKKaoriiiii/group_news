"""群新闻事件处理器：收集群聊消息。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event.core import EventDecision
from src.kernel.logger import get_logger

logger = get_logger("group_news.collector")


class GroupNewsCollector(BaseEventHandler):
    """收集群聊消息用于后续生成群新闻。

    订阅 ON_MESSAGE_RECEIVED 事件，过滤群聊消息，将消息内容
    交给 GroupNewsService 的原始缓冲区。
    """

    handler_name: str = "group_news_collector"
    handler_description: str = "收集群聊消息用于生成群新闻"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息接收事件，收集群聊文本消息。

        Args:
            event_name: 事件名称。
            params: 事件参数，包含 message 对象。

        Returns:
            (EventDecision, 更新后的 params)。
        """
        if event_name != EventType.ON_MESSAGE_RECEIVED.value:
            return EventDecision.PASS, params

        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params

        if message.chat_type != "group":
            return EventDecision.PASS, params

        content = message.processed_plain_text
        if isinstance(content, str):
            content = content.strip()
        if not content:
            content = str(getattr(message, "content", "")).strip()
        if not content:
            return EventDecision.PASS, params

        group_id = ""
        extra = getattr(message, "extra", None)
        if isinstance(extra, dict):
            group_id = str(extra.get("group_id", ""))

        platform = getattr(message, "platform", "") or ""
        stream_id = getattr(message, "stream_id", "") or ""
        sender_name = getattr(message, "sender_name", "") or "匿名群友"

        try:
            service = self.plugin._news_service
            service.add_message(
                platform=platform,
                group_id=group_id,
                stream_id=stream_id,
                sender_name=sender_name,
                content=content,
            )
        except Exception:
            logger.exception("收集群聊消息失败")

        return EventDecision.SUCCESS, params
