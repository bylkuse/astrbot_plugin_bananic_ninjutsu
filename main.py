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
from .core.config_mgr import ConfigManager
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

    async def _save_connections(self):
        if "Connection_Config" not in self.conf:
            self.conf["Connection_Config"] = {}

        serialized_data = await asyncio.to_thread(
            ConfigSerializer.dump_json_list, 
            self.connection_presets
        )
        self.conf["Connection_Config"]["connection_presets"] = serialized_data

        await self.config_mgr.save_config()

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

    async def _handle_connection_extras(
        self, event: AstrMessageEvent, parts: List[str], is_admin: bool
    ) -> Any:
        """lmc扩展指令"""
        sub = parts[0].lower() if parts else ""

        if not parts or sub in ["l", "list"]:
            help_text = ResponsePresenter.connection(is_admin, self.main_prefix)
            if not self.connection_presets:
                return event.plain_result(f"供应商:\n- 暂无可用供应商。\n\n{help_text}")

            msg = ["供应商:"]
            current_active_name = self.generation_service.conn_config.get("name")
            for name, data in self.connection_presets.items():
                prefix = "➡️" if name == current_active_name else "▪️"
                msg.append(
                    f"{prefix} {name} ({data.get('api_type', 'N/A')}, {len(data.get('api_keys', []))} keys)"
                )
            msg.extend(["", help_text])
            return event.plain_result("\n".join(msg))

        if sub == "to" and len(parts) == 2:
            target = parts[1]
            if target not in self.connection_presets:
                return event.plain_result(ResponsePresenter.item_not_found("预设", target))

            if "Connection_Config" not in self.conf:
                self.conf["Connection_Config"] = {}
            self.conf["Connection_Config"]["current_preset_name"] = target

            self.generation_service.set_active_preset(self.connection_presets[target])
            await self.config_mgr.save_config()
            return event.plain_result(
                ResponsePresenter.format_connection_switch_success(
                    target, self.connection_presets[target]
                )
            )

        if sub in ["debug", "d"] and is_admin:
            basic_conf = self.conf.get("Basic_Config", {})
            new_state = not basic_conf.get("debug_prompt", False)

            if "Basic_Config" not in self.conf:
                self.conf["Basic_Config"] = {}
            self.conf["Basic_Config"]["debug_prompt"] = new_state

            await self.config_mgr.save_config()
            return event.plain_result(
                f"{'✅' if new_state else '❌'} 调试模式已{'开启' if new_state else '关闭'}。"
            )

        if len(parts) >= 5 and parts[0].lower() == "add":
            if not is_admin:
                return event.plain_result(ResponsePresenter.unauthorized_admin())
            name, api_type, api_url, model = parts[1], parts[2], parts[3], parts[4]
            keys = parts[5].split(",") if len(parts) > 5 else []
            new_data = {
                "name": name,
                "api_type": api_type,
                "api_url": api_url,
                "model": model,
                "api_keys": keys,
            }
            async for r in self.config_mgr.perform_save_with_confirm(
                event, self.connection_presets, name, new_data, "连接预设"
            ):
                await event.send(r)
            await self._save_connections()
            return True  # Signal that we handled it

        return None

    async def _handle_connection_update(
        self, event: AstrMessageEvent, target_name: str, args: List[str], is_admin: bool
    ) -> bool:
        if not is_admin:
            await event.send(event.plain_result(ResponsePresenter.unauthorized_admin()))
            return True

        if len(args) != 2:
            return False

        target_key, target_val = args[0], args[1]
        allowed_keys = {"api_url", "model", "api_type", "api_base"}

        if target_key not in allowed_keys:
            await event.send(
                event.plain_result(
                    f"❌ 属性 [{target_key}] 不可修改。\n可选: {', '.join(allowed_keys)}"
                )
            )
            return True

        preset = self.connection_presets[target_name]
        async for r in self.config_mgr.perform_save_with_confirm(
            event,
            preset,
            target_key,
            target_val,
            f"预设[{target_name}]的{target_key}",
        ):
            await event.send(r)

        if self.generation_service.conn_config.get("name") == target_name:
            self.generation_service.set_active_preset(preset)
        await self._save_connections()
        return True

    async def _on_connection_delete(self, deleted_key: str):
        current_active_name = self.generation_service.conn_config.get("name")
        if current_active_name == deleted_key:
            new_name = next(iter(self.connection_presets.keys()), "GoogleDefault")

            if "Connection_Config" not in self.conf:
                self.conf["Connection_Config"] = {}
            self.conf["Connection_Config"]["current_preset_name"] = new_name

            if new_name in self.connection_presets:
                self.generation_service.set_active_preset(
                    self.connection_presets[new_name]
                )
            else:
                self.generation_service.set_active_preset(
                    {"name": "None", "api_keys": []}
                )
        await self._save_connections()

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
        params = parsed.params
        if parsed.first_at:
            params["first_at"] = parsed.first_at

        target_text = ""
        cmd_display = ""

        if cmd_pure == bnn_command or cmd_pure == "图生图":
            target_text = parsed.text
            cmd_display = f"#{cmd_pure}"
        elif self.pm.get_preset(cmd_pure):
            target_text = cmd_pure
            if parsed.text:
                params["additional_prompt"] = parsed.text
            cmd_display = f"#{cmd_pure}"
        else:
            return

        async for res in self.generation_service.run_generation_workflow(
            event,
            target_text,
            params,
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
        params = parsed.params
        if parsed.first_at:
            params["first_at"] = parsed.first_at
        cmd_name = first_token if first_token else "lmt"
        cmd_display = f"{self.main_prefix}{cmd_name}"

        async for res in self.generation_service.run_generation_workflow(
            event,
            parsed.text,
            params,
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
        async for res in self.config_mgr.handle_crud_command(
            event,
            ["lm优化", "lmo"],
            self.pm.get_target_dict("optimizer"),
            "优化预设",
            duplicate_check_type="optimizer",
        ):
            yield res

    @filter.command("lm预设", alias={"lmp"}, prefix_optional=True)
    async def on_preset_management(self, event: AstrMessageEvent):
        """管理生图预设"""
        async for res in self.config_mgr.handle_crud_command(
            event,
            ["lm预设", "lmp"],
            self.pm.get_target_dict("prompt"),
            "生图预设",
            duplicate_check_type="prompt",
        ):
            yield res

    @filter.command("lm连接", alias={"lmc"}, prefix_optional=True)
    @require_service
    async def on_connection_management(self, event: AstrMessageEvent):
        """管理连接预设"""
        is_admin = self.config_mgr.is_admin(event)

        extra_handler = lambda evt, pts: self._handle_connection_extras(
            evt, pts, is_admin
        )
        update_handler = lambda evt, name, args: self._handle_connection_update(
            evt, name, args, is_admin
        )
        delete_handler = self._on_connection_delete
        display_handler = lambda k, v: ResponsePresenter.format_connection_detail(
            k, v, self.main_prefix
        )

        async for res in self.config_mgr.handle_crud_command(
            event,
            ["lm连接", "lmc"],
            self.connection_presets,
            "连接预设",
            after_delete_callback=delete_handler,
            extra_cmd_handler=extra_handler,
            custom_update_handler=update_handler,
            custom_display_handler=display_handler,
        ):
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

        parsed = CommandParser.parse(
            event, cmd_aliases=["lm密钥", "lmk"], prefixes=self.global_prefixes
        )
        cmd_text = parsed.text
        parts = cmd_text.split()

        if not parts:
            current_preset = self.generation_service.conn_config.get("name", "Unknown")
            yield event.plain_result(
                ResponsePresenter.key_management(current_preset, self.main_prefix)
            )
            return

        sub = parts[0].lower()
        args = parts[1:]

        try:
            if sub == "add":
                if len(args) < 2:
                    yield event.plain_result(
                        f"格式错误: {self.main_prefix}lmk add <预设名> <Key1> [Key2]..."
                    )
                    return
                name = args[0]
                if name not in self.connection_presets:
                    yield event.plain_result(
                        ResponsePresenter.item_not_found("预设", name)
                    )
                    return

                preset = self.connection_presets[name]
                current_keys = preset.get("api_keys", [])
                new_keys_to_add = [k for k in args[1:] if k not in current_keys]

                async for res in self.config_mgr.perform_save_with_confirm(
                    event,
                    preset,
                    "api_keys",
                    current_keys + new_keys_to_add,
                    f"密钥组({name})",
                ):
                    yield res

                await self._save_connections()

            elif sub == "del":
                if len(args) < 2:
                    yield event.plain_result(
                        f"格式错误: {self.main_prefix}lmk del <预设名> <序号|all>"
                    )
                    return
                name, idx_str = args[0], args[1]
                if name not in self.connection_presets:
                    yield event.plain_result(
                        ResponsePresenter.item_not_found("预设", name)
                    )
                    return

                preset = self.connection_presets[name]
                keys = preset.get("api_keys", [])

                new_key_list = None
                if idx_str.lower() == "all":
                    new_key_list = []
                elif idx_str.isdigit():
                    idx = int(idx_str)
                    if 1 <= idx <= len(keys):
                        new_key_list = keys[: idx - 1] + keys[idx:]

                if new_key_list is None:
                    yield event.plain_result("❌ 序号无效。")
                    return

                async for res in self.config_mgr.perform_save_with_confirm(
                    event, preset, "api_keys", new_key_list, f"密钥组({name})"
                ):
                    yield res

                await self._save_connections()

            else:
                target_preset_name = ""

                if sub == "list" and args:
                    target_preset_name = args[0]
                else:
                    target_preset_name = parts[0]

                if target_preset_name not in self.connection_presets:
                    yield event.plain_result(
                        ResponsePresenter.item_not_found("预设", target_preset_name)
                    )
                    return

                keys = self.connection_presets[target_preset_name].get("api_keys", [])

                yield event.plain_result(
                    ResponsePresenter.format_key_list(
                        target_preset_name, keys, self.main_prefix
                    )
                )

        except Exception as e:
            logger.error(f"Key 操作失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {e}")

    async def terminate(self):
        await self.stats.stop_auto_save()
        await ImageUtils.terminate()
        await self.api_client.terminate()

        logger.info("[香蕉忍法帖] 插件已终止")
