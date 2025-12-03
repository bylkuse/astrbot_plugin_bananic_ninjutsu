import json
from typing import Dict, List, Any, Optional

class ConfigSerializer:
    """序列化，兼容性妥协"""
    @staticmethod
    def parse_single_kv(text: str, separator: str = ":") -> Optional[tuple[str, str]]:
        if separator in text:
            k, v = text.split(separator, 1)
            k, v = k.strip(), v.strip()
            if k and v:
                return k, v
        return None

    @staticmethod
    def load_kv_list(data_list: List[str]) -> Dict[str, str]:
        result = {}
        if not data_list: return result
        for item in data_list:
            if kv := ConfigSerializer.parse_single_kv(item):
                result[kv[0]] = kv[1]
        return result

    @staticmethod
    def dump_kv_list(data_dict: Dict[str, str], sort: bool = True) -> List[str]:
        if not data_dict: return []
        keys = sorted(data_dict.keys()) if sort else data_dict.keys()
        return [f"{k}:{data_dict[k]}" for k in keys]

    @staticmethod
    def load_json_list(data_list: List[str], key_field: str = "name") -> Dict[str, Dict[str, Any]]:
        result = {}
        if not data_list: return result
        for item_str in data_list:
            try:
                data = json.loads(item_str)
                if key_field in data: result[data[key_field]] = data
            except (json.JSONDecodeError, TypeError): continue
        return result

    @staticmethod
    def dump_json_list(data_map: Dict[str, Dict[str, Any]]) -> List[str]:
        return [json.dumps(data, ensure_ascii=False) for data in data_map.values()]