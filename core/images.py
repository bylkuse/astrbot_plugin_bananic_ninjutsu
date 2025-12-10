import aiohttp
import asyncio
import base64
import io
import urllib.parse
import hashlib
import time
from pathlib import Path
from typing import List
from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.api.star import StarTools


def get_plugin_data_dir() -> Path:
    return StarTools.get_data_dir("astrbot_plugin_bananic_ninjutsu")

CACHE_DIR = get_plugin_data_dir() / "cache"

class ImageUtils:
    @staticmethod
    def _ensure_cache_dir():
        if not CACHE_DIR.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    async def clean_cache(retention_seconds: int = 86400): # 1. 改为 async def
        if not CACHE_DIR.exists():
            return

        def _clean_sync():
            now = time.time()
            deleted_count = 0
            logger.debug(f"[ImageUtils] 开始清理缓存，保留时间: {retention_seconds}秒")
            try:
                for file_path in CACHE_DIR.iterdir():
                    if not file_path.is_file():
                        continue
                    try:
                        mtime = file_path.stat().st_mtime
                        if now - mtime > retention_seconds:
                            file_path.unlink()
                            deleted_count += 1
                    except Exception as e:
                        logger.warning(f"[ImageUtils] 删除缓存文件失败 {file_path.name}: {e}")
                if deleted_count > 0:
                    logger.info(f"[ImageUtils] 缓存清理完成，共删除 {deleted_count} 个过期文件")
            except Exception as e:
                logger.error(f"[ImageUtils] 缓存清理过程发生错误: {e}")
        await asyncio.to_thread(_clean_sync)

    @staticmethod
    def _get_smart_headers(url: str) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }

        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}"

            if "qpic.cn" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
            if "nt.qq.com" in (parsed.netloc or ""):
                headers["Referer"] = "https://qun.qq.com"
                headers["Origin"] = "https://qun.qq.com"
        except Exception:
            pass

        return headers

    @classmethod
    async def _try_adapter_download(cls, url: str, event: AstrMessageEvent) -> bytes | None:
        if not event or not hasattr(event, "bot"):
            return None

        async def _call_get_image(client, **kwargs):
            if hasattr(client, "call_action"):
                return await client.call_action("get_image", **kwargs)
            if hasattr(client, "api") and hasattr(client.api, "call_action"):
                return await client.api.call_action("get_image", **kwargs)
            return None

        try:
            parsed = urllib.parse.urlparse(url)
            file_id = None
            qs = urllib.parse.parse_qs(parsed.query or "")

            if "fileid" in qs and qs["fileid"]:
                file_id = qs["fileid"][0]
            elif "file" in qs and qs["file"]:
                file_id = qs["file"][0]

            payloads = []
            if file_id:
                payloads.append({"file": file_id})
            payloads.append({"file": url})

            for payload in payloads:
                resp = await _call_get_image(event.bot, **payload)

                if isinstance(resp, dict):
                    if base64_str := resp.get("base64"):
                        return base64.b64decode(base64_str)

                    if file_path := resp.get("file"):
                        path_obj = Path(file_path)
                        if path_obj.exists() and path_obj.is_file():
                            return await asyncio.to_thread(path_obj.read_bytes)

                    if new_url := resp.get("url"):
                        if new_url != url:
                            logger.debug(f"[ImageUtils] Adapter 重定向 URL: {new_url}")
                            return None
                            
        except Exception as e:
            logger.debug(f"[ImageUtils] Adapter API 下载尝试跳过: {e}")

        return None

    @staticmethod
    def _pick_avatar_url(data: dict | None) -> str | None:
        if not isinstance(data, dict):
            return None

        candidate_keys = ["avatar", "avatar_url", "user_avatar", "head_image", "url"]
        for key in candidate_keys:
            url = data.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url

        if isinstance(data.get("data"), dict):
            return ImageUtils._pick_avatar_url(data["data"])
        return None

    @classmethod
    async def download_image(
        cls, 
        url: str, 
        session: aiohttp.ClientSession,
        event: AstrMessageEvent | None = None,
        proxy: str | None = None, 
        timeout: int = 20,
        use_cache: bool = True
    ) -> bytes | None:
        if not url:
            return None

        cache_file = None
        if use_cache:
            try:
                cls._ensure_cache_dir()
                cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
                cache_file = CACHE_DIR / cache_key

                if cache_file.exists() and cache_file.stat().st_size > 0:
                    logger.debug(f"[Cache] 命中本地缓存: {url[:30]}...")
                    return await asyncio.to_thread(cache_file.read_bytes)
            except Exception as e:
                logger.warning(f"[Cache] 读取缓存失败: {e}")

        logger.debug(f"正在尝试下载图片: {url[:60]}...")

        if event:
            if adapter_bytes := await cls._try_adapter_download(url, event):
                if use_cache and cache_file:
                    try:
                        await asyncio.to_thread(cache_file.write_bytes, adapter_bytes)
                    except Exception: pass
                return adapter_bytes

        headers = cls._get_smart_headers(url)
        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=10)

        try:
            target_urls = [url]
            if url.startswith("http://"):
                 target_urls.append(url.replace("http://", "https://", 1))

            for try_url in target_urls:
                try:
                    async with session.get(try_url, headers=headers, proxy=proxy, timeout=client_timeout) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data:
                                if use_cache and cache_file:
                                    try:
                                        await asyncio.to_thread(cache_file.write_bytes, data)
                                    except Exception: pass
                                return data
                except Exception:
                    continue

        except Exception as e:
            logger.error(f"图片下载最终失败: {e}")
            return None

        return None

    @classmethod
    async def get_avatar(
        cls, 
        user_id: str, 
        session: aiohttp.ClientSession,
        event: AstrMessageEvent | None = None,
        proxy: str | None = None,
    ) -> bytes | None:
        if not user_id.isdigit():
            return None

        if event:
            try:
                sender = getattr(event.message_obj, "sender", None)
                if sender:
                    sender_data = sender.__dict__ if hasattr(sender, "__dict__") else sender
                    if found_url := cls._pick_avatar_url(sender_data):
                        logger.debug(f"从事件 Sender 中提取到头像: {found_url}")
                        return await cls.download_image(found_url, session, event, proxy)
            except Exception:
                pass

            try:
                raw = getattr(event.message_obj, "raw_message", None)
                if found_url := cls._pick_avatar_url(raw):
                    return await cls.download_image(found_url, session, event, proxy)
            except Exception:
                pass

        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return await cls.download_image(avatar_url, session, event, proxy)

    @staticmethod
    def _standardize_image_sync(
        raw: bytes, 
        max_size: int = 2048, 
        ensure_white_bg: bool = False,
        format_hint: str = "JPEG",
        quality: int = 85
    ) -> bytes:
        if not raw:
            return raw

        try:
            img_io = io.BytesIO(raw)
            with PILImage.open(img_io) as img:
                width, height = img.size
                original_format = img.format
                original_mode = img.mode

                needs_resize = width > max_size or height > max_size
                needs_mode_convert = False
                target_mode = original_mode

                if ensure_white_bg:
                    if original_mode in ("RGBA", "LA") or (original_mode == "P" and "transparency" in img.info):
                        needs_mode_convert = True
                        target_mode = "RGB"
                elif original_mode not in ("RGB", "RGBA"):
                    needs_mode_convert = True
                    target_mode = "RGB"

                target_format = "JPEG" if (ensure_white_bg or target_mode == "RGB") else "PNG"
                if format_hint:
                    target_format = format_hint.upper()

                is_format_match = (original_format == target_format)

                if not needs_resize and not needs_mode_convert and is_format_match:
                    return raw

                process_img = img

                if getattr(img, "is_animated", False):
                    img.seek(0)
                    process_img = img.copy()

                if needs_resize:
                    process_img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)

                if ensure_white_bg:
                    if process_img.mode in ("RGBA", "LA") or (process_img.mode == "P" and "transparency" in process_img.info):
                        if process_img.mode != "RGBA":
                            process_img = process_img.convert("RGBA")
                        bg = PILImage.new("RGB", process_img.size, (255, 255, 255))
                        bg.paste(process_img, (0, 0), process_img)
                        process_img = bg
                    else:
                        if process_img.mode != "RGB":
                            process_img = process_img.convert("RGB")
                else:
                    if process_img.mode == "P":
                        process_img = process_img.convert("RGBA")
                    elif process_img.mode == "CMYK":
                        process_img = process_img.convert("RGB")

                out_io = io.BytesIO()

                if target_format == "JPEG" and process_img.mode in ("RGBA", "LA"):
                    bg = PILImage.new("RGB", process_img.size, (255, 255, 255))
                    bg.paste(process_img, (0, 0), process_img)
                    process_img = bg

                if target_format == "JPEG":
                    process_img.save(out_io, format="JPEG", quality=quality, optimize=True)
                else:
                    process_img.save(out_io, format="PNG", compress_level=3)

                return out_io.getvalue()

        except PILImage.DecompressionBombError:
            logger.warning(f"检测到超大分辨率图片(DecompressionBomb)，跳过处理，使用原图。")
            return raw
        except Exception as e:
            logger.warning(f"图片处理出错 (使用原图): {e}")
            return raw

    @classmethod
    async def load_and_process(
        cls,
        src: str | bytes,
        event: AstrMessageEvent | None = None,
        proxy: str | None = None,
        ensure_white_bg: bool = False,
        max_size: int = 2048,
        session: aiohttp.ClientSession | None = None
    ) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()

        should_close_session = False
        if not session and isinstance(src, str) and src.startswith("http"):
            session = aiohttp.ClientSession()
            should_close_session = True

        try:
            if isinstance(src, bytes):
                raw = src
            elif isinstance(src, str):
                src = src.strip()
                if src.startswith("http"):
                    raw = await cls.download_image(src, session, event=event, proxy=proxy)
                elif src.startswith("base64://"):
                    try:
                        raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
                    except Exception:
                        pass
                elif src.startswith("data:image"):
                    try:
                        if ";base64," in src:
                            _, b64_data = src.split(";base64,", 1)
                            raw = await loop.run_in_executor(None, base64.b64decode, b64_data)
                    except Exception:
                        pass
                elif Path(src).is_file():
                    raw = await loop.run_in_executor(None, Path(src).read_bytes)
        finally:
            if should_close_session and session:
                await session.close()

        if not raw:
            return None

        return await asyncio.to_thread(cls._standardize_image_sync, raw, max_size, ensure_white_bg)

    @classmethod
    async def get_images_from_event(
        cls, 
        event: AstrMessageEvent, 
        max_count: int = 5, 
        proxy: str | None = None,
        session: aiohttp.ClientSession | None = None
    ) -> List[bytes]:
        tasks = []

        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            url_or_file = s_chain.url or s_chain.file
                            if url_or_file:
                                tasks.append(cls.load_and_process(
                                    url_or_file, event=event, proxy=proxy, session=session
                                ))

                elif isinstance(seg, Image):
                    url_or_file = seg.url or seg.file
                    if url_or_file:
                        tasks.append(cls.load_and_process(
                            url_or_file, event=event, proxy=proxy, session=session
                        ))

                elif isinstance(seg, At):
                    async def _process_avatar(uid):
                        local_session = None
                        target_session = session
                        if not target_session:
                            local_session = aiohttp.ClientSession()
                            target_session = local_session
                        try:
                            avatar = await cls.get_avatar(uid, target_session, event=event, proxy=proxy)
                            if avatar:
                                return await cls.load_and_process(avatar, event=event, max_size=1024)
                        finally:
                            if local_session:
                                await local_session.close()
                        return None
                    tasks.append(_process_avatar(str(seg.qq)))

        if not tasks:
            async def _process_sender():
                sender_id = event.get_sender_id()
                local_session = None
                target_session = session
                if not target_session:
                    local_session = aiohttp.ClientSession()
                    target_session = local_session
                try:
                    avatar = await cls.get_avatar(sender_id, target_session, event=event, proxy=proxy)
                    if avatar:
                        return await cls.load_and_process(avatar, event=event, max_size=1024)
                finally:
                    if local_session:
                        await local_session.close()
                return None
            tasks.append(_process_sender())

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        img_bytes_list: List[bytes] = []

        for res in results:
            if isinstance(res, Exception):
                logger.warning(f"[ImageUtils] 图片获取任务异常: {res}")
                continue
            if res:
                img_bytes_list.append(res)
            if len(img_bytes_list) >= max_count:
                break
        return img_bytes_list

    @classmethod
    async def compress_image(
        cls,
        raw_bytes: bytes, 
        quality: int = 85, 
        threshold_mb: float = 1.0,
        target_size: int = 2048
    ) -> bytes:
        if not raw_bytes:
            return raw_bytes

        if len(raw_bytes) <= threshold_mb * 1024 * 1024:
            return raw_bytes

        def is_likely_image(data: bytes) -> bool:
            if len(data) < 4: return False
            if data.startswith(b"\xff\xd8"): return True # JPEG
            if data.startswith(b"\x89PNG\r\n\x1a\n"): return True # PNG
            if data.startswith(b"GIF8"): return True # GIF
            if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP": return True # WEBP
            if len(data) >= 12 and data[4:12] in {b"ftypheic", b"ftypheif", b"ftypmif1", b"ftypmsf1", b"ftyphevc"}:
                return True
            return False

        if not is_likely_image(raw_bytes):
            return raw_bytes

        try:
            return await asyncio.to_thread(
                cls._standardize_image_sync,
                raw=raw_bytes,
                max_size=target_size,
                ensure_white_bg=True,
                format_hint="JPEG",
                quality=quality
            )
        except Exception as e:
            logger.warning(f"图片压缩失败: {e}")
            return raw_bytes