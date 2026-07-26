"""GEX（Gamma Exposure，伽马敞口）计算。

对标 Unusual Whales 的 Gex/Greeks 分类（11 个端点）。

━━━ GEX 是什么 ━━━
做市商卖期权给散户后必须对冲，股价每动一点就要买/卖对应数量的正股来保持中性。
GEX 衡量的就是「股价每变动 1%，做市商需要买卖多少名义金额的正股」。

· GEX 为**正** → 做市商 long gamma → 涨了要卖、跌了要买 → **抑制波动**（价格被"钉住"）
· GEX 为**负** → 做市商 short gamma → 涨了要追买、跌了要杀跌 → **放大波动**
· **Gamma Flip** = GEX 由正转负的价位，被视为市场结构的分水岭

━━━ ⚠️ 一个必须说清的假设 ━━━
GEX 的计算需要知道「做市商在每个合约上是多头还是空头」，而这个信息**市场上不公开**。
业界通用做法（SqueezeMetrics 提出、gex-tracker 等主流实现沿用）是假设：
    **做市商对 call 是 long gamma，对 put 是 short gamma**
即 call 记正、put 记负。这是**假设不是事实** —— 真实持仓方向无从得知。
本模块用 `dealer_convention` 参数把这个假设显式化，并在输出里标注，
而不是把它藏进代码当成真理。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from sources.cboe import Chain, Contract
from modules import bs

CONTRACT_SIZE = 100          # 美股 1 张期权 = 100 股

DealerConvention = Literal["long_call_short_put", "short_call_long_put"]


@dataclass(frozen=True)
class StrikeGex:
    strike: float
    call_gex: float
    put_gex: float
    net_gex: float
    call_oi: float
    put_oi: float


@dataclass(frozen=True)
class GexProfile:
    """一次 GEX 计算的完整结果。"""
    ticker: str
    spot: float
    total_gex: float                  # 名义美元 / 1% 变动
    by_strike: tuple[StrikeGex, ...]
    by_expiry: tuple[tuple[str, float], ...]
    gamma_flip: Optional[float]       # GEX 由正转负的价位（None = 区间内未翻转）
    call_wall: Optional[float]        # call GEX 最大的行权价（常表现为阻力）
    put_wall: Optional[float]         # put GEX 最负的行权价（常表现为支撑）
    convention: str
    scope: str                        # 本次计算覆盖了哪些合约


def contract_gex(c: Contract, spot: float,
                 convention: DealerConvention = "long_call_short_put") -> float:
    """单合约 GEX（名义美元 / 标的每 1% 变动）。

        GEX = gamma × OI × 100 × spot² × 0.01

    spot² 的来历：gamma 是「delta 对价格的二阶导」，乘一次 spot 把它换成
    「价格变动 1 单位时 delta 的变化量」，再乘一次 spot 换成名义美元；
    ×0.01 是把「1 美元变动」换算成「1% 变动」。
    """
    if not c.gamma or not c.open_interest:
        return 0.0
    raw = c.gamma * c.open_interest * CONTRACT_SIZE * spot * spot * 0.01
    if convention == "long_call_short_put":
        return raw if c.type == "call" else -raw
    return -raw if c.type == "call" else raw


def total_gex_at(contracts: Iterable[Contract], hypo_spot: float,
                 convention: DealerConvention = "long_call_short_put",
                 rate: float = 0.04) -> float:
    """假设股价变成 hypo_spot 时的总 GEX。

    ⚠️ 必须用 Black-Scholes **重算** gamma，不能复用 CBOE 给的那个 ——
    那是当前股价下的 gamma，而 gamma 随股价剧烈变化（ATM 最高、两端趋零）。
    直接拿现成 gamma 去套不同股价，算出来的 flip 是错的。
    """
    total = 0.0
    for c in contracts:
        if not c.open_interest or not c.iv or c.iv <= 0:
            continue
        g = bs.bs_gamma(hypo_spot, c.strike, bs.years_to_expiry(c.dte), c.iv, rate)
        if not g:
            continue
        raw = g * c.open_interest * CONTRACT_SIZE * hypo_spot * hypo_spot * 0.01
        if convention == "long_call_short_put":
            total += raw if c.type == "call" else -raw
        else:
            total += -raw if c.type == "call" else raw
    return total


def _find_gamma_flip(contracts: list[Contract], spot: float,
                     convention: DealerConvention,
                     search_pct: float = 0.10, steps: int = 60,
                     rate: float = 0.04) -> Optional[float]:
    """求 Gamma Flip：总 GEX 由正转负的那个股价。

    做法：在 spot ±search_pct 区间扫描假设股价，每个点用 BS 重算全链 GEX，
    找符号变化处再二分细化。找不到符号变化就返回 None ——
    **不硬凑一个数字**（很多标的在合理区间内确实不翻转）。
    """
    lo, hi = spot * (1 - search_pct), spot * (1 + search_pct)
    xs = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    vals = [total_gex_at(contracts, x, convention, rate) for x in xs]

    # ⚠️ 曲线在搜索区间内可能穿越零点**多次**（call/put OI 混杂时常见）。
    # 从低价往高扫「取第一个」会报出一个远离现价的交叉点 —— 那不是市场结构的分水岭。
    # 正确做法：收集所有交叉，取**离现价最近**的那个。
    crossings = []
    for i in range(1, len(xs)):
        a, b = vals[i - 1], vals[i]
        if (a > 0 >= b) or (a < 0 <= b):
            crossings.append((xs[i - 1], xs[i], a, b))
    if not crossings:
        return None
    cross = min(crossings, key=lambda c: abs((c[0] + c[1]) / 2 - spot))

    x0, x1, v0, _ = cross
    for _ in range(40):                       # 二分细化到约 1e-4 精度
        mid = (x0 + x1) / 2
        vm = total_gex_at(contracts, mid, convention, rate)
        if (v0 > 0 >= vm) or (v0 < 0 <= vm):
            x1 = mid
        else:
            x0, v0 = mid, vm
    return (x0 + x1) / 2


def compute(chain: Chain,
            expiry: Optional[str] = None,
            dte_max: Optional[int] = None,
            strike_pct: float = 0.15,
            convention: DealerConvention = "long_call_short_put") -> GexProfile:
    """计算 GEX 画像。

    expiry / dte_max：限定到期日（'0DTE' = 当日到期）。都不传 = 全链。
    strike_pct：只统计现价 ±该比例内的行权价（默认 ±15%，远端 OI 对 GEX 影响可忽略
                但会污染 call/put wall 的识别）。
    """
    spot = chain.spot
    lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)

    cs = chain.filter(expiry=expiry, dte_max=dte_max)
    cs = [c for c in cs if lo <= c.strike <= hi]
    if not cs:
        scope = expiry or (f"≤{dte_max}DTE" if dte_max is not None else "全链")
        raise ValueError(f"{chain.ticker} 在 {scope} / ±{strike_pct:.0%} 范围内无合约")

    agg: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    by_exp: dict[str, float] = defaultdict(float)
    for c in cs:
        g = contract_gex(c, spot, convention)
        slot = agg[c.strike]
        if c.type == "call":
            slot[0] += g
            slot[2] += c.open_interest
        else:
            slot[1] += g
            slot[3] += c.open_interest
        by_exp[c.expiry] += g

    rows = [StrikeGex(strike=k, call_gex=v[0], put_gex=v[1],
                      net_gex=v[0] + v[1], call_oi=v[2], put_oi=v[3])
            for k, v in sorted(agg.items())]

    call_wall = max(rows, key=lambda r: r.call_gex).strike if rows else None
    put_wall = min(rows, key=lambda r: r.put_gex).strike if rows else None
    # call_gex 全为 0（或全为负）时「最大值」没有意义，别硬报一个墙
    if call_wall is not None and max(r.call_gex for r in rows) <= 0:
        call_wall = None
    if put_wall is not None and min(r.put_gex for r in rows) >= 0:
        put_wall = None

    scope = expiry or (f"≤{dte_max}DTE" if dte_max is not None else "全链")
    return GexProfile(
        ticker=chain.ticker,
        spot=spot,
        total_gex=sum(r.net_gex for r in rows),
        by_strike=tuple(rows),
        by_expiry=tuple(sorted(by_exp.items())),
        gamma_flip=_find_gamma_flip(cs, spot, convention),
        call_wall=call_wall,
        put_wall=put_wall,
        convention=convention,
        scope=f"{scope} · 行权价 ±{strike_pct:.0%} · {len(cs)} 个合约",
    )


def to_dict(p: GexProfile) -> dict:
    """转成 API / 前端用的 JSON 结构（单位统一为十亿美元，前端不用再换算）。"""
    B = 1e9
    return {
        "ticker": p.ticker,
        "spot": round(p.spot, 2),
        "total_gex_bn": round(p.total_gex / B, 4),
        "gamma_flip": round(p.gamma_flip, 2) if p.gamma_flip else None,
        "call_wall": p.call_wall,
        "put_wall": p.put_wall,
        # 零敞口（全链 gamma 缺失，或 call/put 恰好抵消）既不是正也不是负 ——
        # 报成 negative 会让界面说「做市商对冲会放大波动」，那是无中生有。
        "regime": ("positive" if p.total_gex > 0
                   else "negative" if p.total_gex < 0 else "neutral"),
        "by_strike": [
            {"strike": r.strike,
             "call_gex_bn": round(r.call_gex / B, 4),
             "put_gex_bn": round(r.put_gex / B, 4),
             "net_gex_bn": round(r.net_gex / B, 4),
             "call_oi": r.call_oi, "put_oi": r.put_oi}
            for r in p.by_strike
        ],
        "by_expiry": [{"expiry": e, "gex_bn": round(v / B, 4)} for e, v in p.by_expiry],
        "meta": {
            "convention": p.convention,
            "convention_note": (
                "做市商持仓方向不公开，此处按业界通用假设：call 记正 / put 记负。"
                "这是假设，不是事实。"
            ),
            "scope": p.scope,
            "unit": "十亿美元 / 标的每 1% 变动",
        },
    }
