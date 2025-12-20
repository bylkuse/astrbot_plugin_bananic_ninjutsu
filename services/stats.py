import asyncio
import time
import copy
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from astrbot.api import logger

from ..utils import AtomicJsonStore
from ..domain import QuotaContext

class StatsService:
    def __init__(self, data_dir: Path, global_config: Dict[str, Any]):
        self.data_dir = data_dir
        self.global_config = global_config

        # 存储
        self._store_user = AtomicJsonStore(data_dir / "user_counts.json")
        self._store_group = AtomicJsonStore(data_dir / "group_counts.json")
        self._store_checkin = AtomicJsonStore(data_dir / "user_checkin.json")
        self._store_daily = AtomicJsonStore(data_dir / "daily_stats.json")

        # Cache
        self.user_counts: Dict[str, int] = {}
        self.group_counts: Dict[str, int] = {}
        self.checkin_data: Dict[str, str] = {}
        self.daily_stats: Dict[str, Any] = {"date": "", "users": {}, "groups": {}}

        # 脏标记
        self._dirty_flags = set() 
        self._auto_save_task: Optional[asyncio.Task] = None

        # 限流桶
        self._rate_limits: Dict[str, List[float]] = {}
        self._rl_lock = asyncio.Lock()

    async def initialize(self):
        self.user_counts = await self._store_user.load(dict)
        self.group_counts = await self._store_group.load(dict)
        self.checkin_data = await self._store_checkin.load(dict)
        self.daily_stats = await self._store_daily.load(lambda: {"date": datetime.now().strftime("%Y-%m-%d"), "users": {}, "groups": {}})

        self._start_auto_save()
        logger.info(f"[StatsService] 数据加载完成 (用户:{len(self.user_counts)}, 群组:{len(self.group_counts)})")

    async def shutdown(self):
        if self._auto_save_task:
            self._auto_save_task.cancel()
            try:
                await self._auto_save_task
            except asyncio.CancelledError:
                pass
        await self._flush_data()

    async def get_quota_context(self, user_id: str, group_id: Optional[str], is_admin: bool) -> QuotaContext:
        perm_conf = self.global_config.get("Permission_Config", {})

        u_bal = self.user_counts.get(str(user_id), 0)
        g_bal = 0
        if group_id:
            g_bal = self.group_counts.get(str(group_id), 0)

        return QuotaContext(
            user_id=str(user_id),
            group_id=str(group_id) if group_id else None,
            is_admin=is_admin,
            user_balance=u_bal,
            group_balance=g_bal,
            user_blacklist=perm_conf.get("user_blacklist", []),
            group_blacklist=perm_conf.get("group_blacklist", []),
            user_whitelist=perm_conf.get("user_whitelist", []),
            group_whitelist=perm_conf.get("group_whitelist", []),
            enable_user_limit=perm_conf.get("enable_user_limit", True),
            enable_group_limit=perm_conf.get("enable_group_limit", False)
        )

    async def check_rate_limit(self, group_id: str) -> bool:
        if not group_id:
            return True

        perm_conf = self.global_config.get("Permission_Config", {})
        if not perm_conf.get("enable_rate_limit", True):
            return True

        period = int(perm_conf.get("rate_limit_period", 60))
        max_req = int(perm_conf.get("max_requests_per_group", 3))
        now = time.time()
        window_start = now - period

        async with self._rl_lock:
            timestamps = self._rate_limits.get(group_id, [])
            valid_timestamps = [ts for ts in timestamps if ts > window_start]

            if len(valid_timestamps) >= max_req:
                self._rate_limits[group_id] = valid_timestamps
                return False

            valid_timestamps.append(now)
            self._rate_limits[group_id] = valid_timestamps
            return True

    async def update_balance(self, user_id: str, group_id: Optional[str], new_user_bal: int, new_group_bal: int):
        if user_id:
            self.user_counts[str(user_id)] = new_user_bal
            self._dirty_flags.add("user")

        if group_id:
            self.group_counts[str(group_id)] = new_group_bal
            self._dirty_flags.add("group")

    async def record_usage(self, user_id: str, group_id: Optional[str], success: bool):
        if not success:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            self.daily_stats = {"date": today, "users": {}, "groups": {}}

        uid = str(user_id)
        self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1

        if group_id:
            gid = str(group_id)
            self.daily_stats["groups"][gid] = self.daily_stats["groups"].get(gid, 0) + 1

        self._dirty_flags.add("daily")

    async def perform_checkin(self, user_id: str) -> Tuple[bool, int, str]:
        conf = self.global_config.get("Checkin_Config", {})
        if not conf.get("enable_checkin", False):
            return False, 0, "📅 签到功能未开启。"

        uid = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")

        if self.checkin_data.get(uid) == today:
            return False, 0, "📅 您今天已经签到过了。"

        import random
        is_random = str(conf.get("enable_random_checkin", "false")).lower() == "true"
        if is_random:
            base = int(conf.get("checkin_random_reward_max", 5))
            reward = random.randint(1, max(1, base))
        else:
            reward = int(conf.get("checkin_fixed_reward", 3))

        self.checkin_data[uid] = today
        current = self.user_counts.get(uid, 0)
        self.user_counts[uid] = current + reward

        self._dirty_flags.add("checkin")
        self._dirty_flags.add("user")

        return True, reward, f"🎉 签到成功！获得 {reward} 次。"

    # --- 管理员操作 ---

    async def admin_modify_balance(self, target_id: str, delta: int, is_group: bool) -> int:
        target_id = str(target_id)
        if is_group:
            current = self.group_counts.get(target_id, 0)
            new_val = max(0, current + delta)
            self.group_counts[target_id] = new_val
            self._dirty_flags.add("group")
        else:
            current = self.user_counts.get(target_id, 0)
            new_val = max(0, current + delta)
            self.user_counts[target_id] = new_val
            self._dirty_flags.add("user")
        return new_val

    async def admin_set_balance(self, target_id: str, value: int, is_group: bool) -> int:
        target_id = str(target_id)
        new_val = max(0, value)
        if is_group:
            self.group_counts[target_id] = new_val
            self._dirty_flags.add("group")
        else:
            self.user_counts[target_id] = new_val
            self._dirty_flags.add("user")
        return new_val

    # --- 数据持久化 ---

    def _start_auto_save(self):
        async def _loop():
            while True:
                try:
                    await asyncio.sleep(30)
                    await self._flush_data()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[StatsService] 自动保存出错: {e}")
        self._auto_save_task = asyncio.create_task(_loop())

    async def _flush_data(self):
        if not self._dirty_flags:
            return

        tasks = []
        if "user" in self._dirty_flags:
            tasks.append(self._store_user.save(self.user_counts))
        if "group" in self._dirty_flags:
            tasks.append(self._store_group.save(self.group_counts))
        if "checkin" in self._dirty_flags:
            tasks.append(self._store_checkin.save(self.checkin_data))
        if "daily" in self._dirty_flags:
            snapshot = copy.deepcopy(self.daily_stats)
            tasks.append(self._store_daily.save(snapshot))

        if tasks:
            await asyncio.gather(*tasks)
            self._dirty_flags.clear()

    def get_dashboard_data(self) -> Dict[str, Any]:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            return {"date": today, "users": [], "groups": []}

        top_users = sorted(self.daily_stats.get("users", {}).items(), key=lambda x: x[1], reverse=True)[:10]
        top_groups = sorted(self.daily_stats.get("groups", {}).items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "date": today,
            "users": top_users,
            "groups": top_groups
        }