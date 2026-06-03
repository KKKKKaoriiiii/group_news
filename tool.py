"""群新闻工具：允许 Chatter 手动触发新闻生成。"""

from __future__ import annotations

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

logger = get_logger("group_news.tool")


class GroupNewsTool(BaseTool):
    """手动触发当前群聊的新闻摘要生成。

    仅在插件配置 allow_tool=True 时生效，
    由 Chatter 判断是否调用此工具。
    """

    tool_name: str = "generate_group_news"
    tool_description: str = "手动触发当前群聊的夸张风格新闻摘要生成"

    async def execute(self) -> tuple[bool, str]:
        """执行手动新闻生成。

        通过当前聊天流 ID 查找对应群聊的累积摘要，
        生成新闻文本并返回。不会自动发送到群聊。

        Returns:
            (是否成功, 新闻文本或错误信息)。
        """
        try:
            config = self.plugin.config
            if hasattr(config, "plugin") and hasattr(config.plugin, "allow_tool"):
                if not config.plugin.allow_tool:
                    return False, "我会定时生成新闻哦，不需要手动触发呢！"
        except Exception:
            pass

        stream_id = self.get_current_stream_id()
        service = self.plugin._news_service

        news_text = await service.generate_news_for_stream(stream_id)
        if news_text is None:
            return False, "当前群聊暂无足够的聊天记录可生成新闻"

        return True, news_text
