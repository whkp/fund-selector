"""限流内核：进程内固定窗口计数器。

为什么又手写一遍而不是装 `slowapi` / `limits`：和 `security.py` 同一个理由 ——
这个项目的 AKShare 依赖链已经让装包变得昂贵，限流是最不该再引入第三方包的地方。
固定窗口计数器的全部逻辑就是「一个键、一个计数、一个窗口起点」，标准库足够。

**作用域是单进程内存。** 当前两种部署（本机 + Cloudflare 隧道、Render 单实例）
都只有一个 worker，这就是全部所需。将来上多副本时，只需把 `_buckets` 换成
Redis 实现，`RateLimiter` 的公开方法签名不用动。

三类被保护的动作，对应三类不同的攻击：

- 登录 / 注册 → 按 IP 限总频率（挡 PBKDF2 算力耗尽），按账号限失败次数（挡口令爆破）
- 模型研究 → 按用户限小时/天配额，外加全局日兜底（挡服务端 API Key 被刷爆）
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .config import config_value

# 桶超过这个时间没被访问就到点清理，避免长期运行后 keys 无限增长。
_SWEEP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int
    label: str = ""

    @property
    def enabled(self) -> bool:
        """上限配成 0 或负数表示**停用该规则**，便于本地调试。"""
        return self.limit > 0 and self.window_seconds > 0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


_ALLOW = RateLimitDecision(allowed=True, limit=0, remaining=0, retry_after=0)


class RateLimiter:
    """固定窗口计数器。

    固定窗口的已知代价：窗口边界两侧各放行一批，瞬时峰值可达上限的两倍。
    对这里的目标（限制单个账号的爆破次数、限制单个用户的模型调用总量）完全够用，
    因为真正兜底的是长窗口的每日配额，短窗口只负责削峰。
    """

    def __init__(self) -> None:
        # key -> [窗口起点 epoch, 已消费次数]
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _window_start(now: float, window_seconds: int) -> float:
        return (now // window_seconds) * window_seconds

    def _sweep_locked(self, now: float) -> None:
        """调用方必须已持有锁。只清理明确过期的桶。"""
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        stale = [key for key, (start, _) in self._buckets.items() if now - start > 86400]
        for key in stale:
            self._buckets.pop(key, None)

    def _allow(self, rule: RateLimitRule, consumed: float) -> RateLimitDecision:
        return RateLimitDecision(True, rule.limit, max(int(rule.limit - consumed), 0), 0)

    @staticmethod
    def _deny(rule: RateLimitRule, start: float, now: float) -> RateLimitDecision:
        return RateLimitDecision(False, rule.limit, 0, max(int(start + rule.window_seconds - now), 1))

    # -- 公开 API -----------------------------------------------------------

    def peek(self, key: str, rule: RateLimitRule) -> RateLimitDecision:
        """只看不消费：预测「下一次 hit 会不会成功」。

        判据是 `count >= limit`（已经用满），而不是 `count > limit` ——
        hit 成功时最多把 count 推到 limit，所以用 `>` 的话这个前置检查
        永远不会拒绝任何请求，等于白写。
        """
        if not rule.enabled:
            return _ALLOW
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self._allow(rule, 0)
            start, count = bucket
            if self._window_start(now, rule.window_seconds) != start:
                return self._allow(rule, 0)
            if count >= rule.limit:
                return self._deny(rule, start, now)
            return self._allow(rule, count)

    def hit(self, key: str, rule: RateLimitRule) -> RateLimitDecision:
        """消费一次配额并返回决定。

        并发安全：判断与自增在同一个锁内完成，所以 N 个并发请求不可能同时
        通过「还剩 1 次」的检查 —— 这正是把配额放在 LLM 调用之前的意义。

        超限时**不再累加**计数：持续攻击不该把计数器越推越高，反正 retry_after
        是按窗口结束时间算的，与计数无关。
        """
        if not rule.enabled:
            return _ALLOW
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            start, count = self._buckets.get(key, (self._window_start(now, rule.window_seconds), 0.0))
            if self._window_start(now, rule.window_seconds) != start:
                start, count = self._window_start(now, rule.window_seconds), 0.0
            if count + 1 > rule.limit:
                return self._deny(rule, start, now)
            count += 1
            self._buckets[key] = [start, count]
            return self._allow(rule, count)

    def refund(self, key: str, rule: RateLimitRule) -> None:
        """退还一次消费。用于「占位之后这步失败了」的补偿。

        只减当前窗口的计数并夹到 0 —— 窗口若已滚动，这次退还没有意义，
        但绝不能让计数变成负数去凭空放大后续额度。
        """
        if not rule.enabled:
            return
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return
            start, count = bucket
            if self._window_start(now, rule.window_seconds) != start:
                return
            self._buckets[key] = [start, max(count - 1, 0.0)]

    def reset(self, key: str) -> None:
        """清空某个键。登录成功后抹掉失败计数靠这个。"""
        with self._lock:
            self._buckets.pop(key, None)

    def reset_all(self) -> None:
        """清空全部计数。仅供测试在每个用例之间隔离。"""
        with self._lock:
            self._buckets.clear()
            self._last_sweep = time.monotonic()

    def count(self, key: str, rule: RateLimitRule) -> int:
        """当前窗口内已消费的次数。只读，供运维排查与测试断言。"""
        if not rule.enabled:
            return 0
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 0
            start, value = bucket
            if self._window_start(now, rule.window_seconds) != start:
                return 0
            return int(value)


limiter = RateLimiter()


# ---------------------------------------------------------------------------
# 规则：全部可在运行时被环境变量或 config 文件覆盖
# ---------------------------------------------------------------------------

def _rule(name: str, env_suffix: str, default_limit: int, default_window: int, label: str) -> RateLimitRule:
    """从配置读取一条规则。

    刻意做成函数而不是模块级常量：测试需要在运行时注入小上限，如果在 import
    时就把值定死，`monkeypatch.setenv` 将完全不起作用。
    """
    limit = config_value(
        "security", f"rate_limit_{name}_max", default_limit,
        env_name=f"FUND_COMPASS_RATE_LIMIT_{env_suffix}_MAX",
    )
    window = config_value(
        "security", f"rate_limit_{name}_window_seconds", default_window,
        env_name=f"FUND_COMPASS_RATE_LIMIT_{env_suffix}_WINDOW_SECONDS",
    )
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        limit_value = default_limit
    try:
        window_value = int(window)
    except (TypeError, ValueError):
        window_value = default_window
    return RateLimitRule(max(limit_value, 0), max(window_value, 1), label)


def login_ip_rule() -> RateLimitRule:
    """登录的总尝试频率。单个出口 IP 5 分钟 30 次 —— 正常人手速远达不到，
    但足以让「用一个邮箱反复触发热 PBKDF2」打不出 CPU 尖峰。"""
    return _rule("login_ip", "LOGIN_IP", 30, 300, "登录尝试")


def login_account_rule() -> RateLimitRule:
    """单个账号 15 分钟内允许 5 次失败。挡的是针对已知邮箱的慢速口令爆破。"""
    return _rule("login_account", "LOGIN_ACCOUNT", 5, 900, "账号登录失败")


def register_ip_rule() -> RateLimitRule:
    """单个 IP 每小时 10 次注册尝试。挡的是拿脚本批量试邀请码。"""
    return _rule("register_ip", "REGISTER_IP", 10, 3600, "注册尝试")


def register_global_rule() -> RateLimitRule:
    """全站每小时 200 次注册尝试。挡的是换 IP 的分布式邀请码爆破。

    比按 IP 宽得多：邀请制下正常注册量很小，这个上限只会在被攻击时才会碰到。
    """
    return _rule("register_global", "REGISTER_GLOBAL", 200, 3600, "全站注册尝试")


def research_user_hourly_rule() -> RateLimitRule:
    return _rule("research_user_hourly", "RESEARCH_USER_HOURLY", 20, 3600, "每小时研究调用")


def research_user_daily_rule() -> RateLimitRule:
    return _rule("research_user_daily", "RESEARCH_USER_DAILY", 100, 86400, "每日研究调用")


def research_global_daily_rule() -> RateLimitRule:
    """全站每日兜底。服务端 API Key 是共享钱包，这一条是最后一道闸。"""
    return _rule("research_global_daily", "RESEARCH_GLOBAL_DAILY", 500, 86400, "全站每日研究调用")
