import asyncio
import base64
import io
import json
import re
import time
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Tuple
from abc import ABC, abstractmethod

import aiohttp
from PIL import Image as PILImage
from astrbot.api import logger
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    GoogleSearch,
    HttpOptions,
    Tool,
)

try:
    from google.genai.types import ThinkingConfig
except ImportError:
    ThinkingConfig = None

from .core.images import ImageUtils
from .utils.serializer import ConfigSerializer
from .utils.result import Result, Ok, Err
from .utils.zai import ZaiTokenManager


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


class APIError(Exception):
    def __init__(self, error_type: APIErrorType, raw_message: str, 
            status_code: int | None = None, 
            data: Dict[str, Any] | None = None):
        self.error_type = error_type
        self.raw_message = raw_message
        self.status_code = status_code
        self.data = data or {}
        super().__init__(raw_message)


@dataclass
class ApiRequestConfig:
    api_keys: List[str]
    api_type: str = "google"
    api_base: str = "https://generativelanguage.googleapis.com"
    model: str = "gemini-3-pro-image-preview"
    prompt: str = ""
    image_bytes_list: List[bytes] = field(default_factory=list)
    timeout: int = 300
    image_size: str = "1K"
    aspect_ratio: str = "default"
    enable_search: bool = False
    proxy_url: str | None = None
    debug_mode: bool = False
    enhancer_model_name: str | None = None
    enhancer_preset: str | None = None
    thinking: bool = False


@dataclass
class GenResult:
    image: bytes
    thoughts: str = ""


class BaseGenerationProvider(ABC):
    _ERROR_MAPPING = [
        ({400}, {"invalid_argument", "bad request", "parse error"}, APIErrorType.INVALID_ARGUMENT, False),
        ({401, 403}, {"unauthenticated", "permission", "access denied", "invalid api key", "signature"}, APIErrorType.AUTH_FAILED, True),
        ({402}, {"billing", "payment", "quota"}, APIErrorType.QUOTA_EXHAUSTED, True),
        ({404}, {"not found", "404"}, APIErrorType.NOT_FOUND, False),
        ({429}, {"resource_exhausted", "too many requests", "rate limit"}, APIErrorType.RATE_LIMIT, True),
        (set(range(500, 600)), {"internal error", "server error", "timeout", "connect", "ssl", "503", "502", "504", "overloaded"}, APIErrorType.SERVER_ERROR, True),
        (set(), {"safety", "blocked", "content filter"}, APIErrorType.SAFETY_BLOCK, False),
    ]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    @abstractmethod
    async def generate(self, api_key: str, config: 'ApiRequestConfig', images: List[bytes]) -> 'GenResult':
        pass

    def analyze_exception(self, e: Exception) -> Tuple[APIError, bool]:
        if isinstance(e, APIError):
            for _, _, error_type, should_switch in self._ERROR_MAPPING:
                if error_type == e.error_type:
                    return e, should_switch
            return e, False

        error_str = str(e)[:1000].lower()
        status_code = None

        for attr in ["status_code", "code", "status", "http_code", "http_status"]:
            val = getattr(e, attr, None)
            if isinstance(val, int):
                status_code = val
                break
            if isinstance(val, str) and val.strip().isdigit():
                status_code = int(val)
                break

        if isinstance(e, asyncio.TimeoutError):
            return APIError(APIErrorType.SERVER_ERROR, f"请求超时: {str(e)}", 408), True
        if isinstance(e, aiohttp.ClientError):
            return APIError(APIErrorType.SERVER_ERROR, f"网络连接错误: {str(e)}"), True

        for codes, keywords, error_type, should_switch_key in self._ERROR_MAPPING:
            code_match = status_code in codes if status_code else False
            keyword_match = any(k in error_str for k in keywords)

            if code_match or keyword_match:
                return APIError(error_type, str(e), status_code), should_switch_key

        return APIError(APIErrorType.UNKNOWN, f"未知错误: {str(e)}", status_code), False


