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

    async def process(self, event: AstrMessageEvent, sub_cmd: str, args: List[str]):
        if sub_cmd in ["l", "list"] or (not sub_cmd and not args):
            yield event.plain_result(self.get_summary(simple=(sub_cmd == "l")))
            return

        extra_res = await self.handle_custom_command(event, sub_cmd, args)
        if extra_res:
            yield extra_res
            return

        if not self.mgr.is_admin(event):
            yield event.plain_result(ResponsePresenter.unauthorized_admin())
            return

        if sub_cmd == "del":
            if not args:
                yield event.plain_result(f"❌ 格式错误: 请指定要删除的{self.item_name}名称。")
                return
            _, msg = await self.do_delete(args[0])
            if msg: yield event.plain_result(msg)

        elif sub_cmd == "ren":
            if len(args) < 2:
                yield event.plain_result(f"❌ 格式错误: ren <旧名> <新名>")
                return
            _, msg = await self.do_rename(args[0], args[1])
            if msg: yield event.plain_result(msg)

        elif sub_cmd == "add":
            async for res in self.do_add(event, args):
                yield res
            
        else:
            async for res in self.do_update_or_view(event, sub_cmd, args):
                yield res

    @abstractmethod
    def get_summary(self, simple: bool = False) -> str:
        pass

    async def handle_custom_command(self, event, cmd, args) -> Any | None:
        return None

    async def do_delete(self, key: str) -> Tuple[bool, str]:
        return False, "❌ 该类型不支持删除操作。"

    async def do_rename(self, old_key: str, new_key: str) -> Tuple[bool, str]:
        return False, "❌ 该类型不支持重命名操作。"

    async def do_add(self, event, args: List[str]) -> Any:
        yield event.plain_result("❌ 请使用 update 格式直接添加。")

    @abstractmethod
    async def do_update_or_view(self, event, key: str, args: List[str]) -> Any:
        pass

    async def generic_rename(self, old_key: str, new_key: str, rename_logic: Callable[[str, str], None]) -> Tuple[bool, str]:
        if old_key not in self.data:
            return False, ResponsePresenter.item_not_found(self.item_name, old_key)
        if new_key in self.data:
            return False, f"❌ 重命名失败: {self.item_name} [{new_key}] 已存在。"

        if self.item_name == "优化预设" and old_key == "default":
            return False, "❌ 'default' 是系统保留的核心预设，禁止重命名。"

        rename_logic(old_key, new_key)

        if hasattr(self, 'save_callback') and self.save_callback:
            await self.save_callback()
        else:
            await self.mgr.save_config()

        return True, f"✅ 已将 {self.item_name} [{old_key}] 重命名为 [{new_key}]。"

