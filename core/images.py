import aiohttp
import asyncio
import base64
import io
from pathlib import Path
from typing import List, Optional, Union
from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ImageUtils:
    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        """单例 Session"""
        if cls._session is None or cls._session.closed:
            connector = aiohttp.TCPConnector(limit=100)
            cls._session = aiohttp.ClientSession(connector=connector)
        return cls._session

    @classmethod
    async def download_image(
        cls, url: str, proxy: Optional[str] = None, timeout: int = 60
    ) -> bytes | None:
        logger.debug(f"正在尝试下载图片: {url}")
        try:
            session = await cls.get_session()
            async with session.get(url, proxy=proxy, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning(f"图片下载失败 HTTP {resp.status}: {url}")
                    return None
                return await resp.read()
        except Exception as e:
            logger.error(f"图片下载异常: {e}")
            return None

    @classmethod
    async def get_avatar(
        cls, user_id: str, proxy: Optional[str] = None
    ) -> bytes | None:
        """获取QQ头像"""
        if not user_id.isdigit():
            return None
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return await cls.download_image(avatar_url, proxy=proxy)

    @staticmethod
    def _process_image_sync(
        raw: bytes, ensure_white_bg: bool = False, max_size: int = 2048
    ) -> bytes:
        img_io = io.BytesIO(raw)
        try:
            with PILImage.open(img_io) as img:

                width, height = img.size
                if width > max_size or height > max_size:
                    img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)

                if getattr(img, "is_animated", False):
                    img.seek(0)

                if ensure_white_bg or img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")

                if ensure_white_bg and img.mode == "RGBA":
                    bg = PILImage.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, (0, 0), img)
                    img = bg
                elif img.mode == "RGBA":
                    pass

                out_io = io.BytesIO()
                if img.mode == "RGB":
                    img.save(out_io, format="JPEG", quality=85)
                else:
                    img.save(out_io, format="PNG", compress_level=3)

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
        src: Union[str, bytes],
        proxy: Optional[str] = None,
        ensure_white_bg: bool = False,
    ) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()

        if isinstance(src, bytes):
            raw = src
        elif isinstance(src, str):
            if src.startswith("http"):
                raw = await cls.download_image(src, proxy=proxy)
            elif src.startswith("base64://"):
                try:
                    raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
                except Exception:
                    pass
            elif Path(src).is_file():
                raw = await loop.run_in_executor(None, Path(src).read_bytes)

        if not raw:
            return None

        return await asyncio.to_thread(cls._process_image_sync, raw, ensure_white_bg)

    @classmethod
    async def get_images_from_event(
        cls, event: AstrMessageEvent, max_count: int = 5, proxy: Optional[str] = None
    ) -> List[bytes]:
        """提取图片"""
        img_bytes_list: List[bytes] = []
        at_user_ids: List[str] = []

        for seg in event.message_obj.message:
            # 回复链
            if isinstance(seg, Reply) and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        url_or_file = s_chain.url or s_chain.file
                        if url_or_file and (
                            img := await cls.load_and_process(url_or_file, proxy=proxy)
                        ):
                            img_bytes_list.append(img)

            # 发送图
            elif isinstance(seg, Image):
                url_or_file = seg.url or seg.file
                if url_or_file and (
                    img := await cls.load_and_process(url_or_file, proxy=proxy)
                ):
                    img_bytes_list.append(img)

            # 收集 @
            elif isinstance(seg, At):
                at_user_ids.append(str(seg.qq))

        #  @ 头像
        if not img_bytes_list and at_user_ids:
            for user_id in at_user_ids:
                if avatar := await cls.get_avatar(user_id, proxy=proxy):
                    processed = await cls.load_and_process(avatar, proxy=proxy)
                    if processed:
                        img_bytes_list.append(processed)

        # 发送者头像
        if not img_bytes_list:
            sender_id = event.get_sender_id()
            if avatar := await cls.get_avatar(sender_id, proxy=proxy):
                processed = await cls.load_and_process(avatar, proxy=proxy)
                if processed:
                    img_bytes_list.append(processed)

        return img_bytes_list[:max_count]

    @staticmethod
    async def compress_image(
        raw_bytes: bytes, quality: int = 85, threshold_mb: float = 1.0
    ) -> bytes:
        if not raw_bytes:
            return raw_bytes

        if len(raw_bytes) <= threshold_mb * 1024 * 1024:
            return raw_bytes

        # 魔数
        def is_likely_image(data: bytes) -> bool:
            if len(data) < 12:
                return False
            # JPEG
            if data.startswith(b"\xff\xd8\xff"):
                return True
            # PNG
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return True
            # GIF
            if data.startswith(b"GIF8"):
                return True
            # WEBP
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                return True
            # BMP
            if data.startswith(b"BM"):
                return True
            return False

        if not is_likely_image(raw_bytes):
            return raw_bytes

        def _blocking_compress():
            try:
                with PILImage.open(io.BytesIO(raw_bytes)) as img:
                    if max(img.size) > 2048:
                        img.thumbnail((2048, 2048), PILImage.Resampling.LANCZOS)

                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")

                    output_io = io.BytesIO()
                    img.save(output_io, format="JPEG", quality=quality, optimize=True)
                    compressed_bytes = output_io.getvalue()

                    if len(compressed_bytes) < len(raw_bytes):
                        return compressed_bytes
                    return raw_bytes
            except Exception as e:
                logger.warning(f"图片压缩失败 (使用原图): {e}")
                return raw_bytes

        return await asyncio.to_thread(_blocking_compress)

    @classmethod
    async def terminate(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None
            logger.debug("ImageUtils session closed.")
