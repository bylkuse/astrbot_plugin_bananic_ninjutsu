import asyncio
import base64
import json
from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, Dict, Any, Union

import aiohttp
from astrbot.api import logger

from ..domain.model import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils.result import Result, Err

class BaseProvider(ABC):
    # 错误映射
    _ERROR_MAPPING = [
        ({400}, {"invalid_argument", "bad request", "parse error"}, APIErrorType.INVALID_ARGUMENT, False),
        ({401, 403}, {"unauthenticated", "permission", "access denied", "invalid api key", "signature"}, APIErrorType.AUTH_FAILED, True),
        ({402}, {"billing", "payment", "quota"}, APIErrorType.QUOTA_EXHAUSTED, True),
        ({404}, {"not found", "404"}, APIErrorType.NOT_FOUND, False),
        ({429}, {"resource_exhausted", "too many requests", "rate limit"}, APIErrorType.RATE_LIMIT, True),
        (set(range(500, 600)), {"internal error", "server error", "timeout", "connect", "ssl", "503", "502", "504"}, APIErrorType.SERVER_ERROR, True),
        (set(), {"safety", "blocked", "content filter", "prohibited"}, APIErrorType.SAFETY_BLOCK, False),
    ]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    @abstractmethod
    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        pass

    @abstractmethod
    async def get_models(self, request: ApiRequest) -> List[str]:
        pass

    async def _post_json(
        self, 
        url: str, 
        payload: Dict, 
        headers: Dict, 
        request: ApiRequest,
        ssl: bool = True
    ) -> Any:

        timeout = aiohttp.ClientTimeout(total=request.gen_config.timeout, connect=20)

        try:
            async with self.session.post(
                url, json=payload, headers=headers, 
                proxy=request.proxy_url, timeout=timeout, ssl=ssl
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    try:
                        err = json.loads(text)
                        msg = err.get("error", {}).get("message", text)
                    except:
                        msg = text[:200]
                    raise PluginError(APIErrorType.SERVER_ERROR, f"HTTP {resp.status}: {msg}", resp.status)

                return await resp.json()
        except Exception as e:
            raise e

    async def _post_stream(
        self, 
        url: str, 
        payload: Dict, 
        headers: Dict, 
        request: ApiRequest,
        ssl: bool = True
    ) -> aiohttp.ClientResponse:

        timeout = aiohttp.ClientTimeout(
            total=None, 
            connect=10, 
            sock_read=request.gen_config.timeout
        )

        return await self.session.post(
            url, json=payload, headers=headers, 
            proxy=request.proxy_url, timeout=timeout, ssl=ssl
        )

    def _get_request_kwargs(self, request: ApiRequest, stream: bool = False) -> Dict[str, Any]:
        if stream:
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=request.gen_config.timeout)
        else:
            timeout = aiohttp.ClientTimeout(total=request.gen_config.timeout, connect=10)

        return {
            "proxy": request.proxy_url,
            "timeout": timeout
        }

    async def _download_or_decode(self, url_or_b64: str, proxy: Optional[str], timeout: int = 60) -> bytes:
        if url_or_b64.startswith("data:") or ";base64," in url_or_b64:
            if ";base64," in url_or_b64:
                _, b64_data = url_or_b64.split(";base64,", 1)
            else:
                b64_data = url_or_b64

            # 清理
            cleaned = b64_data.strip().translate(str.maketrans({"\n": "", "\r": "", " ": ""}))
            cleaned = cleaned.replace("-", "+").replace("_", "/")
            return base64.b64decode(cleaned + '=' * (-len(cleaned) % 4))

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with self.session.get(url_or_b64, headers=headers, proxy=proxy, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.read()
                raise PluginError(APIErrorType.SERVER_ERROR, f"下载图片失败 HTTP {resp.status}")
        except Exception as e:
             raise PluginError(APIErrorType.SERVER_ERROR, f"下载图片异常: {str(e)}")

    def convert_exception(self, e: Exception) -> Tuple[PluginError, bool]:
        if isinstance(e, PluginError):
            is_retryable = e.error_type not in [
                APIErrorType.INVALID_ARGUMENT, 
                APIErrorType.SAFETY_BLOCK, 
                APIErrorType.NOT_FOUND,
                APIErrorType.DEBUG_INFO
            ]
            return e, is_retryable

        error_str = str(e)[:1000].lower()
        status_code = getattr(e, "status", None) or getattr(e, "status_code", None)

        if isinstance(e, asyncio.TimeoutError):
            return PluginError(APIErrorType.SERVER_ERROR, f"请求超时: {e}", 408), True
        if isinstance(e, aiohttp.ClientError):
            return PluginError(APIErrorType.SERVER_ERROR, f"网络连接错误: {e}"), True

        for codes, keywords, error_type, should_retry in self._ERROR_MAPPING:
            code_match = status_code in codes if status_code else False
            keyword_match = any(k in error_str for k in keywords)

            if code_match or keyword_match:
                return PluginError(error_type, str(e), status_code), should_retry

        return PluginError(APIErrorType.UNKNOWN, f"未知错误: {str(e)}", status_code), False