import aiohttp
import asyncio
import base64
import io
from pathlib import Path
from typing import List
from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ImageUtils:
    @classmethod
    async def download_image(
        cls, 
        url: str, 
        proxy: str | None = None, 
        timeout: int = 60,
        session: aiohttp.ClientSession | None = None
    ) -> bytes | None:
        logger.debug(f"正在尝试下载图片: {url}")
        try:
            if session:
                async with session.get(url, proxy=proxy, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.warning(f"图片下载失败 HTTP {resp.status}: {url}")
                        return None
                    return await resp.read()
            else:
                # 兜底
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as temp_session:
                    async with temp_session.get(url, proxy=proxy, timeout=timeout) as resp:
                        if resp.status != 200:
                            logger.warning(f"图片下载失败 HTTP {resp.status}: {url}")
                            return None
                        return await resp.read()

        except Exception as e:
            logger.error(f"图片下载异常: {e}")
            return None

    @classmethod
    async def get_avatar(
        cls, 
        user_id: str, 
        proxy: str | None = None,
        session: aiohttp.ClientSession | None = None
    ) -> bytes | None:
        if not user_id.isdigit():
            return None
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return await cls.download_image(avatar_url, proxy=proxy, session=session)

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
                save_format = target_format
                if process_img.mode == "RGBA" and save_format == "JPEG":
                    save_format = "PNG"

                if save_format == "JPEG":
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
        proxy: str | None = None,
        ensure_white_bg: bool = False,
        max_size: int = 2048,
        session: aiohttp.ClientSession | None = None
    ) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()

        if isinstance(src, bytes):
            raw = src
        elif isinstance(src, str):
            if src.startswith("http"):
                raw = await cls.download_image(src, proxy=proxy, session=session)
            elif src.startswith("base64://"):
                try:
                    raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
                except Exception:
                    pass
            elif Path(src).is_file():
                raw = await loop.run_in_executor(None, Path(src).read_bytes)

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
        img_bytes_list: List[bytes] = []
        at_user_ids: List[str] = []

        async def _add_img(source):
            if img := await cls.load_and_process(source, proxy=proxy, ensure_white_bg=False, session=session):
                img_bytes_list.append(img)

        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        url_or_file = s_chain.url or s_chain.file
                        if url_or_file:
                            await _add_img(url_or_file)

            elif isinstance(seg, Image):
                url_or_file = seg.url or seg.file
                if url_or_file:
                    await _add_img(url_or_file)

            elif isinstance(seg, At):
                at_user_ids.append(str(seg.qq))

        if not img_bytes_list and at_user_ids:
            for user_id in at_user_ids:
                if avatar := await cls.get_avatar(user_id, proxy=proxy, session=session):
                    await _add_img(avatar)

        if not img_bytes_list:
            sender_id = event.get_sender_id()
            if avatar := await cls.get_avatar(sender_id, proxy=proxy, session=session):
                await _add_img(avatar)

        return img_bytes_list[:max_count]

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

        # 魔数
        def is_likely_image(data: bytes) -> bool:
            if len(data) < 12: return False
            if data.startswith(b"\xff\xd8\xff"): return True # JPEG
            if data.startswith(b"\x89PNG\r\n\x1a\n"): return True # PNG
            if data.startswith(b"GIF8"): return True # GIF
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP": return True # WEBP
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