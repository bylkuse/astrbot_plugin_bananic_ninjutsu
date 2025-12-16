import asyncio
from typing import List, Optional, Tuple

from astrbot.api import logger
from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from ..domain.model import ConnectionPreset, ApiType, GenerationConfig, ApiRequest
from ..domain.prompt import PromptResolver
from ..services.config import ConfigService, KVHelper
from ..services.stats import StatsService
from ..services.resource import ResourceService
from ..providers.manager import ProviderManager
from ..utils.parser import CommandParser
from ..views import ResponsePresenter
from .platform import PlatformAdapter

class ManagementHandler:
    def __init__(
        self,
        config_service: ConfigService,
        stats_service: StatsService,
        provider_manager: ProviderManager,
        prompt_resolver: PromptResolver,
        admin_ids: List[str]
    ):
        self.cfg = config_service
        self.stats = stats_service
        self.provider_mgr = provider_manager
        self.prompt_resolver = prompt_resolver
        self.admin_ids = admin_ids

    async def _check_admin(self, adapter: PlatformAdapter, is_admin: bool) -> bool:
        if not is_admin:
            await adapter.send_text(ResponsePresenter.unauthorized_admin())
            return False
        return True

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in self.admin_ids

    # 预设管理 (lmp/lmo)

    async def handle_preset_cmd(self, event: AstrMessageEvent, cmd_name: str, is_optimizer: bool):
        adapter = PlatformAdapter(event)

        # 1. 准备上下文
        if is_optimizer:
            data = self.cfg.optimizers
            item_name = "优化预设"
            update_func = self.cfg.update_optimizer
            del_func = self.cfg.delete_optimizer
            check_dup_func = self.cfg.find_optimizer_by_value
            ren_func = self.cfg.rename_optimizer
        else:
            data = self.cfg.prompts
            item_name = "生图预设"
            update_func = self.cfg.update_prompt
            del_func = self.cfg.delete_prompt
            check_dup_func = self.cfg.find_prompt_by_value
            ren_func = self.cfg.rename_prompt

        # 2. 解析
        prefixes = self.cfg.get_prefixes()
        parsed = CommandParser.parse(
            adapter.message_str.strip(),
            prefixes=prefixes,
            cmd_aliases={cmd_name, "lm预设", "lm优化", "lmp", "lmo"}
        )
        args = parsed.args

        # 3. 分发逻辑
        is_list_cmd = not args or args[0].lower() in ["l", "list"]
        if is_list_cmd:
            keys = sorted(data.keys())

            if not keys:
                await adapter.send_text(f"✨ {item_name}列表为空。")
                return

            is_simple = len(args) > 0 and args[0].lower() in ["l", "list"]
            cmd_p = self.cfg.get_display_prefix()

            full_text = ResponsePresenter.preset_list(
                data,
                item_name,
                p=cmd_p,
                cmd=cmd_name,
                simple_mode=is_simple
            )

            view_lines = full_text.split('\n')
            header = view_lines[0] if view_lines else f"✨ {item_name}列表"
            content = view_lines[1:] if view_lines else []

            await adapter.send_text_as_nodes(content, header=header)
            return

        sub_cmd = args[0]

        if sub_cmd.lower() == "del":
            if not await self._check_admin(adapter, self._is_admin(event)): return
            if len(args) < 2:
                await adapter.send_text(f"❌ 请指定要删除的{item_name}名称。")
                return
            name = args[1]
            success = await del_func(name)
            if success:
                await adapter.send_text(f"✅ 已删除 {item_name} [{name}]。")
            else:
                await adapter.send_text(f"❌ 删除失败: {item_name} [{name}] 不存在或受保护。")
            return

        if sub_cmd.lower() == "ren":
            if not await self._check_admin(adapter, self._is_admin(event)): return
            if len(args) < 3:
                await adapter.send_text("❌ 格式错误: ren <旧名> <新名>")
                return
            old, new = args[1], args[2]
            success = await ren_func(old, new)
            if success:
                await adapter.send_text(f"✅ 已重命名: [{old}] -> [{new}]")
            else:
                await adapter.send_text(f"❌ 重命名失败: 可能旧名不存在、新名已存在或为保留项。")
            return

        # 搜索
        full_arg_str = " ".join(args)
        if full_arg_str.startswith(":") and len(full_arg_str) > 1:
            keyword = full_arg_str[1:].strip()
            found = []
            for k, v in data.items():
                if keyword.lower() in k.lower() or keyword.lower() in v.lower():
                    found.append((k, v))

            full_search_text = ResponsePresenter.search_result(keyword, found)
            search_lines = full_search_text.split('\n')

            search_header = search_lines[0] if search_lines else "🔍 搜索结果"
            search_body = search_lines[1:] if search_lines else []

            if not found:
                await adapter.send_text(full_search_text)
            else:
                await adapter.send_text_as_nodes(search_body, header=search_header)
            return

        # 增改
        kv = KVHelper.parse(full_arg_str)
        if kv:
            key, val = kv
            if not await self._check_admin(adapter, self._is_admin(event)): return

            dup_key = check_dup_func(val)
            if dup_key and dup_key != key:
                await adapter.send_text(ResponsePresenter.duplicate_item(item_name, dup_key) + " 无需重复添加。")
                return

            if key in data:
                old_val = data[key]
                if old_val == val:
                    await adapter.send_text(f"💡 {item_name} [{key}] 内容未变更。")
                    return

                confirm_text = ResponsePresenter.overwrite_confirmation(item_name, key, old_val, val)
                confirm_msg_id = await adapter.send_text(confirm_text)

                @session_waiter(timeout=30, record_history_chains=False)
                async def _waiter(controller: SessionController, ctx: AstrMessageEvent):
                    text = ctx.message_str.strip().lower()
                    if text in ["是", "y", "yes"]:
                        await update_func(key, val)
                        await ctx.send(ctx.plain_result(f"✅ 已更新 {item_name} [{key}]。"))
                        controller.stop()
                    elif text in ["否", "n", "no"]:
                        await ctx.send(ctx.plain_result("❌ 操作已取消。"))
                        controller.stop()

                try:
                    await _waiter(event)
                except (asyncio.TimeoutError, TimeoutError):
                    await adapter.send_text("⌛ 操作超时，已取消。")
                except Exception as e:
                    logger.error(f"[Ninjutsu] 会话等待异常: {e}")
                    await adapter.send_text("❌ 发生未知错误，操作取消。")
                finally:
                    if confirm_msg_id:
                        await adapter.recall_message(confirm_msg_id)
            else:
                await update_func(key, val)
                await adapter.send_text(f"✅ 已添加 {item_name} [{key}]。")
            return

        # 详情
        key = args[0]
        content = data.get(key)
        if content:
            var_definitions = self.prompt_resolver.get_definitions()
            detail_text = ResponsePresenter.preset_detail(item_name, key, content, var_definitions)
            await adapter.send_text_as_nodes(detail_text.split('\n'))
        else:
            await adapter.send_text(f"❌ {item_name} [{key}] 不存在。")

    # 连接管理 (lmc)

    def _render_preset_feedback(self, success_msg: str, preset: ConnectionPreset) -> str:
        detail = ResponsePresenter.connection_detail(preset, simple_mode=True)
        return f"{success_msg}\n\n{detail}"

    async def handle_connection_cmd(self, event: AstrMessageEvent):
        adapter = PlatformAdapter(event)
        prefixes = self.cfg.get_prefixes()
        cmd_p = self.cfg.get_display_prefix()

        parsed = CommandParser.parse(
            adapter.message_str.strip(),
            prefixes=prefixes,
            cmd_aliases={"lmc", "lm连接"}
        )
        args = parsed.args

        if not args or args[0].lower() in ["l", "list"]:
            msg = ResponsePresenter.connection_list_summary(
                self.cfg.connections, 
                self.cfg.active_preset_name,
                p=cmd_p
            )
            await adapter.send_text_as_nodes(msg.split('\n'))
            return

        sub_cmd = args[0]
        is_admin = self._is_admin(event)

        if sub_cmd.lower() == "to":
            if len(args) < 2:
                await adapter.send_text("❌ 请指定要切换的预设名称。")
                return
            target = args[1]
            success = await self.cfg.set_active_connection(target)
            if success:
                preset = self.cfg.get_active_preset()
                msg = self._render_preset_feedback(f"✅ 已切换至连接预设 [{target}]。", preset)
                await adapter.send_text(msg)
            else:
                await adapter.send_text(f"❌ 连接预设 [{target}] 不存在。")
            return

        if sub_cmd.lower() in ["add", "del", "ren", "debug", "d"]:
            if not await self._check_admin(adapter, is_admin): return

            if sub_cmd == "del":
                if len(args) < 2: return
                name = args[1]
                if await self.cfg.delete_connection(name):
                    await adapter.send_text(f"✅ 已删除连接预设 [{name}]。")
                else:
                    await adapter.send_text(f"❌ 删除失败: [{name}] 不存在。")

            elif sub_cmd == "ren":
                if len(args) < 3: return
                if await self.cfg.rename_connection(args[1], args[2]):
                    await adapter.send_text(f"✅ 已重命名 [{args[1]}] -> [{args[2]}]。")
                else:
                    await adapter.send_text("❌ 重命名失败。")

            elif sub_cmd == "add":
                if not await self._check_admin(adapter, is_admin): return
                if len(args) < 5:
                    await adapter.send_text("❌ 格式: add <name> <type> <url> <model> [流式开关] [key1],[key2],...")
                    return
                name, api_type_str, url, model = args[1], args[2], args[3], args[4]
                stream_setting = None
                keys_str = ""
                if len(args) > 5:
                    remaining = args[5:]
                    first_arg = remaining[0]
                    if first_arg.lower() in ["true", "on", "1"]:
                        stream_setting = True
                        if len(remaining) > 1: keys_str = remaining[1]
                    elif first_arg.lower() in ["false", "off", "0"]:
                        stream_setting = False
                        if len(remaining) > 1: keys_str = remaining[1]
                    elif first_arg.lower() in ["auto", "none", "null"]:
                        stream_setting = None
                        if len(remaining) > 1: keys_str = remaining[1]
                    else:
                        keys_str = first_arg

                key_list = [k.strip() for k in keys_str.split(",") if k.strip()]

                try:
                    preset = ConnectionPreset(
                        name=name,
                        api_type=ApiType(api_type_str),
                        api_base=url,
                        model=model,
                        stream=stream_setting,
                        api_keys=key_list
                    )
                    await self.cfg.update_connection(preset)

                    msg = self._render_preset_feedback(f"✅ 已成功添加连接预设 [{name}]。", preset)
                    await adapter.send_text(msg)
                except ValueError:
                    await adapter.send_text(f"❌ 不支持的 API 类型: {api_type_str}")
                return

            elif sub_cmd in ["debug", "d"]:
                basic_conf = self.cfg._astr_config.get("Basic_Config", {})
                old_state = basic_conf.get("debug_prompt", False)
                new_state = not old_state
                basic_conf["debug_prompt"] = new_state

                await self.cfg.save_all()
                await adapter.send_text(f"{'✅' if new_state else '❌'} 调试模式已{'开启' if new_state else '关闭'}。")

            return

        target_name = args[0]
        preset = self.cfg.connections.get(target_name)
        if not preset:
            await adapter.send_text(f"❌ 连接预设 [{target_name}] 不存在。")
            return

        if len(args) >= 3:
            prop = args[1].lower()
            val = args[2]

            if prop == "api_url": prop = "api_base"
            allowed = {"model", "api_type", "api_base", "stream"}

            if prop in allowed:
                if not await self._check_admin(adapter, is_admin): return

                new_val = val
                if prop == "stream":
                    if val.lower() in ["true", "on", "1"]: new_val = True
                    elif val.lower() in ["false", "off", "0"]: new_val = False
                    elif val.lower() in ["auto", "none", "null"]: new_val = None
                    else:
                        await adapter.send_text("❌ 参数错误。Stream可选: on | off | auto")
                        return
                elif prop == "api_type":
                    try: new_val = ApiType(val)
                    except ValueError: 
                        await adapter.send_text(f"❌ 不支持的 API 类型: {val}")
                        return

                setattr(preset, prop, new_val)
                await self.cfg.update_connection(preset)

                msg = self._render_preset_feedback(f"✅ 已更新 [{target_name}] 的 {prop} 属性。", preset)
                await adapter.send_text(msg)
                return
            else:
                await adapter.send_text(f"❌ 属性不可修改或不存在。可选: {', '.join(allowed)}")
                return

        # 详情 & 可用模型
        waiting_msg_id = await adapter.send_text("🔍 正在连接服务器获取可用模型列表，请稍候...")

        from ..domain.model import ApiRequest, GenerationConfig
        dummy_req = ApiRequest(
            api_key="",
            preset=preset,
            gen_config=GenerationConfig(prompt=""),
            proxy_url=self.cfg._astr_config.get("Connection_Config", {}).get("proxy_url")
        )

        models = None
        try:
            models = await self.provider_mgr.get_models(dummy_req)
        except Exception as e:
            logger.warning(f"[Management] 获取模型列表失败: {e}")
            models = []

        if waiting_msg_id:
            await adapter.recall_message(waiting_msg_id)

        msg = ResponsePresenter.connection_detail(preset, p=cmd_p, available_models=models)
        await adapter.send_text_as_nodes(msg.split('\n'))

    # 密钥管理 (lmk)

    async def handle_key_cmd(self, event: AstrMessageEvent):
        if not await self._check_admin(PlatformAdapter(event), self._is_admin(event)): return
        adapter = PlatformAdapter(event)

        prefixes = self.cfg.get_prefixes()
        cmd_p = self.cfg.get_display_prefix()

        parsed = CommandParser.parse(
            adapter.message_str.strip(),
            prefixes=prefixes,
            cmd_aliases={"lmk", "lm密钥"}
        )
        args = parsed.args

        target_name = self.cfg.active_preset_name
        is_explicit_target = False

        if args and args[0] in self.cfg.connections:
            target_name = args[0]
            args = args[1:]
            is_explicit_target = True

        preset = self.cfg.connections.get(target_name)
        if not preset:
            await adapter.send_text("❌ 当前无有效连接预设。")
            return

        def _get_current_list_view(status_map=None) -> str:
            current_preset = self.cfg.connections.get(target_name)
            if not current_preset: return "❌ 预设已丢失"

            return ResponsePresenter.key_list(
                target_name, 
                current_preset.api_keys, 
                p=cmd_p,
                status_map=status_map
            )

        if not args or args[0].lower() in ["l", "list"]:
            status_map = {}

            if is_explicit_target and preset.api_keys:
                waiting_msg_id = await adapter.send_text(f"🔍 正在检测 [{target_name}] 的 {len(preset.api_keys)} 个密钥可用性，请稍候...")

                semaphore = asyncio.Semaphore(5)
                async def _check(k):
                    async with semaphore:
                        res = await self.provider_mgr.test_key_availability(preset, k)
                        status_map[k] = res

                tasks = [_check(k) for k in preset.api_keys]
                if tasks:
                    await asyncio.gather(*tasks)

                await adapter.recall_message(waiting_msg_id)

            msg = _get_current_list_view(status_map)
            await adapter.send_text_as_nodes(msg.split('\n'))
            return

        sub = args[0]

        if sub.lower() == "del":
            if len(args) < 2: 
                await adapter.send_text("❌ 请指定序号。用法: lmk del <序号|all>")
                return

            idx_str = args[1]
            if idx_str.lower() == "all":
                preset.api_keys = []
                await self.cfg.update_connection(preset)
                await adapter.send_text(f"🗑️ 已清空 [{target_name}] 的所有 Key。")
                return

            if idx_str.isdigit():
                success, remaining = await self.cfg.delete_api_key(target_name, int(idx_str))
                if success:
                    msg = f"🗑️ 已删除 [{target_name}] 的第 {idx_str} 个 Key。\n\n"
                    msg += _get_current_list_view()
                    await adapter.send_text_as_nodes(msg.split('\n'))
                else:
                    await adapter.send_text("❌ 删除失败: 序号无效。")
            else:
                await adapter.send_text("❌ 序号格式错误。")
            return

        keys_to_add = args
        added, dups = await self.cfg.add_api_keys(target_name, keys_to_add)

        if added > 0:
            msg = f"✅ 已向 [{target_name}] 添加 {added} 个 Key。"
            if dups > 0:
                msg += f" (忽略 {dups} 个重复)"
            msg += "\n\n" + _get_current_list_view()
            await adapter.send_text_as_nodes(msg.split('\n'))
        elif dups > 0:
            first_dup = keys_to_add[0]
            await adapter.send_text(ResponsePresenter.duplicate_item("API Key", first_dup) + " 无需重复添加。")
        else:
            await adapter.send_text("❌ 未提供有效的 Key。")

    # 数据&看板 (lm)

    async def handle_stats_cmd(self, event: AstrMessageEvent):
        adapter = PlatformAdapter(event)
        is_admin = self._is_admin(event)

        target_user_id = None
        target_group_id = adapter.group_id
        number_arg = None

        for seg in event.message_obj.message:
            if isinstance(seg, At):
                target_user_id = str(seg.qq)
                break

        parsed = CommandParser.parse(
            adapter.message_str.strip(),
            prefixes=self.cfg.get_prefixes(),
            cmd_aliases={"lm", "lm次数"}
        )

        for arg in parsed.args:
            if arg.lstrip("-").isdigit():
                val = int(arg)
                if number_arg is None:
                    number_arg = val
            elif not target_user_id and arg.isdigit() and len(arg) > 5:
                target_user_id = arg

        if is_admin and number_arg is not None:
            delta = number_arg
            if target_user_id:
                t_id = target_user_id
                is_group_target = False
                target_name = f"用户 {t_id}"
            elif target_group_id:
                t_id = target_group_id
                is_group_target = True
                target_name = f"群 {t_id}"
            else:
                t_id = adapter.sender_id
                is_group_target = False
                target_name = "自己"

            if delta == 0:
                new_val = await self.stats.admin_set_balance(t_id, 0, is_group=is_group_target)
                action_str = "清空/重置"
            else:
                new_val = await self.stats.admin_modify_balance(t_id, delta, is_group=is_group_target)
                action_str = "增加" if delta > 0 else "扣除"

            await adapter.send_text(f"✅ 已{action_str} {target_name} 的额度。\n当前余额: {new_val}")
            return

        if is_admin and target_user_id:
            ctx = await self.stats.get_quota_context(target_user_id, None, False)
            from ..domain.model import UserQuota
            uq = UserQuota(target_user_id, ctx.user_balance)
            msg = f"👤 用户 {target_user_id} 数据:\n"
            msg += f"💳 剩余额度: {uq.remaining} 次"
            await adapter.send_text(msg)
            return

        user_id = adapter.sender_id
        checkin_res = await self.stats.perform_checkin(user_id)
        dash_data = self.stats.get_dashboard_data()

        ctx = await self.stats.get_quota_context(user_id, adapter.group_id, is_admin)
        from ..domain.model import UserQuota
        uq = UserQuota(user_id, ctx.user_balance)

        msg = ResponsePresenter.stats_dashboard(
            uq, ctx.group_balance, checkin_res, dash_data
        )
        msg_id = await adapter.send_text(msg)
        recall_conf = self.cfg._astr_config.get("Recall_Config", {})
        if recall_conf.get("enable_result_recall", False):
            delay = int(recall_conf.get("result_recall_time", 90))
            adapter.schedule_recall(msg_id, delay)
        return

    # 帮助 (lmh)

    async def handle_help_cmd(self, event: AstrMessageEvent):
        adapter = PlatformAdapter(event)
        cmd_p = self.cfg.get_display_prefix()

        parsed = CommandParser.parse(
            adapter.message_str.strip(),
            prefixes=self.cfg.get_prefixes(),
            cmd_aliases={"lmh", "lm帮助"}
        )
        args = parsed.args

        sub = args[0].lower() if args else ""
        content = ""

        if sub in ["参数", "param", "p"]:
            content = ResponsePresenter.help_params()
        elif sub in ["变量", "var", "v"]:
            var_definitions = self.prompt_resolver.get_definitions()
            content = ResponsePresenter.help_vars(var_definitions)
        else:
            prefix = self.cfg._astr_config.get("Basic_Config", {}).get("extra_prefix", "lmi")
            content = ResponsePresenter.main_menu(prefix, p=cmd_p)
        await adapter.send_text_as_nodes(content.split('\n'))