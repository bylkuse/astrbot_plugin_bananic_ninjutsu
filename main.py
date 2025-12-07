import asyncio
from functools import wraps
from typing import Any, Dict, List, Tuple
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .api_client import APIClient
from .core.prompt import PromptManager
from .core.stats import StatsManager
from .core.images import ImageUtils
from .core.config_mgr import (
    ConfigManager, 
    DictDataStrategy, 
    ListKeyStrategy, 
    ConnectionStrategy
)
from .services.generation import GenerationService
from .utils.serializer import ConfigSerializer
from .utils.parser import CommandParser, ParsedCommand
from .utils.views import ResponsePresenter

def require_service(func):
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if not self.generation_service:
            logger.error(f"调用 {func.__name__} 失败: 服务未初始化")
            yield event.plain_result("❌ 服务正在初始化或初始化失败，请稍后重试。")
            return

        async for item in func(self, event, *args, **kwargs):
            yield item

    return wrapper

class Ninjutsu(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = StarTools.get_data_dir()

        raw_prefixes = self.context.get_config().get("command_prefixes", ["/"])
        if isinstance(raw_prefixes, str):
            raw_prefixes = [raw_prefixes]
        self.global_prefixes = sorted(raw_prefixes, key=len, reverse=True)
        self.main_prefix = self.global_prefixes[0] if self.global_prefixes else "#"

        self.pm = PromptManager(self.conf, self.plugin_data_dir)
        self.api_client = APIClient()
        self.stats = StatsManager(self.plugin_data_dir)
        self.config_mgr = ConfigManager(self.conf, self.pm, self.context)

        self.connection_presets: Dict[str, Any] = {}
        self.generation_service: GenerationService | None = None

    async def initialize(self):
        await self.stats.load_all_data()
        await self.pm.load_prompts()
        conn_conf = self.conf.get("Connection_Config", {})

        raw_list = conn_conf.get("connection_presets")
        self.connection_presets = ConfigSerializer.load_json_list(
            raw_list, key_field="name"
        )
        current_preset_name = conn_conf.get("current_preset_name")
        active_preset_data = self.connection_presets.get(current_preset_name)
        if not active_preset_data:
            if self.connection_presets:
                first_key = next(iter(self.connection_presets))
                active_preset_data = self.connection_presets[first_key]

                if "Connection_Config" not in self.conf:
                    self.conf["Connection_Config"] = {}
                self.conf["Connection_Config"]["current_preset_name"] = first_key
                logger.warning(f"指定预设不存在，回退至: {first_key}")
            else:
                active_preset_data = {"name": "None", "api_keys": []}
                logger.error("未找到任何连接预设！")

        self.generation_service = GenerationService(
            self.api_client,
            self.stats,
            self.pm,
            self.conf,
            active_preset_data,
            main_prefix=self.main_prefix,
        )
        logger.info("香蕉忍法帖 插件已加载")

    def _resolve_admin_cmd(
        self, event: AstrMessageEvent, parsed: ParsedCommand
    ) -> Tuple[str | None, int | None, bool]:
        target_id = str(parsed.first_at.qq) if parsed.first_at else None
        numbers = [int(x) for x in parsed.text.split() if x.lstrip("-").isdigit()]

        count_val = None
        is_group = False

        if target_id:
            if numbers:
                count_val = numbers[0]

        elif len(numbers) >= 2:
            target_id = str(numbers[0])
            count_val = numbers[1]

        elif len(numbers) == 1:
            val = numbers[0]
            if event.get_group_id():
                target_id = event.get_group_id()
                count_val = val
                is_group = True
            else:
                target_id = str(val)

        if not target_id and not is_group:
            target_id = event.get_sender_id()

        return target_id, count_val, is_group

    # --- Event Handlers ---

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    @require_service
    async def on_figurine_request(self, event: AstrMessageEvent):
        """预设/自定义图生图"""
        basic_conf = self.conf.get("Basic_Config", {})
        if basic_conf.get("prefix", True) and not event.is_at_or_wake_command:
            return

        text = event.message_str.strip()
        if not text:
            return

        cmd_pure = CommandParser.extract_pure_command(text, self.global_prefixes)
        if not cmd_pure:
            return
        bnn_command = basic_conf.get("extra_prefix", "lmi")
        parsed = CommandParser.parse(
            event, cmd_aliases=[cmd_pure], prefixes=self.global_prefixes
        )

        target_text = ""
        cmd_display = ""

        if cmd_pure == bnn_command or cmd_pure == "图生图":
            target_text = parsed.text
            cmd_display = f"{self.main_prefix}{cmd_pure}"
        elif self.pm.get_preset(cmd_pure):
            target_text = cmd_pure
            if parsed.text:
                parsed.params["additional_prompt"] = parsed.text
            cmd_display = f"{self.main_prefix}{cmd_pure}"
        else:
            return

        async for res in self.generation_service.run_generation_workflow(
            event,
            target_text,
            parsed,
            True,
            cmd_display,
            self.context,
            self.config_mgr.is_admin(event),
        ):
            yield res
        event.stop_event()

    @filter.command("文生图", alias={"lmt"}, prefix_optional=True)
    @require_service
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        """预设/自定义文生图"""
        first_token = CommandParser.extract_pure_command(
            event.message_str, self.global_prefixes
        )
        parsed = CommandParser.parse(
            event,
            cmd_aliases=[first_token] if first_token else [],
            prefixes=self.global_prefixes,
        )

        cmd_name = first_token if first_token else "lmt"
        cmd_display = f"{self.main_prefix}{cmd_name}"

        async for res in self.generation_service.run_generation_workflow(
            event,
            parsed.text,
            parsed,
            False,
            cmd_display,
            self.context,
            self.config_mgr.is_admin(event),
        ):
            yield res
        event.stop_event()

    # --- Management Commands ---

    @filter.command("lm优化", alias={"lmo"}, prefix_optional=True)
    async def on_optimizer_management(self, event: AstrMessageEvent):
        """管理优化预设"""
        strategy = DictDataStrategy(
            data=self.pm.get_target_dict("optimizer"),
            item_name="优化预设",
            config_mgr=self.config_mgr,
            duplicate_type="optimizer"
        )
        async for res in self.config_mgr.handle_crud_command(event, ["lm优化", "lmo"], strategy):
            yield res

    @filter.command("lm预设", alias={"lmp"}, prefix_optional=True)
    async def on_preset_management(self, event: AstrMessageEvent):
        """管理生图预设"""
        strategy = DictDataStrategy(
            data=self.pm.get_target_dict("prompt"),
            item_name="生图预设",
            config_mgr=self.config_mgr,
            duplicate_type="prompt"
        )
        async for res in self.config_mgr.handle_crud_command(event, ["lm预设", "lmp"], strategy):
            yield res

    @filter.command("lm连接", alias={"lmc"}, prefix_optional=True)
    @require_service
    async def on_connection_management(self, event: AstrMessageEvent):
        """管理连接预设"""
        async def save_callback():
            await self.config_mgr.save_connection_presets(self.connection_presets)

        strategy = ConnectionStrategy(
            data=self.connection_presets,
            config_mgr=self.config_mgr,
            generation_service=self.generation_service,
            raw_config=self.conf,
            save_callback=save_callback
        )

        async for res in self.config_mgr.handle_crud_command(event, ["lm连接", "lmc"], strategy):
            yield res

    @filter.command("lm帮助", alias={"lmh"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        """使用帮助"""
        parsed = CommandParser.parse(
            event, cmd_aliases=["lm帮助", "lmh"], prefixes=self.global_prefixes
        )
        cmd_text = parsed.text
        sub = cmd_text.strip().lower()
        is_help_flag = parsed.params.get("help") or sub == "--help"

        if sub in ["参数", "param", "params", "p"] or is_help_flag:
            yield event.plain_result(ResponsePresenter.help_params())
            return
        if sub in ["变量", "var", "vars", "v"]:
            yield event.plain_result(ResponsePresenter.help_vars())
            return

        extra_prefix = self.conf.get("Basic_Config", {}).get("extra_prefix", "lmi")
        yield event.plain_result(
            ResponsePresenter.main_menu(extra_prefix, self.main_prefix)
        )

    @filter.command("lm次数", alias={"lm"}, prefix_optional=True)
    async def on_counts_management(self, event: AstrMessageEvent):
        """签到看板&管理次数"""
        parsed = CommandParser.parse(
            event, cmd_aliases=["lm次数", "lm"], prefixes=self.global_prefixes
        )

        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_admin = self.config_mgr.is_admin(event)

        # 看板
        if not parsed.text.strip() and not parsed.first_at:
            data = await self.stats.get_dashboard_with_checkin(
                user_id, group_id, self.conf
            )
            yield event.plain_result(ResponsePresenter.stats_dashboard(data, group_id))
            return

        # 次数
        if is_admin:
            target_id, count_val, is_group = self._resolve_admin_cmd(event, parsed)

            if count_val is not None:
                if not target_id:
                    yield event.plain_result("❌ 无法确定修改目标。")
                    return

                new_val = await self.stats.modify_resource(
                    target_id, count_val, is_group
                )
                yield event.plain_result(
                    ResponsePresenter.admin_count_modification(
                        target_id, count_val, new_val, is_group
                    )
                )
            else:
                # 查询
                u_cnt = self.stats.get_user_count(target_id)
                g_cnt = (
                    self.stats.get_group_count(target_id)
                    if is_group
                    else (self.stats.get_group_count(group_id) if group_id else 0)
                )

                yield event.plain_result(
                    ResponsePresenter.admin_query_result(
                        target_id, u_cnt, group_id, g_cnt
                    )
                )
        else:
            yield event.plain_result("❌ 权限不足或指令格式错误。")

    @filter.command("lm密钥", alias={"lmk"}, prefix_optional=True)
    @require_service
    async def on_key_management(self, event: AstrMessageEvent):
        """独立&快捷的密钥管理"""
        if not self.config_mgr.is_admin(event):
            yield event.plain_result(ResponsePresenter.unauthorized_admin())
            return

        current_name = self.generation_service.conn_config.get("name", "Unknown")

        parsed = CommandParser.parse(event, cmd_aliases=["lm密钥", "lmk"], prefixes=self.global_prefixes)
        parts = parsed.text.split()

        target_preset_name = current_name
        args_to_pass = parts

        if parts:
            if parts[0] in self.connection_presets:
                target_preset_name = parts[0]
                args_to_pass = parts[1:]

            elif parts[0].lower() == "del" and len(parts) >= 3:
                potential_preset = parts[1]
                if potential_preset in self.connection_presets:
                    target_preset_name = potential_preset
                    args_to_pass = ["del", parts[2]]

        target_data = self.connection_presets.get(target_preset_name, {})

        if "api_keys" not in target_data or not isinstance(target_data["api_keys"], list):
            target_data["api_keys"] = []

        keys_list = target_data["api_keys"]

        async def save_keys():
            await self.config_mgr.save_connection_presets(self.connection_presets)

        strategy = ListKeyStrategy(
            preset_name=target_preset_name,
            key_list=keys_list,
            config_mgr=self.config_mgr,
            save_callback=save_keys
        )

        async for res in self.config_mgr.handle_crud_command(event, ["lm密钥", "lmk"], strategy, args_override=args_to_pass):
            yield res

    async def terminate(self):
        await self.stats.stop_auto_save()
        await self.api_client.terminate()

        logger.info("[香蕉忍法帖] 插件已终止")
