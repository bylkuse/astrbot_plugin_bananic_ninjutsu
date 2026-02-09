import json
import base64
import re
import aiohttp
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from ..domain import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils import Result, Ok, Err, ImageUtils
from .base import BaseProvider

class OpenAIProvider(BaseProvider):
    DEFAULT_STREAM_SETTING = False

    def _get_stream_setting(self, preset) -> bool:
        val = getattr(preset, "stream", None)
        if val is not None:
            return val
        return self.DEFAULT_STREAM_SETTING

    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        try:
            headers = await self._get_headers(request)
            payload = await self._build_payload(request)

            is_chat_request = "messages" in payload
            url = self._resolve_endpoint(request.preset.api_base, is_chat=is_chat_request)
            use_stream = payload.get("stream", False)

            kwargs = self._get_request_kwargs(request, stream=use_stream)

            async with self.session.post(url, json=payload, headers=headers, **kwargs) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    msg = f"HTTP {resp.status}"
                    try:
                        err_json = json.loads(text)
                        if "error" in err_json:
                            detail = err_json["error"].get("message", str(err_json["error"]))
                            msg += f": {detail}"
                        else:
                            msg += f" - {text[:200]}"
                    except:
                        msg += f" - {text[:200]}"

                    raise PluginError(APIErrorType.SERVER_ERROR, msg, resp.status)

                if use_stream:
                    response_content = await self._parse_sse_response(resp)
                else:
                    response_content = await resp.json()

            image_url = self._extract_image_url(response_content)

            if not image_url:
                preview = str(response_content)[:200]
                hint = " (流式模式可能丢失了Base64图片，请尝试关闭流式)" if use_stream else ""
                # 使用 TRANSIENT_ERROR: 这类错误通常是API响应不稳定导致的，应重试而非冷却Key
                return Err(PluginError(
                    APIErrorType.TRANSIENT_ERROR, 
                    f"API返回数据结构异常，无法提取图片{hint}。预览: {preview}"
                ))

            image_bytes = await self._download_or_decode(image_url, request.proxy_url)

            return Ok(GenResult(
                images=[image_bytes],
                model_name=request.preset.model,
                finish_reason="success"
            ))

        except Exception as e:
            error, _ = self.convert_exception(e)
            return Err(error)

    async def _parse_sse_response(self, resp: aiohttp.ClientResponse) -> str:
        full_text_accumulator = []

        async for line in self._iter_sse_lines(resp):
            if not line or not line.startswith(b'data: '):
                continue

            data_str = line[6:].decode('utf-8')
            if data_str == '[DONE]':
                continue

            try:
                chunk = json.loads(data_str)
                # OpenAI Chunk
                if "choices" in chunk and chunk["choices"]:
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_text_accumulator.append(content)
            except Exception:
                continue

        return "".join(full_text_accumulator)

    async def _get_headers(self, request: ApiRequest) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {request.api_key}",
            "Accept-Encoding": "gzip, deflate",
        }

    async def _build_payload(self, request: ApiRequest) -> Dict[str, Any]:
        model = request.preset.model.lower()
        is_native_dalle = "dall-e" in model and "chat" not in (request.preset.api_base or "")
        use_stream = False if is_native_dalle else self._get_stream_setting(request.preset)

        # DALL-E
        if is_native_dalle:
            payload = {
                "model": request.preset.model,
                "prompt": request.gen_config.prompt,
                "n": 1,
                "size": self._map_dalle_size(request.gen_config.image_size),
                "response_format": "b64_json"
            }
            if "dall-e-3" in model:
                payload["quality"] = "standard"
            return payload

        # Chat Completions
        content_list: List[Dict[str, Any]] = [{"type": "text", "text": request.gen_config.prompt}]

        for img_bytes in request.image_bytes_list:
            b64_str = base64.b64encode(img_bytes).decode("utf-8")
            mime_type = ImageUtils.get_mime_type(img_bytes)
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64_str}"}
            })

        payload = {
            "model": request.preset.model,
            "messages": [{"role": "user", "content": content_list}],
            "stream": use_stream
        }

        # Gemini
        if "pro" in model and ("image" in model or "banana" in model):
            payload["modalities"] = ["image", "text"]
            img_config = {}
            if request.gen_config.aspect_ratio != "default":
                img_config["aspectRatio"] = request.gen_config.aspect_ratio
            if request.gen_config.image_size != "1K":
                img_config["imageSize"] = request.gen_config.image_size
            if img_config:
                payload.setdefault("generationConfig", {})["imageConfig"] = img_config
        else:
            payload["max_tokens"] = 2048

        return payload

    def _resolve_endpoint(self, base_url: str, is_chat: bool = True) -> str:
        url = (base_url or "").strip().rstrip("/")
        if not url:
            return "https://api.openai.com/v1/chat/completions" if is_chat else "https://api.openai.com/v1/images/generations"

        if is_chat:
            if url.endswith("/chat/completions"): return url
            if re.search(r"/v1(?:beta)?$", url): return f"{url}/chat/completions"
            return f"{url}/v1/chat/completions"
        else:
            if url.endswith("/chat/completions"): 
                return url.replace("/chat/completions", "/images/generations")
            if url.endswith("/v1"): return f"{url}/images/generations"
            if url.endswith("/v1/models"): return url.replace("/models", "/images/generations")
            return f"{url}/v1/images/generations"

    def _extract_image_url(self, content: Any) -> Optional[str]:
        # JSON 对象
        if isinstance(content, dict):
            # DALL-E
            if "data" in content and isinstance(content["data"], list) and content["data"]:
                item = content["data"][0]
                if "url" in item: return item["url"]
                if "b64_json" in item: return f"data:image/png;base64,{item['b64_json']}"

            # Chat Completions
            if "choices" in content and isinstance(content["choices"], list) and content["choices"]:
                choice = content["choices"][0]
                message = choice.get("message", {})

                # Tool Calls
                if "tool_calls" in message and message["tool_calls"]:
                    for tool in message["tool_calls"]:
                        func_args = tool.get("function", {}).get("arguments", "")
                        if "http" in func_args:
                            urls = re.findall(r"(https?://[^\s<>\"'()\[\]]+)", func_args)
                            if urls: return urls[0].strip()

                # Images 数组
                if "images" in message and isinstance(message["images"], list) and message["images"]:
                    img_obj = message["images"][0]
                    if isinstance(img_obj, str): return img_obj
                    if isinstance(img_obj, dict):
                        if "url" in img_obj: return img_obj["url"]
                        if "image_url" in img_obj and isinstance(img_obj["image_url"], dict):
                            return img_obj["image_url"].get("url")

                content = message.get("content", "")

        # 字符串
        if isinstance(content, str):
            content = content.strip()
            # Markdown 图片
            if match := re.search(r"!\[.*?\]\((https?://[^\)]+)\)", content):
                return match.group(1).strip()
            # Markdown 链接
            if match := re.search(r"\[.*?\]\((https?://[^\)]+)\)", content):
                return match.group(1).strip()
            # Base64 Data URI
            if match := re.search(r"(data:image/[a-zA-Z0-9.+-]+;\s*base64\s*,\s*[-A-Za-z0-9+/=_\s]+)", content):
                return match.group(1).strip()
            # 纯 URL 提取
            urls = re.findall(r"(https?://[^\s<>\"'()\[\]]+)", content)
            if urls:
                for u in urls:
                    if any(ext in u.lower() for ext in ['.png', '.jpg', '.jpeg', '.webp', '.gif']):
                        return u.strip()
                return urls[-1].strip()

        return None

    def _map_dalle_size(self, size_str: str) -> str:
        s = size_str.upper()
        if "1K" in s or "1024" in s: return "1024x1024"
        if "512" in s: return "512x512"
        return "1024x1024"

    async def get_models(self, request: ApiRequest) -> List[str]:
        chat_url = self._resolve_endpoint(request.preset.api_base, is_chat=True)

        if "/chat/completions" in chat_url:
            url = chat_url.replace("/chat/completions", "/models")
        else:
            url = chat_url.rstrip("/")
            if url.endswith("/v1"): url += "/models"
            else: url += "/models"

        headers = {"Authorization": f"Bearer {request.api_key}"}
        try:
            async with self.session.get(url, headers=headers, proxy=request.proxy_url, timeout=10) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {text}")

                data = await resp.json()
                if "data" in data and isinstance(data["data"], list):
                    keywords = ["image", "vision", "dall", "pic", "flux", "journey", "mid", "sdxl", "banana", "rec", "o1"]
                    models = [
                        item["id"] for item in data["data"] 
                        if "id" in item and any(k in item["id"].lower() for k in keywords)
                    ]
                    return sorted(models)
                return []
        except Exception as e:
            logger.warning(f"[OpenAIProvider] 获取模型列表失败 ({url}): {e}")
            raise e