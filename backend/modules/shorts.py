"""做空数据：解析与聚合。

━━━ ⭐ 这个分栏的数据是全项目最容易被读反的，三条锚点先摆出来 ━━━

**① FTD 不是日频流量，是累计余额**（SEC 官方说明原文）：

    "Fails to deliver on a given day are a cumulative number of all fails
     outstanding until that day, plus new fails that occur that day, less fails
     that settle that day. The figure is not a daily amount of fails...
     these numbers reflect aggregate fails as of a specific point in time, and
     may have little or no relationship to yesterday's aggregate fails.
     Thus, it is important to note that the age of fails cannot be determined
     by looking at these numbers."

→ 所以**不能把逐日 FTD 当成时间序列画增量**：今天 100 万、昨天 80 万，
  不代表"新增了 20 万笔交割失败"。本模块只呈现余额本身与它的量级，
  **不计算日环比、不谈"激增"**。

**② FTD 不是裸卖空的证据**（SEC 官方说明原文）：

    "fails-to-deliver can occur for a number of reasons on both long and short
     sales. Therefore, fails-to-deliver are not necessarily the result of short
     selling, and are not evidence of abusive short selling or 'naked' short
     selling."

→ 这恰恰是该数据在散户圈最流行的用法。**必须把这句话原样显示给用户。**

**③ 场外空头成交量 ≠ 空头持仓**（FINRA 官方文章原文）：

    "Some market participants mistakenly conclude that the bimonthly short
     interest data is understated because the Short Sale Volume Daily File
     reflects volume that is much larger than the positions reported as short
     interest. However, short interest position data does not—and is not
     intended to—equate to the daily short sale volume data."

→ 而且该文件只含**场外**成交（"all off-exchange short sale trades...
  is not consolidated with exchange data"）—— 拿它算"全市场做空占比"是错的。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

#: SEC 与 FINRA 的官方原文 —— **原样展示，不要改写成自己的话**
OFFICIAL_NOTES = {
    "ftd_cumulative": (
        "SEC 官方说明：「Fails to deliver on a given day are a cumulative number "
        "of all fails outstanding until that day... The figure is not a daily "
        "amount of fails... may have little or no relationship to yesterday's "
        "aggregate fails. Thus, it is important to note that the age of fails "
        "cannot be determined by looking at these numbers.」"
        "→ 它是**某一时点的累计余额**，不是当日新增。本页因此不做日环比、不谈「激增」。"),
    "ftd_not_naked": (
        "SEC 官方说明：「fails-to-deliver can occur for a number of reasons on "
        "both long and short sales. Therefore, fails-to-deliver are not "
        "necessarily the result of short selling, and are not evidence of "
        "abusive short selling or 'naked' short selling.」"
        "→ 交割失败**既可能来自多头也可能来自空头**，不是裸卖空的证据。"),
    "volume_not_interest": (
        "FINRA 官方说明：「short interest position data does not—and is not "
        "intended to—equate to the daily short sale volume data.」"
        "且该文件只含**场外**成交（not consolidated with exchange data）。"
        "→ 「空头成交量」是当日流量且只有场外那部分，"
        "与「空头持仓」（每月两次的存量快照）是两回事，"
        "拿它算全市场做空占比是错的。"),
    "price_caveat": (
        "SEC 说明：价格字段是**前一日收盘价**，且「we cannot guarantee that this "
        "price matches closing prices available from other sources」。"
        "本页的金额估算据此计算，只作量级参考。"),
}


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() in ("", "."):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _parse_date(v: Optional[str]) -> Optional[date]:
    """解析 `20260615`（FTD）或 `2026-06-15`。"""
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
    """一条交割失败记录（某标的在某结算日的**累计余额**）。"""

    settlement_date: Optional[date]
    cusip: str
    symbol: str
    description: str
    quantity: Optional[float]        # 累计余额（股），**不是当日新增**
    price: Optional[float]           # 前一日收盘价，SEC 不保证与他处一致

    @property
    def value(self) -> Optional[float]:
        """名义金额 = 余额 × 前收。只作量级参考（价格口径见 price_caveat）。"""
        if self.quantity is None or self.price is None:
            return None
        return self.quantity * self.price


def parse_ftd(row: dict) -> Optional[Fail]:
    """FTD 一行 → Fail。字段不全就返回 None（文件尾有说明行）。"""
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
    """FINRA 某日**场外**空头成交量（不是持仓，也不含交易所成交）。"""

    trade_date: Optional[date]
    symbol: str
    short_volume: Optional[float]
    short_exempt_volume: Optional[float]
    total_volume: Optional[float]
    market: str

    @property
    def short_pct(self) -> Optional[float]:
        """空头成交占**场外**成交的比例。

        ⚠️ 分母只是场外成交，**不是全市场成交** —— FINRA 明说该文件
        "is not consolidated with exchange data"。把它当"全市场做空占比"是错的。
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
