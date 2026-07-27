"""Macro: the yield curve and the positioning report.

━━━ ⚠️ Two definitions that have to be spelled out ━━━

**① "Inversion" has to say which spread.** Two are in common use:
`10Y − 2Y` and `10Y − 3M`, and **they can invert months apart**.
This module computes and shows both, and **crowns neither "the" inversion**.

**② COT runs three days behind.** It reports positions as of **Tuesday's** close and is published **Friday** afternoon —
what you see is always the state of three days ago, never the present.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sources.macro import TENOR_LABEL, TENORS

#: The two spreads — both computed, with neither crowned "the" inversion
SPREADS = {
    "10Y-2Y": ("BC_10YEAR", "BC_2YEAR"),
    "10Y-3M": ("BC_10YEAR", "BC_3MONTH"),
    "30Y-10Y": ("BC_30YEAR", "BC_10YEAR"),
}

NOTES = {
    "inversion": (
        "\"Yield curve inversion\" has to say which spread: `10Y−2Y` and `10Y−3M` are two different spreads "
        "and **they can invert months apart**. This page shows both and does not pick one for you. "
        "Inversion is an indicator often cited in connection with recessions, but **this page presents the spread values only and predicts nothing**."),
    "cot_lag": (
        "The CFTC positioning report runs **three days behind**: it reports positions as of **Tuesday's close** and is published **Friday** afternoon. "
        "What you see is always the state of three days ago."),
    "cot_scope": (
        "TFF (Traders in Financial Futures) covers **financial futures** only "
        "(rates, equity indices, FX and so on), not agriculture or energy — those are in other reports."),
}


def _f(v) -> Optional[float]:
    return None if v in (None, "") else float(v)


@dataclass(frozen=True)
class CurvePoint:
    """One day's complete yield curve."""

    date: str
    yields: dict[str, Optional[float]]     # BC_* → per cent

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
        """Whether each spread is inverted — **deliberately a dict rather than a single boolean**.

        Flattening two spreads into one answer to "is it inverted" makes the
        "which spread" judgement on the reader's behalf, and that is exactly where opinions divide.
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
    """The spread time series plus the current state."""
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
    """One TFF positioning record (leveraged funds / asset managers / dealers)."""

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
