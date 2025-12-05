import os
import json
from pathlib import Path
from typing import Dict, List, Any
from astrbot.api import logger

class ConfigSerializer:
    """序列化，兼容性妥协"""

    @staticmethod
    def parse_single_kv(text: str, separator: str = ":") -> tuple[str, str] | None:
        if separator in text:
            k, v = text.split(separator, 1)
            k, v = k.strip(), v.strip()
            if k and v:
                return k, v
        return None

    @staticmethod
    def load_kv_list(data_list: List[str]) -> Dict[str, str]:
        result = {}
        if not data_list:
            return result
        for item in data_list:
            if kv := ConfigSerializer.parse_single_kv(item):
                result[kv[0]] = kv[1]
        return result

    @staticmethod
    def dump_kv_list(data_dict: Dict[str, str], sort: bool = True) -> List[str]:
        if not data_dict:
            return []
        keys = sorted(data_dict.keys()) if sort else data_dict.keys()
        return [f"{k}:{data_dict[k]}" for k in keys]

    @staticmethod
    def load_json_list(
        data_list: List[str], key_field: str = "name"
    ) -> Dict[str, Dict[str, Any]]:
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
    def dump_json_list(data_map: Dict[str, Dict[str, Any]]) -> List[str]:
        return [json.dumps(data, ensure_ascii=False) for data in data_map.values()]

    @staticmethod
    def serialize_any(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, ensure_ascii=False)
        return str(value)

    @staticmethod
    def serialize_pretty(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2, ensure_ascii=False)
        return str(value)

    @staticmethod
    def load_json_from_file(file_path: Path, default: Any = None) -> Any:
        if not file_path.exists():
            return default
        try:
            content = file_path.read_text(encoding="utf-8")
            return json.loads(content)
        except Exception as e:
            logger.error(f"ConfigSerializer 加载 {file_path} 失败: {e}")
            return default

    @staticmethod
    def save_json_to_file(file_path: Path, data: Any):
        try:
            content = json.dumps(data, ensure_ascii=False, indent=4)

            # 原子写入
            temp_path = file_path.with_suffix(".tmp")
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, file_path)
        except Exception as e:
            logger.error(f"ConfigSerializer 保存 {file_path} 失败: {e}")