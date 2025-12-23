import json
import base64
import time
import re
import asyncio
import aiohttp
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse, parse_qs
from dataclasses import replace
try:
    from curl_cffi.requests import AsyncSession
except ImportError:
    raise ImportError("请安装 curl_cffi 以通过 Discord 验证: pip install curl_cffi")

from astrbot.api import logger

from ..domain import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils import Result, Ok, Err
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
        # [Mod] 使用 curl_cffi 的 AsyncSession，并指定 impersonate="chrome120"
        # 这会让 TLS 指纹看起来像真正的 Chrome 浏览器
        async with AsyncSession(impersonate="chrome120", headers=self.headers) as session:
            # curl_cffi 自动管理 cookie，无需显式声明 CookieJar
            oauth_info = await self._get_authorize_url(session)
            callback_url = await self._authorize_app(session, discord_token, oauth_info)
            return await self._handle_callback(session, callback_url)

    async def _get_authorize_url(self, session: AsyncSession) -> Dict[str, str]:
        url = f"{self.ZAI_BASE_URL}/oauth/discord/login"
        # [Mod] curl_cffi 参数略有不同，ssl=False 不需要，timeout 是 int
        resp = await session.get(url, allow_redirects=False, proxy=self.proxy, timeout=30)
        
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
        raise PluginError(APIErrorType.AUTH_FAILED, f"无法获取授权 URL, status: {resp.status_code}")

    async def _authorize_app(self, session: AsyncSession, token: str, info: Dict[str, str]) -> str:
        url = f"{self.DISCORD_API_BASE}/oauth2/authorize"
        
        REAL_CLIENT_BUILD_NUMBER = 482285
        super_props_dict = {
            "os": "Windows",
            "browser": "Chrome",
            "device": "",
            "system_locale": "zh-CN",
            "browser_user_agent": self.headers['User-Agent'],
            "browser_version": "120.0.0.0",
            "os_version": "10",
            "referrer": "",
            "referring_domain": "",
            "referrer_current": "",
            "referring_domain_current": "",
            "release_channel": "stable",
            "client_build_number": REAL_CLIENT_BUILD_NUMBER,
            "client_event_source": None
        }
        super_props = base64.b64encode(json.dumps(super_props_dict).encode()).decode()

        headers = self.headers.copy()
        headers.update({
            'Authorization': token,
            'Content-Type': 'application/json',
            'X-Super-Properties': super_props,
            'User-Agent': self.headers['User-Agent']
        })

        payload = {'permissions': '0', 'authorize': True, 'integration_type': 0}
        params = {
            'client_id': info['client_id'],
            'response_type': 'code',
            'redirect_uri': info['redirect_uri'],
            'scope': info['scope']
        }
        if info['state']: params['state'] = info['state']

        # [Mod] json参数在 curl_cffi 中也是 json
        resp = await session.post(url, headers=headers, params=params, json=payload, proxy=self.proxy, timeout=30)
        
        if resp.status_code == 200:
            data = resp.json() # curl_cffi 是方法不是协程
            loc = data.get('location', '')
            if loc.startswith('/'): loc = f"{self.ZAI_BASE_URL}{loc}"
            return loc

        text = resp.text
        if "captcha" in text or "turnstile" in text:
            raise PluginError(APIErrorType.AUTH_FAILED, "Discord 触发了验证码，请更换 IP 或稍后重试")
             
        raise PluginError(APIErrorType.AUTH_FAILED, f"Discord 授权失败: {resp.status_code} {text[:100]}")

    async def _handle_callback(self, session: AsyncSession, url: str) -> str:
        current_url = url
        for _ in range(5):
            if m := re.search(r'[#?&]token=([^&\s]+)', current_url):
                return m.group(1)

            resp = await session.get(current_url, allow_redirects=False, proxy=self.proxy, timeout=30)
            
            # curl_cffi cookie 获取方式
            for name, value in session.cookies.items():
                if name == 'token': return value

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
    
    ZAI_API_ENDPOINT = "https://zai.is/api/chat/completions"

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
            "Accept-Encoding": "gzip, deflate",
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
        # 1. 准备数据
        try:
            headers = await self._get_headers(request)
            payload = await self._build_payload(request)
        except Exception as e:
            return Err(PluginError(APIErrorType.AUTH_FAILED, f"准备请求数据失败: {e}"))

        # Zai 强制不流式，payload在 _build_payload 中已被设置为 stream=False
        
        # 2. 发起请求 (使用 curl_cffi 绕过 TLS 检测)
        response_content = None
        try:
            # impersonate="chrome120" 是绕过检测的关键
            async with AsyncSession(impersonate="chrome120", headers=headers) as session:
                resp = await session.post(
                    self.ZAI_API_ENDPOINT, 
                    json=payload, 
                    proxy=request.proxy_url, 
                    timeout=request.gen_config.timeout
                )
                
                if resp.status_code != 200:
                    text = resp.text
                    # 尝试解析错误信息
                    try:
                        err_json = json.loads(text)
                        msg = err_json.get("detail", text)
                        # 如果是 validation_failed，通常意味着指纹不对
                        if "validation_failed" in str(text):
                            msg += " (指纹验证失败，可能是 curl_cffi 版本过低或 IP 脏了)"
                    except:
                        msg = text[:200]
                        
                    # 403 可能是 Auth 失败，也可能是风控
                    err_type = APIErrorType.AUTH_FAILED if resp.status_code in [401, 403] else APIErrorType.SERVER_ERROR
                    raise PluginError(err_type, f"HTTP {resp.status_code}: {msg}", resp.status_code)

                response_content = resp.json()

        except Exception as e:
            # 异常处理
            if isinstance(e, PluginError):
                # 如果是 Auth 失败，尝试清除缓存
                if e.error_type == APIErrorType.AUTH_FAILED:
                    self._invalidate_cache(request.api_key)
                return Err(e)
            return Err(PluginError(APIErrorType.SERVER_ERROR, f"Zai 请求异常: {e}"))

        # 3. 提取图片
        # 使用父类 OpenAIProvider 的提取逻辑
        image_url = self._extract_image_url(response_content)

        if not image_url:
            return Err(PluginError(
                APIErrorType.SERVER_ERROR, 
                f"API返回数据结构异常，无法提取图片。预览: {str(response_content)[:200]}"
            ))

        # 4. 下载图片
        # 图片 CDN 通常不校验 TLS 指纹，使用父类的 aiohttp 下载即可
        # 如果下载也报 403，则需要把下载也改成 curl_cffi
        try:
            image_bytes = await self._download_or_decode(image_url, request.proxy_url)
            
            return Ok(GenResult(
                images=[image_bytes],
                model_name=request.preset.model,
                finish_reason="success",
                raw_response=response_content
            ))
        except Exception as e:
            return Err(PluginError(APIErrorType.SERVER_ERROR, f"图片下载失败: {e}"))

    async def get_models(self, request: ApiRequest) -> List[str]:
        discord_token = request.api_key
        try:
            zai_token = await self._get_zai_token(discord_token, request.proxy_url)
        except Exception as e:
            logger.warning(f"[ZaiProvider] 获取模型列表时登录失败: {e}")
            return []

        temp_request = replace(request, api_key=zai_token)
        return await super().get_models(temp_request)