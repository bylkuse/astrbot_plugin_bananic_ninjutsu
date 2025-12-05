from datetime import datetime
from typing import List, Any, Dict
from astrbot.api import logger
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from ..api_client import APIClient, ApiRequestConfig, APIError, APIErrorType
from ..core.stats import StatsManager
from ..core.prompt import PromptManager
from ..core.images import ImageUtils
from ..utils.views import ResponsePresenter
from ..utils.parser import ParsedCommand


class GenerationService:
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

    def set_active_preset(self, preset_data: Dict[str, Any]):
        self.conn_config = preset_data
        logger.info(f"GenerationService: 切换连接至 [{self.conn_config.get('name')}]")

    async def _execute_core_generation(
        self,
        event: AstrMessageEvent,
        prompt: str,
        params: dict,
        images: List[bytes],
        is_master: bool,
        enhancer_model_name: str | None = None,
        enhancer_preset: str | None = None,
    ):
        """调用&返回"""
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        display_prompt = prompt[:20] + "..." if len(prompt) > 20 else prompt

        async with self.stats.transaction(
            sender_id, group_id, self.conf, is_master
        ) as txn:
            if not txn.allowed:
                yield event.plain_result(txn.reject_reason)
                return

            yield event.plain_result(ResponsePresenter.generating(display_prompt))

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
                image_size=params.get("image_size", "1K"),
                aspect_ratio=params.get("aspect_ratio", "default"),
                enable_search=bool(params.get("google_search", False)),
                enhancer_model_name=enhancer_model_name,
                enhancer_preset=enhancer_preset,
                thinking=thinking_val,
            )

            try:
                gen_result = await self.api_client.generate_content(request_config)

                image_data = gen_result.image
                thoughts = gen_result.thoughts

                elapsed = (datetime.now() - start_time).total_seconds()
                current_preset_name = self.conn_config.get("name", "Unknown")
                caption = ResponsePresenter.generation_success(
                    elapsed, current_preset_name, enhancer_model_name, enhancer_preset
                )

                result_chain = []
                if thoughts:
                    result_chain.append(Plain(f"🧐 思考过程:\n{thoughts}\n\n"))

                result_chain.append(Image.fromBytes(image_data))
                result_chain.append(Plain(caption))

                yield event.chain_result(result_chain)

            except APIError as e:
                elapsed = (datetime.now() - start_time).total_seconds()

                if e.error_type == APIErrorType.DEBUG_INFO:
                    txn.mark_failed("调试模式")
                    msg = ResponsePresenter.debug_info(e.data, elapsed)
                    yield event.plain_result(msg)
                    return

                txn.mark_failed(f"{e.error_type.name}: {e.raw_message}")
                yield event.plain_result(
                    ResponsePresenter.api_error_message(e, is_master, self.main_prefix)
                )

            except Exception as e:
                txn.mark_failed(str(e))
                elapsed = (datetime.now() - start_time).total_seconds()
                yield event.plain_result(f"❌ 系统内部错误: {e}")

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
        # 预设解析
        prompt_template = self.pm.get_preset(target_text)
        user_prompt = prompt_template if prompt_template else target_text

        # 追加prompt
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

        # 空检查
        if not user_prompt:
            mode_desc = "图生图" if require_image else "文生图"
            yield event.plain_result(
                f"请提供{mode_desc}的描述或预设名。\n用法: {cmd_display_name} <描述|预设名> [--参数]"
            )
            return

        # 变量处理
        prompt = await self.pm.process_variables(user_prompt, parsed_command, event)

        # 提示词优化
        enhancer_model_name = None
        enhancer_preset = None
        if up_val := params.get("upscale_prompt"):
            action_desc = (
                f"（策略: {up_val}）"
                if isinstance(up_val, str) and up_val != "default"
                else ""
            )
            yield event.plain_result(f"✨ 正在使用 AI 优化提示词{action_desc}...")
            prompt, enhancer_model_name, enhancer_preset = await self.pm.enhance_prompt(
                context, prompt, event, up_val
            )

        images_to_process = []
        if require_image:
            conn_conf = self.conf.get("Connection_Config", {})
            proxy = conn_conf.get("proxy_url") if conn_conf.get("use_proxy") else None

            img_bytes_list = await ImageUtils.get_images_from_event(event, proxy=proxy)
            if not img_bytes_list:
                yield event.plain_result(
                    "❌ 请发送图片、引用图片，或直接在图片下配文。"
                )
                return
            images_to_process = (
                img_bytes_list[:5] if len(img_bytes_list) > 5 else img_bytes_list
            )
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
        ):
            yield result
