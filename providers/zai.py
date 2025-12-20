import json
import base64
import time
import re
import asyncio
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse, parse_qs
from dataclasses import replace

import aiohttp
from astrbot.api import logger

from ..domain.model import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils.result import Result, Ok, Err
from .openai import OpenAIProvider

class _DiscordAuthFlow:
    """基于Futureppo大佬的实现，请感谢他"""
    DISCORD_API_BASE = "https://discord.com/api/v9"
    ZAI_BASE_URL = "https://zai.is"

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Referer': f'{self.ZAI_BASE_URL}/auth',
            'Origin': self.ZAI_BASE_URL,
        }

    async def login(self, discord_token: str) -> str:
        async with aiohttp.ClientSession(headers=self.headers, cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            oauth_info = await self._get_authorize_url(session)
            callback_url = await self._authorize_app(session, discord_token, oauth_info)
            return await self._handle_callback(session, callback_url)

    async def _get_authorize_url(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        url = f"{self.ZAI_BASE_URL}/oauth/discord/login"
        async with session.get(url, allow_redirects=False, proxy=self.proxy, timeout=30) as resp:
            location = resp.headers.get('Location', '')
            if 'discord.com' in location:
                parsed = urlparse(location)
                qs = parse_qs(parsed.query)
                return {
                    'client_id': qs.get('client_id', [''])[0],
                    'redirect_uri': qs.get('redirect_uri', [''])[0],
                    'scope': qs.get('scope', ['identify email'])[0],
                    'state': qs.get('state', [''])[0]
                }
            raise PluginError(APIErrorType.AUTH_FAILED, f"无法获取授权 URL, status: {resp.status}")

    async def _authorize_app(self, session: aiohttp.ClientSession, token: str, info: Dict[str, str]) -> str:
        url = f"{self.DISCORD_API_BASE}/oauth2/authorize"
        super_props = base64.b64encode(json.dumps({
            "os": "Windows", "browser": "Chrome", "device": "",
            "browser_user_agent": self.headers['User-Agent'],
        }).encode()).decode()

        headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
            'X-Super-Properties': super_props,
            **self.headers
        }

        payload = {'permissions': '0', 'authorize': True, 'integration_type': 0}
        params = {
            'client_id': info['client_id'],
            'response_type': 'code',
            'redirect_uri': info['redirect_uri'],
            'scope': info['scope']
        }
        if info['state']: params['state'] = info['state']

        async with session.post(url, headers=headers, params=params, json=payload, proxy=self.proxy, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                loc = data.get('location', '')
                if loc.startswith('/'): loc = f"{self.ZAI_BASE_URL}{loc}"
                return loc

            text = await resp.text()
            raise PluginError(APIErrorType.AUTH_FAILED, f"Discord 授权失败: {resp.status} {text[:100]}")

    async def _handle_callback(self, session: aiohttp.ClientSession, url: str) -> str:
        current_url = url
        for _ in range(5):
            if m := re.search(r'[#?&]token=([^&\s]+)', current_url):
                return m.group(1)

            async with session.get(current_url, allow_redirects=False, proxy=self.proxy, timeout=30) as resp:
                for cookie in session.cookie_jar:
                    if cookie.key == 'token': return cookie.value

                location = resp.headers.get('Location')
                if not location: break

                if location.startswith('/'): location = f"{self.ZAI_BASE_URL}{location}"
                current_url = location

        raise PluginError(APIErrorType.AUTH_FAILED, "未能在回调中获取 Token")


class ZaiProvider(OpenAIProvider):
    DEFAULT_STREAM_SETTING = True

    _token_cache: Dict[str, Dict[str, Any]] = {}
    _cache_lock = asyncio.Lock()
    CACHE_DURATION = 3600 * 2.8

    async def _get_zai_token(self, discord_token: str, proxy: Optional[str]) -> str:
        async with self._cache_lock:
            now = time.time()
            cache = self._token_cache.get(discord_token)

            if cache and now < cache['expire']:
                return cache['token']

            logger.info(f"[ZaiProvider] 正在通过 Discord 登录获取新 Token... (Key: ...{discord_token[-6:]})")

            auth = _DiscordAuthFlow(proxy)
            try:
                zai_token = await auth.login(discord_token)
            except Exception as e:
                if isinstance(e, PluginError): raise e
                raise PluginError(APIErrorType.AUTH_FAILED, f"Zai 登录失败: {e}")

            self._token_cache[discord_token] = {
                'token': zai_token,
                'expire': now + self.CACHE_DURATION
            }
            return zai_token

    def _invalidate_cache(self, discord_token: str):
        if discord_token in self._token_cache:
            del self._token_cache[discord_token]
            logger.info(f"[ZaiProvider] 已清除失效 Token 缓存 (Key: ...{discord_token[-6:]})")

    # === 钩子方法 ===

    async def _get_headers(self, request: ApiRequest) -> Dict[str, str]:
        discord_token = request.api_key
        zai_token = await self._get_zai_token(discord_token, request.proxy_url)

        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {zai_token}",
        }

    async def _build_payload(self, request: ApiRequest) -> Dict[str, Any]:
        payload = await super()._build_payload(request)

        payload["stream"] = False

        params = {}
        if request.gen_config.aspect_ratio != "default":
            params["image_aspect_ratio"] = request.gen_config.aspect_ratio
        if request.gen_config.image_size != "1K":
            params["image_resolution"] = request.gen_config.image_size

        if params:
            payload["params"] = params

        return payload

    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        result = await super().generate(request)

        if not result.is_ok() and result.error.error_type == APIErrorType.AUTH_FAILED:
            self._invalidate_cache(request.api_key)

            return Err(PluginError(
                APIErrorType.AUTH_FAILED, 
                "Zai Token 可能已失效，已清除缓存等待重试", 
                is_retryable=True
            ))

        return result

    async def get_models(self, request: ApiRequest) -> List[str]:
        discord_token = request.api_key
        try:
            zai_token = await self._get_zai_token(discord_token, request.proxy_url)
        except Exception as e:
            logger.warning(f"[ZaiProvider] 获取模型列表时登录失败: {e}")
            return []

        temp_request = replace(request, api_key=zai_token)
        return await super().get_models(temp_request)