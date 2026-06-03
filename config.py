"""群新闻插件配置。"""

from __future__ import annotations

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class GroupNewsConfig(BaseConfig):
    """群新闻插件配置。"""

    config_name = "config"
    config_description = "群新闻插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件主配置。"""

        enabled: bool = Field(default=True, description="是否启用")
        allow_tool: bool = Field(
            default=False,
            description="是否允许 Chatter 直接调用生成群新闻工具（默认不允许，只能定时发布）",
        )

    @config_section("schedule")
    class ScheduleSection(SectionBase):
        """定时调度配置。"""

        publish_hour: int = Field(
            default=19,
            ge=0,
            le=23,
            description="每日发布新闻的小时（0-23）",
        )
        publish_minute: int = Field(
            default=0,
            ge=0,
            le=59,
            description="每日发布新闻的分钟（0-59）",
        )
        summarize_interval_minutes: int = Field(
            default=60,
            ge=10,
            le=1440,
            description="整理群聊记录生成摘要的间隔（分钟）",
        )

    @config_section("llm")
    class LLMSection(SectionBase):
        """大模型配置。"""

        news_model: str = Field(
            default="",
            description="生成新闻和整理摘要使用的 LLM 模型名称",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    llm: LLMSection = Field(default_factory=LLMSection)
