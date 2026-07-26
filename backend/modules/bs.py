"""Black-Scholes 希腊字母（只实现 GEX 需要的部分）。

为什么需要自己算 gamma —— CBOE 已经给了 gamma，但那是**当前股价下**的值。
计算 Gamma Flip 要回答的是「**如果**股价变成 X，总 GEX 会是多少」，
而 gamma 随股价变化很大（ATM 最高、两端趋零），必须在假设股价下重算。

只用标准库，不引入 scipy（保持「零重依赖」的项目原则）。
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)
# ⚠️ 用**自然日**（365），不是交易日（252）：期权到期按日历日计，周末也在衰减。
# 名字别写成 TRADING_DAYS —— 那会让人（和代码审查）误以为是 252，
# 进而把别处的 /365 换算误判成不一致。Codex 审计就在这里读错过一次。
DAYS_PER_YEAR = 365.0


def norm_pdf(x: float) -> float:
    """标准正态概率密度 φ(x)。"""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_gamma(spot: float, strike: float, t_years: float,
             sigma: float, rate: float = 0.04) -> float:
    """Black-Scholes gamma。

    gamma = φ(d1) / (S · σ · √T)

    call 和 put 的 gamma **相同**（put-call parity 的直接推论），所以不分类型。
    退化情况（到期、零波动率、无效价格）返回 0 而不是抛异常 ——
    这些在真实期权链里天天出现（已到期合约、报价缺失的深度虚值合约）。
    """
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    try:
        d1 = ((math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years)
              / (sigma * math.sqrt(t_years)))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return norm_pdf(d1) / (spot * sigma * math.sqrt(t_years))


def years_to_expiry(dte_days: float) -> float:
    """天数 → 年（0DTE 按半个交易日算，避免 T=0 让 gamma 爆成 inf）。"""
    return max(dte_days, 0.5) / DAYS_PER_YEAR

def _d1_d2(spot: float, strike: float, t_years: float,
           sigma: float, rate: float) -> tuple[float, float] | None:
    """BS 的 d1/d2；退化输入返回 None（调用方据此返回 0）。"""
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return None
    try:
        sq = sigma * math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / sq
        return d1, d1 - sq
    except (ValueError, ZeroDivisionError):
        return None


def bs_vanna(spot: float, strike: float, t_years: float,
             sigma: float, rate: float = 0.04) -> float:
    """Vanna = ∂delta/∂σ = ∂vega/∂S。

        vanna = -φ(d1) · d2 / σ

    含义：隐含波动率变动时，delta 会怎么变 —— 做市商为保持 delta 中性，
    会因 IV 涨跌而买卖正股。这是「慢速碾压式上涨」的主要推手之一。

    ⚠️ call 与 put 的 vanna **相同**（无股息时）。已用有限差分数值验证。
    """
    dd = _d1_d2(spot, strike, t_years, sigma, rate)
    if dd is None:
        return 0.0
    d1, d2 = dd
    return -norm_pdf(d1) * d2 / sigma


def bs_charm(spot: float, strike: float, t_years: float,
             sigma: float, rate: float = 0.04) -> float:
    """Charm = ∂delta/∂t，**t = 已流逝时间**（不是剩余时间 τ）。

        charm = -φ(d1) · (2rT - d2·σ√T) / (2T·σ√T)

    含义：即使股价和波动率都不动，delta 也会随时间流逝而改变，
    做市商必须相应调仓 —— 这是「尾盘钉住」现象的来源之一。

    ⚠️ **符号约定：公式里那个前导负号已经把 τ→t 的方向换过来了**，
    返回值直接就是「每流逝一单位时间，delta 变化多少」，调用方**不要再取反**。
    （曾把本函数文档写成 ∂delta/∂τ，导致代码审查据此判定符号反了 ——
     数值验证结论：30 天虚值 call 过一天 delta 实际变化 −0.003911，
     本函数 /365 后给 −0.003832，同号吻合；取反则完全错误。
     `h=1e-7` 的有限差分与解析值差 <1e-3。）

    ⚠️ call 与 put 的 charm **相同**（无股息时：put delta = call delta − 1，
    常数项对时间求导为 0）。网上流传的「put 取相反符号」是错的 ——
    已用有限差分数值验证：解析值与 ∂delta/∂τ 数值微分在 call/put 上均吻合，
    取反后则不符。
    """
    dd = _d1_d2(spot, strike, t_years, sigma, rate)
    if dd is None:
        return 0.0
    d1, d2 = dd
    sq = sigma * math.sqrt(t_years)
    return -norm_pdf(d1) * (2 * rate * t_years - d2 * sq) / (2 * t_years * sq)
