"""群新闻服务：收集群聊消息、整理摘要、生成新闻并发送。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_name
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.api.storage_api import delete_json, load_json, save_json
from src.app.plugin_system.base import BaseService
from src.app.plugin_system.types import ROLE, Text
from src.kernel.llm import LLMPayload, LLMRequest
from src.kernel.logger import get_logger

logger = get_logger("group_news.service")

STORAGE_STORE = "group_news"
SUMMARY_KEY_PREFIX = "summary_"
MAX_NEWS_CHARS = 1000

SUMMARIZE_SYSTEM_PROMPT = """你是一个干练的群聊记录整理助手。请将以下群聊记录整理成简洁的摘要，保留关键事件、有趣对话、冲突八卦和重要信息。用简短条目列出即可，不需要过度展开。"""

NEWS_SYSTEM_PROMPT = """你是一个油腔滑调、极其夸张的新闻编辑，专门编造群聊圈的"假新闻"。你的任务是把群友们的日常碎碎念加工成充斥着夸张修辞和幽默讽刺的新闻稿。

写作要求：
1. 必须是纯文本，不能使用任何 Markdown 格式
2. 必须包含一个吸睛的新闻标题，格式为"【群新闻】xxxx"
3. 文风油腔滑调、极其夸张，充满新闻腔调的幽默讽刺
4. 可以编造、夸大、扭曲事实
5. 把普通聊天包装成惊天大新闻的感觉
6. 总字数控制在800字以内"""


def _group_key(platform: str, group_id: str) -> str:
    """生成群聊唯一标识。"""
    return f"{platform}_{group_id}"


def _summary_store_key(group_key: str) -> str:
    """摘要存储键名。"""
    return f"{SUMMARY_KEY_PREFIX}{group_key}"


class GroupNewsService(BaseService):
    """群新闻服务。

    负责收集群聊消息、定期整理摘要、按计划发布新闻。
    """

    service_name = "group_news"
    service_description = "群新闻服务：收集群聊、生成摘要、发布夸张风格的群新闻"

    def __init__(self, plugin: Any) -> None:
        """初始化群新闻服务。

        Args:
            plugin: 所属插件实例。
        """
        super().__init__(plugin)
        self._raw_buffer: dict[str, list[dict[str, str]]] = {}
        self._group_meta: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # 消息收集
    # ------------------------------------------------------------------

    def add_message(
        self,
        platform: str,
        group_id: str,
        stream_id: str,
        sender_name: str,
        content: str,
    ) -> None:
        """添加一条群聊消息到原始缓冲区。

        Args:
            platform: 平台标识。
            group_id: 群 ID。
            stream_id: 聊天流 ID。
            sender_name: 发送者名称。
            content: 消息文本内容。
        """
        gk = _group_key(platform, group_id)
        if gk not in self._raw_buffer:
            self._raw_buffer[gk] = []
        if gk not in self._group_meta:
            self._group_meta[gk] = {
                "platform": platform,
                "group_id": group_id,
                "stream_id": stream_id,
            }
        self._raw_buffer[gk].append({
            "sender": sender_name,
            "content": content,
        })

    # ------------------------------------------------------------------
    # 定时摘要整理
    # ------------------------------------------------------------------

    async def summarize_raw_buffers(self) -> None:
        """遍历所有群聊的原始缓冲区，调用 LLM 生成增量摘要并持久化。"""
        groups = list(self._raw_buffer.keys())
        for gk in groups:
            messages = self._raw_buffer.pop(gk, [])
            if not messages:
                continue
            await self._summarize_and_append(gk, messages)

    async def _summarize_and_append(
        self, group_key: str, messages: list[dict[str, str]]
    ) -> None:
        """将一批消息整理成增量摘要，追加到持久化存储中。

        Args:
            group_key: 群聊唯一标识。
            messages: 待整理的消息列表。
        """
        model_name = self._get_model_name()
        if not model_name:
            logger.warning("未配置 LLM 模型，跳过摘要整理")
            return

        chat_text = "\n".join(
            f"[{m['sender']}]: {m['content']}" for m in messages
        )
        user_prompt = f"以下是过去一段时间内的群聊记录：\n\n{chat_text}\n\n请将这些记录整理成简洁的摘要。"

        summary_text = await self._call_llm(
            model_name=model_name,
            request_name="group_news_summarize",
            system_prompt=SUMMARIZE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        if not summary_text:
            return

        store_key = _summary_store_key(group_key)
        existing = await self._load_summary(store_key)
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_block = f"\n--- {today} ---\n{summary_text.strip()}\n"
        updated = (existing or "") + new_block
        await save_json(STORAGE_STORE, store_key, updated)
        logger.info(f"群 {group_key} 摘要已更新")

    # ------------------------------------------------------------------
    # 每日新闻发布
    # ------------------------------------------------------------------

    async def publish_news_for_all_groups(self) -> None:
        """为所有有摘要的群聊生成并发布新闻。"""
        model_name = self._get_model_name()
        if not model_name:
            logger.warning("未配置 LLM 模型，跳过新闻发布")
            return

        # 先完成最后一次摘要整理
        await self.summarize_raw_buffers()

        # 获取所有有摘要的群
        groups_to_publish: list[tuple[str, str]] = []
        for gk in list(self._group_meta.keys()):
            store_key = _summary_store_key(gk)
            summary = await self._load_summary(store_key)
            if summary and summary.strip():
                groups_to_publish.append((gk, summary))

        if not groups_to_publish:
            logger.info("没有群聊需要发布新闻")
            return

        for gk, summary in groups_to_publish:
            await self._publish_news_for_group(gk, summary)
            # 发布后清空摘要
            store_key = _summary_store_key(gk)
            await delete_json(STORAGE_STORE, store_key)

    async def _publish_news_for_group(
        self, group_key: str, summary: str
    ) -> None:
        """为单个群聊生成新闻并发送。

        Args:
            group_key: 群聊唯一标识。
            summary: 累积的群聊摘要文本。
        """
        model_name = self._get_model_name()
        if not model_name:
            return

        meta = self._group_meta.get(group_key)
        if not meta:
            logger.warning(f"找不到群 {group_key} 的元信息，跳过发布")
            return

        stream_id = meta.get("stream_id", "")

        user_prompt = (
            f"以下是过去一段时间群聊的摘要：\n\n{summary}\n\n"
            "请根据以上摘要，生成今天的群新闻。记住要油腔滑调、极其夸张、充满新闻腔调的幽默讽刺！"
        )

        news_text = await self._call_llm(
            model_name=model_name,
            request_name="group_news_publish",
            system_prompt=NEWS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        if not news_text:
            return

        # 截断过长的新闻
        if len(news_text) > MAX_NEWS_CHARS:
            news_text = news_text[:MAX_NEWS_CHARS]

        try:
            success = await send_text(news_text, stream_id=stream_id)
            if success:
                logger.info(f"群 {group_key} 新闻已发布")
            else:
                logger.error(f"群 {group_key} 新闻发送失败")
        except Exception:
            logger.exception(f"群 {group_key} 新闻发送异常")

    # ------------------------------------------------------------------
    # 工具接口：手动触发新闻生成
    # ------------------------------------------------------------------

    async def generate_news_for_stream(self, stream_id: str) -> str | None:
        """为指定聊天流手动生成新闻文本（不自动发送）。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            生成的新闻文本，如果无法生成则返回 None。
        """
        model_name = self._get_model_name()
        if not model_name:
            return None

        gk: str | None = None
        for key, meta in self._group_meta.items():
            if meta.get("stream_id") == stream_id:
                gk = key
                break

        if gk is None:
            return None

        # 先整理该群未处理的原始消息
        messages = self._raw_buffer.pop(gk, [])
        if messages:
            await self._summarize_and_append(gk, messages)

        store_key = _summary_store_key(gk)
        summary = await self._load_summary(store_key)
        if not summary or not summary.strip():
            return None

        user_prompt = (
            f"以下是过去一段时间群聊的摘要：\n\n{summary}\n\n"
            "请根据以上摘要，生成今天的群新闻。记住要油腔滑调、极其夸张、充满新闻腔调的幽默讽刺！"
        )

        news_text = await self._call_llm(
            model_name=model_name,
            request_name="group_news_manual",
            system_prompt=NEWS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        if not news_text:
            return None

        if len(news_text) > MAX_NEWS_CHARS:
            news_text = news_text[:MAX_NEWS_CHARS]

        return news_text

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get_model_name(self) -> str:
        """获取配置的 LLM 模型名称。"""
        try:
            return self.plugin.config.llm.news_model
        except Exception:
            return ""

    async def _call_llm(
        self,
        model_name: str,
        request_name: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """调用 LLM 并返回文本结果。

        Args:
            model_name: 模型名称。
            request_name: 请求标识名。
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。

        Returns:
            LLM 响应的文本内容，失败时返回空字符串。
        """
        try:
            model_set = get_model_set_by_name(model_name)
            request: LLMRequest = create_llm_request(
                model_set, request_name=request_name
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
            response = await request.send()
            return response.message or ""
        except Exception:
            logger.exception(f"LLM 调用失败 [{request_name}]")
            return ""

    async def _load_summary(self, store_key: str) -> str | None:
        """从持久化存储加载摘要。"""
        try:
            data = await load_json(STORAGE_STORE, store_key)
            return data if isinstance(data, str) else None
        except Exception:
            return None
