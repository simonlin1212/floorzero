"""The market-wide scanner.

━━━━━━━━━━━━━━ ⚠️ Why this section is slow, stated first ━━━━━━━━━━━━━━

UW's scanner returns market-wide results instantly because they **consume the OPRA print feed**:
the state of the whole market already sits in their own database. We have no such pipe, and must ask Cboe one symbol at a time.

Measured (2026-07-26), the two routes differ in cost by **3,750×**:

| Route | Per symbol | All 6,049 symbols |
|---|---|---|
| Light quote `quotes/{SYM}.json` | 0.4KB / ~120ms | ~26 minutes (at our self-imposed 4/s) |
| Full options chain `options/{SYM}.json` | 1.5MB / ~2.1s | **~3.5 hours / 9GB** |

So a scan runs in **two passes**: light quotes across the market → the full chain for a shortlist.
And it is a **background job** by nature, not an endpoint that answers on click. That has to be
visible to the user at a glance, rather than left for them to guess at while a button spins.

━━━━━━━━━━━━━━ ⚠️ IV Rank cannot be computed on day one ━━━━━━━━━━━━━━

**IV Rank and IV percentile are the scanner's central metric, and by definition they need history**
(where current IV sits within the last 252 trading days). Cboe gives only the present `iv30`,
and a lone 28.9% says nothing at all — whether that is high or low for this symbol is knowable only
against **its own** history.

That history **cannot be backfilled**; it accrues day by day from installation onwards (like the OI in `flow_store`).
Therefore:

- Until enough has accrued, `iv_rank` returns **None**, along with how many days are still needed.
  ⛔ **Never compute a number off 20 days and pass it off as IV Rank** — it looks every bit as
  professional, but it measures something else entirely, and the number itself gives the user no way to tell.
- The sample size always travels with the result, so the reader can judge how much the ranking is worth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

#: The standard lookback window for IV Rank (trading days). 252 ≈ one year.
IV_LOOKBACK = 252

#: Below this many days, **no** IV Rank is produced.
#: 60 trading days is about three months — below that, calling it "the past year's range" no longer holds.
IV_MIN_SAMPLE = 60

NOTES = {
    "why_slow": (
        "A market-wide scan is a **background job**, not something that answers on click. Cboe has no "
        "market-wide endpoint, so symbols are asked one at a time: 6,049 light quotes take about "
        "**26 minutes** (at our self-imposed 4 requests/second), and full chains would take 3.5 hours "
        "and 9GB — hence two passes, light across the market and full chains for a shortlist."),
    "iv_rank": (
        "**IV Rank needs history by definition**: where current IV sits within the last 252 trading days. "
        "Cboe gives only the present iv30, and a single point says nothing about high or low. This history "
        f"**cannot be backfilled** and only accrues daily; below {IV_MIN_SAMPLE} trading days this page returns **null** "
        "and **will not** compute a number off a short sample and call it IV Rank."),
    "universe": (
        "The universe is Cboe's official list of **symbols with options** (6,049 after deduplication), "
        "not \"symbols that traded today\" — it holds a great many names with no volume year in, year out."),
    "snapshot": (
        "A scan runs for tens of minutes, so symbols are captured at different moments. Because Cboe is "
        "**delayed** data (and essentially static after the close), comparability within one scan holds; "
        "still, every row carries its own trading session, so a mismatch is visible when there is one."),
}


def _pct_rank(value: float, samples: list[float]) -> Optional[float]:
    """Where `value` falls in the sample as a percentile (0-100).

    ⚠️ This counts "how many samples are ≤ it", and is **not** min-max normalisation.
    The two are routinely both called IV Rank:
    - **IV Rank** (the industry's usual meaning) = (current − low) / (high − low) × 100, which reads only the two extremes
    - **IV percentile** = how many days sat below the current value, which reads the whole distribution
    One day's spike to 200% in a year flattens IV Rank permanently; the percentile is untouched by it.
    This module **computes and returns both**, and crowns neither "the" IV Rank.
    """
    if not samples:
        return None
    below = sum(1 for s in samples if s <= value)
    return below / len(samples) * 100.0


def _minmax_rank(value: float, samples: list[float]) -> Optional[float]:
    """IV Rank in the industry's usual sense: where the current value sits within [low, high]."""
    if not samples:
        return None
    lo, hi = min(samples), max(samples)
    if hi <= lo:
        # Every sample identical — there is **no position to speak of**, which is neither 0 nor 50
        return None
    return (value - lo) / (hi - lo) * 100.0


@dataclass(frozen=True)
class ScanRow:
    """One row of the scanner (a light quote plus rankings computed from local history)."""

    symbol: str
    session: Optional[str]
    price: Optional[float]
    change_pct: Optional[float]
    volume: Optional[float]
    iv30: Optional[float]
    iv30_change: Optional[float]
    security_type: Optional[str]
    #: iv30 samples accrued locally (trading days)
    iv_samples: int
    iv_rank: Optional[float]          # the min-max sense
    iv_percentile: Optional[float]    # the distribution sense
    #: ⚠️ **Why** a ranking is null: insufficient_history / no_current_iv / flat_history.
    #: The three must read differently in the interface — only the first means "a few more days and it will be there".
    iv_reason: Optional[str]
    #: Volume as a multiple of the local historical median (None when it cannot be computed)
    volume_x_median: Optional[float]
    volume_samples: int
    #: Why it is null: insufficient_history / no_current_volume / zero_median
    volume_x_reason: Optional[str]

    @property
    def iv_ready(self) -> bool:
        return self.iv_samples >= IV_MIN_SAMPLE

    @property
    def iv_days_needed(self) -> int:
        return max(0, IV_MIN_SAMPLE - self.iv_samples)


def to_dict(r: ScanRow) -> dict:
    return {
        "symbol": r.symbol, "session": r.session, "price": r.price,
        "change_pct": r.change_pct, "volume": r.volume,
        "iv30": r.iv30, "iv30_change": r.iv30_change,
        "security_type": r.security_type,
        "iv_samples": r.iv_samples,
        "iv_rank": r.iv_rank, "iv_percentile": r.iv_percentile,
        "iv_ready": r.iv_ready, "iv_days_needed": r.iv_days_needed,
        "iv_reason": r.iv_reason,
        "volume_x_median": r.volume_x_median,
        "volume_samples": r.volume_samples,
        "volume_x_reason": r.volume_x_reason,
    }


def build_row(quote: dict, iv_history: list[float],
              volume_history: list[float]) -> ScanRow:
    """Combine one light quote with its local history into a scanner row.

    ⚠️ With too little history, `iv_rank` / `iv_percentile` return **None** rather than a number
    forced out of the few days to hand — see the module docstring.
    """
    iv = quote.get("iv30")
    n_iv = len(iv_history)
    window = iv_history[-IV_LOOKBACK:]

    vol = quote.get("volume")
    n_vol = len(volume_history)
    vx = None
    vx_reason: Optional[str] = None
    if vol is None:
        vx_reason = "no_current_volume"
    elif n_vol < 5:
        vx_reason = "insufficient_history"
    else:
        med = _median(volume_history[-IV_LOOKBACK:])
        if med is None or med <= 0:
            # A median of 0 (nothing traded for a long time) makes the multiple **incomputable**; it is not 0 or infinity.
            # ⚠️ But that is **a different thing** from "not enough history", so the reasons stay apart.
            vx_reason = "zero_median"
        else:
            vx = vol / med

    # ⚠️ `iv_rank=None` has **three** causes and they must be labelled separately:
    #    ① not enough history ② current iv30 missing ③ the whole history is flat (a zero range, so there is no position to speak of)
    #    Calling all three "N days to go" lies about ② and ③ — and on ② it reads "0 days to go".
    iv_reason: Optional[str] = None
    rank = pct = None
    if iv is None:
        iv_reason = "no_current_iv"
    elif n_iv < IV_MIN_SAMPLE:
        iv_reason = "insufficient_history"
    else:
        rank = _minmax_rank(iv, window)
        pct = _pct_rank(iv, window)
        if rank is None:
            iv_reason = "flat_history"

    return ScanRow(
        symbol=quote["symbol"], session=quote.get("session"),
        price=quote.get("price"), change_pct=quote.get("change_pct"),
        volume=vol, iv30=iv, iv30_change=quote.get("iv30_change"),
        security_type=quote.get("security_type"),
        iv_samples=n_iv, iv_rank=rank, iv_percentile=pct,
        iv_reason=iv_reason,
        volume_x_median=vx, volume_samples=n_vol, volume_x_reason=vx_reason,
    )


def _median(xs: list[float]) -> Optional[float]:
    """Median.

    ⚠️ An even-sized sample takes **the mean of the middle two**, not `srt[n//2]` (which is the upper median).
    The median of `[10,20,30,40]` is 25, not 30 — 20% out, and the volume-multiple screen shifts with it.
    """
    if not xs:
        return None
    srt = sorted(xs)
    n = len(srt)
    mid = n // 2
    return srt[mid] if n % 2 else (srt[mid - 1] + srt[mid]) / 2.0


# ─────────────────────────── Filtering ───────────────────────────

def apply_filters(rows: Iterable[ScanRow], *,
                  min_price: Optional[float] = None,
                  max_price: Optional[float] = None,
                  min_volume: Optional[float] = None,
                  min_iv: Optional[float] = None,
                  max_iv: Optional[float] = None,
                  min_iv_rank: Optional[float] = None,
                  min_volume_x: Optional[float] = None,
                  security_type: Optional[str] = None) -> tuple[list[ScanRow], dict]:
    """Filter on the given conditions.

    ⚠️ **It also returns how many rows were excluded because the value could not be computed.**
    With `min_iv_rank=80`, rows whose IV Rank is None fail the condition and are filtered out —
    but that means "this symbol has not accrued enough history", not "its IV Rank is below 80".
    Give the results without that number and the user believes only a handful of symbols in the whole market qualify.
    """
    out = []
    skipped_no_iv_rank = 0
    skipped_no_volume_x = 0
    iv_why: dict = {}
    vol_why: dict = {}
    for r in rows:
        if min_price is not None and (r.price is None or r.price < min_price):
            continue
        if max_price is not None and (r.price is None or r.price > max_price):
            continue
        if min_volume is not None and (r.volume is None or r.volume < min_volume):
            continue
        if min_iv is not None and (r.iv30 is None or r.iv30 < min_iv):
            continue
        if max_iv is not None and (r.iv30 is None or r.iv30 > max_iv):
            continue
        if min_iv_rank is not None:
            if r.iv_rank is None:
                skipped_no_iv_rank += 1
                iv_why[r.iv_reason or "unknown"] = iv_why.get(r.iv_reason or "unknown", 0) + 1
                continue
            if r.iv_rank < min_iv_rank:
                continue
        if min_volume_x is not None:
            if r.volume_x_median is None:
                skipped_no_volume_x += 1
                vol_why[r.volume_x_reason or "unknown"] = vol_why.get(
                    r.volume_x_reason or "unknown", 0) + 1
                continue
            if r.volume_x_median < min_volume_x:
                continue
        if security_type and (r.security_type or "") != security_type:
            continue
        out.append(r)
    return out, {
        # Excluded because it **could not be computed**, not because it failed a numeric condition — the two must be reported apart
        "excluded_no_iv_rank": skipped_no_iv_rank,
        "excluded_no_volume_x": skipped_no_volume_x,
        # Broken down by reason — "a few days to go" and "current IV missing" call for different next steps
        "iv_reasons": iv_why,
        "volume_reasons": vol_why,
    }


SORTS = {
    "iv_rank": lambda r: (r.iv_rank is None, -(r.iv_rank or 0)),
    "iv_percentile": lambda r: (r.iv_percentile is None, -(r.iv_percentile or 0)),
    "iv30": lambda r: (r.iv30 is None, -(r.iv30 or 0)),
    "volume": lambda r: (r.volume is None, -(r.volume or 0)),
    "volume_x": lambda r: (r.volume_x_median is None, -(r.volume_x_median or 0)),
    "change_pct": lambda r: (r.change_pct is None, -(r.change_pct or 0)),
    "symbol": lambda r: (False, r.symbol),
}


def sort_rows(rows: list[ScanRow], key: str = "iv_rank") -> list[ScanRow]:
    """Sorting.

    ⚠️ The first element of every key tuple is "whether the value is None",
    so **anything incomputable sinks to the bottom** rather than being treated as "the lowest value"
    by an `or 0` and mixed in among real ones — a real 0 and a missing value side by side, with no way to tell which is which.
    """
    fn = SORTS.get(key) or SORTS["iv_rank"]
    return sorted(rows, key=fn)


#: Null reasons in plain words (REST / MCP / UI all read this one copy, so they cannot word it differently)
REASON_LABEL = {
    "insufficient_history": "not enough history accrued on this machine yet",
    "no_current_iv": "this snapshot carries no IV30 (upstream did not provide one)",
    "no_current_volume": "this snapshot carries no volume (upstream did not provide one)",
    "flat_history": "the history has a zero range (high = low), so a position within it is undefined",
    "zero_median": "the median historical volume is 0 (nothing traded for a long time), so no multiple can be computed",
    "unknown": "reason unknown",
}
