import os
import asyncio
import json
import time
import random
from astrbot.api import logger
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PermissionTransaction:
    allowed: bool = False
    reject_reason: str = ""
    _deducted_user: bool = False
    _deducted_group: bool = False
    _is_failed: bool = False
    _fail_reason: str = ""

    def mark_failed(self, reason: str):
        self._is_failed = True
        self._fail_reason = reason


@dataclass
class CheckInResult:
    success: bool
    reward: int = 0
    message: str = ""


@dataclass
class DashboardData:
    user_count: int
    group_count: int
    leaderboard_date: str
    top_users: List[Tuple[str, int]]
    top_groups: List[Tuple[str, int]]
    checkin_result: Optional[CheckInResult] = None


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

        self._rate_limit_buckets: Dict[str, List[float]] = {}
        self._rate_limit_lock = asyncio.Lock()

        self._dirty_flags: set[str] = set()
        self._auto_save_task: Optional[asyncio.Task] = None
        self._save_interval = 30

    # --- 数据 ---

    async def load_all_data(self):
        self.user_counts = await self._load_json(self.user_counts_file, {})
        self.group_counts = await self._load_json(self.group_counts_file, {})
        self.user_checkin_data = await self._load_json(self.user_checkin_file, {})
        self.daily_stats = await self._load_json(
            self.daily_stats_file, {"date": "", "users": {}, "groups": {}}
        )
        logger.info(
            f"StatsManager: 数据加载完成。当前记录日期: {self.daily_stats.get('date', '无')}"
        )

        self.start_auto_save()  # 缓存回写

    def start_auto_save(self):
        if self._auto_save_task is None or self._auto_save_task.done():
            self._auto_save_task = asyncio.create_task(self._auto_save_loop())
            logger.debug("StatsManager: 自动保存任务已启动")

    async def stop_auto_save(self):
        if self._auto_save_task:
            self._auto_save_task.cancel()
            try:
                await self._auto_save_task
            except asyncio.CancelledError:
                pass
            self._auto_save_task = None

        await self._flush_dirty_data()
        logger.info("StatsManager: 数据已同步到磁盘，任务终止。")

    async def _auto_save_loop(self):
        while True:
            try:
                await asyncio.sleep(self._save_interval)
                await self._flush_dirty_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动保存循环发生异常: {e}")
                await asyncio.sleep(5)

    async def _flush_dirty_data(self):
        if not self._dirty_flags:
            return

        pending_saves = list(self._dirty_flags)

        try:
            if "user_counts" in pending_saves:
                await self._save_json(self.user_counts_file, self.user_counts)
                self._dirty_flags.discard("user_counts")

            if "group_counts" in pending_saves:
                await self._save_json(self.group_counts_file, self.group_counts)
                self._dirty_flags.discard("group_counts")

            if "checkin" in pending_saves:
                await self._save_json(self.user_checkin_file, self.user_checkin_data)
                self._dirty_flags.discard("checkin")

            if "daily" in pending_saves:
                await self._save_json(self.daily_stats_file, self.daily_stats)
                self._dirty_flags.discard("daily")

        except Exception as e:
            logger.error(f"数据回写失败: {e}")

    async def _load_json(self, file_path: Path, default: Any) -> Any:
        if not file_path.exists():
            return default
        try:
            content = await asyncio.to_thread(file_path.read_text, "utf-8")
            return json.loads(content)
        except Exception as e:
            logger.error(f"加载 {file_path} 失败: {e}")
            return default

    async def _save_json(self, file_path: Path, data: Any):
        """原子写入"""
        try:
            content = json.dumps(data, ensure_ascii=False, indent=4)
            temp_path = file_path.with_suffix(".tmp")
            await asyncio.to_thread(temp_path.write_text, content, "utf-8")
            await asyncio.to_thread(os.replace, temp_path, file_path)
        except Exception as e:
            logger.error(f"保存 {file_path} 失败: {e}")

    # --- 限流 ---

    async def _check_rate_limit(self, group_id: str, config: Dict[str, Any]) -> bool:
        perm_conf = config.get("Permission_Config", {})

        if not perm_conf.get("enable_rate_limit", True) or not group_id:
            return True
        period = int(perm_conf.get("rate_limit_period", 60))
        max_req = int(perm_conf.get("max_requests_per_group", 3))

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
        bucket = self._rate_limit_buckets.get(group_id, [])
        if bucket:
            bucket.pop()

    # --- 事务 ---

    @asynccontextmanager
    async def transaction(
        self,
        user_id: str,
        group_id: Optional[str],
        config: Dict[str, Any],
        is_admin: bool = False,
    ):
        txn = PermissionTransaction()
        perm_conf = config.get("Permission_Config", {})

        if is_admin:
            txn.allowed = True
            yield txn
            return

        if user_id in perm_conf.get("user_blacklist", []):
            txn.allowed = False
            txn.reject_reason = "❌ 您已被加入黑名单。"
            yield txn
            return

        if group_id and group_id in perm_conf.get("group_blacklist", []):
            txn.allowed = False
            txn.reject_reason = "❌ 本群已被加入黑名单。"
            yield txn
            return

        if perm_conf.get("user_whitelist") and user_id not in perm_conf.get(
            "user_whitelist"
        ):
            txn.allowed = False
            txn.reject_reason = "❌ 您不在白名单中。"
            yield txn
            return

        if (
            group_id
            and perm_conf.get("group_whitelist")
            and group_id not in perm_conf.get("group_whitelist")
        ):
            txn.allowed = False
            txn.reject_reason = "❌ 本群不在白名单中。"
            yield txn
            return

        if group_id and not await self._check_rate_limit(group_id, config):
            txn.allowed = False
            period = perm_conf.get("rate_limit_period", 60)
            txn.reject_reason = f"⏳ 群内请求过于频繁，请等待 {period} 秒后再试。"
            yield txn
            return

        user_cnt = self.get_user_count(user_id)
        group_cnt = self.get_group_count(group_id) if group_id else 0

        user_limit_on = perm_conf.get("enable_user_limit", True)
        group_limit_on = perm_conf.get("enable_group_limit", False) and group_id

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
            if txn._is_failed:
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
        self._dirty_flags.add("user_counts")
        return new_val

    async def modify_group_count(self, group_id: str, delta: int) -> int:
        gid = str(group_id)
        current = self.group_counts.get(gid, 0)
        new_val = max(0, current + delta)
        self.group_counts[gid] = new_val
        self._dirty_flags.add("group_counts")
        return new_val

    async def modify_resource(self, target_id: str, count: int, is_group: bool) -> int:
        if is_group:
            return await self.modify_group_count(target_id, count)
        else:
            return await self.modify_user_count(target_id, count)

    async def get_dashboard_with_checkin(
        self, user_id: str, group_id: Optional[str], config: Dict[str, Any]
    ) -> DashboardData:
        checkin_res = await self._try_daily_checkin(user_id, config)

        today, users, groups = self._get_leaderboard_data()

        return DashboardData(
            user_count=self.get_user_count(user_id),
            group_count=self.get_group_count(group_id) if group_id else 0,
            leaderboard_date=today,
            top_users=users,
            top_groups=groups,
            checkin_result=checkin_res,
        )

    # --- 内部 ---

    async def _try_daily_checkin(
        self, user_id: str, config: Dict[str, Any]
    ) -> CheckInResult:
        checkin_conf = config.get("Checkin_Config", {})

        if not checkin_conf.get("enable_checkin", False):
            return CheckInResult(False, 0, "📅 签到功能未开启。")

        uid = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")

        if self.user_checkin_data.get(uid) == today:
            return CheckInResult(False, 0, "📅 您今天已经签到过了。")

        is_random = (
            str(checkin_conf.get("enable_random_checkin", False)).lower() == "true"
        )
        if is_random:
            base_max = int(checkin_conf.get("checkin_random_reward_max", 5))
            reward = random.randint(1, max(1, base_max))
        else:
            reward = int(checkin_conf.get("checkin_fixed_reward", 3))

        self.user_checkin_data[uid] = today
        self._dirty_flags.add("checkin")
        await self.modify_user_count(uid, reward)

        return CheckInResult(True, reward, f"🎉 签到成功！获得 {reward} 次。")

    async def _record_usage_internal(self, user_id: str, group_id: Optional[str]):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            self.daily_stats = {"date": today, "users": {}, "groups": {}}

        uid = str(user_id)
        self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1

        if group_id:
            gid = str(group_id)
            self.daily_stats["groups"][gid] = self.daily_stats["groups"].get(gid, 0) + 1

        self._dirty_flags.add("daily")

    def _get_leaderboard_data(
        self,
    ) -> Tuple[str, List[Tuple[str, int]], List[Tuple[str, int]]]:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            return today, [], []

        users_sorted = sorted(
            self.daily_stats.get("users", {}).items(), key=lambda x: x[1], reverse=True
        )[:10]
        groups_sorted = sorted(
            self.daily_stats.get("groups", {}).items(), key=lambda x: x[1], reverse=True
        )[:10]
        return today, users_sorted, groups_sorted
