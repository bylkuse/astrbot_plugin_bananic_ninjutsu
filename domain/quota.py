from dataclasses import dataclass
from typing import Optional

@dataclass
class QuotaContext:
    user_id: str
    group_id: Optional[str]
    is_admin: bool
    user_balance: int
    group_balance: int

    user_blacklist: list[str]
    group_blacklist: list[str]
    user_whitelist: list[str]
    group_whitelist: list[str]
    enable_user_limit: bool
    enable_group_limit: bool

@dataclass
class QuotaTransaction:
    allowed: bool = False
    reject_reason: str = ""
    cost: int = 1

    real_cost: int = 0
    is_free: bool = False

    _deducted_user: bool = False
    _deducted_group: bool = False
    _committed: bool = False

    def check_permission(self, ctx: QuotaContext, cost: int) -> bool:
        self.cost = cost
        self.real_cost = 0

        # 管理员特权
        if ctx.is_admin:
            self.allowed = True
            self.is_free = True
            return True

        if ctx.user_id in ctx.user_blacklist:
            self.reject_reason = "❌ 您已被加入黑名单。"
            return False
        if ctx.group_id and ctx.group_id in ctx.group_blacklist:
            self.reject_reason = "❌ 本群已被加入黑名单。"
            return False

        if ctx.user_whitelist and ctx.user_id not in ctx.user_whitelist:
            self.reject_reason = "❌ 您不在白名单中。"
            return False
        if ctx.group_id and ctx.group_whitelist and ctx.group_id not in ctx.group_whitelist:
            self.reject_reason = "❌ 本群不在白名单中。"
            return False


        # 用户支付能力
        can_user_pay = (not ctx.enable_user_limit) or (ctx.user_balance >= cost)

        # 群组支付能力：
        can_group_pay = False
        if ctx.group_id and ctx.enable_group_limit:
            can_group_pay = ctx.group_balance >= cost

        # 综合判定
        self.allowed = can_user_pay or can_group_pay

        # 全免
        if not ctx.enable_user_limit and not ctx.enable_group_limit:
            self.is_free = True

        if not self.allowed:
            self.reject_reason = f"❌ 次数不足 (需要 {cost} 次)。"
            return False

        return True

    def commit(self, ctx: QuotaContext) -> tuple[int, int]:
        if not self.allowed or self._committed:
            return ctx.user_balance, ctx.group_balance

        if self.is_free:
            self._committed = True
            self.real_cost = 0
            return ctx.user_balance, ctx.group_balance

        u_bal = ctx.user_balance
        g_bal = ctx.group_balance

        deducted = False

        # 扣费优先级
        if ctx.group_id and ctx.enable_group_limit and g_bal >= self.cost:
            g_bal -= self.cost
            self._deducted_group = True
            deducted = True
            self.real_cost = self.cost

        elif ctx.enable_user_limit and u_bal >= self.cost:
            u_bal -= self.cost
            self._deducted_user = True
            deducted = True
            self.real_cost = self.cost

        if deducted:
            self._committed = True
        else:
            self._committed = True
            self.real_cost = 0

        return u_bal, g_bal

    def rollback(self):
        self._committed = False
        self.real_cost = 0