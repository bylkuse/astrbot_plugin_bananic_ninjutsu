import re
from typing import Any, Dict, List, Optional
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
    "0.0.1", 
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
        self.config_mgr = ConfigManager(self.conf, self.pm)

        self.generation_service = GenerationService(self.api_client, self.stats, self.pm, self.conf)

        self.connection_presets: Dict[str, Dict[str, Any]] = {} 
        self.current_preset_name: str = ""

    async def initialize(self):
        await self.stats.load_all_data()
        await self.pm.load_prompts()
        await self._load_connection_presets()

        logger.info("香蕉忍法帖 插件已加载")

        if not self.conf.get("api_keys"):
            logger.warning("[香蕉忍法帖]!!! API 密钥未配置!!!")

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids

    async def _load_connection_presets(self):
        raw_list = self.conf.get("connection_presets", [])
        self.connection_presets = ConfigSerializer.load_json_list(raw_list, key_field="name")
        self.current_preset_name = self.conf.get("current_preset_name", "GoogleDefault")
        self.generation_service.set_current_preset_name(self.current_preset_name)
    
        current_preset = self.connection_presets.get(self.current_preset_name)
        if current_preset: 
            self._apply_preset_to_config(current_preset)

    def _apply_preset_to_config(self, preset_data: Dict[str, Any]):
        self.conf["api_type"] = preset_data.get("api_type", "google")
        self.conf["api_url"] = preset_data.get("api_url", self.conf.get("api_url"))
        self.conf["model"] = preset_data.get("model", self.conf.get("model"))
        self.conf["api_keys"] = preset_data.get("api_keys", self.conf.get("api_keys"))
        logger.info(f"已应用连接预设: {preset_data['name']}")

    # --- Event Handlers ---

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_figurine_request(self, event: AstrMessageEvent):
        """预设/自定义图生图"""
        if self.conf.get("prefix", True) and not event.is_at_or_wake_command: return
        text = event.message_str.strip()
        if not text: return

        cmd_with_prefix = text.split()[0].strip()
        cmd_pure = cmd_with_prefix.lstrip('#/')
        bnn_command = self.conf.get("extra_prefix", "bnn")

        parsed = CommandParser.parse(event, cmd_with_prefix)
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
            event, target_text, params, True, cmd_display, self.context, self.is_global_admin(event)
        ):
            yield res
        event.stop_event()

    @filter.command("文生图", alias={"lmt"}, prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        raw_text = event.message_str.strip()
        cmd_part = raw_text.split()[0] 
        parsed = CommandParser.parse(event, cmd_part)
        params = parsed.params
        if parsed.first_at: params['first_at'] = parsed.first_at

        async for res in self.generation_service.run_generation_workflow(
            event, parsed.text, params, False, "#lmt", self.context, self.is_global_admin(event)
        ):
            yield res
        event.stop_event()

    # --- Management Commands ---

    @filter.command("lm优化", alias={"lmo"}, prefix_optional=True)
    async def on_optimizer_management(self, event: AstrMessageEvent):
        def parse_simple_kv(parts, text):
            if ":" in text:
                k, v = map(str.strip, text.split(":", 1))
                return (k, v) if k and v else None
            return None
        async for res in self.config_mgr.handle_crud_command(
            event, ["lm优化", "lmo"], self.pm.get_target_dict("optimizer"), "优化预设", 
            self.is_global_admin(event), parse_simple_kv, duplicate_check_type="optimizer"
        ): yield res

    @filter.command("lm预设", alias={"lmp"}, prefix_optional=True)
    async def on_preset_management(self, event: AstrMessageEvent):
        def parse_simple_kv(parts, text):
            if ":" in text:
                k, v = map(str.strip, text.split(":", 1))
                return (k, v) if k and v else None
            return None
        async for res in self.config_mgr.handle_crud_command(
            event, ["lm预设", "lmp"], self.pm.get_target_dict("prompt"), "生图预设", 
            self.is_global_admin(event), parse_simple_kv, duplicate_check_type="prompt"
        ): yield res

    @filter.command("lm连接", alias={"lmc"}, prefix_optional=True)
    async def on_connection_management(self, event: AstrMessageEvent):
        is_admin = self.is_global_admin(event)

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
                self.conf["current_preset_name"] = new_name # Update config directly for save
                self.generation_service.set_current_preset_name(new_name)
                if new_name in self.connection_presets:
                    self._apply_preset_to_config(self.connection_presets[new_name])

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

        self.conf["connection_presets"] = ConfigSerializer.dump_json_list(self.connection_presets)

        async for res in self.config_mgr.handle_crud_command(
            event, ["lm连接", "lmc"], self.connection_presets, "连接预设", 
            is_admin, parse_connection_add, after_delete, handle_extras
        ): yield res

        self.conf["connection_presets"] = ConfigSerializer.dump_json_list(self.connection_presets)

    @filter.command("lm帮助", alias={"lmh"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        cmd_text = self.config_mgr._strip_command(event.message_str.strip(), ["lm帮助", "lmh"])
        sub = cmd_text.strip().lower()
        if sub in ["参数", "param", "params", "p", "--help"]: yield event.plain_result(ResponsePresenter.help_params()); return
        if sub in ["变量", "var", "vars", "v"]: yield event.plain_result(ResponsePresenter.help_vars()); return
        yield event.plain_result(ResponsePresenter.main_menu(self.conf.get("extra_prefix", "bnn")))

    @filter.command("lm次数", alias={"lm"}, prefix_optional=True)
    async def on_counts_management(self, event: AstrMessageEvent):
        cmd_text = self.config_mgr._strip_command(event.message_str.strip(), ["lm次数", "lm"])
        parts = cmd_text.split()
        is_admin = self.is_global_admin(event)
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        if not parts:
            msg_parts = []
            if self.conf.get("enable_checkin", False):
                if self.stats.has_checked_in_today(user_id):
                    msg_parts.append("📅 您今天已经签到过了。")
                else:
                    import random
                    reward = random.randint(1, max(1, int(self.conf.get("checkin_random_reward_max", 5)))) if str(self.conf.get("enable_random_checkin", False)).lower() == 'true' else int(self.conf.get("checkin_fixed_reward", 3))
                    await self.stats.perform_checkin(user_id, reward)
                    msg_parts.append(f"🎉 签到成功！获得 {reward} 次。")
            elif self.conf.get("enable_checkin_display", False): msg_parts.append("📅 签到功能未开启。")

            user_count = self.stats.get_user_count(user_id)
            quota_msg = f"💳 个人剩余: {user_count}次"
            if group_id: quota_msg += f" | 本群共享: {self.stats.get_group_count(group_id)}次"
            msg_parts.append(quota_msg)

            date, users, groups = self.stats.get_leaderboard()
            if date and (users or groups):
                stats_msg = f"\n📊 **今日榜单 ({date})**"
                if groups: stats_msg += "\n👥 群组TOP: " + " | ".join([f"群{gid}({count})" for gid, count in groups[:3]])
                if users: stats_msg += "\n👤 用户TOP: " + " | ".join([f"{uid}({count})" for uid, count in users[:5]])
            msg_parts.append(stats_msg)

            yield event.plain_result("\n".join(msg_parts))
            return

        sub_command = parts[0].lower()
        if sub_command == "用户":
            if not is_admin: return
            at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
            target_qq, count = (str(at_seg.qq), int(re.search(r"(\d+)\s*$", cmd_text).group(1))) if at_seg and re.search(r"(\d+)\s*$", cmd_text) else (parts[1], int(parts[2])) if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit() else (None, 0)
            if not target_qq or count <= 0: yield event.plain_result('格式错误:\n#lm次数 用户 @用户 <次数>\n或 #lm次数 用户 <QQ号> <次数>'); return
            new_val = await self.stats.modify_user_count(target_qq, count)
            yield event.plain_result(f"✅ 已为用户 {target_qq} 增加 {count} 次，TA当前剩余 {new_val} 次。")
            return

        if sub_command == "群组":
            if not is_admin: return
            if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit(): yield event.plain_result('格式错误: #lm次数 群组 <群号> <次数>'); return
            new_val = await self.stats.modify_group_count(parts[1], int(parts[2]))
            yield event.plain_result(f"✅ 已为群组 {parts[1]} 增加 {parts[2]} 次，该群当前剩余 {new_val} 次。")
            return

        # 查询他人
        uid_query = str(next((s.qq for s in event.message_obj.message if isinstance(s, At)), re.search(r"(\d+)", cmd_text).group(1) if re.search(r"(\d+)", cmd_text) else user_id)) if is_admin else user_id
        reply_msg = f"用户 {uid_query} 个人剩余次数为: {self.stats.get_user_count(uid_query)}"
        if group_id: reply_msg += f"\n本群共享剩余次数为: {self.stats.get_group_count(group_id)}"
        yield event.plain_result(reply_msg)

    @filter.command("lm密钥", alias={"lmk"}, prefix_optional=True)
    async def on_key_management(self, event: AstrMessageEvent):
        if not self.is_global_admin(event): return
        cmd_text = self.config_mgr._strip_command(event.message_str.strip(), ["lm密钥", "lmk"])
        parts = cmd_text.split()
        if not parts: yield event.plain_result(ResponsePresenter.key_management(self.current_preset_name)); return

        sub, args = parts[0].lower(), parts[1:]
        try:
            if sub == "add":
                if len(args) < 2: yield event.plain_result("格式错误: #lmkey add <预设名> <Key1> [Key2]..."); return
                if args[0] not in self.connection_presets: yield event.plain_result(ResponsePresenter.item_not_found("预设", args[0])); return
                preset = self.connection_presets[args[0]]
                async for res in self.config_mgr.perform_save_with_confirm(event, preset, "api_keys", preset.get("api_keys", []) + [k for k in args[1:] if k not in preset.get("api_keys", [])], f"密钥组({args[0]})"): yield res
                self.conf["connection_presets"] = ConfigSerializer.dump_json_list(self.connection_presets)
                await self.config_mgr.save_config()

            elif sub == "del":
                if len(args) < 2: yield event.plain_result("格式错误: #lmk del <预设名> <序号|all>"); return
                if args[0] not in self.connection_presets: yield event.plain_result(ResponsePresenter.item_not_found("预设", args[0])); return
                preset = self.connection_presets[args[0]]
                keys = preset.get("api_keys", [])
                new_keys = [] if args[1].lower() == "all" else (keys[:int(args[1])-1] + keys[int(args[1]):] if args[1].isdigit() and 1 <= int(args[1]) <= len(keys) else None)
                if new_keys is None: yield event.plain_result("❌ 序号无效。"); return
                async for res in self.config_mgr.perform_save_with_confirm(event, preset, "api_keys", new_keys, f"密钥组({args[0]})"): yield res
                self.conf["connection_presets"] = ConfigSerializer.dump_json_list(self.connection_presets)
                await self.config_mgr.save_config()

            else:
                target = args[0] if (sub == "list" and args) else parts[0]
                if target not in self.connection_presets: yield event.plain_result(ResponsePresenter.item_not_found("预设", target)); return
                keys = self.connection_presets[target].get("api_keys", [])
                yield event.plain_result(f"🔑 [{target}] Keys:\n" + ("\n".join(f"{i+1}. {k[:8]}...{k[-4:]}" for i, k in enumerate(keys)) if keys else "暂无"))
        except Exception as e:
            logger.error(f"Key 操作失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {e}")

    async def terminate(self):
        await ImageUtils.terminate()
        logger.info("[香蕉忍法帖] 插件已终止")