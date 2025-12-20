import asyncio
from typing import Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools

from .domain import PromptResolver
from .providers import ProviderManager
from .services import ConfigService, StatsService, ResourceService, GenerationService
from .handlers import ManagementHandler, WorkflowHandler
from .utils import CommandParser

class Ninjutsu(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_dir = StarTools.get_data_dir()

        # 同步
        self.prompt_resolver = PromptResolver()
        self.provider_mgr = ProviderManager()
        self.config_service = ConfigService(self.conf, self.context)
        self.stats_service = StatsService(self.data_dir, self.conf)

        # async
        self.resource_service: Optional[ResourceService] = None
        self.generation_service: Optional[GenerationService] = None
        self.mgmt_handler: Optional[ManagementHandler] = None
        self.workflow_handler: Optional[WorkflowHandler] = None

        # 状态标记
        self._is_ready = False
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self):
        if self._is_ready: return

        async with self._init_lock:
            if self._is_ready:
                return

            logger.info("[Ninjutsu] 正在初始化服务组件...")

            # 统计数据
            await self.stats_service.initialize()

            # Session & Resource
            session = await self.provider_mgr.get_session()
            self.resource_service = ResourceService(self.data_dir, session)

            # 缓存清理任务
            asyncio.create_task(self.resource_service.clean_old_cache())

            # GenerationService
            self.generation_service = GenerationService(
                provider_manager=self.provider_mgr,
                config_service=self.config_service,
                stats_service=self.stats_service,
                prompt_resolver=self.prompt_resolver
            )

            global_config = self.context.get_config()
            admin_ids = global_config.get("admins_id", [])

            # 组装 Handlers
            self.mgmt_handler = ManagementHandler(
                config_service=self.config_service,
                stats_service=self.stats_service,
                provider_manager=self.provider_mgr,
                prompt_resolver=self.prompt_resolver,
                admin_ids=admin_ids
            )

            self.workflow_handler = WorkflowHandler(
                context=self.context,
                prompt_resolver=self.prompt_resolver,
                generation_service=self.generation_service,
                resource_service=self.resource_service,
                config_service=self.config_service,
                stats_service=self.stats_service,
                admin_ids=admin_ids
            )

            self._is_ready = True
            logger.info("[Ninjutsu] 所有组件初始化完成，准备就绪。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message_any(self, event: AstrMessageEvent):
        # 1. 唤醒
        basic_conf = self.conf.get("Basic_Config", {})
        if basic_conf.get("prefix", True) and not event.is_at_or_wake_command:
            return

        text = event.message_str.strip()
        if not text:
            return

        # 2. 服务就绪
        await self._ensure_initialized()

        # 3. 去前缀
        current_prefixes = self.config_service.get_prefixes()
        cmd_pure = CommandParser.extract_pure_command(text, current_prefixes)
        if not cmd_pure: return

        # 4. 判定路由
        extra_prefix = basic_conf.get("extra_prefix", "lmi")

        # 自定提示词
        if cmd_pure == extra_prefix or cmd_pure == "图生图":
            await self.workflow_handler.handle_image_to_image(event, cmd_alias=cmd_pure)
            event.stop_event()

        # 匹配到预设
        elif self.config_service.get_prompt(cmd_pure):
            await self.workflow_handler.handle_image_to_image(event, force_preset=cmd_pure)
            event.stop_event()

    @filter.command("文生图", alias=["lmt"], prefix_optional=True)
    async def cmd_text_to_image(self, event: AstrMessageEvent):
        """文生图入口"""
        await self._ensure_initialized()
        await self.workflow_handler.handle_text_to_image(event)
        event.stop_event()

    @filter.command("lm预设", alias=["lmp"], prefix_optional=True)
    async def cmd_preset_prompt(self, event: AstrMessageEvent):
        """生图预设管理"""
        await self._ensure_initialized()
        await self.mgmt_handler.handle_preset_cmd(event, cmd_name="lmp", is_optimizer=False)
        event.stop_event()

    @filter.command("lm优化", alias=["lmo"], prefix_optional=True)
    async def cmd_preset_optimizer(self, event: AstrMessageEvent):
        """优化预设管理"""
        await self._ensure_initialized()
        await self.mgmt_handler.handle_preset_cmd(event, cmd_name="lmo", is_optimizer=True)
        event.stop_event()

    @filter.command("lm连接", alias=["lmc"], prefix_optional=True)
    async def cmd_connection(self, event: AstrMessageEvent):
        """连接管理"""
        await self._ensure_initialized()
        await self.mgmt_handler.handle_connection_cmd(event)
        event.stop_event()

    @filter.command("lm密钥", alias=["lmk"], prefix_optional=True)
    async def cmd_keys(self, event: AstrMessageEvent):
        """密钥管理"""
        await self._ensure_initialized()
        await self.mgmt_handler.handle_key_cmd(event)
        event.stop_event()

    @filter.command("lm次数", alias=["lm"], prefix_optional=True)
    async def cmd_stats(self, event: AstrMessageEvent):
        """综合看板与次数管理"""
        await self._ensure_initialized()
        await self.mgmt_handler.handle_stats_cmd(event)
        event.stop_event()

    @filter.command("lm帮助", alias=["lmh"], prefix_optional=True)
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助菜单"""
        await self._ensure_initialized()
        await self.mgmt_handler.handle_help_cmd(event)
        event.stop_event()

    async def terminate(self):
        logger.info("[Ninjutsu] 正在关闭插件资源...")

        if self._is_ready:
            # 保存统计数据
            await self.stats_service.shutdown()

            # 关闭网络会话
            await self.provider_mgr.terminate()

            self._is_ready = False

        logger.info("[Ninjutsu] 插件已安全停止。")