class GoogleProvider(BaseGenerationProvider):
    async def generate(self, api_key: str, config: 'ApiRequestConfig', images: List[bytes]) -> 'GenResult':
        contents = []
        if config.prompt:
            contents.append(config.prompt)

        if images:
            def _load_images():
                return [PILImage.open(io.BytesIO(img_data)) for img_data in images]
            loaded_images = await asyncio.to_thread(_load_images)
            contents.extend(loaded_images)

        if not contents:
            raise APIError(APIErrorType.INVALID_ARGUMENT, "没有有效的内容发送给 API")

        http_options = HttpOptions(
            base_url=config.api_base,
            api_version="v1beta",
            timeout=config.timeout * 1000,
        )

        full_model_name = (
            config.model
            if config.model.startswith("models/")
            else f"models/{config.model}"
        )
        client = genai.Client(api_key=api_key, http_options=http_options)
        tools = [Tool(google_search=GoogleSearch())] if config.enable_search else []

        image_config = {}
        if config.aspect_ratio != "default":
            image_config["aspect_ratio"] = config.aspect_ratio
        if config.image_size:
            image_config["image_size"] = config.image_size

        thinking_config = None
        if config.thinking:
            if ThinkingConfig:
                thinking_config = ThinkingConfig(include_thoughts=True)
            else:
                logger.warning("ThinkingConfig 导入失败，跳过思维链配置。")

        sdk_retries = 1
        last_exception = None

        for i in range(sdk_retries + 1):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=full_model_name,
                    contents=contents,
                    config=GenerateContentConfig.model_construct(
                        response_modalities=["Text", "Image"],
                        max_output_tokens=2048,
                        tools=tools if tools else None,
                        image_config=image_config if image_config else None,
                        thinking_config=thinking_config,
                    ),
                )

                if not response.candidates:
                    block_reason = "Unknown Block"
                    if hasattr(response, "prompt_feedback") and response.prompt_feedback:
                        block_reason = str(response.prompt_feedback.block_reason)
                    raise APIError(APIErrorType.SAFETY_BLOCK, f"请求被拦截: {block_reason}")

                candidate = response.candidates[0]

                if hasattr(candidate, "finish_reason") and candidate.finish_reason:
                    reason = candidate.finish_reason.name
                    if reason in ["PROHIBITED_CONTENT", "IMAGE_SAFETY", "SAFETY"]:
                        raise APIError(APIErrorType.SAFETY_BLOCK, f"内容安全拦截 ({reason})")
                    elif reason not in ["STOP", "MAX_TOKENS"]:
                        raise APIError(APIErrorType.SERVER_ERROR, f"生成异常中断: {reason}")

                found_image = None
                thoughts_text = []

                for part in candidate.content.parts:
                    if hasattr(part, "inline_data") and part.inline_data:
                        found_image = part.inline_data.data
                    elif hasattr(part, "thought") and part.thought:
                        if hasattr(part, "text") and part.text:
                            thoughts_text.append(part.text)
                    elif hasattr(part, "text") and part.text:
                        thoughts_text.append(part.text)

                if found_image:
                    return GenResult(image=found_image, thoughts="\n".join(thoughts_text))

                all_text = "".join(thoughts_text)
                if all_text:
                    raise APIError(APIErrorType.UNKNOWN, f"API 仅回复了文本 (Thinking?): {all_text}")

                raise APIError(APIErrorType.UNKNOWN, "未收到有效的图片数据")

            except APIError:
                raise
            except Exception as e:
                last_exception = e
                error_obj, is_retryable = self.analyze_exception(e)

                if is_retryable and i < sdk_retries:
                    logger.warning(f"Google SDK 网络抖动 (重试 {i + 1}): {str(e)[:100]}")
                    await asyncio.sleep(1)
                    continue
                else:
                    raise error_obj

        if last_exception:
            error_obj, _ = self.analyze_exception(last_exception)
            raise error_obj


