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

        try:
            first_token = shlex.split(text)[0]
        except (ValueError, IndexError):
            parts = text.split()
            if not parts:
                return None
            first_token = parts[0]

        sorted_prefixes = sorted(prefixes, key=len, reverse=True)
        for p in sorted_prefixes:
            if first_token.startswith(p):
                return first_token[len(p):].strip()
        
        return first_token

    @classmethod
    def _tokenize(cls, event: AstrMessageEvent) -> List[str | At]:
        tokens = []
        if not hasattr(event.message_obj, "message"):
            return tokens

        for seg in event.message_obj.message:
            if isinstance(seg, Plain):
                text = seg.text.strip()
                if not text:
                    continue
                try:
                    split_res = shlex.split(text)
                    tokens.extend(split_res)
                except ValueError:
                    tokens.extend(text.split())
            elif isinstance(seg, At):
                tokens.append(seg)
        
        return tokens

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

        # 扁平化
        tokens = cls._tokenize(event)

        # 辅助信息
        all_ats = [t for t in tokens if isinstance(t, At)]
        images = []
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Image):
                    if seg.url:
                        images.append(seg.url)
                    elif seg.file:
                        images.append(seg.file)

        # 剥离
        if tokens and isinstance(tokens[0], str):
            first_str = tokens[0]

            text_no_prefix = first_str
            sorted_prefixes = sorted(prefixes, key=len, reverse=True)
            for p in sorted_prefixes:
                if first_str.startswith(p):
                    text_no_prefix = first_str[len(p):]
                    break
            
            if cmd_aliases:
                if text_no_prefix.lower() in [a.lower() for a in cmd_aliases]:
                    tokens.pop(0)

        params = {}
        final_text_parts = []
        
        i = 0
        while i < len(tokens):
            token = tokens[i]

            if isinstance(token, str) and token.startswith("--") and len(token) > 2:
                raw_key = token[2:]

                if raw_key.startswith("p") and raw_key[1:].isdigit():
                    key = raw_key
                else:
                    key = cls.KEY_ALIASES.get(raw_key, raw_key)

                next_token = tokens[i + 1] if i + 1 < len(tokens) else None

                is_next_flag = isinstance(next_token, str) and next_token.startswith("--")
                
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
                        and isinstance(next_token, str)
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
                if isinstance(token, str):
                    final_text_parts.append(token)
                i += 1

        text_content = " ".join(final_text_parts)
        first_at = all_ats[0] if all_ats else None

        return ParsedCommand(
            text=text_content,
            params=params,
            first_at=first_at,
            all_ats=all_ats,
            images=images,
        )