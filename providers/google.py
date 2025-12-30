import json
import base64
import asyncio
import aiohttp
from typing import List, Any, Dict

from astrbot.api import logger

from ..domain import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils import Result, Ok, Err, ImageUtils

from .base import BaseProvider

class GoogleProvider(BaseProvider):
    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        model = request.preset.model
        clean_model_name = model.replace("models/", "")

        try:
            payload = await self._build_payload(request, clean_model_name)
        except Exception as e:
            return Err(PluginError(APIErrorType.INVALID_ARGUMENT, f"请求构建失败: {e}"))

        enable_stream = request.preset.stream if request.preset.stream is not None else True
        base_url = (request.preset.api_base or "https://generativelanguage.googleapis.com").rstrip("/")
        for suffix in ["/v1beta", "/v1"]:
            if base_url.endswith(suffix):
                base_url = base_url[:-len(suffix)]

        version = "v1beta"
        method = "streamGenerateContent" if enable_stream else "generateContent"

        url = f"{base_url}/{version}/models/{clean_model_name}:{method}?key={request.api_key}"
        if enable_stream: url += "&alt=sse"
        headers = {"Content-Type": "application/json", "x-goog-api-key": request.api_key}

        try:
            kwargs = self._get_request_kwargs(request, stream=enable_stream)
            kwargs['ssl'] = False 

            async with self.session.post(url, json=payload, headers=headers, **kwargs) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    try:
                        err_json = json.loads(text)
                        msg = err_json.get("error", {}).get("message", text)
                        status = err_json.get("error", {}).get("status", "UNKNOWN")
                        return Err(PluginError(APIErrorType.SERVER_ERROR, f"Google API ({status}): {msg}", resp.status))
                    except:
                        pass
                    return Err(PluginError(APIErrorType.SERVER_ERROR, f"HTTP {resp.status}: {text[:200]}", resp.status))

                if enable_stream:
                    return await self._process_stream_response(resp, clean_model_name)
                else:
                    data = await resp.json()
                    return self._process_unary_response(data, clean_model_name)

        except Exception as e:
            error, _ = self.convert_exception(e)
            return Err(error)

    async def _process_stream_response(self, resp: aiohttp.ClientResponse, model_name: str) -> Result[GenResult, PluginError]:
        collected_text = []
        collected_images = []
        finish_reason = "success"

        try:
            async for line in self._iter_sse_lines(resp):
                if not line or not line.startswith(b"data:"):
                    continue

                json_str = line[5:].strip().decode("utf-8")
                if not json_str: continue

                try:
                    chunk = json.loads(json_str)
                    candidates = chunk.get("candidates", [])
                    if not candidates: continue

                    cand = candidates[0]

                    if "finishReason" in cand:
                        reason = cand["finishReason"]
                        if reason not in ["STOP", "MAX_TOKENS", "NONE"]:
                            finish_reason = reason

                    content = cand.get("content", {})
                    parts = content.get("parts", [])

                    for part in parts:
                        if "text" in part:
                            collected_text.append(part["text"])
                        if "inlineData" in part: 
                            b64_data = part["inlineData"].get("data")
                            if b64_data:
                                collected_images.append(base64.b64decode(b64_data))
                        if "thought" in part and part["thought"]:
                            collected_text.append(f"\n[Thinking]: {part['thought']}\n")

                except json.JSONDecodeError:
                    continue

        except asyncio.TimeoutError:
            return Err(PluginError(APIErrorType.SERVER_ERROR, "请求超时 (数据流读取中断)"))
        except Exception as e:
            return Err(PluginError(APIErrorType.SERVER_ERROR, f"流式读取中断: {e}"))

        if not collected_text and not collected_images:
            if finish_reason == "success":
                return Err(PluginError(APIErrorType.SERVER_ERROR, "API 返回空内容 (请检查 Prompt 是否被安全策略拦截)"))
            return Err(PluginError(APIErrorType.SERVER_ERROR, f"流式请求完成，但未收到有效内容 (Reason: {finish_reason})"))

        return Ok(GenResult(
            images=collected_images,
            text_content="".join(collected_text) if collected_text else None,
            model_name=model_name,
            finish_reason=finish_reason
        ))

    def _process_unary_response(self, data: Dict, model_name: str) -> Result[GenResult, PluginError]:
        candidates = data.get("candidates", [])
        if not candidates:
            feedback = data.get("promptFeedback", {})
            block_reason = feedback.get("blockReason", "Unknown")
            return Err(PluginError(APIErrorType.SAFETY_BLOCK, f"请求被拦截: {block_reason}"))

        cand = candidates[0]

        finish_reason = cand.get("finishReason", "success")
        if finish_reason in ["PROHIBITED_CONTENT", "IMAGE_SAFETY", "SAFETY"]:
            return Err(PluginError(APIErrorType.SAFETY_BLOCK, f"内容安全拦截 ({finish_reason})"))
        elif finish_reason == "OTHER":
            return Err(PluginError(APIErrorType.SERVER_ERROR, "API 返回 OTHER 错误 (可能是参数不兼容)"))

        parts = cand.get("content", {}).get("parts", [])

        text_list = []
        images_list = []

        for part in parts:
            if "text" in part:
                text_list.append(part["text"])
            if "inlineData" in part:
                b64_data = part["inlineData"].get("data")
                if b64_data:
                    images_list.append(base64.b64decode(b64_data))
            if "thought" in part:
                text_list.append(f"[Thinking] {part['thought']}")

        if not text_list and not images_list:
             return Err(PluginError(APIErrorType.UNKNOWN, "未收到有效内容"))

        return Ok(GenResult(
            images=images_list,
            text_content="".join(text_list) if text_list else None,
            model_name=model_name,
            finish_reason=finish_reason
        ))

    async def _build_payload(self, request: ApiRequest, model_name: str) -> Dict[str, Any]:

        parts = []
        if request.gen_config.prompt:
            parts.append({"text": request.gen_config.prompt})

        if request.image_bytes_list:
            for img_bytes in request.image_bytes_list:
                mime_type = ImageUtils.get_mime_type(img_bytes)
                b64_str = base64.b64encode(img_bytes).decode("utf-8")
                parts.append({
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": b64_str
                    }
                })

        gen_config = {
            "responseModalities": ["TEXT", "IMAGE"],
            "maxOutputTokens": 2048,
        }

        image_config = {}
        if request.gen_config.aspect_ratio != "default":
            image_config["aspectRatio"] = request.gen_config.aspect_ratio

        model_lower = model_name.lower()
        should_send_size = "pro" in model_lower and ("image" in model_lower or "banana" in model_lower)

        if should_send_size and request.gen_config.image_size != "1K":
            image_config["imageSize"] = request.gen_config.image_size

        if image_config:
            gen_config["imageConfig"] = image_config

        if request.gen_config.enable_thinking:
            gen_config["thinkingConfig"] = {"includeThoughts": True}

        tools = []
        if request.gen_config.enable_search:
            tools.append({"googleSearch": {}})

        safety_settings = [
            {"category": cat, "threshold": "BLOCK_NONE"}
            for cat in [
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_CIVIC_INTEGRITY"
            ]
        ]

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_config,
            "safetySettings": safety_settings
        }

        if tools:
            payload["tools"] = tools

        return payload

    async def get_models(self, request: ApiRequest) -> List[str]:
        base_url = (request.preset.api_base or "https://generativelanguage.googleapis.com").rstrip("/")
        for suffix in ["/v1beta", "/v1"]:
            if base_url.endswith(suffix):
                base_url = base_url[:-len(suffix)]

        url = f"{base_url}/v1beta/models?key={request.api_key}"

        try:
            async with self.session.get(url, proxy=request.proxy_url, timeout=20, ssl=False) as resp:
                if resp.status != 200:
                    logger.warning(f"[GoogleProvider] 获取模型列表失败: HTTP {resp.status}")
                    return []

                data = await resp.json()
                models = data.get("models", [])

                model_ids = []
                for m in models:
                    raw_name = m.get("name", "")
                    clean_name = raw_name.replace("models/", "")
                    name_lower = clean_name.lower()

                    if any(k in name_lower for k in ["banana", "image", "vision"]):
                        model_ids.append(clean_name)

                return sorted(model_ids)

        except Exception as e:
            logger.warning(f"[GoogleProvider] 获取模型列表异常: {e}")
            return []