"""群新闻服务：收集群聊消息、整理摘要、生成新闻并发送。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.app.plugin_system.api.llm_api import (
    create_llm_request,
    get_model_set_by_name,
    get_model_set_by_task,
)
from src.app.plugin_system.api.send_api import send_image
from src.app.plugin_system.api.storage_api import delete_json, load_json, save_json
from src.app.plugin_system.base import BaseService
from src.app.plugin_system.types import ROLE, Text
from src.kernel.llm import LLMPayload, LLMRequest, LLMContextManager
from src.kernel.logger import get_logger

from .renderer import images_to_base64_list, render_news_to_images

logger = get_logger("group_news.service")

STORAGE_STORE = "group_news"
SUMMARY_KEY_PREFIX = "summary_"

SUMMARIZE_SYSTEM_PROMPT = """你是一个干练的群聊记录整理助手。请将以下群聊记录整理成简洁的摘要，保留关键事件、有趣对话、冲突八卦和重要信息。用简短条目列出即可，不需要过度展开。"""

NEWS_SYSTEM_PROMPT = """你是一个油腔滑调、极其夸张的新闻编辑，专门编造群聊圈的"假新闻"。你的任务是把群友们的日常碎碎念加工成充斥着夸张修辞和幽默讽刺的新闻稿。最终输出将排版为报纸风格图片，请严格按照以下 Markdown 格式输出：

**格式规则（严格遵守）：**
1. 第一行必须是 "# 群新闻 · YYYY年MM月DD日" 格式的主标题
2. 用 "## 爆炸标题" 格式划分每个新闻板块，标题要吸睛夸张
3. 正文直接写自然段落，段落之间用一个空行分隔
4. 用 "---" 作为板块之间的花式分隔线
5. 可以用 "> 辛辣点评" 格式插入编辑的风趣吐槽
6. 不要使用加粗、斜体、列表、代码块等其他 Markdown 语法
7. 字数不限，尽情发挥，但正文中不要复现 # 或 ## 符号（它们只用作格式标记）

**示例格式：**
# 群新闻 · 2026年6月3日

## 惊天头条：表情包大战席卷全群 参战人数突破历史纪录

昨日晚间，本群爆发了一场史无前例的"表情包大战"...

据前线记者不完全统计，群友小王单人贡献了47张表情包...

---

## 八卦速递：某群友的猫再次踩键盘 全群解码神秘代码

知情人士透露，某群友的猫于昨晚22:13分再次作案...

