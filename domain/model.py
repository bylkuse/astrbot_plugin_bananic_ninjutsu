from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any


class ApiType(str, Enum):
    GOOGLE = "google"
    OPENAI = "openai"
    ZAI = "zai"

class APIErrorType(Enum):
    INVALID_ARGUMENT = "invalid_argument"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    SAFETY_BLOCK = "safety_block"
    DEBUG_INFO = "debug_info"
    UNKNOWN = "unknown"

@dataclass
class PluginError:
    error_type: APIErrorType
    message: str
    status_code: Optional[int] = None
    raw_data: Optional[Dict[str, Any]] = None

    def __str__(self):
        return f"[{self.error_type.name}] {self.message}"

@dataclass
class ConnectionPreset:
    name: str
    api_type: ApiType
    api_base: str
    model: str
    stream: Optional[bool] = None
    api_keys: List[str] = field(default_factory=list)
    extra_config: Dict[str, Any] = field(default_factory=dict)

@dataclass
class GenerationConfig:
    prompt: str
    negative_prompt: Optional[str] = None
    image_size: str = "1K"             # --r
    aspect_ratio: str = "default"      # --ar
    steps: int = 20
    timeout: int = 300                 # --to
    enable_search: bool = False        # --s
    enable_thinking: bool = False      # --t
    upscale_instruction: Optional[str] = None # --up
    sender_id: Optional[str] = None    # --q
    target_user_id: Optional[str] = None 

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__

@dataclass
class ApiRequest:
    api_key: str
    preset: ConnectionPreset
    gen_config: GenerationConfig
    image_bytes_list: List[bytes] = field(default_factory=list)

    proxy_url: Optional[str] = None
    debug_mode: bool = False

@dataclass
class GenResult:
    images: List[bytes]
    text_content: Optional[str] = None
    model_name: str = ""
    cost_time: float = 0.0
    finish_reason: str = "success"
    actual_cost: int = 0
    enhancer_model: Optional[str] = None
    enhancer_instruction: Optional[str] = None

@dataclass
class UserQuota:
    user_id: str
    remaining: int
    used_today: int = 0
    last_checkin: str = ""