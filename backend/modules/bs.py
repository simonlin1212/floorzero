"""Black-Scholes 希腊字母（只实现 GEX 需要的部分）。

为什么需要自己算 gamma —— CBOE 已经给了 gamma，但那是**当前股价下**的值。
计算 Gamma Flip 要回答的是「**如果**股价变成 X，总 GEX 会是多少」，
而 gamma 随股价变化很大（ATM 最高、两端趋零），必须在假设股价下重算。

只用标准库，不引入 scipy（保持「零重依赖」的项目原则）。
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)
TRADING_DAYS = 365.0          # 用自然日：期权到期按日历日计


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
    return max(dte_days, 0.5) / TRADING_DAYS