class DictDataStrategy(DataStrategy):
    def __init__(self, data: Dict[str, str], item_name: str, config_mgr, duplicate_type: str | None = None, 
        cmd_name: str = "lmp"):
        super().__init__(item_name, config_mgr)
        self.data = data
        self.dup_type = duplicate_type
        self.cmd_name = cmd_name

    def get_summary(self, simple: bool = False) -> str:
        keys = sorted(self.data.keys())
        if not keys:
            return f"✨ {self.item_name}列表为空。"

        if simple:
            return f"✨ {self.item_name}名录:\n" + ", ".join(keys)

        lines = [f"✨ {self.item_name}列表 (详细):"]
        for k in keys:
            content = str(self.data.get(k, "")).replace("\n", " ").strip()
            preview = content[:30] + "..." if len(content) > 30 else content
            lines.append(f"▪️ [{k}]: {preview}")

        cmd_p = self.mgr.main_prefix
        lines.append(f"\n💡 指令: {cmd_p}{self.cmd_name} <名> (查看) | {cmd_p}{self.cmd_name} <名>:[内容] (添加/修改)")
        return "\n".join(lines)

    async def do_delete(self, key: str) -> Tuple[bool, str]:
        if key not in self.data:
            return False, ResponsePresenter.item_not_found(self.item_name, key)
        if self.item_name == "优化预设" and key == "default":
            return False, "❌ default 预设不可删除。"
        del self.data[key]
        await self.mgr.save_config()
        return True, f"✅ 已删除 {self.item_name} [{key}]。"

    async def do_rename(self, old_key: str, new_key: str) -> Tuple[bool, str]:
        def logic(o, n):
            self.data[n] = self.data.pop(o)
        return await self.generic_rename(old_key, new_key, logic)

    async def do_update_or_view(self, event, key: str, args: List[str]) -> Any:
        full_text = key + " " + " ".join(args) if args else key

        if full_text.startswith(":") and len(full_text) > 1:
            keyword = full_text[1:].strip().lower()
            found = []

            for k, v in self.data.items():
                if keyword in k.lower() or keyword in str(v).lower():
                    found.append((k, v))

            if not found:
                yield event.plain_result(f"🔍 未找到包含关键词 [{keyword}] 的{self.item_name}。")
            else:
                msg_lines = [f"🔍 搜索 [{keyword}] 结果 (共{len(found)}条):"]
                for k, v in found:
                    preview = str(v).replace("\n", " ")
                    if len(preview) > 50:
                        preview = preview[:50] + "..."
                    msg_lines.append(f"▪️ **{k}**: {preview}")
                yield event.plain_result("\n".join(msg_lines))
            return

        parsed = ConfigSerializer.parse_single_kv(full_text)
        if not parsed and (not args and ":" not in key):
            detail = self.data.get(key)
            if detail:
                yield event.plain_result(ResponsePresenter.format_preset_detail(self.item_name, key, detail))
            else:
                yield event.plain_result(ResponsePresenter.item_not_found(self.item_name, key))
            return

        if parsed:
            real_key, val = parsed
        else:
            parts = full_text.split(None, 1)
            if len(parts) == 2:
                real_key, val = parts[0], parts[1]
            else:
                yield event.plain_result(f"❌ 格式错误。正确格式: <名称>:[内容] 或 <名称> [内容]")
                return

        if self.dup_type:
            dup = self.mgr.pm.check_duplicate(self.dup_type, str(val))
            if dup and dup != real_key:
                yield event.plain_result(ResponsePresenter.duplicate_item("内容于", dup) + " 无需重复添加。")
                return

        async for res in self.mgr.perform_save_with_confirm(
            event, self.data, real_key, val, self.item_name
        ):
            yield res


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

    def get_summary(self, simple: bool = False) -> str:
        return ResponsePresenter.format_key_list(self.preset_name, self.data, self.mgr.main_prefix)

    async def do_delete(self, key: str) -> Tuple[bool, str]:
        if key.lower() == "all":
            self.data.clear()
            msg = "🗑️ 已清空所有 Key。"
        elif key.isdigit():
            idx = int(key)
            if 1 <= idx <= len(self.data):
                self.data.pop(idx - 1)
                summary = self.get_summary()
                msg = f"🗑️ 已删除第 {idx} 个 Key。\n\n{summary}"
            else:
                return False, f"❌ 序号 {idx} 无效。"
        else:
            return False, "❌ 序号格式错误。"

        if self.save_callback: await self.save_callback()
        else: await self.mgr.save_config()

        return True, f"{msg}\n当前剩余: {len(self.data)} 个。"

    async def do_update_or_view(self, event, key: str, args: List[str]) -> Any:
        keys_to_add = [key] + args
        added = 0
        first_duplicate = None

        for k in keys_to_add:
            if not k: continue
            if k not in self.data:
                self.data.append(k)
                added += 1
            else:
                if first_duplicate is None: first_duplicate = k

        if added > 0:
            if self.save_callback: await self.save_callback()
            else: await self.mgr.save_config()

            summary = self.get_summary()
            yield event.plain_result(f"✅ 已添加 {added} 个 Key。\n\n{summary}")
        elif first_duplicate:
            yield event.plain_result(ResponsePresenter.duplicate_item("API Key", first_duplicate) + " 无需重复添加。")
        else:
            yield event.plain_result("❌ 未提供有效的 Key。")


