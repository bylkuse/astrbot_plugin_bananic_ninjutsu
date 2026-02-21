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

            # 对于 Images API，实现 response_format fallback 机制
            is_images_api = "response_format" in payload and not is_chat_request
            
            result = await self._do_request(url, payload, headers, kwargs, use_stream)
            
            # 如果是 Images API 且使用 b64_json 失败，尝试 fallback 到 url
            if result.is_err() and is_images_api and payload.get("response_format") == "b64_json":
                error = result.unwrap_err()
                # 检查是否是 response_format 不支持导致的错误
                if self._is_response_format_error(error):
                    logger.info("[OpenAIProvider] b64_json 不支持，尝试使用 url 格式")
                    payload["response_format"] = "url"
                    result = await self._do_request(url, payload, headers, kwargs, use_stream)
            
            if result.is_err():
                return result
                
            response_content = result.unwrap()
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

    async def _do_request(
        self, 
        url: str, 
        payload: Dict[str, Any], 
        headers: Dict[str, str], 
        kwargs: Dict[str, Any],
        use_stream: bool
    ) -> Result[Any, PluginError]:
        """执行 HTTP 请求并返回响应内容"""
        try:
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

                    return Err(PluginError(APIErrorType.SERVER_ERROR, msg, resp.status))

                if use_stream:
                    response_content = await self._parse_sse_response(resp)
                else:
                    response_content = await resp.json()
                    
            return Ok(response_content)
        except PluginError as e:
            return Err(e)
        except Exception as e:
            error, _ = self.convert_exception(e)
            return Err(error)

    def _is_response_format_error(self, error: PluginError) -> bool:
        """判断错误是否由 response_format 参数不支持导致"""
        msg = error.message.lower()
        keywords = [
            "response_format", 
            "b64_json", 
            "invalid", 
            "not supported",
            "unknown parameter",
            "unrecognized"
        ]
        return any(k in msg for k in keywords)

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

    def _is_images_api_endpoint(self, api_base: Optional[str]) -> bool:
        """判断是否为 /v1/images/generations 接入点"""
        if not api_base:
            return False
        url = api_base.lower().strip().rstrip("/")
        return url.endswith("/images/generations") or "/images/" in url

    async def _build_payload(self, request: ApiRequest) -> Dict[str, Any]:
        model = request.preset.model.lower()
        api_base = request.preset.api_base or ""
        
        # 判断接口类型优先级：
        # 1. 用户显式配置了 /images/generations 端点
        # 2. 模型名包含 dall-e 且不是 chat 端点
        is_images_api = self._is_images_api_endpoint(api_base)
        is_native_dalle = "dall-e" in model and "chat" not in api_base.lower()
        
        # 使用 Images API 的情况：显式配置了 images 端点，或者是原生 DALL-E
        use_images_api = is_images_api or is_native_dalle
        
        # Images API 不支持流式
        use_stream = False if use_images_api else self._get_stream_setting(request.preset)

        # Images API (包括 /v1/images/generations 和原生 DALL-E)
        if use_images_api:
            payload = {
                "model": request.preset.model,
                "prompt": request.gen_config.prompt,
                "n": 1,
            }
            
            # 尺寸映射
            size = self._map_images_api_size(request.gen_config.image_size, model)
            if size:
                payload["size"] = size
            
            # response_format: 优先 b64_json，但某些 API 可能只支持 url
            # 通过配置或自动降级处理
            payload["response_format"] = "b64_json"
            
            # DALL-E 3 特有参数
            if "dall-e-3" in model:
                payload["quality"] = "standard"
            
            return payload

        # Chat Completions API
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

        # Gemini via OpenAI 兼容层
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

        # 已经是完整端点的情况，直接返回
        if url.endswith("/images/generations"):
            return url if not is_chat else url.replace("/images/generations", "/chat/completions")
        if url.endswith("/chat/completions"):
            return url if is_chat else url.replace("/chat/completions", "/images/generations")

        # 需要补全端点
        if is_chat:
            if re.search(r"/v1(?:beta)?$", url): return f"{url}/chat/completions"
            return f"{url}/v1/chat/completions"
        else:
            if url.endswith("/v1"): return f"{url}/images/generations"
            if url.endswith("/v1/models"): return url.replace("/models", "/images/generations")
            return f"{url}/v1/images/generations"

    def _extract_image_url(self, content: Any) -> Optional[str]:
        """
        从 API 响应中提取图片 URL 或 Base64 数据。
        
        支持的响应格式：
        1. Images API 标准格式: {"data": [{"url": "..."} 或 {"b64_json": "..."}]}
        2. Images API 变体: {"data": [{"image": "base64..."}]}
        3. Chat Completions 格式: {"choices": [{"message": {"content": "..."}}]}
        4. 其他自定义格式
        """
        # JSON 对象
        if isinstance(content, dict):
            # Images API 标准格式 (OpenAI DALL-E, Flux, SDXL 等)
            if "data" in content and isinstance(content["data"], list) and content["data"]:
                item = content["data"][0]
                # 标准字段 - 注意：某些 API 会返回 url=None，所以需要检查值是否为真
                if item.get("url"):
                    return item["url"]
                if item.get("b64_json"):
                    return f"data:image/png;base64,{item['b64_json']}"
                # 某些 API 使用 "image" 字段返回 base64
                if "image" in item:
                    img_data = item["image"]
                    if isinstance(img_data, str):
                        if img_data.startswith("data:") or img_data.startswith("http"):
                            return img_data
                        # 假设是纯 base64
                        return f"data:image/png;base64,{img_data}"
                # 某些 API 使用 "base64" 字段
                if "base64" in item:
                    return f"data:image/png;base64,{item['base64']}"
                # revised_prompt 旁边可能有 url（某些 API）
                for key in ["image_url", "imageUrl", "output"]:
                    if key in item:
                        val = item[key]
                        if isinstance(val, str):
                            return val
                        if isinstance(val, dict) and "url" in val:
                            return val["url"]

            # Chat Completions 格式
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
            
            # 某些 API 直接在顶层返回 url 或 image
            if "url" in content:
                return content["url"]
            if "image" in content and isinstance(content["image"], str):
                img = content["image"]
                if img.startswith("data:") or img.startswith("http"):
                    return img
                return f"data:image/png;base64,{img}"

        # 字符串内容解析
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

    def _map_images_api_size(self, size_str: str, model: str = "") -> Optional[str]:
        """
        将插件的尺寸配置映射为 Images API 的 size 参数。
        
        不同 API 支持的尺寸不同：
        - DALL-E 2: 256x256, 512x512, 1024x1024
        - DALL-E 3: 1024x1024, 1792x1024, 1024x1792
        - Flux/SDXL 等: 通常支持更多尺寸
        
        返回 None 表示不设置 size，让 API 使用默认值（提高兼容性）
        """
        s = size_str.upper()
        model_lower = model.lower()
        
        # DALL-E 3 特殊处理
        if "dall-e-3" in model_lower:
            if "1792" in s or "HD" in s or "2K" in s:
                return "1792x1024"
            return "1024x1024"
        
        # DALL-E 2
        if "dall-e-2" in model_lower or "dall-e" in model_lower:
            if "512" in s: return "512x512"
            if "256" in s: return "256x256"
            return "1024x1024"
        
        # 其他模型（Flux, SDXL, Midjourney 等）使用通用映射
        # 尽量返回常见尺寸，提高兼容性
        if "2K" in s or "2048" in s: return "2048x2048"
        if "1K" in s or "1024" in s: return "1024x1024"
        if "768" in s: return "768x768"
        if "512" in s: return "512x512"
        
        # 默认 1024x1024，这是最广泛支持的尺寸
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