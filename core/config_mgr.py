import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple, Callable, TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.utils.session_waiter import SessionController, session_waiter
from astrbot.api.star import Context

from ..api_client import ApiRequestConfig, KeyStatus
from ..utils.parser import CommandParser
from ..utils.serializer import ConfigSerializer
from ..utils.views import ResponsePresenter
from .prompt import PromptManager

if TYPE_CHECKING:
    from ..services.generation import GenerationService

def _extract_message_id(resp: Any) -> int | None:
    if not resp:
        return None
    try:
        return int(resp)
    except (ValueError, TypeError):
        pass

    if isinstance(resp, dict):
        if "data" in resp and isinstance(resp["data"], dict):
            mid = resp["data"].get("message_id")
            if mid:
                return int(mid)
        if "message_id" in resp:
            return int(resp["message_id"])

    if hasattr(resp, "message_id"):
        try:
            return int(resp.message_id)
        except (ValueError, TypeError):
            pass

    return None

async def _send_message(event: AstrMessageEvent, payload: Any) -> int | None:
    if hasattr(event, "_parse_onebot_json") and hasattr(event.bot, "call_action"):
        try:
            chain = payload.chain if hasattr(payload, "chain") else payload
            if isinstance(chain, list):
                msg_chain = MessageChain(chain=chain)
                obmsg = await event._parse_onebot_json(msg_chain)
                params = {"message": obmsg}

                if gid := event.get_group_id():
                    params["group_id"] = int(gid)
                    action = "send_group_msg"
                elif uid := event.get_sender_id():
                    params["user_id"] = int(uid)
                    action = "send_private_msg"
                else:
                    raise ValueError("无法确定发送目标")

                resp = await event.bot.call_action(action, **params)
                return _extract_message_id(resp)
        except Exception as e:
            pass
    resp = await event.send(payload)
    return _extract_message_id(resp)

async def _safe_recall(event: AstrMessageEvent, message_obj: Any):
    if not message_obj:
        return

    msg_id = _extract_message_id(message_obj)

    if msg_id:
        try:
            if hasattr(event.bot, "delete_msg"):
                await event.bot.delete_msg(message_id=msg_id)
            elif hasattr(event.bot, "recall_message"):
                await event.bot.recall_message(msg_id)
        except Exception as e:
            logger.debug(f"撤回等待消息失败 (可忽略): {e}")
    else:
        logger.debug(f"无法从对象中提取 message_id: {message_obj}")

