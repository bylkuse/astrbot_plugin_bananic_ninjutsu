import json
import base64
import re
import aiohttp
from typing import Any, Dict, List, Optional, Tuple, Union

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
            api_base = request.preset.api_base or ""
            has_images = bool(request.image_bytes_list)
            
            # 判断是否使用 Images API
            is_images_api = self._is_images_api_endpoint(api_base)
            
            if is_images_api:
                # Images API 路径：根据有无图片路由到 generations 或 edits
                return await self._generate_via_images_api(request, has_images)
            else:
                # Chat Completions API 路径
                return await self._generate_via_chat_api(request)

        except Exception as e:
            error, _ = self.convert_exception(e)
            return Err(error)

    async def _generate_via_chat_api(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        """通过 Chat Completions API 生成图片"""
        headers = await self._get_headers(request)
        payload = self._build_chat_payload(request)
        
        url = self._resolve_endpoint(request.preset.api_base, endpoint_type="chat")
        use_stream = payload.get("stream", False)
        kwargs = self._get_request_kwargs(request, stream=use_stream)
        
        result = await self._do_request(url, payload, headers, kwargs, use_stream)
        
        if result.is_err():
            return result
            
        return await self._process_response(result.unwrap(), request, use_stream)

    async def _generate_via_images_api(self, request: ApiRequest, has_images: bool) -> Result[GenResult, PluginError]:
        """通过 Images API 生成图片（自动路由 generations/edits）"""
        
        if has_images:
            # 图生图：使用 /v1/images/edits (multipart/form-data)
            return await self._generate_via_images_edits(request)
        else:
            # 文生图：使用 /v1/images/generations (JSON)
            return await self._generate_via_images_generations(request)

    async def _generate_via_images_generations(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        """通过 /v1/images/generations 生成图片（文生图）"""
        headers = await self._get_headers(request)
        payload = self._build_images_generations_payload(request)
        
        url = self._resolve_endpoint(request.preset.api_base, endpoint_type="images_generations")
        kwargs = self._get_request_kwargs(request, stream=False)
        
        result = await self._do_request(url, payload, headers, kwargs, use_stream=False)
        
        # response_format fallback 机制
        if result.is_err() and payload.get("response_format") == "b64_json":
            error = result.unwrap_err()
            if self._is_response_format_error(error):
                logger.info("[OpenAIProvider] b64_json 不支持，尝试使用 url 格式")
                payload["response_format"] = "url"
                result = await self._do_request(url, payload, headers, kwargs, use_stream=False)
        
        if result.is_err():
            return result
            
        return await self._process_response(result.unwrap(), request, use_stream=False)

    async def _generate_via_images_edits(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        """通过 /v1/images/edits 生成图片（图生图，multipart/form-data）"""
        url = self._resolve_endpoint(request.preset.api_base, endpoint_type="images_edits")
        
        # 构建 multipart/form-data
        form_data = aiohttp.FormData()
        form_data.add_field("prompt", request.gen_config.prompt)
        form_data.add_field("model", request.preset.model)
        form_data.add_field("n", "1")
        form_data.add_field("response_format", "b64_json")
        
        # 尺寸
        size = self._map_images_api_size(request.gen_config.image_size, request.preset.model)
        if size:
            form_data.add_field("size", size)
        
        # 添加图片（取第一张）
        img_bytes = None
        mime_type = "image/png"
        if request.image_bytes_list:
            img_bytes = request.image_bytes_list[0]
            mime_type = ImageUtils.get_mime_type(img_bytes)
            ext = mime_type.split("/")[-1] if "/" in mime_type else "png"
            form_data.add_field(
                "image",
                img_bytes,
                filename=f"image.{ext}",
                content_type=mime_type
            )
        
        headers = {
            "Authorization": f"Bearer {request.api_key}",
            "Accept-Encoding": "gzip, deflate",
        }
        
        kwargs = self._get_request_kwargs(request, stream=False)
        
        result = await self._do_multipart_request(url, form_data, headers, kwargs)
        
        # 如果 /v1/images/edits 返回 404，尝试 fallback 到 /v1/images/generations 带 image 参数
        if result.is_err():
            error = result.unwrap_err()
            if error.status_code == 404:
                logger.info("[OpenAIProvider] /v1/images/edits 返回 404，尝试 fallback 到 /v1/images/generations 带 image 参数")
                return await self._generate_via_images_generations_with_image(request)
            
            # response_format fallback
            if self._is_response_format_error(error):
                logger.info("[OpenAIProvider] edits b64_json 不支持，尝试使用 url 格式")
                # 重新构建 form_data
                form_data = aiohttp.FormData()
                form_data.add_field("prompt", request.gen_config.prompt)
                form_data.add_field("model", request.preset.model)
                form_data.add_field("n", "1")
                form_data.add_field("response_format", "url")
                if size:
                    form_data.add_field("size", size)
                if img_bytes:
                    ext = mime_type.split("/")[-1] if "/" in mime_type else "png"
                    form_data.add_field("image", img_bytes, filename=f"image.{ext}", content_type=mime_type)
                result = await self._do_multipart_request(url, form_data, headers, kwargs)
        
        if result.is_err():
            return result
            
        return await self._process_response(result.unwrap(), request, use_stream=False)

    async def _generate_via_images_generations_with_image(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        """
        通过 /v1/images/generations 带 image 参数进行图生图。
        
        某些中转站扩展了 generations 接口，支持传入 image 参数进行图生图，
        作为 /v1/images/edits 不可用时的 fallback。
        """
        headers = await self._get_headers(request)
        payload = self._build_images_generations_payload(request)
        
        # 添加 image 参数（Base64 格式）
        if request.image_bytes_list:
            img_bytes = request.image_bytes_list[0]
            mime_type = ImageUtils.get_mime_type(img_bytes)
            b64_str = base64.b64encode(img_bytes).decode("utf-8")
            payload["image"] = f"data:{mime_type};base64,{b64_str}"
        
        url = self._resolve_endpoint(request.preset.api_base, endpoint_type="images_generations")
        kwargs = self._get_request_kwargs(request, stream=False)
        
        result = await self._do_request(url, payload, headers, kwargs, use_stream=False)
        
        # response_format fallback
        if result.is_err() and payload.get("response_format") == "b64_json":
            error = result.unwrap_err()
            if self._is_response_format_error(error):
                logger.info("[OpenAIProvider] generations+image b64_json 不支持，尝试使用 url 格式")
                payload["response_format"] = "url"
                result = await self._do_request(url, payload, headers, kwargs, use_stream=False)
        
        if result.is_err():
            return result
            
        return await self._process_response(result.unwrap(), request, use_stream=False)

    async def _process_response(self, response_content: Any, request: ApiRequest, use_stream: bool) -> Result[GenResult, PluginError]:
        """处理 API 响应，提取图片"""
        image_url = self._extract_image_url(response_content)

        if not image_url:
            preview = str(response_content)[:200]
            hint = " (流式模式可能丢失了Base64图片，请尝试关闭流式)" if use_stream else ""
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

    async def _do_request(
        self, 
        url: str, 
        payload: Dict[str, Any], 
        headers: Dict[str, str], 
        kwargs: Dict[str, Any],
        use_stream: bool
    ) -> Result[Any, PluginError]:
        """执行 JSON 请求并返回响应内容"""
        try:
            async with self.session.post(url, json=payload, headers=headers, **kwargs) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    msg = f"HTTP {resp.status}"
                    try:
                        err_json = json.loads(text)
                        if "error" in err_json:
                            error_field = err_json["error"]
                            # error 可能是字典或字符串
                            if isinstance(error_field, dict):
                                detail = error_field.get("message", str(error_field))
                            else:
                                detail = str(error_field)
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

    async def _do_multipart_request(
        self,
        url: str,
        form_data: aiohttp.FormData,
        headers: Dict[str, str],
        kwargs: Dict[str, Any]
    ) -> Result[Any, PluginError]:
        """执行 multipart/form-data 请求"""
        try:
            async with self.session.post(url, data=form_data, headers=headers, **kwargs) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    msg = f"HTTP {resp.status}"
                    try:
                        err_json = json.loads(text)
                        if "error" in err_json:
                            error_field = err_json["error"]
                            # error 可能是字典或字符串
                            if isinstance(error_field, dict):
                                detail = error_field.get("message", str(error_field))
                            else:
                                detail = str(error_field)
                            msg += f": {detail}"
                        else:
                            msg += f" - {text[:200]}"
                    except:
                        msg += f" - {text[:200]}"

                    return Err(PluginError(APIErrorType.SERVER_ERROR, msg, resp.status))

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
        """判断是否为 Images API 接入点 (/v1/images/*)"""
        if not api_base:
            return False
        url = api_base.lower().strip().rstrip("/")
        # 匹配 /images/generations, /images/edits, /images/variations 或 /images
        return "/images/" in url or url.endswith("/images")

    def _build_chat_payload(self, request: ApiRequest) -> Dict[str, Any]:
        """构建 Chat Completions API 的请求体"""
        model = request.preset.model.lower()
        use_stream = self._get_stream_setting(request.preset)
        
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

    def _build_images_generations_payload(self, request: ApiRequest) -> Dict[str, Any]:
        """构建 /v1/images/generations 的请求体（文生图）"""
        model = request.preset.model.lower()
        
        payload = {
            "model": request.preset.model,
            "prompt": request.gen_config.prompt,
            "n": 1,
            "response_format": "b64_json"
        }
        
        # 尺寸映射
        size = self._map_images_api_size(request.gen_config.image_size, model)
        if size:
            payload["size"] = size
        
        # DALL-E 3 特有参数
        if "dall-e-3" in model:
            payload["quality"] = "standard"
        
        return payload

    def _resolve_endpoint(self, base_url: str, endpoint_type: str = "chat") -> str:
        """
        解析并返回正确的 API 端点。
        
        Args:
            base_url: 用户配置的 API 基础地址
            endpoint_type: "chat" | "images_generations" | "images_edits"
        """
        url = (base_url or "").strip().rstrip("/")
        
        # 默认端点
        defaults = {
            "chat": "https://api.openai.com/v1/chat/completions",
            "images_generations": "https://api.openai.com/v1/images/generations",
            "images_edits": "https://api.openai.com/v1/images/edits"
        }
        
        if not url:
            return defaults.get(endpoint_type, defaults["chat"])

        # 提取基础 URL（去掉具体端点路径）
        base = url
        for suffix in ["/chat/completions", "/images/generations", "/images/edits", "/images/variations"]:
            if url.lower().endswith(suffix):
                base = url[:-len(suffix)]
                break
        
        # 确保 base 以 /v1 结尾
        if not re.search(r"/v1(?:beta)?$", base.lower()):
            if not base.endswith("/v1"):
                base = f"{base}/v1"
        
        # 根据类型返回完整端点
        endpoints = {
            "chat": f"{base}/chat/completions",
            "images_generations": f"{base}/images/generations",
            "images_edits": f"{base}/images/edits"
        }
        
        return endpoints.get(endpoint_type, endpoints["chat"])

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
            
            # 某些 API 直接在顶层返回 url 或 image（需要确保 content 是字典）
            if isinstance(content, dict):
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
        chat_url = self._resolve_endpoint(request.preset.api_base, endpoint_type="chat")

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