import time
import json
import base64
import asyncio
import requests
from astrbot.api import logger
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any

class DiscordOAuthHandler:
    """基于Futureppo大佬的实现，请感谢他"""
    DISCORD_API_BASE = "https://discord.com/api/v9"

    def __init__(self, base_url: str = "https://zai.is"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Referer': f'{base_url}/auth',
            'Origin': base_url,
        })

    def backend_login(self, discord_token: str) -> Dict[str, Any]:
        if not discord_token:
             return {'error': '无效的 Discord Token'}

        try:
            # 获取URL
            oauth_info = self._get_discord_authorize_url()
            if 'error' in oauth_info: return oauth_info

            # 授权应用
            auth_result = self._authorize_discord_app(
                discord_token, 
                oauth_info['client_id'], 
                oauth_info['redirect_uri'], 
                oauth_info.get('scope', 'identify email'), 
                oauth_info.get('state', '')
            )
            if 'error' in auth_result: return auth_result

            return self._handle_oauth_callback(auth_result['callback_url'])

        except Exception as e:
            return {'error': f'登录异常: {str(e)}'}

    def _get_discord_authorize_url(self) -> Dict[str, Any]:
        try:
            url = f"{self.base_url}/oauth/discord/login"
            resp = self.session.get(url, allow_redirects=False)
            if resp.status_code in [301, 302, 303, 307, 308]:
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
            return {'error': f'无法获取授权 URL, status: {resp.status_code}'}
        except Exception as e:
            return {'error': str(e)}

    def _authorize_discord_app(self, discord_token, client_id, redirect_uri, scope, state) -> Dict[str, Any]:
        try:
            url = f"{self.DISCORD_API_BASE}/oauth2/authorize"
            super_props = base64.b64encode(json.dumps({
                "os": "Windows", "browser": "Chrome", "device": "",
                "browser_user_agent": self.session.headers['User-Agent'],
            }).encode()).decode()

            headers = {
                'Authorization': discord_token,
                'Content-Type': 'application/json',
                'X-Super-Properties': super_props,
            }
            payload = {
                'permissions': '0', 'authorize': True, 'integration_type': 0
            }
            params = {
                'client_id': client_id, 'response_type': 'code',
                'redirect_uri': redirect_uri, 'scope': scope
            }
            if state: params['state'] = state

            resp = self.session.post(url, headers=headers, params=params, json=payload)
            if resp.status_code == 200:
                loc = resp.json().get('location', '')
                if loc:
                    if loc.startswith('/'): loc = f"{self.base_url}{loc}"
                    return {'callback_url': loc}
            return {'error': f'授权失败: {resp.status_code} {resp.text[:100]}'}
        except Exception as e:
            return {'error': str(e)}

    def _handle_oauth_callback(self, callback_url: str) -> Dict[str, Any]:
        try:
            resp = self.session.get(callback_url, allow_redirects=False)
            for _ in range(5):
                if resp.status_code not in [301, 302, 303, 307, 308]: break
                loc = resp.headers.get('Location', '')
                token = self._extract_token(loc)
                if token: return {'token': token}
                if loc.startswith('/'): loc = f"{self.base_url}{loc}"
                resp = self.session.get(loc, allow_redirects=False)

            token = self._extract_token(resp.url)
            if token: return {'token': token}

            for cookie in self.session.cookies:
                if cookie.name == 'token': return {'token': cookie.value}

            return {'error': '未能在回调中找到 Token'}
        except Exception as e:
            return {'error': str(e)}

    def _extract_token(self, s: str) -> Optional[str]:
        if not s: return None
        import re
        m = re.search(r'[#?&]token=([^&\s]+)', s)
        return m.group(1) if m else None


class ZaiTokenManager:
    _instance = None
    _cache: Dict[str, Dict[str, Any]] = {}  # {discord_token: {'zai_token': str, 'expire_at': float}}
    _lock = asyncio.Lock()

    CACHE_DURATION = 3600 * 2.8 

    @classmethod
    async def get_access_token(cls, discord_token: str) -> str:
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

            handler = DiscordOAuthHandler()
            result = await asyncio.to_thread(handler.backend_login, discord_token)

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