class DataStrategy(ABC):
    """抽象基类"""
    def __init__(self, item_name: str, config_mgr: 'ConfigManager'):
        self.item_name = item_name
        self.mgr = config_mgr

    async def process(self, event: AstrMessageEvent, sub_cmd: str, args: List[str]):
        if sub_cmd in ["l", "list"] or (not sub_cmd and not args):
            summary = self.get_summary(simple=(sub_cmd in ["l", "list"]))
            yield event.plain_result(summary)
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

    async def process(self, event: AstrMessageEvent, sub_cmd: str, args: List[str]):
        if sub_cmd in ["l", "list"] or (not sub_cmd and not args):
            simple_mode = (sub_cmd in ["l", "list"])
            keys = sorted(self.data.keys())

            if not keys:
                yield event.plain_result(f"✨ {self.item_name}列表为空。")
                return

            header_title = "名录" if simple_mode else "列表 (详细)"
            header_text = f"✨ {self.item_name}{header_title} (共{len(keys)}条):"
            yield event.plain_result(header_text)

            if simple_mode:
                # 简略模式 (按字符数分包)
                CHAR_LIMIT = 3000

                buffer = []
                current_len = 0

                for k in keys:
                    delta_len = len(k) + 2

                    if current_len + delta_len > CHAR_LIMIT:

                        msg = ", ".join(buffer)
                        yield event.plain_result(msg)

                        buffer = [k]
                        current_len = len(k)
                        await asyncio.sleep(0.2)
                    else:
                        buffer.append(k)
                        current_len += delta_len

                if buffer:
                    yield event.plain_result(", ".join(buffer))

            else:
                # 详细模式 (按条目数分包)
                BATCH_SIZE = 150

                current_batch = []

                for k in keys:
                    content = str(self.data.get(k, "")).replace("\n", " ").strip()
                    preview = content[:25] + "..." if len(content) > 25 else content
                    current_batch.append(f"▪️ [{k}]: {preview}")

                    if len(current_batch) >= BATCH_SIZE:
                        msg = "\n".join(current_batch)
                        yield event.plain_result(msg)
                        current_batch = []
                        await asyncio.sleep(0.2)

                if current_batch:
                    yield event.plain_result("\n".join(current_batch))

            cmd_p = self.mgr.main_prefix
            footer = f"💡 指令: {cmd_p}{self.cmd_name} <名> (查看) | {cmd_p}{self.cmd_name} :<关键词> (搜索) | {cmd_p}{self.cmd_name} <名>:[内容] (添加/修改)"
            yield event.plain_result(footer)
            return

        async for res in super().process(event, sub_cmd, args):
            yield res

    def get_summary(self, simple: bool = False) -> str:
        return ""

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
        api_client = None,
        preset_config: Dict[str, Any] = None,
        raw_config: Dict[str, Any] = None,
        save_callback: Callable | None = None
    ):
        super().__init__("API Key", config_mgr)
        self.preset_name = preset_name
        self.data = key_list
        self.api_client = api_client
        self.preset_config = preset_config or {}
        self.raw_config = raw_config or {}
        self.save_callback = save_callback
        self.status_map: Dict[str, str] = {}

    def get_summary(self, simple: bool = False) -> str:
        return ResponsePresenter.format_key_list(
            self.preset_name, 
            self.data, 
            self.mgr.main_prefix,
            status_map=self.status_map
        )

    async def process(self, event: AstrMessageEvent, sub_cmd: str, args: List[str]):
        if sub_cmd in ["l", "list"] or (not sub_cmd and not args):
            if not self.data:
                yield event.plain_result(f"✨ {self.item_name}列表为空。")
                return

            waiting_msg = None
            if self.api_client and self.preset_config:
                waiting_msg_id = await _send_message(
                    event,
                    event.plain_result(f"🔍 正在检测 {len(self.data)} 个密钥的可用性，请稍候...")
                )
                await self._check_keys_parallel()

            yield event.plain_result(self.get_summary())
            await _safe_recall(event, waiting_msg_id)
            return

        async for res in super().process(event, sub_cmd, args):
            yield res

    async def _check_keys_parallel(self):
        conn_conf = self.raw_config.get("Connection_Config", {})
        use_proxy = conn_conf.get("use_proxy", False)
        proxy_url = conn_conf.get("proxy_url") if use_proxy else None
        base_request_config = ApiRequestConfig(
            api_keys=[],
            api_type=self.preset_config.get("api_type", "google"),
            api_base=self.preset_config.get("api_url", ""),
            proxy_url=proxy_url
        )
        semaphore = asyncio.Semaphore(5)
        async def check_single(key: str):
            async with semaphore:
                status = await self.api_client.test_key_availability(key, base_request_config)
                self.status_map[key] = status
        tasks = [check_single(k) for k in self.data]
        if tasks:
            await asyncio.gather(*tasks)

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
                target_data = self.data[key]
                waiting_msg_id = await _send_message(
                    event, 
                    event.plain_result(f"🔍 正在连接服务器获取 [{key}] 的可用模型列表...")
                )

                temp_conf = ApiRequestConfig(
                    api_keys=target_data.get("api_keys", []),
                    api_type=target_data.get("api_type", "google"),
                    api_base=target_data.get("api_url", ""),
                    proxy_url=self.raw_config.get("Connection_Config", {}).get("proxy_url")
                )

                fetched_models = []
                if self.gen_service and self.gen_service.api_client:
                    fetched_models = await self.gen_service.api_client.get_available_models(temp_conf)
                yield event.plain_result(
                    ResponsePresenter.format_connection_detail(
                        key, 
                        target_data, 
                        self.mgr.main_prefix, 
                        available_models=fetched_models
                    )
                )
                await _safe_recall(event, waiting_msg_id)
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
        self, 
        config_obj: Any, 
        prompt_manager: PromptManager, 
        context: Context,
        prefixes: List[str]
    ):
        self.conf = config_obj
        self.pm = prompt_manager
        self.context = context
        self.prefixes = prefixes
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