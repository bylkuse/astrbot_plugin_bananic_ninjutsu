import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Any
from astrbot.core.message.components import At, Plain, Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent


@dataclass
class ParsedCommand:
    text: str
    params: Dict[str, Any] = field(default_factory=dict)
    first_at: At | None = None
    all_ats: List[At] = field(default_factory=list)
    images: List[str] = field(default_factory=list)


class CommandParser:

    KEY_ALIASES = {
        "s": "google_search",
        "gs": "google_search",
        "ar": "aspect_ratio",
        "r": "image_size",
        "to": "timeout",
        "t": "thinking",
        "up": "upscale_prompt",
        "q": "q",
        "a": "additional_prompt",
        "add": "additional_prompt",
    }

    VALUE_KEYS = {"aspect_ratio", "image_size", "timeout", "q", "additional_prompt"}
    OPTIONAL_VALUE_KEYS = {"upscale_prompt"}
    BOOLEAN_VALUE_KEYS = {"thinking"}

    @classmethod
    def extract_pure_command(cls, text: str, prefixes: List[str]) -> str | None:
        if not text:
            return None

        text = text.strip()

        # 优先匹配最长的前缀
        matched_prefix = ""
        sorted_prefixes = sorted(prefixes, key=len, reverse=True)
        
        for p in sorted_prefixes:
            if text.startswith(p):
                matched_prefix = p
                break

        text_no_prefix = text[len(matched_prefix):].strip()

        if not text_no_prefix:
            return None

        parts = text_no_prefix.split(maxsplit=1)
        return parts[0] if parts else None

    @classmethod
    def parse(
        cls,
        event: AstrMessageEvent,
        cmd_aliases: List[str] = None,
        prefixes: List[str] = None,
    ) -> ParsedCommand:
        if prefixes is None:
            prefixes = []
        if cmd_aliases is None:
            cmd_aliases = []

        raw_text = event.message_str.strip()

        # 提取辅助组件 (At, Image)
        all_ats = []
        images = []
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, At):
                    all_ats.append(seg)
                elif isinstance(seg, Image):
                    if seg.url: images.append(seg.url)
                    elif seg.file: images.append(seg.file)

        # 剥离前缀和指令名
        matched_prefix = ""
        sorted_prefixes = sorted(prefixes, key=len, reverse=True)
        for p in sorted_prefixes:
            if raw_text.startswith(p):
                matched_prefix = p
                break

        content_after_prefix = raw_text[len(matched_prefix):].strip()

        parts = content_after_prefix.split(maxsplit=1)

        if not parts:
            return ParsedCommand(text="", first_at=None, all_ats=all_ats, images=images)

        potential_cmd = parts[0]
        args_text = parts[1] if len(parts) > 1 else ""

        is_alias_match = False
        if cmd_aliases:
            if potential_cmd.lower() in [a.lower() for a in cmd_aliases]:
                is_alias_match = True

        if is_alias_match:
            final_args_str = args_text
        else:
            final_args_str = content_after_prefix

        # 参数Tokenize
        tokens = []
        try:
            tokens = shlex.split(final_args_str, posix=True)
        except ValueError:
            tokens = final_args_str.split()

        # 解析参数键值对
        params = {}
        final_text_parts = []

        i = 0
        while i < len(tokens):
            token = tokens[i]

            if token.startswith("--") and len(token) > 2:
                raw_key = token[2:]

                if raw_key.startswith("p") and raw_key[1:].isdigit():
                    key = raw_key
                else:
                    key = cls.KEY_ALIASES.get(raw_key, raw_key)

                next_token = tokens[i + 1] if i + 1 < len(tokens) else None
                is_next_flag = next_token is not None and next_token.startswith("--")

                if key in cls.VALUE_KEYS or (key.startswith("p") and key[1:].isdigit()):
                    if next_token is not None and not is_next_flag:
                        params[key] = next_token
                        i += 2
                    else:
                        params[key] = True
                        i += 1

                elif key in cls.OPTIONAL_VALUE_KEYS:
                    if next_token is not None and not is_next_flag:
                        params[key] = next_token
                        i += 2
                    else:
                        params[key] = "default"
                        i += 1

                elif key in cls.BOOLEAN_VALUE_KEYS:
                    if (
                        next_token is not None
                        and next_token.lower() in ("true", "false", "1", "0", "on", "off")
                    ):
                        params[key] = next_token
                        i += 2
                    else:
                        params[key] = True
                        i += 1
                else:
                    params[key] = True
                    i += 1
            else:
                final_text_parts.append(token)
                i += 1

        # 重组Prompt
        text_content = " ".join(final_text_parts)
        first_at = all_ats[0] if all_ats else None

        return ParsedCommand(
            text=text_content,
            params=params,
            first_at=first_at,
            all_ats=all_ats,
            images=images,
        )