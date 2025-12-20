from typing import Optional, List, Any, Tuple

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.platform import AstrMessageEvent, Image, Plain, At

from ..domain.model import (
    GenerationConfig, 
    APIErrorType, 
    PluginError
)
from ..services.generation import GenerationService
from ..services.resource import ResourceService
from ..services.config import ConfigService
from ..services.stats import StatsService
from ..utils.parser import CommandParser, ParsedCommand
from ..utils.result import Result, Ok, Err
from ..views import ResponsePresenter
from ..domain.prompt import PromptResolver
from .platform import PlatformAdapter

class WorkflowHandler:
    def __init__(
        self,
        context: Context,
        prompt_resolver: PromptResolver,
        generation_service: GenerationService,
        resource_service: ResourceService,
        config_service: ConfigService,
        stats_service: StatsService,
        admin_ids: List[str]
    ):
        self.context = context
        self.prompt_resolver = prompt_resolver
        self.gen_service = generation_service
        self.res_service = resource_service
        self.cfg = config_service
        self.stats = stats_service
        self.admin_ids = admin_ids

    def _get_effective_proxy(self) -> Optional[str]:
        conn_conf = self.cfg._astr_config.get("Connection_Config", {})
        plugin_proxy = conn_conf.get("proxy_url")
        use_plugin_proxy = conn_conf.get("use_proxy", False)

        if plugin_proxy:
            if use_plugin_proxy or use_plugin_proxy is None:
                return plugin_proxy

        try:
            global_conf = self.context.get_config()
            global_proxy = global_conf.get("proxy")
            if global_proxy:
                return global_proxy
        except Exception:
            pass

        return None

    async def _enhance_prompt(
        self,
        original_prompt: str,
        instruction_key: str,
        event: AstrMessageEvent
    ) -> Tuple[str, Optional[str]]:

        # 1. Prompt & Template
        optimizer_presets = self.cfg.optimizers

        instruction_val = instruction_key if instruction_key is not True else "default"

        if instruction_val in optimizer_presets:
            system_instruction = optimizer_presets[instruction_val]
            user_content_template = "User Description: {prompt}"
        else:
            system_instruction = (
                "身份：你是一名专业的提示词工程师\n"
                "任务：分析需求，重写以提升提示词质量（精确、稳定、可复现）"
            )
            user_content_template = (
                "Original Prompt: {prompt}\n"
                f"Modification Requirement: {instruction_val}\n"
                "Refined Prompt:"
            )

        # 2. 获取 LLM
        gen_conf = self.context.get_config().get("Generation_Config", {})
        provider_id = gen_conf.get("prompt_enhance_provider_id")
        provider = None

        if provider_id:
            try: provider = self.context.get_provider_by_id(provider_id)
            except: pass

        if not provider:
            try: provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            except: pass

        if not provider and hasattr(self.context, "get_default_provider"):
            try: provider = self.context.get_default_provider()
            except: pass

        if not provider:
            logger.warning("[Ninjutsu] 提示词优化失败: 未找到可用的 LLM Provider")
            return original_prompt, None

        # 3. 构造请求
        try:
            full_prompt = user_content_template.format(prompt=original_prompt)
            full_prompt += "\n总则:\n* 沿用提示词原来的language，使用自然语言&叙述性段落，而非关键词\n* (关键)直接输出优化后的最终提示词，不要包含任何解释、前缀或Markdown代码块。"

            p_name = getattr(provider, "id", None) or type(provider).__name__
            logger.info(f"[Ninjutsu] 正在调用 LLM ({p_name}) 优化提示词...")

            # 4. 解析结果
            response = await provider.text_chat(
                prompt=full_prompt,
                session_id=None,
                context=[],
                system_prompt=system_instruction
            )

            content = (
                getattr(response, "completion_text", None) or 
                getattr(response, "text", None) or 
                getattr(response, "content", None)
            )

            if not content:
                rc = getattr(response, "result_chain", None)
                if rc and getattr(rc, "chain", None):
                    segs = [str(seg.text) for seg in rc.chain if hasattr(seg, "text")]
                    content = "\n".join(segs)

            content = str(content or "").strip()
            content = content.replace("```markdown", "").replace("```", "").strip()

            if not content or len(content) < 2 or "error" in content.lower() and len(content) < 50:
                logger.warning(f"[Ninjutsu] LLM 返回内容异常: {content}")
                return original_prompt, None

            enhancer_model_name = getattr(response.raw_completion, "model_version", None) or p_name
            return content, enhancer_model_name

        except Exception as e:
            logger.error(f"[Ninjutsu] 提示词优化过程发生异常: {e}", exc_info=True)
            return original_prompt, None

    async def handle_text_to_image(self, event: AstrMessageEvent):
        adapter = PlatformAdapter(event)
        raw_text = adapter.message_str.strip()
        prefixes = self.cfg.get_prefixes()
        cmd_p = self.cfg.get_display_prefix()

        parsed = CommandParser.parse(
            raw_text, 
            prefixes=prefixes,
            cmd_aliases={"lmt", "文生图"}
        )

        target_text, preset_name = self._resolve_prompt_and_preset(parsed, force_preset_name=None)

        if not target_text:
            await adapter.send_text("❌ 请提供提示词或预设名。\n用法: /lmt <描述|预设名> [--参数]")
            return

        await self._execute_generation(adapter, target_text, parsed, require_image=False, preset_name=preset_name)

    async def handle_image_to_image(self, event: AstrMessageEvent, force_preset: str = None, cmd_alias: str = None):
        adapter = PlatformAdapter(event)
        raw_text = adapter.message_str.strip()
        prefixes = self.cfg.get_prefixes()
        cmd_p = self.cfg.get_display_prefix()
        aliases = set()
        if force_preset: aliases.add(force_preset)
        if cmd_alias: aliases.add(cmd_alias)

        parsed = CommandParser.parse(
            raw_text, 
            prefixes=prefixes,
            cmd_aliases=aliases
        )

        target_text, preset_name = self._resolve_prompt_and_preset(parsed, force_preset_name=force_preset)

        if not target_text:
            await adapter.send_text(f"❌ 请提供描述或预设名。\n用法: {cmd_p}lmi <描述|预设名> [--参数]")
            return

        await self._execute_generation(adapter, target_text, parsed, require_image=True, preset_name=preset_name)

    async def _execute_generation(
        self,
        adapter: PlatformAdapter,
        input_text: str,
        parsed_cmd: ParsedCommand,
        require_image: bool,
        preset_name: Optional[str] = None
    ):
        sender_id = adapter.sender_id
        target_uid_from_cmd = adapter.resolve_target_user_id(parsed_cmd.params)

        if target_uid_from_cmd:
            final_uid = target_uid_from_cmd
            final_un = await adapter.fetch_user_name(final_uid)
        else:
            final_uid = sender_id
            final_un = adapter.event.get_sender_name() or sender_id

        is_admin = self._is_admin(adapter.event)
        group_name = await adapter.fetch_group_name()

        ctx_map = {
            "user_id": sender_id,
            "group_id": adapter.group_id,
            "is_admin": is_admin,
            "uid": final_uid,
            "un": final_un,
            "g": group_name,
        }

        prompt = input_text

        if "%run%" in prompt:
            try:
                random_name = "群友"
                if adapter.group_id and hasattr(adapter.bot, "get_group_member_list"):
                    members = await adapter.bot.get_group_member_list(group_id=int(adapter.group_id))
                    if members:
                        import random
                        lucky = random.choice(members)
                        random_name = lucky.get("card") or lucky.get("nickname") or str(lucky.get("user_id"))
                ctx_map["run"] = random_name
            except Exception as e:
                logger.warning(f"获取随机群友失败: {e}")
                ctx_map["run"] = "随机群友"

        if "%age%" in prompt or "%bd%" in prompt:
            age_str, bd_str = "", ""
            if hasattr(adapter.bot, "get_stranger_info"):
                try:
                    info = await adapter.bot.get_stranger_info(user_id=int(final_uid), no_cache=True)
                    age_str = str(info.get("age", ""))
                    if (m := info.get("birthday_month")) and (d := info.get("birthday_day")):
                        bd_str = f"{m}月{d}日"
                    elif y := info.get("birthday_year"):
                        bd_str = f"{y}年"
                except Exception:
                    pass
            if "%age%" in prompt: ctx_map["age"] = age_str
            if "%bd%" in prompt: ctx_map["bd"] = bd_str

        resolved_prompt = self.prompt_resolver.resolve(input_text, parsed_cmd.params, ctx_map)

        gen_config = self._build_generation_config(resolved_prompt, parsed_cmd)
        gen_config.target_user_id = final_uid

        opt_msg_id = None
        used_enhancer_model = None
        used_enhancer_instr = None

        if gen_config.upscale_instruction:
            used_enhancer_instr = str(gen_config.upscale_instruction)
            if used_enhancer_instr.lower() == "true":
                used_enhancer_instr = "default"
            opt_msg_id = await adapter.send_text(f"✨ 正在使用 AI 优化提示词 (策略: {used_enhancer_instr})...")

            new_prompt, e_model = await self._enhance_prompt(
                original_prompt=gen_config.prompt,
                instruction_key=gen_config.upscale_instruction,
                event=adapter.event
            )

            gen_config.prompt = new_prompt
            if e_model:
                used_enhancer_model = e_model

            if opt_msg_id:
                await adapter.recall_message(opt_msg_id)

        proxy_url = self._get_effective_proxy()

        images: List[bytes] = []
        if require_image:
            images = await self.res_service.get_images_from_adapter(adapter, proxy=proxy_url)
            if not images:
                await adapter.send_text("❌ 图生图模式需要图片。\n请发送图片、引用图片，或在指令中包含图片。")
                return

        waiting_msg_id = None
        wait_text = ResponsePresenter.generating(prompt)
        if gen_config.enable_thinking:
            wait_text += "\n🤔 (思维链模式已开启，可能需要较长时间...)"
        waiting_msg_id = await adapter.send_text(wait_text)

        try:
            result = await self.gen_service.generate_image(
                ctx_map=ctx_map,
                gen_config=gen_config,
                image_bytes=images,
                preset_name=preset_name,
                proxy_url=proxy_url
            )
        except Exception as e:
            logger.error(f"生图流程发生未捕获异常: {e}", exc_info=True)
            result = Err(PluginError(APIErrorType.UNKNOWN, str(e)))

        if waiting_msg_id:
            await adapter.recall_message(waiting_msg_id)

        recall_conf = self.cfg._astr_config.get("Recall_Config", {})
        enable_recall = recall_conf.get("enable_result_recall", False)
        recall_delay = int(recall_conf.get("result_recall_time", 90))

        if result.is_ok():
            gen_res = result.unwrap()
            if used_enhancer_model:
                gen_res.enhancer_model = used_enhancer_model
                gen_res.enhancer_instruction = used_enhancer_instr
            quota_ctx = await self.stats.get_quota_context(adapter.sender_id, adapter.group_id, is_admin)
            from ..domain.model import UserQuota
            uq = UserQuota(adapter.sender_id, quota_ctx.user_balance)

            cost = 1
            if "4K" in gen_config.image_size.upper(): cost = 4
            elif "2K" in gen_config.image_size.upper(): cost = 2

            current_preset = self.cfg.get_active_preset()
            from ..domain.model import ApiRequest
            dummy_req = ApiRequest(api_key="", preset=current_preset, gen_config=gen_config)

            caption = ResponsePresenter.generation_success(
                result=gen_res,
                request=dummy_req,
                cost=gen_res.actual_cost,
                quota=uq,
                group_balance=quota_ctx.group_balance,
                preset_name=preset_name
            )

            chain = []
            if gen_res.text_content:
                chain.append(Plain(f"🧐 思考过程:\n{gen_res.text_content}\n\n"))

            for img_bytes in gen_res.images:
                chain.append(Image.fromBytes(img_bytes))
            chain.append(Plain(caption))

            final_msg_id = await adapter.send_payload(adapter.event.chain_result(chain))
            if enable_recall:
                adapter.schedule_recall(final_msg_id, recall_delay)
        else:
            error = result.error
            if error.error_type == APIErrorType.DEBUG_INFO and error.raw_data:
                if used_enhancer_model:
                    error.raw_data["enhancer_model"] = used_enhancer_model
                    error.raw_data["enhancer_preset"] = used_enhancer_instr
            if error.error_type == APIErrorType.DEBUG_INFO:
                msg = ResponsePresenter.debug_info(error)
                await adapter.send_text(msg)
            else:
                cmd_p = self.cfg.get_display_prefix()
                msg = ResponsePresenter.api_error_message(error, is_admin, p=cmd_p)
                err_msg_id = await adapter.send_text(msg)
                if enable_recall:
                    adapter.schedule_recall(err_msg_id, recall_delay)

    def _resolve_prompt_and_preset(self, parsed: ParsedCommand, force_preset_name: Optional[str]) -> tuple[str, Optional[str]]:
        prompt = ""
        preset_name = None

        if force_preset_name:
            preset_content = self.cfg.get_prompt(force_preset_name)
            if preset_content:
                prompt = preset_content
                preset_name = force_preset_name
            else:
                prompt = force_preset_name
                preset_name = None
        else:
            # 硬截断提取
            clean_tokens = []
            for token in parsed.args:
                if token.startswith("--"):
                    break
                clean_tokens.append(token)

            raw_prompt = " ".join(clean_tokens).strip()

            preset_content = self.cfg.get_prompt(raw_prompt)
            if preset_content:
                prompt = preset_content
                preset_name = raw_prompt
            else:
                prompt = raw_prompt
                preset_name = None

        # 追加
        add_p = parsed.params.get("additional_prompt")
        if add_p and isinstance(add_p, str):
            if prompt:
                if not prompt.endswith((",", "，", ".", "。", "!", "！")):
                    prompt += ","
                prompt += f" {add_p}"
            else:
                prompt = add_p

        return prompt, preset_name

    def _build_generation_config(self, prompt: str, parsed: ParsedCommand) -> GenerationConfig:
        p = parsed.params
        raw_size = p.get("image_size", "1K")
        if raw_size is True:
            raw_size = "1K"
        final_size = str(raw_size).upper()

        return GenerationConfig(
            prompt=prompt,
            image_size=final_size,
            aspect_ratio=p.get("aspect_ratio", "default"),
            timeout=int(p.get("timeout", 300)),
            enable_search=bool(p.get("enable_search", False)),
            enable_thinking=bool(p.get("enable_thinking", False)),
            upscale_instruction=p.get("upscale_instruction"), 
            target_user_id=p.get("target_user_id"),
            sender_id=None
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in self.admin_ids