class ConnectionStrategy(DataStrategy):
    def __init__(
        self, 
        data: Dict, 
        config_mgr: 'ConfigManager',
        generation_service: 'GenerationService',
        raw_config: Dict[str, Any],
        save_callback: Callable | None = None
    ):
        super().__init__("连接预设", config_mgr)
        self.data = data
        self.gen_service = generation_service
        self.raw_config = raw_config
        self.save_callback = save_callback

    @property
    def active_preset_name(self) -> str:
        return self.gen_service.conn_config.get("name", "None")

    def get_summary(self, simple: bool = False) -> str:
        if not self.data:
            return f"✨ {self.item_name}列表为空。"

        if simple:
            keys_str = ", ".join(sorted(self.data.keys()))
            return f"✨ {self.item_name}名录:\n{keys_str}"

        msg = [f"✨ {self.item_name}名录:"]
        for name, data in self.data.items():
            prefix = "➡️" if name == self.active_preset_name else "▪️"
            key_count = len(data.get('api_keys', []))
            msg.append(f"{prefix} {name} ({data.get('api_type', 'N/A')}, {key_count} keys)")

        msg.append(f"\n💡 使用 {self.mgr.main_prefix}lmc <名称> 查看详情。")
        return "\n".join(msg)

    async def handle_custom_command(self, event, cmd, args) -> Any | None:
        cmd_lower = cmd.lower()
        if cmd == "to":
            if not args:
                return event.plain_result("❌ 请指定要切换的预设名称。")
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

    async def do_add(self, event, args: List[str]) -> Any:
        if len(args) < 4: 
            yield event.plain_result("❌ 格式: add <name> <type> <url> <model> [keys]")
            return

        name, type_, url, model = args[0], args[1], args[2], args[3]
        keys = args[4].split(",") if len(args) > 4 else []

        if name in self.data:
            yield event.plain_result(ResponsePresenter.duplicate_item("连接预设", name))
            return

        new_data = {"name": name, "api_type": type_, "api_url": url, "model": model, "api_keys": keys}

        async for res in self.mgr.perform_save_with_confirm(
            event, self.data, name, new_data, "连接预设", custom_save_func=self.save_callback
        ):
            yield res

    async def do_delete(self, key: str) -> Tuple[bool, str]:
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

        if self.save_callback: await self.save_callback() 
        else: await self.mgr.save_config()

        return True, msg

    async def do_rename(self, old_key: str, new_key: str) -> Tuple[bool, str]:
        def logic(o, n):
            val = self.data.pop(o)
            if isinstance(val, dict): val["name"] = n
            self.data[n] = val
            if self.raw_config.get("Connection_Config", {}).get("current_preset_name") == o:
                self.raw_config["Connection_Config"]["current_preset_name"] = n
                self.gen_service.set_active_preset(val)
        return await self.generic_rename(old_key, new_key, logic)

    async def do_update_or_view(self, event, key: str, args: List[str]) -> Any:
        if not args:
            if key not in self.data: 
                yield event.plain_result(ResponsePresenter.item_not_found(self.item_name, key))
            else:
                yield event.plain_result(ResponsePresenter.format_connection_detail(key, self.data[key], self.mgr.main_prefix))
            return

        if len(args) < 2:
            yield event.plain_result(f"❌ 格式错误: {self.mgr.main_prefix}lmc <预设名> <属性> <值>")
            return

        target_name = key
        prop = args[0]
        val = args[1]

        if target_name not in self.data:
            yield event.plain_result(ResponsePresenter.item_not_found("预设", target_name))
            return

        allowed = {"api_url", "model", "api_type", "api_base"}
        if prop not in allowed:
            yield event.plain_result(f"❌ 属性不可修改。可选: {allowed}")
            return

        target_obj = self.data[target_name]

        async for res in self.mgr.perform_save_with_confirm(
            event, target_obj, prop, val, f"预设[{target_name}]的{prop}", custom_save_func=self.save_callback
        ):
            yield res


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
        if args_override is not None:
            parts = args_override
        else:
            parsed = CommandParser.parse(event, cmd_aliases=cmd_aliases, prefixes=self.prefixes)
            parts = parsed.text.split()

        sub_cmd = parts[0] if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        async for res in strategy.process(event, sub_cmd, args):
            yield res