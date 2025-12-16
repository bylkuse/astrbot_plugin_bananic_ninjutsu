import json
import os
import asyncio
from pathlib import Path
from typing import Any, Optional, TypeVar, Generic

from astrbot.api import logger

T = TypeVar("T")

class AtomicJsonStore(Generic[T]):
    def __init__(self, file_path: Path):
        self.file_path = file_path

    async def load(self, default_factory: Optional[callable] = None) -> Any:
        if not self.file_path.exists():
            return default_factory() if default_factory else {}

        try:
            content = await asyncio.to_thread(self.file_path.read_text, encoding="utf-8")
            if not content.strip():
                return default_factory() if default_factory else {}
            return json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[AtomicJsonStore] 加载 {self.file_path.name} 失败: {e}，将使用默认值。")
            if self.file_path.exists():
                try:
                    bak_path = self.file_path.with_suffix(".bak")
                    os.rename(self.file_path, bak_path)
                    logger.warning(f"已将损坏的文件备份为: {bak_path.name}")
                except OSError:
                    pass
            return default_factory() if default_factory else {}

    async def save(self, data: Any):
        def _write_sync():
            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)

                # 序列化
                json_str = json.dumps(data, ensure_ascii=False, indent=2)

                temp_path = self.file_path.with_suffix(".tmp")
                temp_path.write_text(json_str, encoding="utf-8")

                os.replace(temp_path, self.file_path)
            except Exception as e:
                logger.error(f"[AtomicJsonStore] 保存 {self.file_path.name} 失败: {e}")

        await asyncio.to_thread(_write_sync)