class OpenAIProvider(BaseGenerationProvider):
    @staticmethod
    def _validate_and_normalize_b64(raw_data: str) -> str:
        # 基础清洗
        cleaned = (raw_data or "").strip().replace("\n", "").replace("\r", "")
        # 去前缀
        if ";base64," in cleaned:
            _, _, cleaned = cleaned.partition(";base64,")
        # 标准解码
        def try_decode(data: str) -> str:
            base64.b64decode(data, validate=True)
            return data
        try:
            return try_decode(cleaned)
        except Exception:
            pass
        # URL-safe Base64 & Padding
        alt = cleaned.replace("-", "+").replace("_", "/")
        pad_len = (-len(alt)) % 4
        if pad_len:
            alt += "=" * pad_len
        try:
            return try_decode(alt)
        except Exception:
            pass
        # 正则重组
        relaxed = re.sub(r"[^A-Za-z0-9+/=]", "", cleaned)
        pad_len2 = (-len(relaxed)) % 4
        if pad_len2:
            relaxed += "=" * pad_len2
        return relaxed

    @staticmethod
    def _resolve_endpoint(base_url: str) -> str:
        url = (base_url or "").strip().rstrip("/")
        if not url:
            return "https://api.openai.com/v1/chat/completions"
        if url.endswith("/chat/completions"):
            return url
        if re.search(r"/v1(?:beta)?$", url):
            return f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"

    def _parse_sse_response(self, raw_text: str) -> Dict[str, Any]:
        full_content = ""
        last_valid_event = {}

        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                json_str = line[5:].strip()
                if json_str == "[DONE]":
                    continue
                try:
                    event = json.loads(json_str)
                    if isinstance(event, dict):
                        last_valid_event = event
                        # 拼接content
                        if "choices" in event and len(event["choices"]) > 0:
                            delta = event["choices"][0].get("delta", {})
                            if "content" in delta:
                                full_content += delta["content"]
                except json.JSONDecodeError:
                    continue

        if not full_content and not last_valid_event:
             raise APIError(APIErrorType.SERVER_ERROR, f"无法解析 SSE 响应: {raw_text[:200]}")

        # 伪非流
        simulated_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": full_content
                }
            }]
        }

        if last_valid_event:
            simulated_response.update({k: v for k, v in last_valid_event.items() if k not in ["choices"]})
        return simulated_response

    async def _stream_generate(self, session, url, payload, headers, proxy, timeout) -> str:
        """防CF-524超时"""
        payload["stream"] = True
        full_content = ""
        async with session.post(url, json=payload, headers=headers, proxy=proxy, timeout=timeout) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise APIError(APIErrorType.SERVER_ERROR, f"Stream Init Failed: {resp.status} - {text}", status_code=resp.status)

            async for line in resp.content:
                line = line.strip()
                if not line:
                    continue

                if line.startswith(b"data: "):
                    line = line[6:]

                if line == b"[DONE]":
                    break

                try:
                    chunk_json = json.loads(line)
                    if "choices" in chunk_json and len(chunk_json["choices"]) > 0:
                        delta = chunk_json["choices"][0].get("delta", {})
                        content_piece = delta.get("content", "")
                        if content_piece:
                            full_content += content_piece
                except json.JSONDecodeError:
                    continue

        if not full_content:
            raise APIError(APIErrorType.SERVER_ERROR, "Stream completed but no content received")

        return full_content

    async def generate(self, api_key: str, config: 'ApiRequestConfig', images: List[bytes]) -> 'GenResult':
        request_api_key = api_key
        if config.api_type.lower() == "zai":
            if ZaiTokenManager:
                try:
                    request_api_key = await ZaiTokenManager.get_access_token(
                        api_key, 
                        proxy=config.proxy_url
                    )
                except Exception as e:
                    logger.error(f"Zai Token 交换失败: {e}")
                    raise APIError(APIErrorType.AUTH_FAILED, f"Discord Token 登录失败: {e}")
            else:
                logger.error("ZaiTokenManager 未加载，无法进行 Token 交换")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {request_api_key}",
        }

        content_list = [{"type": "text", "text": config.prompt}]
        for img_data in images:
            img_b64 = base64.b64encode(img_data).decode("utf-8")
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })

        payload = {
            "model": config.model,
            "messages": [{"role": "user", "content": content_list}],
            "stream": False,
        }
        # zAI
        if config.api_type.lower() in ["zai"]:
            extra_params = {}
            if config.aspect_ratio != "default":
                extra_params["image_aspect_ratio"] = config.aspect_ratio
            if config.image_size and config.image_size != "default":
                extra_params["image_resolution"] = config.image_size
            if extra_params:
                payload["params"] = extra_params

        # 兼容Gemini
        elif "gemini" in config.model.lower() or config.api_type == "openai_gemini":
            payload["modalities"] = ["image", "text"]
            gen_config = {}
            img_config = {}
            if config.aspect_ratio != "default":
                img_config["aspectRatio"] = config.aspect_ratio
            if config.image_size and config.image_size != "default":
                img_config["imageSize"] = config.image_size
            if img_config:
                gen_config["imageConfig"] = img_config
                payload["generationConfig"] = gen_config

        else:
            payload["max_tokens"] = 1500

        target_url = self._resolve_endpoint(config.api_base)

        debug_payload = payload.copy()
        if "messages" in debug_payload:
            debug_payload["messages"] = "[(Hidden content with Base64 images)]"
        
        masked_key = request_api_key[:6] + "..." if request_api_key else "None"
        logger.info(f"调用 OpenAI 兼容接口 ({config.api_type}): {config.model} @ {target_url}\nKey: {masked_key}\nParams: {json.dumps(debug_payload, ensure_ascii=False)}")
        
        use_stream = config.api_type.lower() == "zai"
        raw_image_bytes = None
        content_result = ""

        if use_stream:
            try:
                logger.info(f"正在尝试流式请求 (Anti-524 Mode)...")
                content_result = await self._stream_generate(
                    self.session, 
                    target_url, 
                    payload.copy(),
                    headers, 
                    config.proxy_url, 
                    config.timeout
                )
                logger.info("流式接收完成，正在解析图片地址...")
                
            except Exception as e:
                logger.warning(f"流式请求失败，尝试回退到普通模式: {e}")
                use_stream = False

        if not use_stream:
            payload["stream"] = False
            async with self.session.post(
                target_url,
                json=payload,
                headers=headers,
                proxy=config.proxy_url,
                timeout=config.timeout,
            ) as resp:
                response_text = await resp.text()

                if resp.status != 200:
                    try:
                        err_data = json.loads(response_text)
                        if "error" in err_data:
                            if isinstance(err_data["error"], dict):
                                msg = err_data["error"].get("message", str(err_data["error"]))
                            else:
                                msg = str(err_data["error"])
                            if resp.status == 401 and config.api_type == "zai":
                                if ZaiTokenManager:
                                    ZaiTokenManager.invalidate_cache(api_key)
                                raise APIError(APIErrorType.AUTH_FAILED, "Zai Token 已失效 (401) - 已清除缓存等待重试", status_code=401)
                            msg_lower = msg.lower()
                            if any(k in msg_lower for k in ["blocked", "prohibited", "safety", "nsfw"]):
                                raise APIError(APIErrorType.SAFETY_BLOCK, f"内容安全拦截: {msg}", status_code=resp.status)
                            if resp.status == 400:
                                raise APIError(APIErrorType.INVALID_ARGUMENT, f"请求参数错误: {msg}", status_code=400)
                            raise APIError(APIErrorType.SERVER_ERROR, f"API Error: {msg}", status_code=resp.status)
                    except json.JSONDecodeError:
                        pass

                    error_type = APIErrorType.SERVER_ERROR if 500 <= resp.status < 600 else APIErrorType.UNKNOWN
                    clean_msg = response_text[:200]
                    if "<html" in clean_msg.lower() or "<!doctype" in clean_msg.lower():
                        clean_msg = "Cloudflare/Server Error Page (HTML)"

                    raise APIError(error_type, f"HTTP {resp.status}: {clean_msg}", status_code=resp.status)

                try:
                    data = json.loads(response_text)
                    image_url = self._extract_image_url(data)
                    if image_url:
                        content_result = image_url
                    else:
                        if "choices" in data and data["choices"]:
                            content_result = data["choices"][0]["message"].get("content", "")

                except json.JSONDecodeError:
                    if "data:" in response_text:
                        simulated_data = self._parse_sse_response(response_text)
                        content_result = simulated_data["choices"][0]["message"]["content"]
                    else:
                        raise APIError(APIErrorType.SERVER_ERROR, f"无效的 JSON 响应: {response_text[:200]}")

        image_url = None

        if content_result.startswith("http") or content_result.startswith("data:image"):
            image_url = content_result
        else:
            fake_data = {
                "choices": [{"message": {"content": content_result}}]
            }
            image_url = self._extract_image_url(fake_data)

        if not image_url:
            logger.error(f"无法提取图片 URL。原始内容预览: {content_result[:200]}")
            raise APIError(APIErrorType.SERVER_ERROR, "API响应中未找到有效的图片地址")

        if image_url.startswith("data:image/") or ";base64," in image_url or not image_url.startswith("http"):
            try:
                normalized_b64 = self._validate_and_normalize_b64(image_url)
                raw_image_bytes = base64.b64decode(normalized_b64)
            except Exception as e:
                logger.error(f"Base64 解码失败: {e}")
                raise APIError(APIErrorType.SERVER_ERROR, "图片数据 Base64 解码失败")
        else:
            raw_image_bytes = await ImageUtils.download_image(
                image_url, proxy=config.proxy_url, session=self.session, timeout=60
            )

        if not raw_image_bytes:
            raise APIError(APIErrorType.SERVER_ERROR, "下载生成图片失败或内容为空")

        return GenResult(image=raw_image_bytes)

    def _extract_image_url(self, data: Dict[str, Any]) -> str | None:
        # 标准DALL-E
        if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            item = data["data"][0]
            if "url" in item:
                return item["url"]
            if "b64_json" in item:
                return f"data:image/png;base64,{item['b64_json']}"

        # 兼容Gemini
        if "candidates" in data:
            try:
                parts = data["candidates"][0]["content"]["parts"]
                for p in parts:
                    if "inlineData" in p:
                        return f"data:{p['inlineData']['mimeType']};base64,{p['inlineData']['data']}"
                    if "text" in p:
                        match = re.search(r'https?://[^\s<>")\]]+', p["text"])
                        if match:
                            return match.group(0).rstrip(")>,'\"")
            except (KeyError, IndexError):
                pass

        # Chat Completion
        content = ""
        if "choices" in data and isinstance(data["choices"], list) and len(data["choices"]) > 0:
            message = data["choices"][0].get("message", {})

            # 中转/本地API
            if "images" in message and isinstance(message["images"], list) and message["images"]:
                img_obj = message["images"][0]
                if isinstance(img_obj, dict):
                    return img_obj.get("image_url", {}).get("url") or img_obj.get("url")
                if isinstance(img_obj, str):
                    return img_obj

            content = message.get("content", "")

        # content解析
        if content and isinstance(content, str):
            # Data URI
            data_uri_match = re.search(
                r"(data:image/[a-zA-Z0-9.+-]+;\s*base64\s*,\s*[-A-Za-z0-9+/=_\s]+)", 
                content
            )
            if data_uri_match:
                return data_uri_match.group(1).strip()
            # MD图片语法
            md_match = re.search(r"!\[.*?\]\((https?://[^\)]+)\)", content)
            if md_match:
                return md_match.group(1).strip()
            # MD Data URI
            md_data_match = re.search(r"!\[.*?\]\((data:image/[^\)]+)\)", content)
            if md_data_match:
                return md_data_match.group(1).strip()
            # HTTP(S)
            url_match = re.search(r"(https?://[^\s<>\"')\]]+\.(?:png|jpe?g|gif|webp|bmp|tiff|avif))", content, re.IGNORECASE)
            if url_match:
                return url_match.group(1).strip()

        return None


