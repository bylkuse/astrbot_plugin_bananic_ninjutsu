import shlex
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Set

@dataclass
class ParsedCommand:
    raw_text: str           # 原始文本
    clean_text: str         # Prompt
    params: Dict[str, Any]  # 参数字典
    args: List[str]         # 位置参数列表

class CommandParser:
    # 参数别名映射
    KEY_ALIASES = {
        "s": "enable_search",
        "gs": "enable_search",
        "t": "enable_thinking",
        "th": "enable_thinking",
        "g": "enable_gif",
        "gif": "enable_gif",
        "ar": "aspect_ratio",
        "r": "image_size",
        "size": "image_size",
        "to": "timeout",
        "up": "upscale_instruction",
        "q": "target_user_id",
        "a": "additional_prompt",
        "add": "additional_prompt",
        # p, p1, p2... 特殊处理
    }

    # 需值参数
    VALUE_KEYS = {
        "aspect_ratio", "image_size", "timeout", "target_user_id", 
        "additional_prompt", "upscale_instruction"
    }

    # 布尔开关 (无值为 True)
    BOOLEAN_KEYS = {
        "enable_search", "enable_thinking", "enable_gif"
    }

    @classmethod
    def extract_pure_command(cls, text: str, prefixes: List[str]) -> Optional[str]:
        if not text:
            return None

        text = text.strip()
        matched_prefix = ""

        for p in prefixes:
            if text.startswith(p):
                matched_prefix = p
                break

        content = text[len(matched_prefix):].strip()
        if not content:
            return None

        parts = content.split(maxsplit=1)
        return parts[0] if parts else None

    @classmethod
    def parse(cls, text: str, prefixes: List[str] = None, cmd_aliases: Set[str] = None) -> ParsedCommand:
        if prefixes is None: prefixes = []
        if cmd_aliases is None: cmd_aliases = set()

        text = text.strip()

        # 1. 剥离前缀
        matched_prefix = ""
        for p in prefixes:
            if text.startswith(p):
                matched_prefix = p
                break

        content = text[len(matched_prefix):].strip()

        # 2. Tokenize
        try:
            tokens = shlex.split(content, posix=True)
        except ValueError:
            tokens = content.split()

        params: Dict[str, Any] = {}
        text_parts: List[str] = []
        args: List[str] = []

        i = 0
        while i < len(tokens):
            token = tokens[i]

            # 3. 剥离指令名
            if i == 0 and token.lower() in [a.lower() for a in cmd_aliases]:
                i += 1
                continue

            # 4. 解析参数
            if token.startswith("--") and len(token) > 2:
                raw_key = token[2:]

                if raw_key.startswith("p") and (len(raw_key) == 1 or raw_key[1:].isdigit()):
                    key = raw_key
                    is_p = True
                else:
                    key = cls.KEY_ALIASES.get(raw_key, raw_key)
                    is_p = False

                next_token = tokens[i + 1] if i + 1 < len(tokens) else None
                is_next_flag = next_token is not None and next_token.startswith("--")

                if is_p or key in cls.VALUE_KEYS:
                    if next_token is not None and not is_next_flag:
                        params[key] = next_token
                        i += 2
                    else:
                        if key == "upscale_instruction": params[key] = "default"
                        else: params[key] = True
                        i += 1
                elif key in cls.BOOLEAN_KEYS:
                    if next_token is not None and next_token.lower() in ("true", "false", "1", "0", "on", "off"):
                        params[key] = (next_token.lower() in ("true", "1", "on", "yes"))
                        i += 2
                    else:
                        params[key] = True
                        i += 1
                else:
                    params[key] = True
                    i += 1
            else:
                if not token.startswith("@") and not token.startswith("[CQ:at"):
                    text_parts.append(token)

                args.append(token)
                i += 1

        clean_text = " ".join(text_parts)

        return ParsedCommand(
            raw_text=text,
            clean_text=clean_text,
            params=params,
            args=args
        )

    @staticmethod
    def _to_bool(val: str) -> bool:
        return val.lower() in ("true", "1", "on", "yes")

    @staticmethod
    def extract_target_id(text: str) -> Optional[str]:
        clean = text.replace("@", "").strip()
        if clean.isdigit():
            return clean
        return None