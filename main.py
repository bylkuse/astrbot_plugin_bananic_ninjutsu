import re
from typing import Any, Dict, List, Optional, Tuple
from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
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
from .utils.parser import CommandParser
from .utils.views import ResponsePresenter

@register(
    "astrbot_plugin_bananic_ninjutsu",
    "LilDawn",
    "适配napcat的Astrbot插件，主攻用于🍌（nano banana）生图的各种奇妙的小巧思。",
    "0.0.4", 
    "https://github.com/bylkuse/astrbot_plugin_bananic_ninjutsu",
)
class Ninjutsu(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = StarTools.get_data_dir()

        self.api_client = APIClient() 
        self.pm = PromptManager(self.plugin_data_dir, self.conf)
        self.stats = StatsManager(self.plugin_data_dir)
        self.config_mgr = ConfigManager(self.conf, self.pm, self.context)

        self.generation_service = GenerationService(self.api_client, self.stats, self.pm, self.conf)
        self.connection_presets: Dict[str, Dict[str, Any]] = {} 
        self.current_preset_name: str = ""

        raw_prefixes = self.context.get_config().get("command_prefixes", ["/"])
        if isinstance(raw_prefixes, str): raw_prefixes = [raw_prefixes]
        self.global_prefixes = sorted(raw_prefixes, key=len, reverse=True)

    async def initialize(self):
        await self.stats.load_all_data()
        await self.pm.load_prompts()
        await self._load_connection_presets()
        logger.info("香蕉忍法帖 插件已加载")
        if not self.conf.get("api_keys"):
            logger.warning("[香蕉忍法帖]!!! API 密钥未配置!!!")

    async def _load_connection_presets(self):
        raw_list = self.conf.get("connection_presets", [])
        self.connection_presets = ConfigSerializer.load_json_list(raw_list, key_field="name")
        self.current_preset_name = self.conf.get("current_preset_name", "GoogleDefault")
        self.generation_service.set_current_preset_name(self.current_preset_name)

        if self.current_preset_name in self.connection_presets:
            self._apply_preset_to_config(self.connection_presets[self.current_preset_name])

    def _apply_preset_to_config(self, preset_data: Dict[str, Any]):
        for key in ["api_type", "api_url", "model", "api_keys"]:
            if key in preset_data:
                self.conf[key] = preset_data[key]
        logger.info(f"已应用连接预设: {preset_data.get('name')}")

    async def _save_connections(self):
        self.conf["connection_presets"] = ConfigSerializer.dump_json_list(self.connection_presets)
        await self.config_mgr.save_config()

    def _extract_pure_command(self, text: str) -> str:
        for p in self.global_prefixes:
            if text.startswith(p):
                return text[len(p):]
        return text

    def _kv_adapter(self, parts, text):
        return ConfigSerializer.parse_single_kv(text)

    def _resolve_admin_cmd(self, event: AstrMessageEvent, parts: List[str]) -> Tuple[Optional[str], Optional[int], bool]:
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target_id = str(at_seg.qq) if at_seg else None
        count_val = None
        is_group = False

        numbers = [p for p in parts if p.lstrip("-").isdigit()]
        
        if at_seg:
            if numbers: count_val = int(numbers[0])
            else: pass
        elif len(numbers) >= 2:
            target_id = numbers[0]
            count_val = int(numbers[1])
        elif len(numbers) == 1:
            val = int(numbers[0])
            if event.get_group_id():
                target_id = event.get_group_id()
                count_val = val
                is_group = True
            else:
                target_id = str(val) 
        else:
            pass

        if not target_id:
            target_id = event.get_sender_id()
        
        return target_id, count_val, is_group
    
    # --- Event Handlers ---

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_figurine_request(self, event: AstrMessageEvent):
        """预设/自定义图生图"""
        if self.conf.get("prefix", True) and not event.is_at_or_wake_command: return
        text = event.message_str.strip()
        if not text: return

        cmd_with_prefix = text.split()[0].strip()
        cmd_pure = self._extract_pure_command(cmd_with_prefix) 

        bnn_command = self.conf.get("extra_prefix", "bnn")

        parsed = CommandParser.parse(event, cmd_with_prefix, prefixes=self.global_prefixes)
        params = parsed.params
        if parsed.first_at: params['first_at'] = parsed.first_at

        target_text = ""
        cmd_display = ""

        if cmd_pure == bnn_command or cmd_pure == "图生图":
            target_text = parsed.text
            cmd_display = f"#{cmd_pure}"
        elif self.pm.get_preset(cmd_pure):
            target_text = cmd_pure
            cmd_display = f"#{cmd_pure}"
        else:
            return

        async for res in self.generation_service.run_generation_workflow(
            event, target_text, params, True, cmd_display, self.context, self.config_mgr.is_admin(event)
        ):
            yield res
        event.stop_event()

    @filter.command("文生图", alias={"lmt"}, prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        raw_text = event.message_str.strip()
        cmd_part = raw_text.split()[0]
        parsed = CommandParser.parse(event, cmd_part, prefixes=self.global_prefixes)
        params = parsed.params
        if parsed.first_at: params['first_at'] = parsed.first_at

        async for res in self.generation_service.run_generation_workflow(
            event, parsed.text, params, False, "#lmt", self.context, self.config_mgr.is_admin(event)
        ):
            yield res
        event.stop_event()

    # --- Management Commands ---

    @filter.command("lm优化", alias={"lmo"}, prefix_optional=True)
    async def on_optimizer_management(self, event: AstrMessageEvent):
        async for res in self.config_mgr.handle_crud_command(
            event, ["lm优化", "lmo"], self.pm.get_target_dict("optimizer"), "优化预设", 
            duplicate_check_type="optimizer"
        ): yield res

    @filter.command("lm预设", alias={"lmp"}, prefix_optional=True)
    async def on_preset_management(self, event: AstrMessageEvent):
        async for res in self.config_mgr.handle_crud_command(
            event, ["lm预设", "lmp"], self.pm.get_target_dict("prompt"), "生图预设", 
            duplicate_check_type="prompt"
        ): yield res

    @filter.command("lm连接", alias={"lmc"}, prefix_optional=True)
    async def on_connection_management(self, event: AstrMessageEvent):
        is_admin = self.config_mgr.is_admin(event)

        def parse_connection_add(parts: List[str], text: str):
            if not is_admin: return None
            if len(parts) >= 5 and parts[0].lower() == "add":
                name, api_type, api_url, model = parts[1], parts[2], parts[3], parts[4]
                keys = parts[5].split(',') if len(parts) > 5 else []
                return name, {"name": name, "api_type": api_type, "api_url": api_url, "model": model, "api_keys": keys}
            return None

        async def after_delete(deleted_key: str):
            if self.current_preset_name == deleted_key:
                new_name = next(iter(self.connection_presets.keys()), "GoogleDefault")
                self.current_preset_name = new_name
                self.conf["current_preset_name"] = new_name
                self.generation_service.set_current_preset_name(new_name)
                if new_name in self.connection_presets:
                    self._apply_preset_to_config(self.connection_presets[new_name])
            await self._save_connections()

        async def handle_extras(evt, parts):
            sub = parts[0].lower() if parts else ""
            if not parts or sub in ["l", "list"]:
                help_text = ResponsePresenter.connection(is_admin)
                if not self.connection_presets: return evt.plain_result(f"供应商:\n- 暂无可用供应商。\n\n{help_text}")
                msg = ["供应商:"]
                for name, data in self.connection_presets.items():
                    prefix = "➡️" if name == self.current_preset_name else "▪️"
                    msg.append(f"{prefix} {name} ({data.get('api_type', 'N/A')}, {len(data.get('api_keys', []))} keys)")
                msg.extend(["", help_text])
                return evt.plain_result("\n".join(msg))

            if sub == "to" and len(parts) == 2:
                target = parts[1]
                if target not in self.connection_presets: return evt.plain_result(ResponsePresenter.item_not_found("预设", target))
                self.current_preset_name = target
                self.generation_service.set_current_preset_name(target)
                self.conf["current_preset_name"] = target
                self._apply_preset_to_config(self.connection_presets[target])
                await self.config_mgr.save_config()
                return evt.plain_result(ResponsePresenter.format_connection_switch_success(target, self.connection_presets[target]))

            if sub in ["debug", "d"] and is_admin:
                new_state = not self.conf.get("debug_prompt", False)
                self.conf["debug_prompt"] = new_state
                await self.config_mgr.save_config()
                return evt.plain_result(f"{'✅' if new_state else '❌'} 调试模式已{'开启' if new_state else '关闭'}。")

            if len(parts) == 1 and parts[0] not in ["add", "del", "ren"]:
                if parts[0] in self.connection_presets:
                    return evt.plain_result(ResponsePresenter.format_connection_detail(parts[0], self.connection_presets[parts[0]]))
            return None

        async for res in self.config_mgr.handle_crud_command(
            event, ["lm连接", "lmc"], self.connection_presets, "连接预设", 
            after_delete_callback=after_delete, 
            extra_cmd_handler=handle_extras
        ): yield res

        if parsed := parse_connection_add(self.config_mgr.strip_command(event.message_str.strip(), ["lm连接", "lmc"]).split(), ""):
            key, value = parsed
            async for r in self.config_mgr.perform_save_with_confirm(event, self.connection_presets, key, value, "连接预设"): yield r

        await self._save_connections()

    @filter.command("lm帮助", alias={"lmh"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        cmd_text = self.config_mgr.strip_command(event.message_str.strip(), ["lm帮助", "lmh"])
        sub = cmd_text.strip().lower()
        if sub in ["参数", "param", "params", "p", "--help"]: yield event.plain_result(ResponsePresenter.help_params()); return
        if sub in ["变量", "var", "vars", "v"]: yield event.plain_result(ResponsePresenter.help_vars()); return
        yield event.plain_result(ResponsePresenter.main_menu(self.conf.get("extra_prefix", "bnn")))

    @filter.command("lm次数", alias={"lm"}, prefix_optional=True)
    async def on_counts_management(self, event: AstrMessageEvent):
        cmd_text = self.config_mgr.strip_command(event.message_str.strip(), ["lm次数", "lm"])
        parts = cmd_text.split()
        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_admin = self.config_mgr.is_admin(event)

        # 看板
        if not parts:
            data = await self.stats.get_dashboard_with_checkin(user_id, group_id, self.conf)
            yield event.plain_result(ResponsePresenter.stats_dashboard(data, group_id))
            return

        # 次数
        if is_admin:
            target_id, count_val, is_group = self._resolve_admin_cmd(event, parts)

            if count_val is not None:
                if not target_id:
                    yield event.plain_result("❌ 无法确定修改目标。")
                    return
                     
                new_val = await self.stats.modify_resource(target_id, count_val, is_group)
                yield event.plain_result(ResponsePresenter.admin_count_modification(target_id, count_val, new_val, is_group))
            else:
                # 查询
                u_cnt = self.stats.get_user_count(target_id)
                g_cnt = self.stats.get_group_count(target_id) if is_group else (self.stats.get_group_count(group_id) if group_id else 0)

                yield event.plain_result(ResponsePresenter.admin_query_result(target_id, u_cnt, group_id, g_cnt))
        else:
            yield event.plain_result("❌ 权限不足或指令格式错误。")

    @filter.command("lm密钥", alias={"lmk"}, prefix_optional=True)
    async def on_key_management(self, event: AstrMessageEvent):
        if not self.config_mgr.is_admin(event):
            yield event.plain_result(ResponsePresenter.unauthorized_admin())
            return

        cmd_text = self.config_mgr.strip_command(event.message_str.strip(), ["lm密钥", "lmk"])
        parts = cmd_text.split()

        if not parts: 
            yield event.plain_result(ResponsePresenter.key_management(self.current_preset_name))
            return

        sub = parts[0].lower()
        args = parts[1:]

        try:
            if sub == "add":
                if len(args) < 2: yield event.plain_result("格式错误: #lmkey add <预设名> <Key1> [Key2]..."); return
                name = args[0]
                if name not in self.connection_presets: yield event.plain_result(ResponsePresenter.item_not_found("预设", name)); return

                preset = self.connection_presets[name]
                current_keys = preset.get("api_keys", [])
                new_keys_to_add = [k for k in args[1:] if k not in current_keys]

                async for res in self.config_mgr.perform_save_with_confirm(
                    event, preset, "api_keys", current_keys + new_keys_to_add, f"密钥组({name})"
                ): yield res

                await self._save_connections()

            elif sub == "del":
                if len(args) < 2: yield event.plain_result("格式错误: #lmk del <预设名> <序号|all>"); return
                name, idx_str = args[0], args[1]
                if name not in self.connection_presets: yield event.plain_result(ResponsePresenter.item_not_found("预设", name)); return

                preset = self.connection_presets[name]
                keys = preset.get("api_keys", [])

                new_key_list = None
                if idx_str.lower() == "all":
                    new_key_list = []
                elif idx_str.isdigit():
                    idx = int(idx_str)
                    if 1 <= idx <= len(keys):
                        new_key_list = keys[:idx-1] + keys[idx:]

                if new_key_list is None: yield event.plain_result("❌ 序号无效。"); return

                async for res in self.config_mgr.perform_save_with_confirm(
                    event, preset, "api_keys", new_key_list, f"密钥组({name})"
                ): yield res
                
                await self._save_connections()

            else:
                target_preset_name = ""

                if sub == "list" and args:
                    target_preset_name = args[0]
                else:
                    target_preset_name = parts[0]

                if target_preset_name not in self.connection_presets:
                    yield event.plain_result(ResponsePresenter.item_not_found("预设", target_preset_name))
                    return

                keys = self.connection_presets[target_preset_name].get("api_keys", [])

                yield event.plain_result(ResponsePresenter.format_key_list(target_preset_name, keys))

        except Exception as e:
            logger.error(f"Key 操作失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {e}")

    async def terminate(self):
        await self.stats.stop_auto_save()
        await ImageUtils.terminate()

        logger.info("[香蕉忍法帖] 插件已终止")
