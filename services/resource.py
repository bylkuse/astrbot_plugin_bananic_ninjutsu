import asyncio
import hashlib
import time
import urllib.parse
import aiohttp
from pathlib import Path
from typing import List, Optional, Any

from astrbot.api import logger

from ..utils import ImageUtils

class ResourceService:
    def __init__(self, data_dir: Path, session: aiohttp.ClientSession):
        self.cache_dir = data_dir / "cache"
        self.session = session
        self._ensure_cache_dir()

    def _ensure_cache_dir(self):
        if not self.cache_dir.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_smart_headers(self, url: str) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}"

            netloc = parsed.netloc or ""
            if "qpic.cn" in netloc or "qlogo.cn" in netloc:
                headers["Referer"] = "https://qun.qq.com"
            if "nt.qq.com" in netloc:
                headers["Referer"] = "https://qun.qq.com"
                headers["Origin"] = "https://qun.qq.com"
        except Exception:
            pass
        return headers

    async def get_images_from_adapter(
        self, 
        adapter: Any, 
        max_count: int = 5, 
        proxy: Optional[str] = None
    ) -> List[bytes]:

        raw_sources = adapter.get_image_sources()

        tasks = []

        for src in raw_sources:
            if isinstance(src, str):
                tasks.append(self.load_and_process(src, adapter, proxy))

        if not tasks:
            avatar_url = adapter.get_sender_avatar_url()
            if avatar_url:
                tasks.append(self.load_and_process(avatar_url, adapter, proxy, max_size=1024))

        if not tasks:
            return []

        # 并发
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_images = []
        for res in results:
            if isinstance(res, Exception): continue
            if res: valid_images.append(res)
            if len(valid_images) >= max_count: break
        return valid_images

    async def load_and_process(
        self,
        source: str,
        adapter: Optional[Any] = None,
        proxy: Optional[str] = None,
        max_size: int = 2048
    ) -> Optional[bytes]:

        raw_bytes = None
        source = source.strip()

        # Base64
        if source.startswith(("base64://", "data:image")):
            raw_bytes = ImageUtils.decode_base64(source)

        # URL
        elif source.startswith(("http://", "https://")):
            raw_bytes = await self._download_with_cache(source, proxy)
            if not raw_bytes and adapter:
                raw_bytes = await adapter.fetch_onebot_image(source)

        # 本地文件
        elif Path(source).is_file():
            raw_bytes = await asyncio.to_thread(Path(source).read_bytes)

        # 字符串
        elif adapter:
            raw_bytes = await adapter.fetch_onebot_image(source)

        if not raw_bytes:
            return None

        return await asyncio.to_thread(
            ImageUtils.standardize_image, 
            raw_bytes, 
            max_size=max_size,
            ensure_white_bg=True
        )

    async def _download_with_cache(self, url: str, proxy: Optional[str]) -> Optional[bytes]:
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_file = self.cache_dir / cache_key

        # 命中缓存
        if cache_file.exists() and cache_file.stat().st_size > 0:
            try:
                return await asyncio.to_thread(cache_file.read_bytes)
            except Exception:
                pass

        target_urls = [url]
        if url.startswith("http://"):
            target_urls.append(url.replace("http://", "https://", 1))

        headers = self._get_smart_headers(url)

        for try_url in target_urls:
            try:
                async with self.session.get(try_url, headers=headers, proxy=proxy, timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        try:
                            await asyncio.to_thread(cache_file.write_bytes, data)
                        except Exception as e:
                            logger.warning(f"[ResourceService] 写入缓存失败: {e}")
                        return data
            except Exception as e:
                logger.debug(f"[ResourceService] 下载尝试失败 {try_url[:30]}... : {e}")
                continue

        return None

    async def clean_old_cache(self, retention_seconds: int = 86400):
        if not self.cache_dir.exists():
            return

        def _clean():
            now = time.time()
            count = 0
            for f in self.cache_dir.iterdir():
                if f.is_file():
                    try:
                        if now - f.stat().st_mtime > retention_seconds:
                            f.unlink()
                            count += 1
                    except Exception:
                        pass
            if count > 0:
                logger.info(f"[ResourceService] 已清理 {count} 个过期缓存文件")

        await asyncio.to_thread(_clean)