"""Short-sale data: parsing and aggregation.

━━━ ⭐ This section's data is the easiest in the project to read backwards, so three anchors come first ━━━

**① FTD is not a daily flow; it is a cumulative balance** (the SEC's own words):

    "Fails to deliver on a given day are a cumulative number of all fails
     outstanding until that day, plus new fails that occur that day, less fails
     that settle that day. The figure is not a daily amount of fails...
     these numbers reflect aggregate fails as of a specific point in time, and
     may have little or no relationship to yesterday's aggregate fails.
     Thus, it is important to note that the age of fails cannot be determined
     by looking at these numbers."

→ So **daily FTD must not be drawn as a time series of increments**: 1m today against 800k yesterday
  does not mean "200,000 new failures". This module presents the balance itself and its magnitude,
  and **computes no day-on-day change and speaks of no "surge"**.

**② FTD is not evidence of naked shorting** (the SEC's own words):

    "fails-to-deliver can occur for a number of reasons on both long and short
     sales. Therefore, fails-to-deliver are not necessarily the result of short
     selling, and are not evidence of abusive short selling or 'naked' short
     selling."

→ Which is precisely the most popular use of this data in retail circles. **That sentence has to be shown to the user verbatim.**

**③ Off-exchange short volume ≠ short interest** (FINRA's own article, verbatim):

    "Some market participants mistakenly conclude that the bimonthly short
     interest data is understated because the Short Sale Volume Daily File
     reflects volume that is much larger than the positions reported as short
     interest. However, short interest position data does not—and is not
     intended to—equate to the daily short sale volume data."

→ And the file holds **off-exchange** trades only ("all off-exchange short sale trades...
  is not consolidated with exchange data") — using it for a "market-wide short share" is wrong.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

#: The SEC's and FINRA's own words — **shown verbatim, never paraphrased into ours**
OFFICIAL_NOTES = {
    "ftd_cumulative": (
        "SEC guidance, verbatim: \"Fails to deliver on a given day are a cumulative number "
        "of all fails outstanding until that day... The figure is not a daily "
        "amount of fails... may have little or no relationship to yesterday's "
        "aggregate fails. Thus, it is important to note that the age of fails "
        "cannot be determined by looking at these numbers.\" "
        "→ It is a **cumulative balance at a point in time**, not that day's additions. So this page computes no day-on-day change and speaks of no surge."),
    "ftd_not_naked": (
        "SEC guidance, verbatim: \"fails-to-deliver can occur for a number of reasons on "
        "both long and short sales. Therefore, fails-to-deliver are not "
        "necessarily the result of short selling, and are not evidence of "
        "abusive short selling or 'naked' short selling.\" "
        "→ A failure to deliver **can come from a long just as much as a short**, and is not evidence of naked shorting."),
    "volume_not_interest": (
        "FINRA guidance, verbatim: \"short interest position data does not—and is not "
        "intended to—equate to the daily short sale volume data.\" "
        "And the file holds **off-exchange** trades only (not consolidated with exchange data). "
        "→ Short sale volume is a daily flow, and only the off-exchange part of it; "
        "short interest is a twice-monthly snapshot of a standing position. They are different things, "
        "and using the former for a market-wide short share is wrong."),
    "price_caveat": (
        "SEC guidance: the price field is **the previous day's close**, and \"we cannot guarantee that this "
        "price matches closing prices available from other sources\". "
        "The value estimates on this page follow from it and indicate magnitude only."),
}


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() in ("", "."):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _parse_date(v: Optional[str]) -> Optional[date]:
    """Parse `20260615` (FTD) or `2026-06-15`."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Fail:
    """One fail-to-deliver record (a symbol's **cumulative balance** on a settlement date)."""

    settlement_date: Optional[date]
    cusip: str
    symbol: str
    description: str
    quantity: Optional[float]        # the cumulative balance in shares, **not that day's additions**
    price: Optional[float]           # the previous day's close; the SEC does not guarantee it matches other sources

    @property
    def value(self) -> Optional[float]:
        """Notional = balance × previous close. Indicative of magnitude only (see price_caveat for the caveat)."""
        if self.quantity is None or self.price is None:
            return None
        return self.quantity * self.price


def parse_ftd(row: dict) -> Optional[Fail]:
    """One FTD row → a Fail. Returns None when fields are missing (the file ends with explanatory lines)."""
    sym = (row.get("SYMBOL") or "").strip().upper()
    cusip = (row.get("CUSIP") or "").strip().upper()
    if not sym and not cusip:
        return None
    d = _parse_date(row.get("SETTLEMENT DATE"))
    if d is None:
        return None
    return Fail(
        settlement_date=d, cusip=cusip, symbol=sym,
        description=(row.get("DESCRIPTION") or "").strip(),
        quantity=_num(row.get("QUANTITY (FAILS)")),
        price=_num(row.get("PRICE")),
    )


def to_dict(f: Fail) -> dict:
    return {
        "settlement_date": f.settlement_date.isoformat() if f.settlement_date else None,
        "cusip": f.cusip, "symbol": f.symbol, "description": f.description,
        "quantity": f.quantity, "price": f.price, "value": f.value,
    }


@dataclass(frozen=True)
class ShortVolume:
    """FINRA's **off-exchange** short sale volume for one day (not a position, and not including exchange trades)."""

    trade_date: Optional[date]
    symbol: str
    short_volume: Optional[float]
    short_exempt_volume: Optional[float]
    total_volume: Optional[float]
    market: str

    @property
    def short_pct(self) -> Optional[float]:
        """Short volume as a share of **off-exchange** volume.

        ⚠️ The denominator is off-exchange volume alone and is **not market-wide volume** — FINRA states
        that this file "is not consolidated with exchange data". Reading it as a market-wide short share is wrong.
        """
        if not self.total_volume or self.short_volume is None:
            return None
        return self.short_volume / self.total_volume * 100.0


def parse_short_volume(row: dict, market: str) -> Optional[ShortVolume]:
    sym = (row.get("Symbol") or row.get("SYMBOL") or "").strip().upper()
    if not sym:
        return None
    return ShortVolume(
        trade_date=_parse_date(row.get("Date") or row.get("DATE")),
        symbol=sym,
        short_volume=_num(row.get("ShortVolume")),
        short_exempt_volume=_num(row.get("ShortExemptVolume")),
        total_volume=_num(row.get("TotalVolume")),
        market=market,
    )


def sv_to_dict(s: ShortVolume) -> dict:
    return {
        "trade_date": s.trade_date.isoformat() if s.trade_date else None,
        "symbol": s.symbol, "short_volume": s.short_volume,
        "short_exempt_volume": s.short_exempt_volume,
        "total_volume": s.total_volume, "short_pct": s.short_pct,
        "market": s.market,
    }
