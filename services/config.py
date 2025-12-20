import json
import asyncio
from typing import Dict, Any, List, Optional, Tuple

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context

from ..domain.model import ConnectionPreset, ApiType

class KVHelper:
    """以兼容AstrBot前端&Dict"""
    @staticmethod
    def parse(text: str, separator: str = ":") -> Optional[Tuple[str, str]]:
        if separator in text:
            k, v = text.split(separator, 1)
            return k.strip(), v.strip()
        return None

    @staticmethod
    def list_to_dict(data_list: List[str]) -> Dict[str, str]:
        result = {}
        if not data_list:
            return result
        for item in data_list:
            if kv := KVHelper.parse(item):
                result[kv[0]] = kv[1]
        return result

    @staticmethod
    def dict_to_list(data_dict: Dict[str, str], sort: bool = True) -> List[str]:
        if not data_dict:
            return []
        keys = sorted(data_dict.keys()) if sort else data_dict.keys()
        return [f"{k}:{data_dict[k]}" for k in keys]

class JsonListHelper:
    @staticmethod
    def list_to_dict(data_list: List[str], key_field: str = "name") -> Dict[str, Dict[str, Any]]:
        result = {}
        if not data_list:
            return result
        for item_str in data_list:
            try:
                data = json.loads(item_str)
                if key_field in data:
                    result[data[key_field]] = data
            except (json.JSONDecodeError, TypeError):
                continue
        return result

    @staticmethod
    def dict_to_list(data_map: Dict[str, Any]) -> List[str]:
        return [json.dumps(data, ensure_ascii=False) for data in data_map.values()]

