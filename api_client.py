import asyncio
import base64
import io
import logging
import re
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from PIL import Image as PILImage
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    GoogleSearch,
    HttpOptions,
    Tool,
)

from .core.images import ImageUtils

logger = logging.getLogger("astrbot")

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
    def __init__(self, error_type: APIErrorType, raw_message: str, status_code: Optional[int] = None, data: Optional[Dict[str, Any]] = None):
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
    proxy_url: Optional[str] = None
    debug_mode: bool = False
    enhancer_model_name: Optional[str] = None
    enhancer_preset: Optional[str] = None

class APIClient:
    # 错误映射: (状态码, 关键词, 类型, 是否可重试)
    _ERROR_MAPPING = [
        (
            {400}, 
            {"invalid_argument", "bad request"}, 
            APIErrorType.INVALID_ARGUMENT, 
            False
        ),
        (
            {401, 403}, 
            {"unauthenticated", "permission", "access denied", "invalid api key"}, 
            APIErrorType.AUTH_FAILED, 
            False
        ),
        (
            {402}, 
            {"billing", "payment", "quota"}, 
            APIErrorType.QUOTA_EXHAUSTED, 
            False
        ),
        (
            {404}, 
            {"not found"}, 
            APIErrorType.NOT_FOUND, 
            False
        ),
        (
            {429}, 
            {"resource_exhausted", "too many requests", "rate limit"}, 
            APIErrorType.RATE_LIMIT, 
            False
        ),
        (
            set(range(500, 600)), 
            {"internal error", "server error", "timeout", "connect", "ssl", "503", "500", "reset", "socket", "handshake"}, 
            APIErrorType.SERVER_ERROR, 
            True
        )
    ]

    def __init__(self):
        self._key_index = 0
        self._key_lock = asyncio.Lock()

    async def _get_next_api_key(self, keys: List[str]) -> str:
        """轮询"""
        if not keys:
            raise APIError(APIErrorType.AUTH_FAILED, "未配置 API Key")
        
        async with self._key_lock:
            if self._key_index >= len(keys):
                self._key_index = 0
            
            key = keys[self._key_index]
            self._key_index = (self._key_index + 1) % len(keys)
            return key

    def _analyze_exception(self, e: Exception) -> Tuple[APIError, bool]:
        """结构化"""
        error_str = str(e).lower()
        status_code = None

        # 状态码
        for attr in ['status_code', 'code', 'status', 'http_code', 'http_status']:
            val = getattr(e, attr, None)
            if isinstance(val, int):
                status_code = val
                break
            if isinstance(val, str) and val.strip().isdigit():
                status_code = int(val)
                break

        # 匹配
        for codes, keywords, error_type, should_retry in self._ERROR_MAPPING:
            code_match = status_code in codes if status_code else False
            keyword_match = any(k in error_str for k in keywords)

            if code_match or keyword_match:
                return APIError(error_type, str(e), status_code), should_retry

        # 兜底
        return APIError(APIErrorType.UNKNOWN, str(e), status_code), False

    async def _process_and_validate_images(self, config: ApiRequestConfig) -> List[bytes]:
        """处理图片"""
        if not config.image_bytes_list:
            return []

        valid_images = []
        for img_bytes in config.image_bytes_list:
            try:
                processed = await ImageUtils.load_and_process(
                    img_bytes, 
                    proxy=config.proxy_url, 
                    ensure_white_bg=True
                )
                if processed:
                    valid_images.append(processed)
            except Exception as e:
                logger.warning(f"单张图片处理失败: {e}")

        if config.image_bytes_list and not valid_images:
            raise APIError(
                APIErrorType.INVALID_ARGUMENT, 
                "图片加载失败：无法获取或解析提供的图片，请检查链接或文件格式。"
            )

        return valid_images

    async def generate_content(self, config: ApiRequestConfig) -> bytes:
        if not config.api_keys:
            raise APIError(APIErrorType.AUTH_FAILED, "未配置有效的 API Key")

        api_key = await self._get_next_api_key(config.api_keys)

        if config.debug_mode:
            debug_data = {
                "api_type": config.api_type,
                "model": config.model,
                "prompt": config.prompt,
                "image_count": len(config.image_bytes_list),
                "enhancer_model": config.enhancer_model_name,
                "enhancer_preset": config.enhancer_preset
            }

            raise APIError(APIErrorType.DEBUG_INFO, "调试模式阻断", data=debug_data)

        try:
            if config.api_type == "openai":
                return await self._call_openai(api_key, config)
            else:
                return await self._call_google(api_key, config)
        except APIError:
            raise
        except Exception as e:
            logger.error(f"API Client Error: {e}", exc_info=True)
            api_error, _ = self._analyze_exception(e)
            raise api_error

    async def _call_google(self, api_key: str, config: ApiRequestConfig) -> bytes:
        processed_images_bytes = await self._process_and_validate_images(config)
        contents = []
        if config.prompt:
            contents.append(config.prompt)
            
        for img_data in processed_images_bytes:
            contents.append(PILImage.open(io.BytesIO(img_data)))

        if not contents:
            raise APIError(APIErrorType.INVALID_ARGUMENT, "没有有效的内容发送给 API")

        http_options = HttpOptions(
            base_url=config.api_base,
            api_version="v1beta",
            timeout=config.timeout * 1000
        )

        full_model_name = config.model if config.model.startswith("models/") else f"models/{config.model}"
        client = genai.Client(api_key=api_key, http_options=http_options)
        tools = [Tool(google_search=GoogleSearch())] if config.enable_search else []

        image_config = {}
        if config.aspect_ratio != "default":
            image_config["aspect_ratio"] = config.aspect_ratio
        if config.image_size:
            image_config["image_size"] = config.image_size

        max_retries = 2
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=full_model_name,
                    contents=contents,
                    config=GenerateContentConfig.model_construct(
                        response_modalities=['Text', 'Image'],
                        max_output_tokens=2048,
                        tools=tools if tools else None,
                        image_config=image_config if image_config else None
                    )
                )

                if not response.candidates:
                    block_reason = "Unknown Block"
                    if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                         block_reason = str(response.prompt_feedback.block_reason)
                    raise APIError(APIErrorType.SAFETY_BLOCK, f"请求被拦截: {block_reason}")

                candidate = response.candidates[0]

                if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                    reason = candidate.finish_reason.name
                    if reason in ['PROHIBITED_CONTENT', 'IMAGE_SAFETY', 'SAFETY']:
                        raise APIError(APIErrorType.SAFETY_BLOCK, f"内容安全拦截 ({reason})")
                    elif reason not in ['STOP', 'MAX_TOKENS']:
                        raise APIError(APIErrorType.SERVER_ERROR, f"生成异常中断: {reason}")

                for part in candidate.content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        return part.inline_data.data

                text_resp = "".join([part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text])
                if text_resp:
                    raise APIError(APIErrorType.UNKNOWN, f"API 仅回复了文本: {text_resp}")

                raise APIError(APIErrorType.UNKNOWN, "未收到有效的图片数据")

            except APIError:
                raise
            except Exception as e:
                last_error = e
                api_error, is_retryable = self._analyze_exception(e)

                if is_retryable and attempt < max_retries:
                    logger.warning(f"Google SDK 调用临时失败 (尝试 {attempt+1}/{max_retries+1}): {str(e)[:100]}")
                    await asyncio.sleep(1.5)
                    continue
                else:
                    logger.error(f"Google SDK 调用最终失败: {e}", exc_info=True)
                    raise api_error

        # 兜底
        api_error, _ = self._analyze_exception(last_error)
        raise api_error

    async def _call_openai(self, api_key: str, config: ApiRequestConfig) -> bytes:
        processed_images_bytes = await self._process_and_validate_images(config)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        content_list = [{"type": "text", "text": config.prompt}]

        for img_data in processed_images_bytes:
            img_b64 = base64.b64encode(img_data).decode("utf-8")
            content_list.append({
                "type": "image_url", 
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })

        payload = {
            "model": config.model,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": content_list}]
        }

        logger.info(f"调用 OpenAI 兼容接口: {config.model} @ {config.api_base}")

        raw_image_bytes = None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.api_base, 
                    json=payload, 
                    headers=headers, 
                    proxy=config.proxy_url, 
                    timeout=120
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise Exception(f"HTTP {resp.status}: {text[:200]}")
                    
                    data = await resp.json()

            if "error" in data:
                err_msg = str(data["error"].get("message", data["error"]))
                raise Exception(f"OpenAI Error: {err_msg}")

            image_url = self._extract_image_url_from_response(data)
            
            if not image_url:
                debug_json = json.dumps(data, ensure_ascii=False, indent=2)
                logger.error(f"OpenAI 响应解析失败，无法提取图片 URL。\n完整响应数据:\n{debug_json}")
                raise APIError(APIErrorType.UNKNOWN, "API响应中未找到有效的图片地址 (详情已记录到日志)")

            if image_url.startswith("data:image/"):
                raw_image_bytes = base64.b64decode(image_url.split(",", 1)[1])
            else:
                raw_image_bytes = await ImageUtils.download_image(image_url, proxy=config.proxy_url)
                
            if not raw_image_bytes:
                raise APIError(APIErrorType.SERVER_ERROR, "下载生成图片失败或内容为空")

        except asyncio.TimeoutError:
            raise APIError(APIErrorType.SERVER_ERROR, "请求超时")
        except APIError:
            raise
        except Exception as e:
            api_error, _ = self._analyze_exception(e)
            raise api_error

        return await ImageUtils.compress_image(raw_image_bytes)

    def _extract_image_url_from_response(self, data: Dict[str, Any]) -> str | None:
        try:
            return data["data"][0]["url"]
        except (KeyError, IndexError, TypeError):
            pass

        try:
            return data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        except (KeyError, IndexError, TypeError):
            pass

        try:
            return data["choices"][0]["message"]["images"][0]["url"]
        except (KeyError, IndexError, TypeError):
            pass

        try:
            content = data["choices"][0]["message"]["content"]

            if '![image](' in content:
                start = content.find('![image](') + 9
                end = content.find(')', start)
                if end > start: return content[start:end]

            match = re.search(r'https?://[^\s<>")\]]+', content)
            if match: return match.group(0).rstrip(")>,'\"")
            
        except (KeyError, IndexError, TypeError):
            pass

        return None