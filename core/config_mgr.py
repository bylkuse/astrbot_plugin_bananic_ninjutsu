import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple, Callable, TYPE_CHECKING

from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.utils.session_waiter import SessionController, session_waiter
from astrbot.api.star import Context

from ..utils.parser import CommandParser
from ..utils.serializer import ConfigSerializer
from ..utils.views import ResponsePresenter
from .prompt import PromptManager

if TYPE_CHECKING:
    from ..services.generation import GenerationService

class DataStrategy(ABC):
    """抽象基类"""
    def __init__(self, item_name: str, config_mgr: 'ConfigManager'):
        self.item_name = item_name
        self.mgr = config_mgr

    @abstractmethod
    def get_all_keys(self) -> List[str]:
        pass

    @abstractmethod
    def get_details(self, key: str) -> str | None:
        pass

    def get_list_summary(self) -> str:
        keys = self.get_all_keys()
        if not keys:
            return f"✨ {self.item_name}列表为空。"
        keys_str = ", ".join(keys)
        return f"✨ {self.item_name}名录:\n{keys_str}\n\n💡 使用 {self.mgr.main_prefix}lmc <名称> 查看详情。"

    @abstractmethod
    async def delete(self, key: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def update(self, event: AstrMessageEvent, key: str, args: List[str]) -> Tuple[bool, str]:
        pass

    async def handle_extra(self, event: AstrMessageEvent, cmd: str, args: List[str]) -> Any | None:
        return None

class DictDataStrategy(DataStrategy):
    def __init__(self, data: Dict[str, str], item_name: str, config_mgr, duplicate_type: str | None = None):
        super().__init__(item_name, config_mgr)
        self.data = data
        self.dup_type = duplicate_type

    def get_all_keys(self) -> List[str]:
        return sorted(self.data.keys())

    def get_details(self, key: str) -> str | None:
        if key not in self.data: return None
        return ResponsePresenter.format_preset_detail(self.item_name, key, self.data[key])

    async def delete(self, key: str) -> Tuple[bool, str]:
        if key not in self.data:
            return False, ResponsePresenter.item_not_found(self.item_name, key)
        if self.item_name == "优化预设" and key == "default":
            return False, "❌ default 预设不可删除。"
        del self.data[key]
        await self.mgr.save_config()
        return True, f"✅ 已删除 {self.item_name} [{key}]。"

    async def update(self, event: AstrMessageEvent, key: str, args: List[str]) -> Tuple[bool, str]:
        full_text = ""
        if args:
            full_text = key + " " + " ".join(args)
        else:
            full_text = key

        parsed = ConfigSerializer.parse_single_kv(full_text)
        if not parsed:
            parts = full_text.split(None, 1)
            if len(parts) == 2:
                parsed = (parts[0], parts[1])
            else:
                return False, f"❌ 格式错误。正确格式: {self.mgr.main_prefix}lmp <名称>:[内容] 或 <名称> [内容]"

        real_key, val = parsed

        if self.dup_type:
            val_str = str(val)
            dup = self.mgr.pm.check_duplicate(self.dup_type, val_str)
            if dup and dup != real_key:
                return False, ResponsePresenter.duplicate_item("内容于", dup) + " 无需重复添加。"

        async for res in self.mgr.perform_save_with_confirm(
            event, self.data, real_key, val, self.item_name
        ):
            await event.send(res)

        return True, ""

class ListKeyStrategy(DataStrategy):
    def __init__(
        self, 
        preset_name: str, 
        key_list: List[str], 
        config_mgr, 
        save_callback: Callable | None = None
    ):
        super().__init__("API Key", config_mgr)
        self.preset_name = preset_name
        self.data = key_list
        self.save_callback = save_callback

    def get_all_keys(self) -> List[str]:
        return [str(i+1) for i in range(len(self.data))]

    def get_list_summary(self) -> str:
        return ResponsePresenter.format_key_list(self.preset_name, self.data, self.mgr.main_prefix)

    def get_details(self, key: str) -> str | None:
        if key.lower() == "all":
            return self.get_list_summary()
        return None

    async def delete(self, key: str) -> Tuple[bool, str]:
        if key.lower() == "all":
            self.data.clear()
            msg = "🗑️ 已清空所有 Key。"
        elif key.isdigit():
            idx = int(key)
            if 1 <= idx <= len(self.data):
                self.data.pop(idx - 1)
                msg = f"🗑️ 已删除第 {idx} 个 Key。"
            else:
                return False, f"❌ 序号 {idx} 无效。"
        else:
            return False, "❌ 序号格式错误。"
        if self.save_callback:
            await self.save_callback()
        else:
            await self.mgr.save_config()

        summary = self.get_list_summary()
        return True, f"{msg}\n当前剩余: {len(self.data)} 个。\n\n{summary}"

    async def update(self, event: AstrMessageEvent, key: str, args: List[str]) -> Tuple[bool, str]:
        keys_to_add = [key] + args
        added = 0
        first_duplicate = None

        for k in keys_to_add:
            if not k: 
                continue

            if k not in self.data:
                self.data.append(k)
                added += 1
            else:
                if first_duplicate is None:
                    first_duplicate = k

        if added > 0:
            if self.save_callback:
                await self.save_callback()
            else:
                await self.mgr.save_config()

            summary = self.get_list_summary()
            return True, f"✅ 已添加 {added} 个 Key。\n\n{summary}"

        if first_duplicate:
            return False, ResponsePresenter.duplicate_item("API Key", first_duplicate) + " 无需重复添加。"

        return False, "❌ 未提供有效的 Key。"

class ConnectionStrategy(DataStrategy):
    def __init__(
        self, 
        data: Dict, 
        config_mgr: 'ConfigManager',
        generation_service: Any,
        raw_config: Dict[str, Any],
        save_callback: Callable | None = None
    ):
        super().__init__("连接预设", config_mgr)
        self.data = data
        self.gen_service = generation_service
        self.raw_config = raw_config
        self.save_callback = save_callback

    def get_all_keys(self) -> List[str]:
        return sorted(self.data.keys())

    @property
    def active_preset_name(self) -> str:
        return self.gen_service.conn_config.get("name", "None")

    def get_list_summary(self) -> str:
        if not self.data:
            return f"✨ {self.item_name}列表为空。"

        msg = [f"✨ {self.item_name}名录:"]
        for name, data in self.data.items():
            prefix = "➡️" if name == self.active_preset_name else "▪️"
            key_count = len(data.get('api_keys', []))
            msg.append(f"{prefix} {name} ({data.get('api_type', 'N/A')}, {key_count} keys)")

        msg.append(f"\n💡 使用 {self.mgr.main_prefix}lmc <名称> 查看详情。")
        return "\n".join(msg)

    def get_details(self, key: str) -> str | None:
        if key not in self.data: return None
        return ResponsePresenter.format_connection_detail(key, self.data[key], self.mgr.main_prefix)

    async def delete(self, key: str) -> Tuple[bool, str]:
        if key not in self.data: 
            return False, ResponsePresenter.item_not_found(self.item_name, key)

        del self.data[key]

        msg = f"✅ 已删除连接预设 [{key}]。"

        if self.active_preset_name == key:
            new_name = next(iter(self.data.keys()), None)

            if "Connection_Config" not in self.raw_config:
                self.raw_config["Connection_Config"] = {}

            if new_name:
                self.raw_config["Connection_Config"]["current_preset_name"] = new_name
                self.gen_service.set_active_preset(self.data[new_name])
                msg += f"\n⚠️ 当前连接已被删除，自动切换至: {new_name}"
            else:
                self.raw_config["Connection_Config"]["current_preset_name"] = "None"
                self.gen_service.set_active_preset({"name": "None", "api_keys": []})
                msg += "\n⚠️ 当前连接已被删除，且无备用连接。"

        if self.save_callback: 
            await self.save_callback() 
        else: 
            await self.mgr.save_config()

        return True, msg

    async def update(self, event: AstrMessageEvent, key: str, args: List[str]) -> Tuple[bool, str]:
        if not args and key != "add":
            return False, "❌ 参数不足。"

        if key == "add":
            if len(args) < 4: return False, "❌ 格式: add <name> <type> <url> <model> [keys]"
            name, type_, url, model = args[0], args[1], args[2], args[3]
            keys = args[4].split(",") if len(args) > 4 else []

            new_data = {"name": name, "api_type": type_, "api_url": url, "model": model, "api_keys": keys}

            async for res in self.mgr.perform_save_with_confirm(
                event, self.data, name, new_data, "连接预设", custom_save_func=self.save_callback
            ):
                await event.send(res)
            return True, ""

        target_name = key
        prop = args[0]
        val = args[1]

        if target_name not in self.data:
            return False, ResponsePresenter.item_not_found("预设", target_name)

        allowed = {"api_url", "model", "api_type", "api_base"}
        if prop not in allowed:
            return False, f"❌ 属性不可修改。可选: {allowed}"

        target_obj = self.data[target_name]

        async for res in self.mgr.perform_save_with_confirm(
            event, target_obj, prop, val, f"预设[{target_name}]的{prop}", custom_save_func=self.save_callback
        ):
            await event.send(res)

        return True, ""

    async def handle_extra(self, event, cmd, args) -> Any | None:
        if cmd == "to" and args:
            target = args[0]
            if target in self.data:
                if "Connection_Config" not in self.raw_config: 
                    self.raw_config["Connection_Config"] = {}
                self.raw_config["Connection_Config"]["current_preset_name"] = target
                self.gen_service.set_active_preset(self.data[target])

                if self.save_callback: await self.save_callback()
                else: await self.mgr.save_config()

                return event.plain_result(ResponsePresenter.format_connection_switch_success(target, self.data[target]))
            else:
                return event.plain_result(ResponsePresenter.item_not_found("预设", target))

        if cmd in ["debug", "d"]:
            if not self.mgr.is_admin(event):
                return event.plain_result(ResponsePresenter.unauthorized_admin())

            if "Basic_Config" not in self.mgr.conf:
                self.mgr.conf["Basic_Config"] = {}

            basic_conf = self.mgr.conf["Basic_Config"]
            new_state = not basic_conf.get("debug_prompt", False)
            basic_conf["debug_prompt"] = new_state
            await self.mgr.save_config()

            return event.plain_result(f"{'✅' if new_state else '❌'} 调试模式已{'开启' if new_state else '关闭'}。")

        return None

class ConfigManager:
    def __init__(
        self, config_obj: Any, prompt_manager: PromptManager, context: Context
    ):
        self.conf = config_obj
        self.pm = prompt_manager
        self.context = context

        raw_prefixes = context.get_config().get("command_prefixes", ["/"])
        if isinstance(raw_prefixes, str):
            raw_prefixes = [raw_prefixes]
        self.prefixes = sorted(raw_prefixes, key=len, reverse=True)
        self.main_prefix = self.prefixes[0] if self.prefixes else "#"

    def is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admins

    async def save_config(self):
        self.pm.sync_to_config()
        try:
            await asyncio.to_thread(self.conf.save_config)
        except Exception as e:
            raise RuntimeError(f"保存配置失败: {e}")

    async def save_connection_presets(self, presets: Dict[str, Any]):
        if "Connection_Config" not in self.conf:
            self.conf["Connection_Config"] = {}

        serialized_data = await asyncio.to_thread(
            ConfigSerializer.dump_json_list, 
            presets
        )
        self.conf["Connection_Config"]["connection_presets"] = serialized_data

        await self.save_config()

    async def perform_save_with_confirm(
        self,
        event: AstrMessageEvent,
        target_dict: Dict[str, Any],
        key: str,
        new_value: Any,
        item_name: str,
        custom_save_func: Callable | None = None
    ):
        """覆盖查验"""
        async def perform_save():
            target_dict[key] = new_value
            if custom_save_func:
                if asyncio.iscoroutinefunction(custom_save_func):
                    await custom_save_func()
                else:
                    custom_save_func()
            else:
                await self.save_config()
            yield event.plain_result(f"✅ 已保存{item_name} [{key}]。")

        if key in target_dict:
            old_value = target_dict[key]
            if old_value == new_value:
                yield event.plain_result(f"💡 {item_name} [{key}] 内容未变更。")
                return

            old_str = await asyncio.to_thread(ConfigSerializer.serialize_any, old_value)
            new_str = await asyncio.to_thread(ConfigSerializer.serialize_any, new_value)
            preview_old = old_str[:100] + "..." if len(old_str) > 100 else old_str
            preview_new = new_str[:100] + "..." if len(new_str) > 100 else new_str

            yield event.plain_result(
                f"⚠ {item_name} [{key}] 已存在，是否覆盖？（是/否 30秒倒计时）\n\n"
                f"🔻旧内容:\n{preview_old}\n\n"
                f"🔺新内容:\n{preview_new}"
            )

            @session_waiter(timeout=30, record_history_chains=False)
            async def confirmation_waiter(
                controller: SessionController, response_event: AstrMessageEvent
            ):
                resp = response_event.message_str.strip().lower()
                if resp in ["是", "yes", "y"]:
                    async for r in perform_save():
                        await response_event.send(r)
                    controller.stop()
                elif resp in ["否", "no", "n"]:
                    await response_event.send(
                        response_event.plain_result("❌ 操作已取消。")
                    )
                    controller.stop()

            try:
                await confirmation_waiter(event)
            except (asyncio.TimeoutError, TimeoutError):
                yield event.plain_result("⏰ 操作超时，已自动取消。")
        else:
            async for r in perform_save():
                yield r

    async def handle_crud_command(
        self, 
        event: AstrMessageEvent, 
        cmd_aliases: List[str], 
        strategy: DataStrategy,
        args_override: List[str] | None = None
    ):
        is_admin = self.is_admin(event)
        cmd_display = f"{self.main_prefix}{cmd_aliases[0]}"

        if args_override is not None:
            parts = args_override
        else:
            parsed = CommandParser.parse(event, cmd_aliases=cmd_aliases, prefixes=self.prefixes)
            parts = parsed.text.split()

        raw_sub = parts[0] if parts else ""
        sub_cmd = raw_sub.lower()
        args = parts[1:] if len(parts) > 1 else []

        if sub_cmd in ["l", "list"]:
            yield event.plain_result(strategy.get_list_summary())
            return

        if not parts:
            if isinstance(strategy, DictDataStrategy):
                keys = strategy.get_all_keys()
                if not keys:
                    yield event.plain_result(strategy.get_list_summary())
                    return

                lines = [f"✨ {strategy.item_name}列表 (详细):"]
                for k in keys:
                    content = strategy.data.get(k, "")
                    content_str = str(content).replace("\n", " ").strip()
                    preview = content_str[:30] + "..." if len(content_str) > 30 else content_str
                    lines.append(f"▪️ [{k}]: {preview}")

                lines.append(ResponsePresenter.presets_common(strategy.item_name, cmd_display, is_admin))
                yield event.plain_result("\n".join(lines))
                return

            yield event.plain_result(strategy.get_list_summary())
            return

        extra_res = await strategy.handle_extra(event, sub_cmd, args)
        if extra_res:
            yield extra_res
            return

        if sub_cmd == "del":
            if not is_admin:
                yield event.plain_result(ResponsePresenter.unauthorized_admin())
                return
            if not args:
                yield event.plain_result(f"❌ 格式错误: {cmd_display} del <名称>")
                return

            success, msg = await strategy.delete(args[0])
            yield event.plain_result(msg)
            return

        if not args and sub_cmd not in ["add", "ren"] and "=" not in raw_sub and ":" not in raw_sub:
            detail = strategy.get_details(raw_sub)
            if detail:
                yield event.plain_result(detail)
                return

            if isinstance(strategy, ListKeyStrategy):
                pass
            elif isinstance(strategy, ConnectionStrategy):
                pass 
            else:
                yield event.plain_result(ResponsePresenter.item_not_found(strategy.item_name, raw_sub))
                return

        if not is_admin:
             yield event.plain_result(ResponsePresenter.unauthorized_admin())
             return

        success, msg = await strategy.update(event, raw_sub, args)
        if msg:
            yield event.plain_result(msg)