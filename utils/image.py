import io
import base64
from typing import Optional
from PIL import Image as PILImage
from astrbot.api import logger

class ImageUtils:
    @staticmethod
    def get_mime_type(data: bytes) -> str:
        if not data or len(data) < 4: return "image/jpeg"
        if data.startswith(b"\xff\xd8"): return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
        if data.startswith(b"GIF8"): return "image/gif"
        if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP": return "image/webp"
        if len(data) >= 12 and data[4:12] in {b"ftypheic", b"ftypheif", b"ftypmif1", b"ftypmsf1", b"ftyphevc"}:
            return "image/heic"
        return "image/jpeg"

    @staticmethod
    def decode_base64(data: str) -> Optional[bytes]:
        if not data: return None
        try:
            if ";base64," in data:
                _, data = data.split(";base64,", 1)
            elif data.startswith("base64://"):
                data = data[9:]
            return base64.b64decode(data)
        except Exception:
            return None

    @staticmethod
    def standardize_image(
        raw: bytes, 
        max_size: int = 2048, 
        ensure_white_bg: bool = False,
        quality: int = 85
    ) -> bytes:

        if not raw: return raw

        try:
            img_io = io.BytesIO(raw)
            with PILImage.open(img_io) as img:
                # 第一帧
                if getattr(img, "is_animated", False):
                    img.seek(0)
                    img = img.copy()

                # 规范化
                if img.mode == "CMYK":
                    img = img.convert("RGB")
                elif img.mode == "P":
                    img = img.convert("RGBA")

                # 背景
                if ensure_white_bg or img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    bg = PILImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")
                    bg.paste(img, (0, 0), img)
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                # 缩放
                width, height = img.size
                if width > max_size or height > max_size:
                    img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)

                out_io = io.BytesIO()
                img.save(out_io, format="JPEG", quality=quality, optimize=True)
                return out_io.getvalue()

        except PILImage.DecompressionBombError:
            logger.warning("[ImageUtils] 检测到超大分辨率图片(Bomb)，跳过处理使用原图。")
            return raw
        except Exception as e:
            logger.warning(f"[ImageUtils] 图片标准化失败: {e}，使用原图")
            return raw