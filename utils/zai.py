import time
import json
import base64
import asyncio
import re
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any

import aiohttp
from astrbot.api import logger

class DiscordOAuthHandler:
    """基于Futureppo大佬的实现，请感谢他"""
    DISCORD_API_BASE = "https://discord.com/api/v9"

    def __init__(self, base_url: str = "https://zai.is", proxy: str | None = None):
        self.base_url = base_url
        self.proxy = proxy
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Referer': f'{base_url}/auth',
            'Origin': base_url,
        }
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)

    async def backend_login(self, discord_token: str) -> Dict[str, Any]:
        if not discord_token:
             return {'error': '无效的 Discord Token'}

        async with aiohttp.ClientSession(
            headers=self.headers, 
            cookie_jar=self.cookie_jar, 
            trust_env=True
        ) as session:
            try:
                if self.proxy:
                    logger.debug(f"[Zai] 使用配置代理: {self.proxy}")
                else:
                    logger.debug(f"[Zai] 未配置显式代理，尝试使用系统环境代理 (trust_env=True)")

                # 获取 URL
                oauth_info = await self._get_discord_authorize_url(session)
                if 'error' in oauth_info: return oauth_info

                # 授权应用
                auth_result = await self._authorize_discord_app(
                    session,
                    discord_token, 
                    oauth_info['client_id'], 
                    oauth_info['redirect_uri'], 
                    oauth_info.get('scope', 'identify email'), 
                    oauth_info.get('state', '')
                )
                if 'error' in auth_result: return auth_result

                # 回调
                return await self._handle_oauth_callback(session, auth_result['callback_url'])

            except Exception as e:
                return {'error': f'登录异常: {str(e)}'}

    async def _get_discord_authorize_url(self, session: aiohttp.ClientSession) -> Dict[str, Any]:
        try:
            url = f"{self.base_url}/oauth/discord/login"
            timeout = aiohttp.ClientTimeout(total=30)
            async with session.get(url, allow_redirects=False, proxy=self.proxy, timeout=timeout) as resp:
                if resp.status in [301, 302, 303, 307, 308]:
                    location = resp.headers.get('Location', '')
                    if 'discord.com' in location:
                        parsed = urlparse(location)
                        params = parse_qs(parsed.query)
                        return {
                            'authorize_url': location,
                            'client_id': params.get('client_id', [''])[0],
                            'redirect_uri': params.get('redirect_uri', [''])[0],
                            'scope': params.get('scope', ['identify email'])[0],
                            'state': params.get('state', [''])[0]
                        }
                return {'error': f'无法获取授权 URL, status: {resp.status}'}
        except Exception as e:
            return {'error': str(e)}

    async def _authorize_discord_app(self, session: aiohttp.ClientSession, discord_token, client_id, redirect_uri, scope, state) -> Dict[str, Any]:
        try:
            url = f"{self.DISCORD_API_BASE}/oauth2/authorize"

            super_props = base64.b64encode(json.dumps({
                "os": "Windows", "browser": "Chrome", "device": "",
                "browser_user_agent": self.headers['User-Agent'],
            }).encode()).decode()

            req_headers = {
                'Authorization': discord_token,
                'Content-Type': 'application/json',
                'X-Super-Properties': super_props,
                **self.headers 
            }

            payload = {
                'permissions': '0', 'authorize': True, 'integration_type': 0
            }
            params = {
                'client_id': client_id, 'response_type': 'code',
                'redirect_uri': redirect_uri, 'scope': scope
            }
            if state: params['state'] = state

            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            async with session.post(url, headers=req_headers, params=params, json=payload, proxy=self.proxy, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    loc = data.get('location', '')
                    if loc:
                        if loc.startswith('/'): loc = f"{self.base_url}{loc}"
                        return {'callback_url': loc}

                text = await resp.text()
                return {'error': f'授权失败: {resp.status} {text[:100]}'}
        except Exception as e:
            return {'error': str(e)}

    async def _handle_oauth_callback(self, session: aiohttp.ClientSession, callback_url: str) -> Dict[str, Any]:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with session.get(callback_url, allow_redirects=False, proxy=self.proxy, timeout=timeout) as resp:
                current_loc = resp.headers.get('Location', '')
                current_url = str(resp.url)

            for _ in range(5):
                token = self._extract_token(current_loc or current_url)
                if token: return {'token': token}

                if not current_loc:
                    break

                if current_loc.startswith('/'): 
                    current_loc = f"{self.base_url}{current_loc}"

                async with session.get(current_loc, allow_redirects=False, proxy=self.proxy, timeout=timeout) as resp:
                    current_url = str(resp.url)
                    token = self._extract_token(current_url)
                    if token: return {'token': token}

                    if resp.status not in [301, 302, 303, 307, 308]:
                        break
                    current_loc = resp.headers.get('Location', '')

            for cookie in session.cookie_jar:
                if cookie.key == 'token': return {'token': cookie.value}

            return {'error': '未能在回调中找到 Token'}
        except Exception as e:
            return {'error': str(e)}

    def _extract_token(self, s: str) -> Optional[str]:
        if not s: return None
        m = re.search(r'[#?&]token=([^&\s]+)', s)
        return m.group(1) if m else None


class ZaiTokenManager:
    _instance = None
    _cache: Dict[str, Dict[str, Any]] = {}  # {discord_token: {'zai_token': str, 'expire_at': float}}
    _lock = asyncio.Lock()

    CACHE_DURATION = 3600 * 2.8 

    @classmethod
    async def get_access_token(cls, discord_token: str, proxy: str | None = None) -> str:
        async with cls._lock:
            now = time.time()
            cached = cls._cache.get(discord_token)

            if cached:
                if now < cached['expire_at']:
                    return cached['zai_token']
                else:
                    logger.info(f"Zai Token 已过期，正在刷新 (Key: ...{discord_token[-6:]})")
            else:
                logger.info(f"首次获取 Zai Token (Key: ...{discord_token[-6:]})")

            handler = DiscordOAuthHandler(proxy=proxy)

            result = await handler.backend_login(discord_token)

            if 'error' in result:
                raise Exception(f"Zai 登录失败: {result['error']}")

            zai_token = result.get('token')
            if not zai_token:
                raise Exception("Zai 登录成功但未返回 Token")

            cls._cache[discord_token] = {
                'zai_token': zai_token,
                'expire_at': now + cls.CACHE_DURATION
            }
            logger.info(f"Zai Token 获取成功，有效期至 {time.strftime('%H:%M:%S', time.localtime(now + cls.CACHE_DURATION))}")

            return zai_token

    @classmethod
    def invalidate_cache(cls, discord_token: str):
        if discord_token in cls._cache:
            try:
                del cls._cache[discord_token]
                logger.info(f"已清除失效的 Zai Token 缓存 (Key: ...{discord_token[-6:]})")
            except KeyError:
                pass