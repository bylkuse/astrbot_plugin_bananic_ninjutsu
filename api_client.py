import asyncio
import base64
import io
import logging
import re
from dataclasses import dataclass, field
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

class APIError(Exception):
    """API异常基类"""
    pass

class APIClient:
    # 错误捕获(状态码, 关键词, 提示, 是否可重试)
    _ERROR_PATTERNS = [
        (
            {400}, 
            {"invalid_argument", "bad request"}, 
            "\n💡请求无效 🔧检查提示词、参数、连接配置格式。", 
            False
        ),
        (
            {401, 403}, 
            {"unauthenticated", "permission", "access denied", "invalid api key"}, 
            "\n💡鉴权失败 🔧检查账户、密钥有效性。", 
            False
        ),
        (
            {402}, 
            {"quota", "billing", "payment"}, 
            "\n💡支付无效 🔧检查支持方式、套餐有效性。", 
            False
        ),
        (
            {404}, 
            {"not found"}, 
            "\n💡接入错误 🔧检查接入点、模型名有效性。", 
            False
        ),
        (
            {429}, 
            {"resource_exhausted", "too many requests", "rate limit"}, 
            "\n💡超额请求 🔧更换不受限的节点、账户", 
            False
        ),
        (
            set(range(500, 600)), 
            {"internal error", "server error", "timeout", "connect", "ssl", "503", "500", "reset", "socket", "handshake"}, 
            "\n💡网络异常 🔧更换稳定的上游服务、节点", 
            True
        )
    ]

    def __init__(self):
        self._key_index = 0
        self._key_lock = asyncio.Lock()

    async def _get_next_api_key(self, keys: List[str]) -> str:
        """轮询"""
        if not keys:
            raise APIError("未配置 API Key")
        
        async with self._key_lock:
            if self._key_index >= len(keys):
                self._key_index = 0
            
            key = keys[self._key_index]
            self._key_index = (self._key_index + 1) % len(keys)
            return key

    async def generate_content(self, config: ApiRequestConfig) -> bytes | str:
        if not config.api_keys:
            return "❌ 未配置有效的 API Key"

        api_key = await self._get_next_api_key(config.api_keys)
        
        if config.debug_mode:
            model_display = config.model
            if config.enhancer_model_name:
                preset_info = f"📒{config.enhancer_preset}" if config.enhancer_preset else ""
                model_display += f"（✨{config.enhancer_model_name}{preset_info}）"
            return (
                f"【调试模式】\n"
                f"API: {config.api_type}\n"
                f"模型: {model_display}\n"
                f"提示词: {config.prompt}\n"
                f"图数: {len(config.image_bytes_list)}张"
            )

        try:
            if config.api_type == "openai":
                return await self._call_openai(api_key, config)
            else:
                return await self._call_google(api_key, config)
        except Exception as e:
            logger.error(f"API Client Error: {e}", exc_info=True)
            return f"生成出错: {str(e)}"

    def _analyze_api_error(self, e: Exception, model_name: str) -> Tuple[str, bool]:
        """统一解析异常"""
        error_str = str(e).lower()
        status_code = None

        for attr in ['status_code', 'code', 'status', 'http_code', 'http_status']:
            val = getattr(e, attr, None)
            if isinstance(val, int):
                status_code = val
                break
            if isinstance(val, str) and val.strip().isdigit():
                status_code = int(val)
                break

        base_msg = "❌ API 请求失败"
        if status_code:
            base_msg += f" (HTTP/Code {status_code})"

        unified_hint = "\n👉 如持续失败，请尝试 #lmc 切换连接"

        for codes, keywords, reason_msg, should_retry in self._ERROR_PATTERNS:
            code_match = status_code in codes if status_code else False
            keyword_match = any(k in error_str for k in keywords)

            if code_match or keyword_match:
                if "不存在" in reason_msg and model_name:
                    reason_msg += f" ({model_name})"
                return base_msg + reason_msg + unified_hint, should_retry

        return base_msg + f"\n💡 详情: {str(e)[:150]}" + unified_hint, False

    async def _call_google(self, api_key: str, config: ApiRequestConfig) -> bytes | str:
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

        contents = []
        if config.prompt:
            contents.append(config.prompt)

        for img_bytes in config.image_bytes_list:
            try:
                processed_bytes = await ImageUtils.load_and_process(img_bytes, proxy=config.proxy_url, ensure_white_bg=True)
                if processed_bytes:
                    contents.append(PILImage.open(io.BytesIO(processed_bytes)))
            except Exception as e:
                logger.warning(f"图片处理失败: {e}")
                pass

        if not contents:
            return "❌ 没有有效的内容发送给 API"

        max_retries = 2
        last_error = None
        unified_safety_error = "❌ 被模型审核拦截，图片或提示词可能存在不当内容。\n💡 建议: 尝试更换图片、调整或简化提示词。"

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
                    if hasattr(response, 'prompt_feedback') and response.prompt_feedback and response.prompt_feedback.block_reason:
                        logger.warning(f"请求被拦截: {response.prompt_feedback.block_reason}")
                    return unified_safety_error

                candidate = response.candidates[0]

                if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                    finish_reason = candidate.finish_reason.name
                    if finish_reason in ['PROHIBITED_CONTENT', 'IMAGE_SAFETY', 'SAFETY']:
                        return unified_safety_error
                    elif finish_reason not in ['STOP', 'MAX_TOKENS']:
                        return f"❌ 生成意外中断: {finish_reason}"

                for part in candidate.content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        return part.inline_data.data

                text_resp = "".join([part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text])
                if text_resp:
                    return f"⚠️ API 仅回复了文本: {text_resp}"

                return unified_safety_error

            except Exception as e:
                last_error = e
                error_msg, is_retryable = self._analyze_api_error(e, full_model_name)

                if is_retryable and attempt < max_retries:
                    logger.warning(f"Google SDK 调用临时失败 (尝试 {attempt+1}/{max_retries+1}): {str(e)[:100]}")
                    await asyncio.sleep(1.5)
                    continue
                else:
                    logger.error(f"Google SDK 调用最终失败: {e}", exc_info=True)
                    return error_msg

        return f"❌ 请求最终失败: {str(last_error)[:150]}"

    async def _call_openai(self, api_key: str, config: ApiRequestConfig) -> bytes | str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        content_list = [{"type": "text", "text": config.prompt}]

        for image_bytes in config.image_bytes_list:
            processed_bytes = await ImageUtils.load_and_process(image_bytes, proxy=config.proxy_url, ensure_white_bg=True)
            if processed_bytes:
                img_b64 = base64.b64encode(processed_bytes).decode("utf-8")
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
                        return f"API请求失败 (HTTP {resp.status}): {text[:200]}"
                    
                    data = await resp.json()

            if "error" in data:
                return str(data["error"].get("message", data["error"]))

            image_url = self._extract_image_url_from_response(data)
            if not image_url:
                return "❌ API响应中未找到有效的图片地址"

            if image_url.startswith("data:image/"):
                return base64.b64decode(image_url.split(",", 1)[1])
            else:
                download_res = await ImageUtils.download_image(image_url, proxy=config.proxy_url)
                return download_res if download_res else "❌ 下载生成图片失败"

        except asyncio.TimeoutError:
            return "❌ 请求超时"
        except Exception as e:
            return f"❌ OpenAI 调用错误: {str(e)}"

    def _extract_image_url_from_response(self, data: Dict[str, Any]) -> str | None:
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
            match = re.search(r'https?://[^\s<>")\]]+', content)
            if match: return match.group(0).rstrip(")>,'\"")
            
            if '![image](' in content:
                start = content.find('![image](') + 9
                end = content.find(')', start)
                if end > start: return content[start:end]
        except (KeyError, IndexError, TypeError):
            pass
            
        return None