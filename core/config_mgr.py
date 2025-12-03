import re
import json
import asyncio
from typing import Any, Dict, List, Optional, Callable, Awaitable
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.utils.session_waiter import SessionController, session_waiter
from astrbot.api.star import Context

from ..utils.serializer import ConfigSerializer
from ..utils.views import ResponsePresenter
from .prompt import PromptManager

class ConfigManager:
    """配置项"""
    def __init__(self, config_obj: Any, prompt_manager: PromptManager, context: Context):
        self.conf = config_obj
        self.pm = prompt_manager

        raw_prefixes = context.get_config().get("command_prefixes", ["/"])
        if isinstance(raw_prefixes, str): raw_prefixes = [raw_prefixes]
        self.prefixes = sorted(raw_prefixes, key=len, reverse=True)

    async def save_config(self):
        self.pm.sync_to_config()
        try:
            await asyncio.to_thread(self.conf.save_config)
        except Exception as e:
            raise RuntimeError(f"保存配置失败: {e}")

    def strip_command(self, text: str, command_aliases: List[str]) -> str:
        """剥离指令"""
        sorted_aliases = sorted(command_aliases, key=len, reverse=True)

        prefix_pattern = "|".join(re.escape(p) for p in self.prefixes)
        alias_pattern = "|".join(re.escape(c) for c in sorted_aliases)
        
        pattern = fr'^({prefix_pattern})?({alias_pattern})\s*'
        return re.sub(pattern, '', text, count=1, flags=re.IGNORECASE).strip()

    async def perform_save_with_confirm(self, event: AstrMessageEvent, 
                                   target_dict: Dict[str, Any], 
                                   key: str, 
                                   new_value: Any, 
                                   item_name: str):
        """覆盖查验"""
        async def perform_save():
            target_dict[key] = new_value
            await self.save_config()
            yield event.plain_result(f"✅ 已保存{item_name} [{key}]。")

        if key in target_dict:
            old_value = target_dict[key]
            if old_value == new_value:
                yield event.plain_result(f"💡 {item_name} [{key}] 内容未变更。")
                return

            old_str = json.dumps(old_value, sort_keys=True, ensure_ascii=False) if isinstance(old_value, (dict, list)) else str(old_value)
            new_str = json.dumps(new_value, sort_keys=True, ensure_ascii=False) if isinstance(new_value, (dict, list)) else str(new_value)

            preview_old = old_str[:100] + "..." if len(old_str) > 100 else old_str
            preview_new = new_str[:100] + "..." if len(new_str) > 100 else new_str

            yield event.plain_result(
                f"⚠ {item_name} [{key}] 已存在，是否覆盖？（是/否 30秒倒计时）\n\n"
                f"🔻旧内容:\n{preview_old}\n\n"
                f"🔺新内容:\n{preview_new}"
            )

            @session_waiter(timeout=30, record_history_chains=False)
            async def confirmation_waiter(controller: SessionController, response_event: AstrMessageEvent):
                resp = response_event.message_str.strip().lower()
                if resp in ["是", "yes", "y"]:
                    async for r in perform_save(): await response_event.send(r)
                    controller.stop()
                elif resp in ["否", "no", "n"]:
                    await response_event.send(response_event.plain_result("❌ 操作已取消。"))
                    controller.stop()
            try:
                await confirmation_waiter(event)
            except (asyncio.TimeoutError, TimeoutError):
                yield event.plain_result("⏰ 操作超时，已自动取消。")
        else:
            async for r in perform_save(): yield r

    async def handle_crud_command(self, event: AstrMessageEvent, cmd_list: List[str], target_dict: Dict, item_name: str, is_admin: bool, 
                                  after_delete_callback: Optional[Callable[[str], Awaitable[None]]] = None, 
                                  extra_cmd_handler: Optional[Callable[[AstrMessageEvent, List[str]], Awaitable[Any]]] = None, duplicate_check_type: Optional[str] = None):
        """增删改查"""
        cmd_text = self.strip_command(event.message_str.strip(), cmd_list)
        parts = cmd_text.split()

        cmd_display_name = f"#{cmd_list[0]}"

        if extra_cmd_handler:
            result = await extra_cmd_handler(event, parts)
            if result:
                yield result
                return

        # 字典操作
        is_handled = False
        async for res in self._handle_standard_dict_ops(event, parts, target_dict, item_name, cmd_display_name, is_admin, after_delete_callback):
            yield res
            is_handled = True

        if is_handled: return

        # 增改
        add_key, add_value = None, None
        if parsed := ConfigSerializer.parse_single_kv(cmd_text):
            add_key, add_value = parsed
            if duplicate_check_type and isinstance(add_value, str):
                dup_key = self.pm.check_duplicate(duplicate_check_type, add_value)
                if dup_key and dup_key != add_key:
                    yield event.plain_result(ResponsePresenter.duplicate_item("内容", dup_key) + " 无需重复添加。")
                    return

            async for res in self.perform_save_with_confirm(event, target_dict, add_key, add_value, item_name):
                yield res
            return

        if parts and not is_handled and parts[0] not in ["l", "list", "del", "ren"]:
            yield event.plain_result(ResponsePresenter.item_not_found(item_name, parts[0]))

    async def _handle_standard_dict_ops(self, event, cmd_parts, target_dict, item_name, cmd_display_name, is_admin, on_delete_callback):
        sub = cmd_parts[0].lower() if cmd_parts else ""

        if sub in ["l", "list"] or not cmd_parts:
            if not target_dict:
                yield event.plain_result(f"✨ {item_name}列表为空。")
                return

            if sub in ["l", "list"]:
                keys_str = ", ".join(sorted(target_dict.keys()))
                yield event.plain_result(
                    f"✨ {item_name}名录 (共{len(target_dict)}个):\n"
                    f"{keys_str}\n\n"
                    f"💡 使用 {cmd_display_name} <名称> 查看具体内容。"
                )
                return

            msg_lines = [f"✨ {item_name}列表 (详细):"]
            for name in sorted(target_dict.keys()):
                content = target_dict[name]
                content_str = str(content)
                preview = content_str[:30] + "..." if len(content_str) > 30 else content_str
                msg_lines.append(f"▪️ [{name}]: {preview}")
            msg_lines.append("\n" + ResponsePresenter.presets_common(item_name, cmd_display_name, is_admin))
            yield event.plain_result("\n".join(msg_lines))
            return

        if sub == "del":
            if not is_admin:
                yield event.plain_result(ResponsePresenter.unauthorized_admin())
                return
            if len(cmd_parts) < 2:
                yield event.plain_result(f"格式错误: {cmd_display_name} del <名称>")
                return

            key = cmd_parts[1]
            if key not in target_dict:
                yield event.plain_result(ResponsePresenter.item_not_found(item_name, key))
                return
            if item_name == "优化预设" and key == "default":
                yield event.plain_result("❌ default 预设不可删除。")
                return

            del target_dict[key]
            if on_delete_callback: await on_delete_callback(key)
            await self.save_config()
            yield event.plain_result(f"✅ 已删除 {item_name} [{key}]。")
            return

        if sub == "ren":
            if not is_admin:
                yield event.plain_result(ResponsePresenter.unauthorized_admin())
                return
            if len(cmd_parts) < 3:
                yield event.plain_result(f"格式错误: {cmd_display_name} ren <旧名> <新名>")
                return

            old_k, new_k = cmd_parts[1], cmd_parts[2]
            if old_k not in target_dict:
                yield event.plain_result(ResponsePresenter.item_not_found(item_name, old_k))
                return
            if new_k in target_dict:
                yield event.plain_result(ResponsePresenter.duplicate_item(item_name, new_k))
                return

            if item_name == "优化预设" and old_k == "default":
                yield event.plain_result("❌ default 预设不可重命名。")
                return

            target_dict[new_k] = target_dict.pop(old_k)
            await self.save_config()
            yield event.plain_result(f"✅ 已重命名: [{old_k}] -> [{new_k}]。")
            return

        if len(cmd_parts) == 1:
            key = cmd_parts[0]
            if key in target_dict:
                content = target_dict[key]
                if isinstance(content, dict):
                    content = json.dumps(content, indent=2, ensure_ascii=False)
                yield event.plain_result(f"📝 {item_name} [{key}] 内容:\n{content}")
                return