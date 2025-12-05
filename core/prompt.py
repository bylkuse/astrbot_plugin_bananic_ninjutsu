import random
import re
import string
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple, Literal

from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.message.components import At

from ..utils.serializer import ConfigSerializer


class PromptManager:
    def __init__(self, config: dict, data_dir: Path):
        self.config = config
        self.prompt_map: Dict[str, str] = {}
        self.optimizer_presets: Dict[str, str] = {}

        # 预编译正则
        self._param_re = re.compile(r"%p(\d*)(?::([^%]*))?%")
        self._var_re = re.compile(r"%([a-zA-Z0-9_:|]+?)%")

        self.color_list = [
            "red",
            "blue",
            "green",
            "yellow",
            "purple",
            "orange",
            "black",
            "white",
            "pink",
            "cyan",
        ]

    def sync_to_config(self):
        if "Generation_Config" not in self.config:
            self.config["Generation_Config"] = {}

        self.config["Generation_Config"]["prompt_list"] = ConfigSerializer.dump_kv_list(
            self.prompt_map
        )
        self.config["Generation_Config"]["optimizer_presets"] = (
            ConfigSerializer.dump_kv_list(self.optimizer_presets)
        )

    async def load_prompts(self):
        gen_conf = self.config.get("Generation_Config", {})

        raw_list = gen_conf.get("prompt_list", [])
        self.prompt_map = ConfigSerializer.load_kv_list(raw_list)

        raw_opt_list = gen_conf.get("optimizer_presets", [])
        self.optimizer_presets = ConfigSerializer.load_kv_list(raw_opt_list)

        if "default" not in self.optimizer_presets:
            self.optimizer_presets["default"] = (
                "You are a professional prompt engineer. Rewrite the user's description into a detailed prompt."
            )

        logger.info(
            f"PromptManager: 已加载 {len(self.prompt_map)} 个生图预设, {len(self.optimizer_presets)} 个优化预设"
        )

    def get_preset(self, key: str) -> str | None:
        return self.prompt_map.get(key)

    def get_target_dict(self, p_type: Literal["prompt", "optimizer"]) -> Dict[str, str]:
        return self.prompt_map if p_type == "prompt" else self.optimizer_presets

    def normalize_for_comparison(self, text: str) -> str:
        symbols_to_strip = string.punctuation + "，。！？；：”’（）《》【】"

        if "%" in text:
            text = self._param_re.sub(lambda m: m.group(2) or "", text)

        return text.rstrip(symbols_to_strip)

    def check_duplicate(self, p_type: Literal["prompt", "optimizer"], new_value: str) -> str | None:
        target_dict = self.get_target_dict(p_type)
        new_val_norm = self.normalize_for_comparison(new_value)

        for key, val in target_dict.items():
            if self.normalize_for_comparison(val) == new_val_norm:
                return key
        return None

    async def process_variables(self, prompt: str, params: dict, event: AstrMessageEvent | None = None) -> str:
        # 防ReDoS
        if len(prompt) > 4096:
            return prompt

        if not prompt or "%" not in prompt:
            return prompt

        target_user_id = event.get_sender_id() if event else None
        q_param = params.get("q")
        if q_param:
            if isinstance(q_param, str):
                clean_q = q_param.replace("@", "").strip()
                if clean_q.isdigit():
                    target_user_id = clean_q
            elif isinstance(q_param, At):
                target_user_id = str(q_param.qq)
            elif q_param is True and event:
                first_at = params.get("first_at") or next(
                    (s for s in event.message_obj.message if isinstance(s, At)), None
                )
                if first_at:
                    target_user_id = str(first_at.qq)

        user_age, user_birthday = "", ""
        if event and target_user_id and ("%age%" in prompt or "%bd%" in prompt):
            try:
                if hasattr(event.bot, "get_stranger_info"):
                    info = await event.bot.get_stranger_info(
                        user_id=int(target_user_id), no_cache=True
                    )
                    user_age = str(info.get("age", ""))
                    if (m := info.get("birthday_month")) and (
                        d := info.get("birthday_day")
                    ):
                        user_birthday = f"{m}月{d}日"
                    elif y := info.get("birthday_year"):
                        user_birthday = f"{y}年"
            except Exception:
                pass

        if event:
            ctx_map = {
                "%g%": lambda: self.get_group_name(event),
                "%un%": lambda: self.get_user_nickname(event, target_user_id),
                "%run%": lambda: self.get_random_member_nickname(event),
                "%uid%": lambda: target_user_id,
            }

            for k, func in ctx_map.items():
                if k in prompt:
                    res = func()
                    if asyncio.iscoroutine(res):
                        res = await res
                    prompt = prompt.replace(k, str(res))

        func_map = {
            "r": lambda v: random.choice(v.split("|")) if v else "",
            "rn": lambda v: str(random.randint(*map(int, v.split("-")))),
            "rl": lambda v: "".join(random.choices(string.ascii_letters, k=int(v))),
        }

        val_map = {
            "rc": lambda: random.choice(self.color_list),
            "t": lambda: datetime.now().strftime("%H:%M:%S"),
            "d": lambda: datetime.now().strftime("%m月%d日"),
            "age": lambda: user_age,
            "bd": lambda: user_birthday,
            "wd": lambda: f"星期{'日一二三四五六'[int(datetime.now().strftime('%w'))]}",
        }

        escaped = "___ESCAPED___"
        for _ in range(5):
            if "%" not in prompt:
                break
            original_loop_prompt = prompt
            if "%%" in prompt:
                prompt = prompt.replace("%%", escaped)

            def param_replacer(m):
                idx = m.group(1) or ""
                default_val = m.group(2) or ""
                key = f"p{idx}"
                return str(params.get(key, default_val))
            prompt = self._param_re.sub(param_replacer, prompt)

            def var_replacer(m):
                raw = m.group(1)
                try:
                    if kv := ConfigSerializer.parse_single_kv(raw):
                        k, v = kv
                        return str(func_map[k](v)) if k in func_map else m.group(0)
                    return str(val_map[raw]()) if raw in val_map else m.group(0)
                except Exception:
                    return m.group(0)

            prompt = self._var_re.sub(var_replacer, prompt)
            if prompt == original_loop_prompt:
                break

        return prompt.replace(escaped, "%")

    async def get_group_name(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if not group_id:
            return "群聊"

        if hasattr(event.bot, "get_group_info"):
            try:
                group_info = await event.bot.get_group_info(group_id=int(group_id))
                return group_info.get("group_name") or str(group_id)
            except Exception:
                pass
        
        return str(group_id)

    async def get_user_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        group_id = event.get_group_id()

        if group_id and hasattr(event.bot, "get_group_member_info"):
            try:
                user_info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(user_id), no_cache=True
                )
                return user_info.get("card") or user_info.get("nickname") or user_id
            except Exception:
                pass

        if user_id == event.get_sender_id():
            sender = event.message_obj.sender
            return getattr(sender, "card", None) or getattr(sender, "nickname", None) or user_id

        return user_id

    async def get_random_member_nickname(self, event: AstrMessageEvent) -> str:
        fallback_name = "用户"
        group_id = event.get_group_id()

        if not group_id:
            return (
                await self.get_user_nickname(event, event.get_self_id())
                or fallback_name
            )

        if hasattr(event.bot, "get_group_member_list"):
            try:
                member_list = await event.bot.get_group_member_list(group_id=int(group_id))
                if member_list:
                    random_member = random.choice(member_list)
                    return (
                        random_member.get("card")
                        or random_member.get("nickname")
                        or fallback_name
                    )
            except Exception:
                pass

        return (
            await self.get_user_nickname(event, event.get_self_id())
            or fallback_name
        )

    async def enhance_prompt(
        self,
        context: Any,
        original_prompt: str,
        event: AstrMessageEvent,
        up_value: Any = "default",
    ) -> Tuple[str, str | None, str | None]:
        instruction_key = str(up_value) if up_value is not True else "default"
        used_preset_name = None

        system_instruction = ""
        user_content_template = ""

        if instruction_key in self.optimizer_presets:
            system_instruction = self.optimizer_presets[instruction_key]
            user_content_template = "User Description: {prompt}"
            used_preset_name = instruction_key
        else:
            system_instruction = (
                "You are a helpful AI assistant for image generation. "
                "Your task is to modify the User's original prompt according to their specific requirements. "
                "Maintain the core subject of the original prompt unless asked to change it. "
            )
            user_content_template = (
                "Original Prompt: {prompt}\n"
                f"Modification Requirement: {instruction_key}\n"
                "Refined Prompt:"
            )
            used_preset_name = "Custom"

        gen_conf = self.config.get("Generation_Config", {})

        provider_id = gen_conf.get("prompt_enhance_provider_id")
        provider = None
        try:
            if provider_id:
                provider = context.get_provider_by_id(provider_id)
        except Exception:
            provider = None

        if provider is None:
            provider = context.get_using_provider(umo=event.unified_msg_origin)

        if provider is None:
            logger.warning("提示词优化失败: 未找到可用的 LLM 供应商。")
            return original_prompt, None, None

        try:
            full_prompt = user_content_template.format(prompt=original_prompt)
            full_prompt += " Directly output the final prompt without explanation."

            resp = await provider.text_chat(
                prompt=full_prompt,
                context=[],
                system_prompt=system_instruction,
                model=gen_conf.get("prompt_enhanced_model"),
            )

            enhancer_model_name = getattr(resp.raw_completion, "model_version", None)

            content = getattr(resp, "text", None) or getattr(resp, "content", None)
            if not content:
                rc = getattr(resp, "result_chain", None)
                if rc and getattr(rc, "chain", None):
                    parts = [str(seg.text) for seg in rc.chain if hasattr(seg, "text")]
                    content = "\n".join(parts)
            if not content:
                content = str(resp)

            content = content.strip()
            if content.startswith("LLMResponse("):
                content = ""

            if not content or "error" in content.lower():
                logger.warning(f"提示词优化失败: LLM 返回无效内容: {content}")
                return original_prompt, None, None

            logger.info(
                f"提示词优化 [{used_preset_name}]: {original_prompt} -> {content}"
            )
            return content, enhancer_model_name, used_preset_name
        except Exception as e:
            logger.error(f"调用 LLM 进行提示词优化时出错: {e}", exc_info=True)
            return original_prompt, None, None
