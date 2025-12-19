import asyncio
import time
import random
from typing import List, Dict, Optional, Type

import aiohttp
from astrbot.api import logger

from ..domain.model import ApiRequest, GenResult, PluginError, APIErrorType, ApiType, ConnectionPreset, GenerationConfig
from ..utils.result import Result, Ok, Err
from . import BaseProvider

class ProviderManager:
    ERROR_CONFIG = {
        APIErrorType.AUTH_FAILED:     ("🔒️", 3600 * 24),
        APIErrorType.QUOTA_EXHAUSTED: ("💰️", 3600 * 5),
        APIErrorType.RATE_LIMIT:      ("🛡️", 300),
        APIErrorType.SERVER_ERROR:    ("⌛", 60),
        APIErrorType.UNKNOWN:         ("❌", 0),
    }

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._providers: Dict[str, BaseProvider] = {}
        self._cooldown_keys: Dict[str, float] = {}
        self._key_status_cache: Dict[str, str] = {}
        self._key_lock = asyncio.Lock()
        self._key_cursor = 0

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
                    self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _get_provider_instance(self, api_type: ApiType) -> BaseProvider:
        s_type = api_type.value
        session = await self.get_session()

        if s_type in self._providers:
            provider = self._providers[s_type]
            if not provider.session.closed:
                return provider

        provider_cls: Optional[Type[BaseProvider]] = None

        if s_type == ApiType.OPENAI:
            from .openai import OpenAIProvider
            provider_cls = OpenAIProvider
        elif s_type == ApiType.GOOGLE:
            from .google import GoogleProvider
            provider_cls = GoogleProvider
        elif s_type == ApiType.ZAI:
            from .zai import ZaiProvider
            provider_cls = ZaiProvider
        else:
            raise ValueError(f"不支持的 API 类型: {s_type}")

        instance = provider_cls(session)
        self._providers[s_type] = instance
        return instance

    async def get_models(self, request: ApiRequest) -> List[str]:
        if not request.api_key and request.preset.api_keys:
            try:
                valid_key = await self._get_valid_key(request.preset)
                request.api_key = valid_key
            except Exception as e:
                logger.warning(f"[ProviderManager] 获取模型列表时选 Key 失败: {e}")
                pass

        provider = await self._get_provider_instance(request.preset.api_type)

        return await provider.get_models(request)

    async def _get_valid_key(self, preset: ConnectionPreset) -> str:
        all_keys = preset.api_keys
        if not all_keys:
            raise ValueError("未配置 API Key")

        async with self._key_lock:
            now = time.time()

            expired = [k for k, t in self._cooldown_keys.items() if t <= now]
            for k in expired:
                del self._cooldown_keys[k]

            if self._key_cursor >= len(all_keys):
                self._key_cursor = 0

            chosen_key = None

            for _ in range(len(all_keys)):
                k = all_keys[self._key_cursor]
                self._key_cursor = (self._key_cursor + 1) % len(all_keys)

                if k not in self._cooldown_keys:
                    chosen_key = k
                    break

            if chosen_key:
                return chosen_key

            wait_times = [t - now for k, t in self._cooldown_keys.items() if k in all_keys]
            min_wait = min(wait_times) if wait_times else 60

            raise PluginError(
                APIErrorType.QUOTA_EXHAUSTED,
                f"所有 Key 均在冷却中，请等待约 {int(min_wait)} 秒。",
            )

    def _infer_error_type(self, e: Exception) -> APIErrorType:
        if isinstance(e, PluginError):
            return e.error_type

        if isinstance(e, asyncio.TimeoutError):
            return APIErrorType.SERVER_ERROR

        msg = str(e).lower()
        if "401" in msg or "auth" in msg or "invalid" in msg:
            return APIErrorType.AUTH_FAILED
        if "429" in msg or "quota" in msg or "billing" in msg or "402" in msg:
            if "quota" in msg or "billing" in msg or "402" in msg:
                return APIErrorType.QUOTA_EXHAUSTED
            return APIErrorType.RATE_LIMIT
        if "connect" in msg or "timeout" in msg:
            return APIErrorType.SERVER_ERROR

        return APIErrorType.UNKNOWN

    def _mark_key_cooldown(self, key: str, error_type: APIErrorType):
        config = self.ERROR_CONFIG.get(error_type)
        if not config:
            return

        icon, duration = config

        if duration > 0:
            expire = time.time() + duration
            self._cooldown_keys[key] = expire
            self._key_status_cache[key] = icon

            masked = key[:4] + "..." + key[-4:] if len(key) > 8 else key
            logger.warning(f"[ProviderManager] Key {masked} 进入冷却池 ({duration}s). 状态: {icon} 原因: {error_type.name}")

    def _mark_key_success(self, key: str):
        if key in self._key_status_cache:
            self._key_status_cache.pop(key)
        if key in self._cooldown_keys:
            del self._cooldown_keys[key]

    def get_cached_key_status(self, key: str) -> Optional[str]:
        return self._key_status_cache.get(key)

    async def test_key_availability(self, preset: ConnectionPreset, key: str, proxy_url: Optional[str] = None) -> str:
        if key in self._cooldown_keys:
            expiry = self._cooldown_keys[key]
            if time.time() < expiry:
                cached_icon = self._key_status_cache.get(key, "❌")
                return f"{cached_icon} (冷却中)"

        status_icon = "❌ (错误)"
        try:
            dummy_req = ApiRequest(
                api_key=key,
                preset=preset,
                gen_config=GenerationConfig(prompt="test"),
                debug_mode=False,
                proxy_url=proxy_url
            )

            provider = await self._get_provider_instance(preset.api_type)
            models = await asyncio.wait_for(provider.get_models(dummy_req), timeout=10)

            if models:
                self._mark_key_success(key)
                return "✅"
            else:
                return "✅ (无模型)"

        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                self._mark_key_cooldown(key, APIErrorType.SERVER_ERROR)
                return "⌛ (超时)"

            error_type = self._infer_error_type(e)

            if error_type != APIErrorType.UNKNOWN:
                self._mark_key_cooldown(key, error_type)
                icon, _ = self.ERROR_CONFIG.get(error_type, ("❌", 0))
                return icon

            return "❌ (错误)"

    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        if request.debug_mode:
            debug_data = {
                "api_type": request.preset.api_type.value,
                "model": request.preset.model,
                "prompt": request.gen_config.prompt,
                "image_count": len(request.image_bytes_list),
                "upscale_instruction": request.gen_config.upscale_instruction,
                "preset_name": request.preset.name,
                "stream": request.preset.stream,
                "timeout": request.gen_config.timeout
            }
            return Err(PluginError(APIErrorType.DEBUG_INFO, "调试模式阻断", raw_data=debug_data))

        max_retries = max(1, min(len(request.preset.api_keys), 5))
        last_error: Optional[PluginError] = None

        for attempt in range(max_retries):
            try:
                current_key = await self._get_valid_key(request.preset)
                request.api_key = current_key
            except Exception as e:
                if isinstance(e, PluginError):
                    return Err(e)
                return Err(PluginError(APIErrorType.UNKNOWN, str(e)))

            try:
                provider = await self._get_provider_instance(request.preset.api_type)
            except Exception as e:
                return Err(PluginError(APIErrorType.INVALID_ARGUMENT, f"Provider初始化失败: {e}"))

            try:
                result = await provider.generate(request)
                if result.is_ok():
                    self._mark_key_success(current_key)
                    return result
                error = result.error
            except Exception as e:
                error, _ = provider.convert_exception(e)

            last_error = error
            is_retryable = (
                error.error_type not in [
                    APIErrorType.INVALID_ARGUMENT, 
                    APIErrorType.SAFETY_BLOCK, 
                    APIErrorType.NOT_FOUND,
                    APIErrorType.DEBUG_INFO
                ]
            )

            self._mark_key_cooldown(current_key, error.error_type)

            if not is_retryable:
                logger.warning(f"[ProviderManager] 遇到致命错误，停止重试: {error}")
                return Err(error)

            logger.warning(
                f"[ProviderManager] 生成失败 (尝试 {attempt+1}/{max_retries}) "
                f"- Type: {error.error_type.name} - Msg: {error.message[:50]}"
            )

            # 指数退避
            if error.error_type in [APIErrorType.RATE_LIMIT, APIErrorType.SERVER_ERROR]:
                delay = min(1.5 * (2 ** attempt), 8.0)
                jitter = random.uniform(0, 1)
                await asyncio.sleep(delay + jitter)
            else:
                await asyncio.sleep(0.5)

        return Err(last_error or PluginError(APIErrorType.UNKNOWN, "所有重试均失败"))

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("[ProviderManager] Session 已关闭")