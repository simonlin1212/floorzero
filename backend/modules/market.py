"""宏观：收益率曲线与持仓报告。

━━━ ⚠️ 两个口径必须讲清 ━━━

**① 「倒挂」要说清是哪一个口径。** 市场常用两条利差：
`10Y − 2Y` 与 `10Y − 3M`，**它们的倒挂时点可以差好几个月**。
本模块两条都算、都显示，**不挑一条当「那个」倒挂**。

**② COT 有三天时滞。** 报告的是**周二**收盘的持仓，**周五**下午才发布 ——
看到的永远是三天前的状态，不是当下。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sources.macro import TENOR_LABEL, TENORS

#: 两条利差口径 —— 都算，不挑一条当「那个倒挂」
SPREADS = {
    "10Y-2Y": ("BC_10YEAR", "BC_2YEAR"),
    "10Y-3M": ("BC_10YEAR", "BC_3MONTH"),
    "30Y-10Y": ("BC_30YEAR", "BC_10YEAR"),
}

NOTES = {
    "inversion": (
        "「收益率曲线倒挂」必须说清口径：`10Y−2Y` 与 `10Y−3M` 是两条不同的利差，"
        "**倒挂时点可以差好几个月**。本页两条都显示，不替你挑一条当「那个倒挂」。"
        "倒挂是历史上常被提及的衰退相关指标，但**本页只呈现利差数值，不做任何预测**。"),
    "cot_lag": (
        "CFTC 持仓报告有**三天时滞**：报告的是**周二收盘**的持仓，**周五**下午才发布。"
        "看到的永远是三天前的状态。"),
    "cot_scope": (
        "TFF（Traders in Financial Futures）只覆盖**金融期货**"
        "（利率、股指、外汇等），不含农产品与能源 —— 那些在另外的报告里。"),
}


def _f(v) -> Optional[float]:
    return None if v in (None, "") else float(v)


@dataclass(frozen=True)
class CurvePoint:
    """某日的完整收益率曲线。"""

    date: str
    yields: dict[str, Optional[float]]     # BC_* → 百分比

    def spread(self, name: str) -> Optional[float]:
        pair = SPREADS.get(name)
        if not pair:
            return None
        a, b = self.yields.get(pair[0]), self.yields.get(pair[1])
        if a is None or b is None:
            return None
        return a - b

    @property
    def is_inverted(self) -> dict[str, Optional[bool]]:
        """每条口径各自是否倒挂 —— **刻意返回字典而不是一个布尔值**。

        把两条口径压成一个「倒挂了吗」的答案，就是在替读者做那个
        「用哪条口径」的判断，而这恰恰是最容易出分歧的地方。
        """
        out = {}
        for k in SPREADS:
            s = self.spread(k)
            out[k] = None if s is None else s < 0
        return out


def to_dict(p: CurvePoint) -> dict:
    return {
        "date": p.date,
        "yields": {TENOR_LABEL[k]: v for k, v in p.yields.items() if k in TENOR_LABEL},
        "raw": p.yields,
        "spreads": {k: p.spread(k) for k in SPREADS},
        "inverted": p.is_inverted,
    }


def parse_curve(row: dict) -> Optional[CurvePoint]:
    d = (row.get("date") or "").strip()
    if not d:
        return None
    ys = {k: _f(row.get(k)) for k in TENORS}
    if not any(v is not None for v in ys.values()):
        return None
    return CurvePoint(date=d, yields=ys)


def curve_series(points: list[CurvePoint]) -> dict:
    """利差时间序列 + 当前状态。"""
    pts = sorted(points, key=lambda p: p.date)
    series = {k: [(p.date, p.spread(k)) for p in pts] for k in SPREADS}
    latest = pts[-1] if pts else None
    return {
        "dates": [p.date for p in pts],
        "spreads": {k: [v for _, v in series[k]] for k in SPREADS},
        "latest": to_dict(latest) if latest else None,
        "tenors": [TENOR_LABEL[k] for k in TENORS],
        "notes": NOTES,
    }


# ─────────────────────────── COT ───────────────────────────

@dataclass(frozen=True)
class CotRow:
    """一条 TFF 持仓记录（杠杆基金 / 资产管理 / 交易商 三类）。"""

    market: str
    report_date: Optional[str]
    lev_long: Optional[float]
    lev_short: Optional[float]
    asset_long: Optional[float]
    asset_short: Optional[float]
    dealer_long: Optional[float]
    dealer_short: Optional[float]
    open_interest: Optional[float]

    @property
    def lev_net(self) -> Optional[float]:
        if self.lev_long is None or self.lev_short is None:
            return None
        return self.lev_long - self.lev_short

    @property
    def asset_net(self) -> Optional[float]:
        if self.asset_long is None or self.asset_short is None:
            return None
        return self.asset_long - self.asset_short


def parse_cot(r: dict) -> Optional[CotRow]:
    m = (r.get("market_and_exchange_names") or "").strip()
    if not m:
        return None
    d = (r.get("report_date_as_yyyy_mm_dd") or "")[:10] or None
    return CotRow(
        market=m, report_date=d,
        lev_long=_f(r.get("lev_money_positions_long")),
        lev_short=_f(r.get("lev_money_positions_short")),
        asset_long=_f(r.get("asset_mgr_positions_long")),
        asset_short=_f(r.get("asset_mgr_positions_short")),
        dealer_long=_f(r.get("dealer_positions_long_all")),
        dealer_short=_f(r.get("dealer_positions_short_all")),
        open_interest=_f(r.get("open_interest_all")),
    )


def cot_to_dict(c: CotRow) -> dict:
    return {
        "market": c.market, "report_date": c.report_date,
        "lev_long": c.lev_long, "lev_short": c.lev_short, "lev_net": c.lev_net,
        "asset_long": c.asset_long, "asset_short": c.asset_short,
        "asset_net": c.asset_net,
        "dealer_long": c.dealer_long, "dealer_short": c.dealer_short,
        "open_interest": c.open_interest,
    }
