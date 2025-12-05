import re
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from astrbot.core.message.components import At, Plain, Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent

@dataclass
class ParsedCommand:
    text: str
    params: Dict[str, Any] = field(default_factory=dict)
    first_at: Optional[At] = None
    all_ats: List[At] = field(default_factory=list)
    images: List[str] = field(default_factory=list)

class CommandParser:
    """参数解析"""
    KEY_ALIASES = {
        "s": "google_search", "gs": "google_search",
        "ar": "aspect_ratio", 
        "r": "image_size",
        "to": "timeout", 
        "t": "thinking", 
        "up": "upscale_prompt",
        "q": "q",
        "a": "additional_prompt",
        "add": "additional_prompt"
    }

    VALUE_KEYS = {"aspect_ratio", "image_size", "timeout", "q", "additional_prompt"}
    OPTIONAL_VALUE_KEYS = {"upscale_prompt"}
    BOOLEAN_VALUE_KEYS = {"thinking"}

    @classmethod
    def extract_pure_command(cls, text: str, prefixes: List[str]) -> Optional[str]:
        """提取指令"""
        text = text.strip()
        sorted_prefixes = sorted(prefixes, key=len, reverse=True)

        text_no_prefix = text
        for p in sorted_prefixes:
            if text.startswith(p):
                text_no_prefix = text[len(p):].strip()
                break

        if not text_no_prefix:
            return None
        return text_no_prefix.split()[0]

    @classmethod
    def parse(cls, event: AstrMessageEvent, cmd_aliases: List[str] = None, prefixes: List[str] = None) -> ParsedCommand:
        """解析指令"""
        if prefixes is None: prefixes = []
        if cmd_aliases is None: cmd_aliases = []
        sorted_prefixes = sorted(prefixes, key=len, reverse=True)
        target_cmds = {c.lower() for c in cmd_aliases}

        raw_tokens = []
        ats = []
        images = []

        # 拆解&提取
        if hasattr(event.message_obj, 'message'):
            for seg in event.message_obj.message:
                if isinstance(seg, Plain):
                    matches = re.findall(r'"([^"]*)"|(\S+)', seg.text.strip())
                    for quoted, plain in matches:
                        raw_tokens.append(quoted if quoted else plain)
                elif isinstance(seg, At):
                    ats.append(seg)
                    raw_tokens.append(seg)
                elif isinstance(seg, Image):
                    if seg.url: images.append(seg.url)
                    elif seg.file: images.append(seg.file)

        # 移除指令头
        clean_tokens = []
        cmd_removed = False

        for token in raw_tokens:
            if not cmd_removed and isinstance(token, str):
                token_lower = token.lower()
                token_pure = token_lower

                for p in sorted_prefixes:
                    if token_lower.startswith(p):
                        token_pure = token_lower[len(p):]
                        break

                if target_cmds and (token_pure in target_cmds):
                    cmd_removed = True
                    continue

            clean_tokens.append(token)

        # 解析循环
        params = {}
        final_text_parts = []

        i = 0
        while i < len(clean_tokens):
            token = clean_tokens[i]
            if isinstance(token, str) and token.startswith("--") and len(token) > 2:
                raw_key = token[2:]
                # 处理p1,p2
                if raw_key.startswith('p') and raw_key[1:].isdigit():
                    key = raw_key
                else:
                    key = cls.KEY_ALIASES.get(raw_key, raw_key)

                next_token = clean_tokens[i+1] if i + 1 < len(clean_tokens) else None
                is_next_flag = isinstance(next_token, str) and next_token.startswith("--")

                if key in cls.VALUE_KEYS or (key.startswith('p') and key[1:].isdigit()):
                    if next_token and not is_next_flag:
                        params[key] = next_token
                        i += 2
                    else:
                        params[key] = True
                        i += 1
                elif key in cls.OPTIONAL_VALUE_KEYS:
                    if next_token and not is_next_flag:
                        params[key] = next_token
                        i += 2
                    else:
                        params[key] = "default"
                        i += 1
                elif key in cls.BOOLEAN_VALUE_KEYS:
                    if next_token and isinstance(next_token, str) and next_token.lower() in ("true", "false", "1", "0", "on", "off"):
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

        first_at = ats[0] if ats else None
        text_content = " ".join(final_text_parts)

        return ParsedCommand(
            text=text_content,
            params=params,
            first_at=first_at,
            all_ats=ats,
            images=images
        )