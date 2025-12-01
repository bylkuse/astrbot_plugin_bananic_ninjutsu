import asyncio
import json
import time
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("astrbot")

@dataclass
class PermissionTransaction:
    """权额事务句柄"""
    allowed: bool = False
    reject_reason: str = ""

    _deducted_user: bool = False
    _deducted_group: bool = False
    _is_failed: bool = False
    _fail_reason: str = ""

    def mark_failed(self, reason: str):
        """标记业务执行失败，触发回滚"""
        self._is_failed = True
        self._fail_reason = reason

class StatsManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

        self.user_counts_file = self.data_dir / "user_counts.json"
        self.group_counts_file = self.data_dir / "group_counts.json"
        self.user_checkin_file = self.data_dir / "user_checkin.json"
        self.daily_stats_file = self.data_dir / "daily_stats.json"

        self.user_counts: Dict[str, int] = {}
        self.group_counts: Dict[str, int] = {}
        self.user_checkin_data: Dict[str, str] = {}
        self.daily_stats: Dict[str, Any] = {"date": "", "users": {}, "groups": {}}

        # 限流桶
        self._rate_limit_buckets: Dict[str, List[float]] = {}
        self._rate_limit_lock = asyncio.Lock()

    async def load_all_data(self):
        self.user_counts = await self._load_json(self.user_counts_file, {})
        self.group_counts = await self._load_json(self.group_counts_file, {})
        self.user_checkin_data = await self._load_json(self.user_checkin_file, {})
        self.daily_stats = await self._load_json(self.daily_stats_file, {"date": "", "users": {}, "groups": {}})
        logger.info("StatsManager: 统计与限流数据已加载")

    async def _load_json(self, file_path: Path, default: Any) -> Any:
        if not file_path.exists():
            return default
        try:
            content = await asyncio.to_thread(file_path.read_text, "utf-8")
            return json.loads(content)
        except Exception as e:
            logger.error(f"加载数据文件失败 {file_path}: {e}")
            return default

    async def _save_json(self, file_path: Path, data: Any):
        try:
            content = json.dumps(data, ensure_ascii=False, indent=4)
            await asyncio.to_thread(file_path.write_text, content, "utf-8")
        except Exception as e:
            logger.error(f"保存数据文件失败 {file_path}: {e}")

    # --- Rate Limiting ---

    async def _check_rate_limit(self, group_id: str, config: Dict[str, Any]) -> bool:
        """群组限流"""
        limit_settings = config.get("limit_settings", {})
        if not limit_settings.get("enable_rate_limit", True) or not group_id:
            return True

        period = int(limit_settings.get("rate_limit_period", 60))
        max_req = int(limit_settings.get("max_requests_per_group", 3))
        
        now = time.time()
        window_start = now - period

        async with self._rate_limit_lock:
            timestamps = self._rate_limit_buckets.get(group_id, [])
            valid_timestamps = [ts for ts in timestamps if ts > window_start]
            
            if len(valid_timestamps) >= max_req:
                self._rate_limit_buckets[group_id] = valid_timestamps
                return False

            valid_timestamps.append(now)
            self._rate_limit_buckets[group_id] = valid_timestamps
            return True

    def _remove_rate_limit_record(self, group_id: str):
        """回滚限流记录"""
        bucket = self._rate_limit_buckets.get(group_id, [])
        if bucket:
            bucket.pop()

    # --- Transaction Context Manager ---

    @asynccontextmanager
    async def transaction(self, user_id: str, group_id: Optional[str], config: Dict[str, Any], is_admin: bool = False):
        """事务上下文"""
        txn = PermissionTransaction()

        # 管理员免检
        if is_admin:
            txn.allowed = True
            yield txn
            return

        # 黑白名单
        if user_id in config.get("user_blacklist", []):
            txn.allowed = False; txn.reject_reason = "❌ 您已被加入黑名单。"; yield txn; return
            
        if group_id and group_id in config.get("group_blacklist", []):
            txn.allowed = False; txn.reject_reason = "❌ 本群已被加入黑名单。"; yield txn; return
            
        if config.get("user_whitelist") and user_id not in config.get("user_whitelist"):
            txn.allowed = False; txn.reject_reason = "❌ 您不在白名单中。"; yield txn; return
            
        if group_id and config.get("group_whitelist") and group_id not in config.get("group_whitelist"):
            txn.allowed = False; txn.reject_reason = "❌ 本群不在白名单中。"; yield txn; return

        # 限流
        if group_id and not await self._check_rate_limit(group_id, config):
            period = config.get("limit_settings", {}).get("rate_limit_period", 60)
            txn.allowed = False
            txn.reject_reason = f"⏳ 群内请求过于频繁，请等待 {period} 秒后再试。"
            yield txn
            return

        # 额度
        user_cnt = self.get_user_count(user_id)
        group_cnt = self.get_group_count(group_id) if group_id else 0

        user_limit_on = config.get("enable_user_limit", True)
        group_limit_on = config.get("enable_group_limit", False) and group_id

        has_user = not user_limit_on or user_cnt > 0
        has_group = not group_limit_on or group_cnt > 0

        if group_id:
            if not has_group and not has_user:
                txn.allowed = False
                txn.reject_reason = "❌ 本群次数与您的个人次数均已用尽。"
                yield txn
                self._remove_rate_limit_record(group_id)
                return
        elif not has_user:
            txn.allowed = False
            txn.reject_reason = "❌ 您的使用次数已用完。"
            yield txn
            return

        # 预扣除
        try:
            if group_limit_on and group_cnt > 0:
                await self.modify_group_count(group_id, -1)
                txn._deducted_group = True
            elif user_limit_on and user_cnt > 0:
                await self.modify_user_count(user_id, -1)
                txn._deducted_user = True
            
            txn.allowed = True
            yield txn

        except Exception as e:
            txn.mark_failed(str(e))
            raise e

        finally:
            # 退出逻辑
            if txn._is_failed:
                logger.info(f"Stats: 任务失败 ({txn._fail_reason})，正在回滚...")
                if txn._deducted_group and group_id:
                    await self.modify_group_count(group_id, 1)
                if txn._deducted_user:
                    await self.modify_user_count(user_id, 1)
                
                if group_id:
                    self._remove_rate_limit_record(group_id)
            elif txn.allowed:
                await self._record_usage_internal(user_id, group_id)

    def get_user_count(self, user_id: str) -> int:
        return self.user_counts.get(str(user_id), 0)

    def get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(str(group_id), 0)

    async def modify_user_count(self, user_id: str, delta: int) -> int:
        uid = str(user_id)
        current = self.user_counts.get(uid, 0)
        new_val = max(0, current + delta)
        self.user_counts[uid] = new_val
        await self._save_json(self.user_counts_file, self.user_counts)
        return new_val

    async def modify_group_count(self, group_id: str, delta: int) -> int:
        gid = str(group_id)
        current = self.group_counts.get(gid, 0)
        new_val = max(0, current + delta)
        self.group_counts[gid] = new_val
        await self._save_json(self.group_counts_file, self.group_counts)
        return new_val

    # 看板
    def has_checked_in_today(self, user_id: str) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.user_checkin_data.get(str(user_id)) == today

    async def perform_checkin(self, user_id: str, reward: int) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        uid = str(user_id)
        self.user_checkin_data[uid] = today
        await self._save_json(self.user_checkin_file, self.user_checkin_data)
        return await self.modify_user_count(uid, reward)

    async def record_usage(self, user_id: str, group_id: Optional[str]):
        await self._record_usage_internal(user_id, group_id)

    async def _record_usage_internal(self, user_id: str, group_id: Optional[str]):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            self.daily_stats = {"date": today, "users": {}, "groups": {}}

        uid = str(user_id)
        self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1
        
        if group_id:
            gid = str(group_id)
            self.daily_stats["groups"][gid] = self.daily_stats["groups"].get(gid, 0) + 1
            
        await self._save_json(self.daily_stats_file, self.daily_stats)

    def get_leaderboard(self) -> Tuple[str, List[Tuple[str, int]], List[Tuple[str, int]]]:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            return today, [], []
            
        users_sorted = sorted(self.daily_stats.get("users", {}).items(), key=lambda x: x[1], reverse=True)[:10]
        groups_sorted = sorted(self.daily_stats.get("groups", {}).items(), key=lambda x: x[1], reverse=True)[:10]
        return today, users_sorted, groups_sorted