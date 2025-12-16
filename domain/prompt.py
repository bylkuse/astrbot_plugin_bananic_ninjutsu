import random
import re
import string
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable, Pattern
from dataclasses import dataclass

@dataclass
class VariableDefinition:
    name: str
    category: str
    pattern: Pattern
    description: str
    resolver: Callable[[re.Match, Dict[str, Any], Dict[str, Any]], str]
    display_formatter: Optional[Callable[[str], str]] = None 

class PromptResolver:
    def __init__(self):
        self.color_list = ["red", "blue", "green", "yellow", "purple", "orange", "black", "white", "pink", "cyan"]
        self._registry: List[VariableDefinition] = self._build_registry()

    def get_definitions(self) -> List[VariableDefinition]:
        return self._registry or []

    def _build_registry(self) -> List[VariableDefinition]:
        registry = []

        # 填空
        def _resolve_param(m, params, ctx):
            idx = m.group(1) or ""
            default_val = m.group(2) or ""
            key = f"p{idx}"
            val = params.get(key)
            if val is True: return default_val
            return str(val) if val else default_val

        registry.append(VariableDefinition(
            name="填空参数",
            category="🔧 工具",
            pattern=re.compile(r"%p(\d*)(?::([^%]*))?%", re.IGNORECASE),
            description="配合 --p 使用 (如 %p%→--p, %p2%→--p2)",
            resolver=_resolve_param,
            display_formatter=lambda s: f"{s} (填空)"
        ))

        # 上下文
        ctx_map_name = {
            "un": "昵称", "uid": "QQ号", "g": "群名", 
            "run": "随机群友", "age": "年龄", "bd": "生日"
        }

        def _resolve_ctx(m, params, ctx):
            key = m.group(1).lower()
            val = ctx.get(key)
            if val is not None:
                return str(val)
            return m.group(0)

        def _fmt_ctx(s: str) -> str:
            core = s.replace("%", "").lower()
            name = ctx_map_name.get(core, "")
            return f"{s}({name})" if name else s

        registry.append(VariableDefinition(
            name="环境信息",
            category="👤 用户/群组",
            pattern=re.compile(r"%(" + "|".join(ctx_map_name.keys()) + r")%", re.IGNORECASE),
            description=", ".join([f"%{k}%({v})" for k, v in ctx_map_name.items()]),
            resolver=_resolve_ctx,
            display_formatter=_fmt_ctx
        ))

        # 随机
        def _resolve_random(m, params, ctx):
            full_str = m.group(0).strip("%")

            if ":" in full_str:
                k, v = full_str.split(":", 1)
            else:
                k, v = full_str, ""

            k = k.lower()

            # %r:A|B%
            if k == "r" and v: 
                return random.choice(v.split("|"))

            # %rn:1-10%
            if k == "rn" and v:
                try:
                    mn, mx = map(int, v.split("-"))
                    return str(random.randint(mn, mx))
                except: pass

            # %rl:5%
            if k == "rl" and v:
                try: 
                    return "".join(random.choices(string.ascii_letters, k=int(v)))
                except: pass

            # %rc%
            if k == "rc": 
                return random.choice(self.color_list)

            return m.group(0)

        registry.append(VariableDefinition(
            name="随机生成",
            category="🎲 随机",
            pattern=re.compile(r"%\b(r|rn|rl|rc)(?::[^%]+)?%", re.IGNORECASE),
            description="%r:A|B%(选项), %rn:1-10%(数字), %rl:5%(字母), %rc%(颜色)",
            resolver=_resolve_random
        ))

        # 时间
        time_map = {
            "d": lambda: datetime.now().strftime("%m月%d日"),
            "t": lambda: datetime.now().strftime("%H:%M:%S"),
            "wd": lambda: f"星期{'日一二三四五六'[int(datetime.now().strftime('%w'))]}",
        }

        def _resolve_time(m, params, ctx):
            key = m.group(1).lower()
            if key in time_map:
                return time_map[key]()
            return m.group(0)

        registry.append(VariableDefinition(
            name="时间日期",
            category="📅 时间",
            pattern=re.compile(r"%(" + "|".join(time_map.keys()) + r")%", re.IGNORECASE),
            description="%d%(日期), %t%(时间), %wd%(星期)",
            resolver=_resolve_time,
            display_formatter=lambda s: f"{s}(当前时间)"
        ))

        return registry

    def resolve(self, prompt: str, params: Dict[str, Any], context_map: Dict[str, Any]) -> str:
        if not prompt or "%" not in prompt:
            return prompt

        for k, v in context_map.items():
            key_token = f"%{k}%"

            if key_token in prompt:
                prompt = prompt.replace(key_token, str(v))

        # 迭代
        for _ in range(5):
            if "%" not in prompt:
                break

            original_prompt = prompt

            for var_def in self._registry:
                def _replacer(m):
                    return var_def.resolver(m, params, context_map)

                prompt = var_def.pattern.sub(_replacer, prompt)

            if prompt == original_prompt:
                break

        return prompt