class ConfigService:
    def __init__(self, astr_config: AstrBotConfig, context: Context):
        self._astr_config = astr_config
        self._context = context

        self.prompts: Dict[str, str] = {}
        self.optimizers: Dict[str, str] = {}
        self.connections: Dict[str, ConnectionPreset] = {}
        self.active_preset_name: str = "None"
        self.prefixes: List[str] = []
        self._init_prefixes()

        self._load_all()

    def _init_prefixes(self):
        global_config = self._context.get_config()
        raw = global_config.get("wake_prefix", ["/"])
        if isinstance(raw, str):
            raw = [raw]

        # shlex 兼容处理
        pset = set(raw)
        pset.add("#")
        self.prefixes = sorted(list(pset), key=len, reverse=True)

    def get_prefixes(self) -> List[str]:
        return self.prefixes

    def get_display_prefix(self) -> str:
        if not self.prefixes:
            return "/"

        for p in self.prefixes:
            if p != "#":
                return p
        return self.prefixes[0]

    def _load_all(self):
        gen_conf = self._astr_config.get("Generation_Config", {})
        conn_conf = self._astr_config.get("Connection_Config", {})

        # 1. 生图预设
        raw_prompts = gen_conf.get("prompt_list", [])
        self.prompts = KVHelper.list_to_dict(raw_prompts)

        # 2. 优化预设
        raw_opts = gen_conf.get("optimizer_presets", [])
        self.optimizers = KVHelper.list_to_dict(raw_opts)

        if "default" not in self.optimizers:
            self.optimizers["default"] = "身份：你是一名专业的提示词工程师。任务：分析需求，重写以提升提示词质量（精确、稳定、可复现）"

        # 3. 连接预设
        raw_conns = conn_conf.get("connection_presets", [])
        conn_dicts = JsonListHelper.list_to_dict(raw_conns, key_field="name")

        self.connections = {}
        for name, data in conn_dicts.items():
            try:
                # dataclass
                preset = ConnectionPreset(
                    name=data.get("name", name),
                    api_type=ApiType(data.get("api_type", "google")),
                    api_base=data.get("api_url", ""),
                    model=data.get("model", ""),
                    stream=data.get("stream"),
                    api_keys=data.get("api_keys", []),
                    extra_config=data
                )
                self.connections[name] = preset
            except Exception as e:
                logger.error(f"[ConfigService] 加载连接预设 {name} 失败: {e}")

        # 4. 当前预设
        self.active_preset_name = conn_conf.get("current_preset_name", "None")

        # 兜底
        if self.active_preset_name not in self.connections and self.connections:
            first_key = next(iter(self.connections))
            self.active_preset_name = first_key
            logger.warning(f"[ConfigService] 指定预设不存在，自动切换至: {first_key}")

        logger.info(f"[ConfigService] 配置加载完成: {len(self.prompts)}个提示词, {len(self.connections)}个连接")

    def find_prompt_by_value(self, value: str) -> Optional[str]:
        target = value.strip()
        for k, v in self.prompts.items():
            if v.strip() == target:
                return k
        return None

    def find_optimizer_by_value(self, value: str) -> Optional[str]:
        target = value.strip()
        for k, v in self.optimizers.items():
            if v.strip() == target:
                return k
        return None

    async def save_all(self):
        if "Generation_Config" not in self._astr_config:
            self._astr_config["Generation_Config"] = {}
        if "Connection_Config" not in self._astr_config:
            self._astr_config["Connection_Config"] = {}

        gen_conf = self._astr_config["Generation_Config"]
        conn_conf = self._astr_config["Connection_Config"]

        # 回写 KV Lists
        gen_conf["prompt_list"] = KVHelper.dict_to_list(self.prompts)
        gen_conf["optimizer_presets"] = KVHelper.dict_to_list(self.optimizers)

        # 回写 JSON List
        raw_conn_dicts = {}
        for name, preset in self.connections.items():
            d = {
                "name": preset.name,
                "api_type": preset.api_type.value,
                "api_url": preset.api_base,
                "model": preset.model,
                "stream": preset.stream,
                "api_keys": preset.api_keys
            }
            if preset.extra_config:
                d.update({k:v for k,v in preset.extra_config.items() if k not in d})
            raw_conn_dicts[name] = d

        conn_conf["connection_presets"] = JsonListHelper.dict_to_list(raw_conn_dicts)
        conn_conf["current_preset_name"] = self.active_preset_name

        await asyncio.to_thread(self._astr_config.save_config)

    def is_debug_mode(self) -> bool:
        return self._astr_config.get("Basic_Config", {}).get("debug_prompt", False)

    def get_active_preset(self) -> Optional[ConnectionPreset]:
        return self.connections.get(self.active_preset_name)

    def get_prompt(self, key: str) -> Optional[str]:
        return self.prompts.get(key)

    async def update_prompt(self, key: str, value: str):
        self.prompts[key] = value
        await self.save_all()

    async def delete_prompt(self, key: str) -> bool:
        if key in self.prompts:
            del self.prompts[key]
            await self.save_all()
            return True
        return False

    async def rename_prompt(self, old_key: str, new_key: str) -> bool:
        if old_key in self.prompts and new_key not in self.prompts:
            self.prompts[new_key] = self.prompts.pop(old_key)
            await self.save_all()
            return True
        return False

    def get_optimizer(self, key: str) -> Optional[str]:
        return self.optimizers.get(key)

    async def update_optimizer(self, key: str, value: str):
        self.optimizers[key] = value
        await self.save_all()

    async def delete_optimizer(self, key: str) -> bool:
        if key == "default": return False
        if key in self.optimizers:
            del self.optimizers[key]
            await self.save_all()
            return True
        return False

    async def rename_optimizer(self, old_key: str, new_key: str) -> bool:
        if old_key == "default": return False
        if old_key in self.optimizers and new_key not in self.optimizers:
            self.optimizers[new_key] = self.optimizers.pop(old_key)
            await self.save_all()
            return True
        return False

    async def update_connection(self, preset: ConnectionPreset):
        self.connections[preset.name] = preset
        if len(self.connections) == 1:
            self.active_preset_name = preset.name
        await self.save_all()

    async def delete_connection(self, name: str) -> bool:
        if name in self.connections:
            del self.connections[name]
            if self.active_preset_name == name:
                if self.connections:
                    self.active_preset_name = next(iter(self.connections))
                else:
                    self.active_preset_name = "None"
            await self.save_all()
            return True
        return False

    async def set_active_connection(self, name: str) -> bool:
        if name in self.connections:
            self.active_preset_name = name
            await self.save_all()
            return True
        return False

    async def rename_connection(self, old_name: str, new_name: str) -> bool:
        if old_name in self.connections and new_name not in self.connections:
            preset = self.connections.pop(old_name)
            preset.name = new_name
            self.connections[new_name] = preset

            if self.active_preset_name == old_name:
                self.active_preset_name = new_name

            await self.save_all()
            return True
        return False

    async def add_api_keys(self, preset_name: str, keys: List[str]) -> Tuple[int, int]:
        if preset_name not in self.connections:
            return 0, 0

        preset = self.connections[preset_name]
        existing = set(preset.api_keys)

        added_count = 0
        dup_count = 0

        for k in keys:
            k = k.strip()
            if not k:
                continue
            if k not in existing:
                preset.api_keys.append(k)
                existing.add(k)
                added_count += 1
            else:
                dup_count += 1

        if added_count > 0:
            await self.save_all()

        return added_count, dup_count

    async def delete_api_key(self, preset_name: str, index: int) -> Tuple[bool, int]:
        if preset_name not in self.connections:
            return False, 0

        preset = self.connections[preset_name]

        idx = index - 1
        if 0 <= idx < len(preset.api_keys):
            preset.api_keys.pop(idx)
            await self.save_all()
            return True, len(preset.api_keys)

        return False, len(preset.api_keys)