> 编辑点评：建议给猫配一台专属键盘，避免混淆视听。"""


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
        self._raw_buffer[gk].append(
            {
                "sender": sender_name,
                "content": content,
            }
        )

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

        chat_text = "\n".join(f"[{m['sender']}]: {m['content']}" for m in messages)
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

        await self._check_summary_overflow(group_key, store_key, updated)

    @staticmethod
    def _count_summary_entries(summary: str) -> int:
        """统计摘要中的条目数（按 --- 分隔线计数）。"""
        if not summary or not summary.strip():
            return 0
        parts = [p.strip() for p in summary.split("\n--- ") if p.strip()]
        return len(parts)

    @staticmethod
    def _trim_summary(summary: str, max_entries: int) -> str:
        """删除最旧的摘要条目直到条目数不超过 max_entries。"""
        if not summary or not summary.strip():
            return ""
        parts = [p.strip() for p in summary.split("\n--- ") if p.strip()]
        if len(parts) <= max_entries:
            return summary
        trimmed = parts[-max_entries:]
        return "\n--- ".join(trimmed)

    async def _check_summary_overflow(
        self, group_key: str, store_key: str, summary: str
    ) -> None:
        """检查摘要是否超过配置的条目上限并处理。

        Args:
            group_key: 群聊唯一标识。
            store_key: 存储键名。
            summary: 当前摘要文本。
        """
        try:
            max_entries = self.plugin.config.storage.summary_max_entries
        except Exception:
            return

        if max_entries < 1:
            return

        count = self._count_summary_entries(summary)
        if count <= max_entries:
            return

        try:
            action = self.plugin.config.storage.overflow_action
        except Exception:
            action = "trim"

        if action == "publish":
            logger.info(f"群 {group_key} 摘要已达 {count} 条（上限 {max_entries}），触发加急发布")
            await self._publish_news_for_group(group_key, summary)
            await delete_json(STORAGE_STORE, store_key)
        else:
            logger.info(f"群 {group_key} 摘要已达 {count} 条（上限 {max_entries}），裁剪最旧条目")
            trimmed = self._trim_summary(summary, max_entries)
            await save_json(STORAGE_STORE, store_key, trimmed)

    # ------------------------------------------------------------------
    # 每日新闻发布
    # ------------------------------------------------------------------

    async def publish_news_for_all_groups(self) -> None:
        """为所有有摘要的群聊生成并发布新闻。"""

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

    async def _publish_news_for_group(self, group_key: str, summary: str) -> None:
        """为单个群聊生成新闻图片并发送。

        Args:
            group_key: 群聊唯一标识。
            summary: 累积的群聊摘要文本。
        """
        model_name = self._get_model_name()

        meta = self._group_meta.get(group_key)
        if not meta:
            logger.warning(f"找不到群 {group_key} 的元信息，跳过发布")
            return

        stream_id = meta.get("stream_id", "")
        date_str = datetime.now().strftime("%Y年%m月%d日")

        user_prompt = (
            f"以下是过去一段时间群聊的摘要：\n\n{summary}\n\n"
            "请根据以上摘要，严格按照格式规则生成今天的群新闻。"
        )

        news_text = await self._call_llm(
            model_name=model_name,
            request_name="group_news_publish",
            system_prompt=NEWS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        if not news_text:
            return

        try:
            images = render_news_to_images(news_text, date_str=date_str)
        except ImportError as exc:
            logger.warning(f"缺少渲染依赖，降级为文本发送: {exc}")
            try:
                from src.app.plugin_system.api.send_api import send_text

                text_preview = news_text[:500]
                await send_text(f"【群新闻 · {date_str}】\n\n{text_preview}", stream_id=stream_id)
            except Exception:
                pass
            return
        except Exception:
            logger.exception("新闻图片渲染失败")
            return

        if not images:
            return

        base64_list = images_to_base64_list(images)
        for i, b64 in enumerate(base64_list):
            try:
                success = await send_image(b64, stream_id=stream_id)
                if not success:
                    logger.error(f"群 {group_key} 新闻图片第 {i + 1} 页发送失败")
            except Exception:
                logger.exception(f"群 {group_key} 新闻图片第 {i + 1} 页发送异常")

        logger.info(f"群 {group_key} 新闻已发布（{len(base64_list)} 张图片）")

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
            "请根据以上摘要，严格按照格式规则生成今天的群新闻。"
        )

        news_text = await self._call_llm(
            model_name=model_name,
            request_name="group_news_manual",
            system_prompt=NEWS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        if not news_text:
            return None

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
        if not model_name:
            model_name = get_model_set_by_task("actor").name
            logger.warning(f"未配置 LLM 模型，默认使用 [{model_name}] 模型")
        try:
            model_set = get_model_set_by_name(model_name)
        except Exception:
            logger.error(f"LLM 模型 [{model_name}] 不存在或无法加载")
            return ""

        try:
            context_manager = LLMContextManager()
            request: LLMRequest = create_llm_request(
                model_set, request_name=request_name, context_manager=context_manager
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
            response = await request.send(stream=False)
        except Exception:
            logger.error(f"LLM 请求发送失败 [{request_name}]")
            return ""


        content = response.message or response.reasoning_content
        if not content:
            logger.warning(f"LLM 返回空响应 [{request_name}]")
        return content

    async def _load_summary(self, store_key: str) -> str | None:
        """从持久化存储加载摘要。"""
        try:
            data = await load_json(STORAGE_STORE, store_key)
            return data if isinstance(data, str) else None
        except Exception:
            return None
