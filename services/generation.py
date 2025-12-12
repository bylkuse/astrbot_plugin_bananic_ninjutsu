import asyncio
from datetime import datetime
from typing import List, Any, Dict, Set
from astrbot.api import logger
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain

from ..api_client import APIClient, ApiRequestConfig, APIError, APIErrorType
from ..core.stats import StatsManager
from ..core.prompt import PromptManager
from ..core.images import ImageUtils
from ..utils.views import ResponsePresenter
from ..utils.parser import ParsedCommand
from ..utils.result import Ok, Err

class GenerationService:
    MAX_IMAGE_COUNT = 5

    def __init__(
        self,
        api_client: APIClient,
        stats_manager: StatsManager,
        prompt_manager: PromptManager,
        config: Any,
        active_preset: Dict[str, Any],
        main_prefix: str = "#",
    ):
        self.api_client = api_client
        self.stats = stats_manager
        self.pm = prompt_manager
        self.conf = config
        self.conn_config = active_preset
        self.main_prefix = main_prefix
        self.recall_tasks: Set[asyncio.Task] = set()

    def set_active_preset(self, preset_data: Dict[str, Any]):
        self.conn_config = preset_data
        logger.info(f"GenerationService: 切换连接至 [{self.conn_config.get('name')}]")

    def _extract_message_id(self, resp: Any) -> int | None:
        if not resp:
            return None
        try:
            return int(resp)
        except (ValueError, TypeError):
            pass

        if isinstance(resp, dict):
            if "data" in resp and isinstance(resp["data"], dict):
                return int(resp["data"].get("message_id", 0) or 0) or None
            if "message_id" in resp:
                return int(resp["message_id"])

        if hasattr(resp, "message_id"):
            try:
                return int(resp.message_id)
            except (ValueError, TypeError):
                pass
        return None

    async def _send_message(self, event: AstrMessageEvent, payload: Any) -> int | None:
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
                    return self._extract_message_id(resp)
            except Exception as e:
                logger.debug(f"[Ninjutsu] OneBot 直发尝试失败: {e}，回退到 event.send")
        resp = await event.send(payload)
        return self._extract_message_id(resp)

    async def _safe_delete_msg(self, bot: Any, message_id: Any):
        if not message_id:
            return
        try:
            msg_id_int = int(message_id)
            logger.debug(f"[Ninjutsu] 正在撤回消息: {msg_id_int}")
            
            if hasattr(bot, "delete_msg"):
                await bot.delete_msg(message_id=msg_id_int)
            elif hasattr(bot, "recall_message"):
                await bot.recall_message(msg_id_int)
            else:
                logger.warning(f"[Ninjutsu] Adapter {type(bot)} 没有找到撤回方法")
        except Exception as e:
            logger.warning(f"[Ninjutsu] 撤回消息 {message_id} 失败: {e}")

    async def _schedule_result_recall(self, bot: Any, message_id: Any):
        if not message_id:
            return
        recall_conf = self.conf.get("Recall_Config", {})
        if not recall_conf.get("enable_result_recall", False):
            return
        delay = int(recall_conf.get("result_recall_time", 120))
        logger.info(f"[Ninjutsu] 计划在 {delay} 秒后撤回消息 {message_id}")
        async def _task():
            try:
                await asyncio.sleep(delay)
                await self._safe_delete_msg(bot, message_id)
            except asyncio.CancelledError:
                pass
            finally:
                if task_ref in self.recall_tasks:
                    self.recall_tasks.remove(task_ref)
        task_ref = asyncio.create_task(_task())
        self.recall_tasks.add(task_ref)

    async def _cleanup_process_msgs(self, bot: Any, msg_ids: List[Any]):
        valid_ids = [mid for mid in msg_ids if mid]
        if valid_ids:
            for mid in valid_ids:
                await self._safe_delete_msg(bot, mid)

    async def _execute_core_generation(
        self,
        event: AstrMessageEvent,
        prompt: str,
        params: dict,
        images: List[bytes],
        is_master: bool,
        enhancer_model_name: str | None = None,
        enhancer_preset: str | None = None,
        gen_preset_name: str | None = None,
        optimization_msg_id: Any = None, 
    ):
        """调用&返回"""
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        display_prompt = prompt[:20] + "..." if len(prompt) > 20 else prompt

        sz_val = params.get("image_size", "1K")
        if sz_val is True: sz_val = "1K"
        sz_str = str(sz_val).upper()
        cost = 1
        if "4K" in sz_str:
            cost = 4
        elif "2K" in sz_str:
            cost = 2

        async with self.stats.transaction(
            sender_id, group_id, self.conf, is_master, cost=cost
        ) as txn:
            if not txn.allowed:
                msg = txn.reject_reason
                if getattr(txn, "exhausted", False):
                    checkin_conf = self.conf.get("Checkin_Config", {})
                    if checkin_conf.get("enable_checkin", False):
                        msg += f"\n💡 提示: 发送 {self.main_prefix}lm 签到可获取次数。"
                yield event.plain_result(msg)
                return

            waiting_msg_payload = event.plain_result(ResponsePresenter.generating(display_prompt))
            waiting_msg_id = await self._send_message(event, waiting_msg_payload)
            process_msg_ids = [waiting_msg_id, optimization_msg_id]

            real_cost = cost if (txn._deducted_user or txn._deducted_group) else 0
            start_time = datetime.now()

            basic_conf = self.conf.get("Basic_Config", {})
            debug_mode = basic_conf.get("debug_prompt", False)
            conn_conf = self.conf.get("Connection_Config", {})
            default_timeout = conn_conf.get("timeout", 300)
            use_proxy = conn_conf.get("use_proxy", False)
            proxy_url = conn_conf.get("proxy_url")

            raw_thinking = params.get("thinking", False)
            thinking_val = False
            if isinstance(raw_thinking, str):
                thinking_val = raw_thinking.lower() in ("true", "1", "on", "yes")
            else:
                thinking_val = bool(raw_thinking)

            request_config = ApiRequestConfig(
                api_keys=self.conn_config.get("api_keys", []),
                api_type=self.conn_config.get("api_type", "google"),
                api_base=self.conn_config.get(
                    "api_url", "https://generativelanguage.googleapis.com"
                ),
                model=self.conn_config.get("model", "gemini-3-pro-image-preview"),
                timeout=int(params.get("timeout", default_timeout)),
                proxy_url=proxy_url if use_proxy else None,
                debug_mode=debug_mode,
                prompt=prompt,
                image_bytes_list=images,
                image_size=sz_str,
                aspect_ratio=params.get("aspect_ratio", "default"),
                enable_search=bool(params.get("google_search", False)),
                enhancer_model_name=enhancer_model_name,
                enhancer_preset=enhancer_preset,
                thinking=thinking_val,
            )

            final_msg_id = None
            result = await self.api_client.generate_content(request_config)
            await self._cleanup_process_msgs(event.bot, process_msg_ids)
            elapsed = (datetime.now() - start_time).total_seconds()

            match result:
                case Ok(gen_data): 
                    image_data = gen_data.image
                    thoughts = gen_data.thoughts
                    current_user_quota = self.stats.get_user_count(sender_id)
                    current_group_quota = self.stats.get_group_count(group_id) if group_id else 0
                    ar_val = params.get("aspect_ratio", "default")
                    if ar_val is True: ar_val = "default"

                    caption = ResponsePresenter.generation_success(
                        elapsed=elapsed,
                        conn_name=self.conn_config.get("name", "Unknown"),
                        model_name=self.conn_config.get("model", "Unknown"),
                        gen_preset_name=gen_preset_name,
                        prompt=prompt,
                        enhancer_model=enhancer_model_name,
                        enhancer_preset=enhancer_preset,
                        aspect_ratio=str(ar_val),
                        image_size=str(sz_val),
                        user_quota=current_user_quota,
                        group_quota=current_group_quota,
                        is_group=bool(group_id),
                        cost=real_cost
                    )

                    result_chain = []
                    if thoughts:
                        result_chain.append(Plain(f"🧐 思考过程:\n{thoughts}\n\n"))
                    result_chain.append(Image.fromBytes(image_data))
                    result_chain.append(Plain(caption))
                    final_msg_id = await self._send_message(event, event.chain_result(result_chain))
                    await self._schedule_result_recall(event.bot, final_msg_id)

                case Err(error):
                    txn.mark_failed()
                    if error.error_type == APIErrorType.DEBUG_INFO:
                        msg = ResponsePresenter.debug_info(error.data, elapsed)
                        await self._send_message(event, event.plain_result(msg))
                        return
                    error_msg = ResponsePresenter.api_error_message(error, is_master, self.main_prefix)
                    final_msg_id = await self._send_message(event, event.plain_result(error_msg))
                    await self._schedule_result_recall(event.bot, final_msg_id)

    async def run_generation_workflow(
        self,
        event: AstrMessageEvent,
        target_text: str,
        parsed_command: ParsedCommand,
        require_image: bool,
        cmd_display_name: str,
        context: Any,
        is_master: bool,
    ):
        """公用生图逻辑"""
        params = parsed_command.params
        prompt_template = self.pm.get_preset(target_text)
        gen_preset_name = None
        if prompt_template:
            user_prompt = prompt_template
            gen_preset_name = target_text
        else:
            user_prompt = target_text
            gen_preset_name = None

        additional = params.get("additional_prompt")
        if additional is True:
            additional = None
        if additional:
            additional = str(additional)
            if user_prompt:
                user_prompt = user_prompt.strip()
                if not user_prompt.endswith(
                    (",", "，", ".", "。", "!", "！", ";", "；")
                ):
                    user_prompt += ","
                user_prompt += f" {additional}"
            else:
                user_prompt = additional

        if not user_prompt:
            mode_desc = "图生图" if require_image else "文生图"
            yield event.plain_result(
                f"请提供{mode_desc}的描述或预设名。\n用法: {cmd_display_name} <描述|预设名> [--参数]"
            )
            return

        prompt = await self.pm.process_variables(user_prompt, parsed_command, event)
        enhancer_model_name = None
        enhancer_preset = None
        optimization_msg_id = None 

        if up_val := params.get("upscale_prompt"):
            action_desc = (
                f"（策略: {up_val}）"
                if isinstance(up_val, str) and up_val != "default"
                else ""
            )
            opt_payload = event.plain_result(f"✨ 正在使用 AI 优化提示词{action_desc}...")
            optimization_msg_id = await self._send_message(event, opt_payload)
            prompt, enhancer_model_name, enhancer_preset = await self.pm.enhance_prompt(
                context, prompt, event, up_val
            )

        images_to_process = []

        if require_image:
            conn_conf = self.conf.get("Connection_Config", {})
            proxy = conn_conf.get("proxy_url") if conn_conf.get("use_proxy") else None
            shared_session = await self.api_client.get_session()
            img_bytes_list = await ImageUtils.get_images_from_event(event, max_count=self.MAX_IMAGE_COUNT, proxy=proxy, session=shared_session)
            if not img_bytes_list:
                if optimization_msg_id:
                     await self._safe_delete_msg(event.bot, optimization_msg_id)

                yield event.plain_result(
                    "❌ 请发送图片、引用图片，或直接在图片下配文。"
                )
                return
            images_to_process = img_bytes_list
        else:
            images_to_process = []

        async for result in self._execute_core_generation(
            event,
            prompt,
            params,
            images_to_process,
            is_master,
            enhancer_model_name,
            enhancer_preset,
            gen_preset_name=gen_preset_name,
            optimization_msg_id=optimization_msg_id,
        ):
            yield result