class APIClient:
    def __init__(self):
        self._key_index = 0
        self._key_lock = asyncio.Lock()
        self._cooldown_keys: Dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._providers = {}

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    connector = aiohttp.TCPConnector(limit=100)
                    self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    def _get_provider(self, api_type: str) -> BaseGenerationProvider:
        if self._session is None:
            raise RuntimeError("Session未初始化")

        if api_type not in self._providers:
            if api_type == "google":
                self._providers[api_type] = GoogleProvider(self._session)
            elif api_type in ["openai", "zai"]:
                self._providers[api_type] = OpenAIProvider(self._session)
            else:
                raise APIError(APIErrorType.INVALID_ARGUMENT, f"不支持的 API 类型: {api_type}")

        if self._providers[api_type].session != self._session:
            self._providers[api_type].session = self._session

        return self._providers[api_type]

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._providers.clear()
            logger.debug("APIClient session closed.")

    async def _get_valid_api_key(self, keys: List[str]) -> str:
        """轮询"""
        if not keys:
            raise APIError(APIErrorType.AUTH_FAILED, "未配置 API Key")

        async with self._key_lock:
            now = time.time()
            expired_keys = [k for k, t in self._cooldown_keys.items() if t <= now]
            for k in expired_keys:
                del self._cooldown_keys[k]

            # 防索引越界
            if self._key_index >= len(keys):
                self._key_index = 0

            available_key = None
            for _ in range(len(keys)):
                current_key = keys[self._key_index]
                self._key_index = (self._key_index + 1) % len(keys)

                if current_key not in self._cooldown_keys:
                    available_key = current_key
                    break

            if available_key:
                return available_key

            active_cooldowns = [t for k, t in self._cooldown_keys.items() if k in keys]
            wait_time = 60
            if active_cooldowns:
                earliest_release = min(active_cooldowns)
                wait_time = int(earliest_release - now)
                wait_time = max(1, wait_time)

            logger.warning(f"所有 {len(keys)} 个 API Key 均在冷却中，请求被阻断。")
            raise APIError(
                APIErrorType.QUOTA_EXHAUSTED, 
                f"所有 API Key 均在冷却/限流中，请等待约 {wait_time} 秒后再试。"
            )

    def _mark_key_failed(self, key: str, duration: int = 60):
        expire_time = time.time() + duration
        self._cooldown_keys[key] = expire_time
        logger.warning(
            f"Key ...{key[-6:]} 被标记冷却 {duration}秒 (当前冷却池大小: {len(self._cooldown_keys)})"
        )

    async def _process_and_validate_images(
        self, config: ApiRequestConfig
    ) -> List[bytes]:
        if not config.image_bytes_list:
            await self.get_session() 
            return []

        session = await self.get_session()
        valid_images = []
        for img_bytes in config.image_bytes_list:
            try:
                processed = await ImageUtils.load_and_process(
                    img_bytes, 
                    proxy=config.proxy_url, 
                    ensure_white_bg=True,
                    session=session
                )
                if processed:
                    valid_images.append(processed)
            except Exception as e:
                logger.warning(f"单张图片处理失败: {e}")

        if config.image_bytes_list and not valid_images:
            raise APIError(
                APIErrorType.INVALID_ARGUMENT,
                "图片加载失败：无法获取或解析提供的图片，请检查链接或文件格式。",
            )

        return valid_images

    async def generate_content(self, config: ApiRequestConfig) -> Result[GenResult, APIError]:
        if not config.api_keys:
            return Err(APIError(APIErrorType.AUTH_FAILED, "未配置有效的 API Key"))

        try:
            processed_images = await self._process_and_validate_images(config)
        except Exception as e:
            if isinstance(e, APIError):
                return Err(e)
            return Err(APIError(APIErrorType.INVALID_ARGUMENT, f"图片处理失败: {str(e)}"))

        if config.debug_mode:
            debug_data = {
                "api_type": config.api_type,
                "model": config.model,
                "prompt": config.prompt,
                "image_count": len(processed_images),
                "enhancer_model": config.enhancer_model_name,
                "enhancer_preset": config.enhancer_preset,
            }
            return Err(APIError(APIErrorType.DEBUG_INFO, "调试模式阻断", data=debug_data))

        last_error = None
        max_attempts = max(1, min(len(config.api_keys), 5))
        base_delay = 1.5
        max_delay = 10.0

        for attempt in range(max_attempts):
            try:
                api_key = await self._get_valid_api_key(config.api_keys)
            except APIError as e:
                return Err(e)

            try:
                provider = self._get_provider(config.api_type)
                result = await provider.generate(api_key, config, processed_images)
                return Ok(result)

            except Exception as e:
                if isinstance(e, APIError):
                    error = e
                else:
                    try:
                        provider = self._get_provider(config.api_type)
                        error, _ = provider.analyze_exception(e)
                    except Exception:
                        error = APIError(APIErrorType.UNKNOWN, str(e))

                last_error = error

                if error.error_type in [
                    APIErrorType.SAFETY_BLOCK,
                    APIErrorType.INVALID_ARGUMENT,
                    APIErrorType.NOT_FOUND,
                    APIErrorType.DEBUG_INFO,
                ]:
                    logger.warning(f"API 致命错误: {error.raw_message}")
                    return Err(error)

                logger.warning(
                    f"API请求失败 (Attempt {attempt + 1}/{max_attempts}) "
                    f"- Key: ...{api_key[-6:]} "
                    f"- Type: {error.error_type.name} "
                    f"- Msg: {error.raw_message[:100]}"
                )

                if error.error_type == APIErrorType.RATE_LIMIT:
                    self._mark_key_failed(api_key, duration=60)
                elif error.error_type in [APIErrorType.AUTH_FAILED, APIErrorType.QUOTA_EXHAUSTED]:
                    self._mark_key_failed(api_key, duration=300)

                if attempt == max_attempts - 1:
                    break

                # 指数退避+随机抖动
                if error.error_type in [APIErrorType.RATE_LIMIT, APIErrorType.SERVER_ERROR]:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = random.uniform(0, 1)
                    actual_delay = delay + jitter
                    logger.debug(f"触发指数退避: 等待 {actual_delay:.2f}s 后重试...")
                    await asyncio.sleep(actual_delay)
                else:
                    await asyncio.sleep(0.5)
                continue

        if last_error:
            return Err(last_error)
        else:
            return Err(APIError(APIErrorType.UNKNOWN, "所有重试均失败，且无明确错误信息"))