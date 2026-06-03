"""群新闻（fake news generator）插件入口。

本插件定期收集群聊记录，整理成摘要，并在每日指定时间
编造一篇夸张幽默的"群内大新闻"公开发布。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager
from src.kernel.logger import get_logger

from .config import GroupNewsConfig
from .event_handler import GroupNewsCollector
from .service import GroupNewsService
from .tool import GroupNewsTool

logger = get_logger("group_news.plugin")


@register_plugin
class GroupNewsPlugin(BasePlugin):
    """群新闻插件——fake news generator。

    每天定时整理群聊记录，编造夸张幽默的"群内大新闻摘要"。
    """

    plugin_name: str = "group_news"
    plugin_description: str = "定时整理群聊记录，编造夸张幽默的群内大新闻摘要并公开发布"
    plugin_version: str = "1.0.0"

    configs: list[type] = [GroupNewsConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """返回插件组件类列表。"""
        if isinstance(self.config, GroupNewsConfig):
            if not self.config.plugin.enabled:
                logger.info("group_news 已在配置中禁用")
                return []

        components: list[type] = [GroupNewsCollector]

        if isinstance(self.config, GroupNewsConfig) and self.config.plugin.allow_tool:
            components.append(GroupNewsTool)

        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载后注册定时任务。"""
        self._news_service = GroupNewsService(self)

        self._publish_schedule_id: str | None = None
        self._summarize_schedule_id: str | None = None
        self._last_publish_date: str = ""

        # 等待 scheduler 就绪后注册任务
        tm = get_task_manager()
        tm.create_task(
            self._register_schedule_when_ready(),
            name="group_news_register_schedule",
        )

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时移除定时任务。"""
        from src.kernel.scheduler import get_unified_scheduler

        scheduler = get_unified_scheduler()
        for sid in [self._publish_schedule_id, self._summarize_schedule_id]:
            if sid:
                try:
                    await scheduler.remove_schedule(sid)
                except Exception:
                    pass

        self._publish_schedule_id = None
        self._summarize_schedule_id = None

    async def _register_schedule_when_ready(self) -> None:
        """等待 scheduler 运行后注册周期任务。"""
        from src.kernel.scheduler import TriggerType, get_unified_scheduler

        scheduler = get_unified_scheduler()

        # 等待 scheduler 启动（Bot.run() 之后才可用）
        for _ in range(600):
            try:
                # 定时摘要整理任务
                interval_sec = self.config.schedule.summarize_interval_minutes * 60
                self._summarize_schedule_id = await scheduler.create_schedule(
                    callback=self._summarize_job,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval_sec},
                    is_recurring=True,
                    task_name="group_news_summarize",
                    force_overwrite=True,
                )

                # 每日发布任务
                await self._schedule_next_publish(scheduler)
                return
            except RuntimeError:
                await asyncio.sleep(0.5)

        logger.error("scheduler 长时间未就绪，群新闻定时任务注册失败")

    async def _schedule_next_publish(
        self, scheduler: Any = None
    ) -> None:
        """计算并注册下一次新闻发布时间。

        每次发布完成后重新调用此方法以安排次日发布。
        """
        from src.kernel.scheduler import TriggerType, get_unified_scheduler

        if scheduler is None:
            scheduler = get_unified_scheduler()

        # 取消旧任务
        if self._publish_schedule_id:
            try:
                await scheduler.remove_schedule(self._publish_schedule_id)
            except Exception:
                pass

        # 计算距离下一次发布时间（秒）
        now = datetime.now()
        publish_hour = self.config.schedule.publish_hour
        publish_minute = self.config.schedule.publish_minute

        next_publish = now.replace(
            hour=publish_hour, minute=publish_minute, second=0, microsecond=0
        )
        if next_publish <= now:
            next_publish += timedelta(days=1)

        delay = (next_publish - now).total_seconds()
        delay = max(delay, 1.0)

        self._publish_schedule_id = await scheduler.create_schedule(
            callback=self._publish_job,
            trigger_type=TriggerType.TIME,
            trigger_config={"delay_seconds": delay},
            is_recurring=False,
            task_name="group_news_publish",
            force_overwrite=True,
        )
        logger.info(
            f"下次群新闻发布时间: {next_publish.strftime('%Y-%m-%d %H:%M:%S')}（{delay:.0f} 秒后）"
        )

    async def _summarize_job(self) -> None:
        """定时摘要整理任务回调。"""
        if not self.config.plugin.enabled:
            return

        try:
            await self._news_service.summarize_raw_buffers()
        except Exception:
            logger.exception("摘要整理任务异常")

    async def _publish_job(self) -> None:
        """每日新闻发布任务回调。"""
        if not self.config.plugin.enabled:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_publish_date == today:
            # 今天已发布过，重新安排下次
            await self._schedule_next_publish()
            return

        try:
            await self._news_service.publish_news_for_all_groups()
        except Exception:
            logger.exception("新闻发布任务异常")

        self._last_publish_date = today
        await self._schedule_next_publish()
