"""Options flow — what a **chain snapshot** can compute, and what it **cannot**.

━━━━━━━━━━━━━━ ⚠️ The limits of the data, stated first ━━━━━━━━━━━━━━

Unusual Whales builds its flow on the **options tape**: every print carries a size,
a trade price, an exchange and a timestamp. That is what makes "this one hit the ask,
not the bid" answerable, and it is what UW's **bullish / bearish flow** labels rest on.

**We have no tape.** Cboe's free delayed feed gives a **snapshot of the chain**:
each contract's **cumulative volume for the day**, its **open interest**, bid/ask and greeks.
Getting the tape means paying for the OPRA feed — precisely what this project avoids so as not to be a redistributor.

So there is a clean line between the two:

| Computable (a snapshot suffices) | Not computable (needs prints) |
|---|---|
| vol/OI ratio (today's volume vs standing open interest) | **Sweeps**: one order split across exchanges within milliseconds |
| P/C ratio (on volume / open interest / notional) | **Block-size tiering**: 5,000 contracts as one trade or as 5,000 looks identical in a snapshot |
| Notional ranking, expiry and strike distribution | **Buyer- or seller-initiated**: whether a print hit the ask or the bid |
| Absolute delta / gamma exposure | **Opening or closing**: whether a print started a position or ended one |
| **Day-over-day OI change** (from history accrued locally, see `history`) | —— |

⛔ **This module therefore produces no directional label at all.** Writing "bullish inflow"
without an aggressor side is selling a guess as a fact — against this project's rule of
outputting data rather than conclusions, and exactly where products like UW mislead people most.

━━━━━━━━━━━━━━ ⭐ The snapshot does hold one thing the tape does not ━━━━━━━━━━━━━━

**Open interest.** The tape tells you what traded; OI tells you **what settled into position**.
Record OI daily and the difference is the net new positioning — the hardest evidence there is
for "someone is building", and it needs **no** guess about direction. The cost: it updates once a day, and **you have to accrue it yourself** (see `flow_store`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sources.cboe import Chain, Contract

#: vol/OI above this multiple counts as "unusual". 1.0 = today's volume already exceeds all standing open interest.
UNUSUAL_RATIO = 1.0

#: Contracts below this volume are kept out of the unusual table.
#: ⚠️ Without that gate, a zombie contract with OI=1 and 3 lots traded tops the table at vol/OI=3.0 —
#:    the ratio is large because the denominator is small, not because anyone is moving it.
MIN_VOLUME = 50.0

#: Contract multiplier (US equity options are fixed at 100 shares)
MULTIPLIER = 100.0

LIMITS = {
    "no_tape": (
        "This page comes from an **options chain snapshot** (each contract's cumulative volume "
        "and open interest for the day), not the print-by-print tape. So **sweep detection, "
        "block-size tiering, buyer/seller direction and opening/closing calls are all out of "
        "reach** — those need OPRA print data, which this project deliberately does not touch (touching it means paying OPRA's redistribution fee, which breaks the whole self-hosted model)."),
    "no_direction": (
        "⛔ **This page attaches no bullish or bearish inflow label.** Telling whether an options "
        "trade was buyer- or seller-initiated requires knowing whether it printed at the ask or the "
        "bid — and a snapshot does not carry that. Labelling direction without it is selling a guess as a fact."),
    "vol_oi": (
        "**vol/OI > 1** means one thing literally: today's volume exceeded that contract's standing "
        "open interest. It **may** be new positions going on and it **may** be existing positions "
        "changing hands — the two look identical in a snapshot, and this data cannot separate them. "
        "To see whether positions actually grew, compare **open interest across two days** (the card below)."),
    "oi_lag": (
        "⚠️ **Open interest settles overnight**, so it reflects positions at **yesterday's close** "
        "and excludes anything opened today. The denominator of vol/OI therefore lags by a day — "
        "that is OCC's settlement rhythm, not a fetching problem on our side."),
    "delayed": (
        "Cboe's free feed is **delayed** data, usually by 15 minutes. Enough for research, not for chasing fills."),
}


def _mid(c: Contract) -> Optional[float]:
    """Mid price. Missing either side of the quote returns None — **`last` is never substituted**.

    ⚠️ `last` is "the price of the most recent print", which may be days old. Used as today's
    price against today's volume, the notional comes out wildly wrong, and wrong invisibly.

    ⚠️ **A zero bid (bid=0, ask>0) still gets a mid, deliberately**: deep out-of-the-money
    contracts sit at bid=0 / ask=0.01 for months, and dropping them all would tilt the premium-weighted
    view heavily towards the in-the-money side — a larger distortion than keeping them. But a zero bid
    means **nobody is bidding at all**, so the mid overstates the contract's worth — which is why these are counted separately and reported in the UI (`one_sided_quotes`).
    """
    if c.bid is None or c.ask is None:
        return None
    if c.bid <= 0 and c.ask <= 0:
        return None
    return (c.bid + c.ask) / 2.0


def _one_sided(r: "FlowRow") -> bool:
    """An ask with no bid — the mid reads optimistically."""
    return r.mid is not None and (r.bid_zero is True)


@dataclass(frozen=True)
class FlowRow:
    """One contract's snapshot metrics for the day."""

    symbol: str
    expiry: str
    type: str
    strike: float
    dte: int
    volume: float
    open_interest: float
    mid: Optional[float]
    #: Bid is 0 (or absent) while the ask is valid — the mid reads optimistically, see `_mid`
    bid_zero: bool
    last: Optional[float]
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]

    @property
    def vol_oi(self) -> Optional[float]:
        """Volume / open interest. When OI is 0 this **returns None, not infinity**.

        ⚠️ OI=0 is the fact that **settled open interest at the prior close was zero**; the ratio
        simply does not exist. Stuffing in 999 disguises it as "an extreme ratio" and sorts it to
        the top; stuffing in 0 buries it. Both lie, so it returns None and `zero_prior_oi` marks it instead.
        """
        if self.open_interest <= 0:
            return None
        return self.volume / self.open_interest

    @property
    def zero_prior_oi(self) -> bool:
        """**Settled open interest at the prior close was zero**, and there was volume today.

        ⚠️ This is a **statement of fact**, not the inference that "all of today is new positions".
        The old name `is_new_strike` ("a brand-new strike") claimed too much —
        a snapshot cannot separate these three cases:
        ① the strike was only just listed (genuinely new)
        ② the strike has been there all along, nobody just happened to hold it (not new, only cold)
        ③ opened and closed intraday (tomorrow's OI is 0 again, nothing settled at all)
        All that can be said is "there was no open position at yesterday's close, and someone traded it today".
        To know whether anything settled, look at **tomorrow's** open interest (the card below).
        """
        return self.open_interest <= 0 and self.volume > 0

    @property
    def notional(self) -> Optional[float]:
        """An **estimate** of premium size = day's cumulative volume × the mid **at capture time** × 100.

        ⚠️⚠️ **This is not the money actually traded, and the gap can be large.**
        A chain snapshot carries no per-print price and no VWAP, only the quote at the instant of capture.
        If 1,000 contracts traded at $1 in the morning and the mid is $5 by capture time,
        this computes $500k against roughly $100k of premium actually paid — **a fivefold overestimate**.

        Without a tape it cannot be made exact, so the approach here is: compute it, but **call it
        an estimate at every exit**, never "the amount traded". The field name `notional` stays;
        the user-facing wording is always "estimated at the current mid".

        Returns None when the mid is missing, and **never falls back to `last`** (see `_mid`).
        """
        return None if self.mid is None else self.volume * self.mid * MULTIPLIER

    @property
    def unusual(self) -> bool:
        return (self.volume >= MIN_VOLUME
                and (self.zero_prior_oi
                     or (self.vol_oi is not None and self.vol_oi >= UNUSUAL_RATIO)))


def parse(chain: Chain, dte_max: Optional[int] = None,
          expiry: Optional[str] = None,
          traded_only: bool = True) -> list[FlowRow]:
    """Chain → one snapshot row per contract.

    ⚠️ `traded_only` has to be chosen explicitly by the caller:
    - **Display** wants True (a contract that did not trade today has no place in "today's unusual")
    - **Archiving and the open-interest view** want **False** — a contract not trading today
      does not mean its open interest is zero. Store only the contracts that traded and, on a day
      it does not trade, it vanishes from the store; the OI difference then records it as "the whole
      position closed out" when not one contract moved. (This bug was real in the first version, caught by codex.)
    """
    rows = []
    for c in chain.filter(expiry=expiry, dte_max=dte_max,
                          traded_only=traded_only):
        rows.append(FlowRow(
            symbol=chain.ticker, expiry=c.expiry, type=c.type, strike=c.strike,
            dte=c.dte, volume=c.volume, open_interest=c.open_interest,
            mid=_mid(c), bid_zero=not (c.bid and c.bid > 0),
            last=c.last, iv=c.iv, delta=c.delta, gamma=c.gamma))
    return rows


def to_dict(r: FlowRow) -> dict:
    return {
        "symbol": r.symbol, "expiry": r.expiry, "type": r.type,
        "strike": r.strike, "dte": r.dte,
        "volume": r.volume, "open_interest": r.open_interest,
        "vol_oi": r.vol_oi, "zero_prior_oi": r.zero_prior_oi,
        "mid": r.mid, "bid_zero": r.bid_zero, "last": r.last,
        "notional": r.notional,
        "iv": r.iv, "delta": r.delta, "gamma": r.gamma,
        "unusual": r.unusual,
    }


# ─────────────────────────── Aggregation ───────────────────────────

def ratios(traded: Iterable[FlowRow], all_rows: Iterable[FlowRow]) -> dict:
    """Put/call ratio — **all three bases are given; none is crowned as "the" P/C**.

    They measure different things, and disagreeing is normal:
    - **By volume**: how many contracts changed hands today (most quoted, and easiest for cheap out-of-the-money contracts to flood)
    - **By open interest**: the structure of positions that settled (slow, but real positioning)
    - **By estimated premium**: where the money is (one $50 deep in-the-money contract ≠ one $0.03 lottery ticket)

    ⚠️ **The two arguments are not redundant.** Volume and premium can only come from contracts that
    **traded today**, while open interest has to come from **every contract in the filtered scope** —
    a contract that did not trade today still holds its open interest. The first version computed all
    three on the traded subset, so "the structure of settled positions" was really "positions in the
    contracts touched today", which on a thinly traded ticker is off by an order of magnitude.
    """
    cv = pv = cn = pn = 0.0
    n_no_mid = 0
    nc = np_ = 0                    # contracts per side whose premium **can be computed**
    one_sided = 0                   # zero-bid contracts (nobody buying) still counted in the estimate
    for r in traded:
        is_put = r.type == "put"
        cv, pv = (cv, pv + r.volume) if is_put else (cv + r.volume, pv)
        nt = r.notional
        if nt is None:
            n_no_mid += 1
        elif is_put:
            pn += nt
            np_ += 1
            if r.bid_zero:
                one_sided += 1
        else:
            cn += nt
            nc += 1
            if r.bid_zero:
                one_sided += 1

    co = po = 0.0
    n_all = 0
    for r in all_rows:
        n_all += 1
        if r.type == "put":
            po += r.open_interest
        else:
            co += r.open_interest

    def _r(p: float, c: float) -> Optional[float]:
        # ⚠️ A zero denominator returns None, not 0 and not infinity —
        #    "no call volume" and "put/call = 0" are opposite statements.
        return None if c <= 0 else p / c

    return {
        "by_volume": {"call": cv, "put": pv, "pc": _r(pv, cv),
                      "basis": "contracts that traded today"},
        "by_oi": {"call": co, "put": po, "pc": _r(po, co),
                  "basis": f"all {n_all} contracts in the filtered scope, including those that did not trade today"},
        # ⚠️ When a side has **nothing computable at all**, give None rather than 0.
        #    With puts missing every quote and calls computable, the output reads put=0 / pc=0.00,
        #    which says "no money on the put side" when it means "the put side cannot be computed".
        "by_notional": {"call": cn if nc else None,
                        "put": pn if np_ else None,
                        "pc": _r(pn, cn) if (nc and np_) else None,
                        "counted_call": nc, "counted_put": np_,
                        "basis": "contracts that traded today, **estimated** at the mid when captured"},
        # Contracts with no quote at all yield no premium — **say how many were left out**
        "notional_excluded": n_no_mid,
        # Contracts with an ask but no bid are counted, but their mid reads optimistically — **say how many**
        "one_sided_quotes": one_sided,
    }


def exposure(rows: Iterable[FlowRow], spot: float) -> dict:
    """Delta and gamma exposure behind today's volume — **absolute only, never net**.

    ⚠️ Why there is no "net delta": a net figure requires knowing whether each print was a buy
    or a sell. The snapshot has no aggressor side, and summing every print as buyer-initiated
    produces a "net delta exposure" that is a **fake number wearing a professional face**.
    What is given here is Σ|delta|×volume — "how much directional exposure sits on the contracts
    traded today" — a statement that does not need to know who the buyer was, and so is true.
    """
    call_d = put_d = gam = 0.0
    n_call = n_put = n_gamma = 0
    miss_call = miss_put = miss_gamma = 0
    for r in rows:
        is_put = r.type == "put"
        if r.delta is None:
            if is_put:
                miss_put += 1
            else:
                miss_call += 1
        else:
            share = abs(r.delta) * r.volume * MULTIPLIER
            if is_put:
                put_d += share
                n_put += 1
            else:
                call_d += share
                n_call += 1
        if r.gamma is None:
            miss_gamma += 1
        else:
            n_gamma += 1
            gam += abs(r.gamma) * r.volume * MULTIPLIER * spot * spot / 100.0

    # ⚠️ **Return None rather than 0 when it cannot be computed — and judge each side separately.**
    #    Counting "how many deltas there are in total" is not enough: with puts complete and calls
    #    all missing, the call side still shows 0, saying "no exposure here" when it means "not computable here".
    #    Same for the total: with one side missing the sum is half a picture, so it is given only when both sides are there.
    hc, hp, hg = n_call > 0, n_put > 0, n_gamma > 0
    return {
        "call_delta_shares": call_d if hc else None,
        "put_delta_shares": put_d if hp else None,
        "total_delta_shares": (call_d + put_d) if (hc and hp) else None,
        "call_delta_notional": call_d * spot if hc else None,
        "put_delta_notional": put_d * spot if hp else None,
        "gamma_notional_per_1pct": gam if hg else None,
        "missing_delta_call": miss_call,
        "missing_delta_put": miss_put,
        "missing_gamma": miss_gamma,
        "counted_delta_call": n_call,
        "counted_delta_put": n_put,
        "counted_gamma": n_gamma,
        "note": ("This is **absolute** exposure (Σ|delta|×volume×100), not net exposure. "
                 "A net figure needs to know whether each print was a buy or a sell, and the snapshot does not carry that."),
    }


def by_expiry(rows: Iterable[FlowRow]) -> list[dict]:
    """Aggregate by expiry — how short-dated today's volume is concentrated."""
    buckets: dict[str, dict] = {}
    for r in rows:
        b = buckets.setdefault(r.expiry, {
            "expiry": r.expiry, "dte": r.dte, "call_volume": 0.0,
            "put_volume": 0.0, "volume": 0.0, "open_interest": 0.0,
            "notional": 0.0, "notional_counted": 0, "contracts": 0})
        b["volume"] += r.volume
        b["open_interest"] += r.open_interest
        b["contracts"] += 1
        if r.type == "put":
            b["put_volume"] += r.volume
        else:
            b["call_volume"] += r.volume
        nt = r.notional
        if nt is not None:
            b["notional"] += nt
            b["notional_counted"] += 1
    out = sorted(buckets.values(), key=lambda b: b["expiry"])
    # Not one quote in the whole expiry bucket → premium is **not computable**, not 0
    for b in out:
        if b["notional_counted"] == 0:
            b["notional"] = None
    return out


def by_strike(rows: Iterable[FlowRow], spot: float,
              width_pct: float = 0.15) -> dict:
    """Aggregate by strike (near spot only; far strikes are noisy and nobody reads them).

    ⚠️ **The clipping window must be returned alongside the result.** This chart shares a card
    with the by-expiry one, which counts **every** strike — so the two volume totals will not
    reconcile. Leave the window unsaid and the user simply assumes one of them is miscomputed.
    """
    lo, hi = spot * (1 - width_pct), spot * (1 + width_pct)
    dropped = 0
    dropped_volume = 0.0
    buckets: dict[float, dict] = {}
    for r in rows:
        if not (lo <= r.strike <= hi):
            dropped += 1
            dropped_volume += r.volume
            continue
        b = buckets.setdefault(r.strike, {
            "strike": r.strike, "call_volume": 0.0, "put_volume": 0.0,
            "call_oi": 0.0, "put_oi": 0.0})
        if r.type == "put":
            b["put_volume"] += r.volume
            b["put_oi"] += r.open_interest
        else:
            b["call_volume"] += r.volume
            b["call_oi"] += r.open_interest
    return {
        "rows": sorted(buckets.values(), key=lambda b: b["strike"]),
        "window_pct": width_pct,
        "low": lo, "high": hi,
        "dropped_contracts": dropped,
        "dropped_volume": dropped_volume,
    }


def summarize(chain: Chain, rows: list[FlowRow],
              all_rows: Optional[list[FlowRow]] = None, top: int = 40) -> dict:
    """Everything one page needs, aggregated.

    `rows` = contracts that traded today (the volume / premium / exposure basis).
    `all_rows` = **every** contract in the same filtered scope (the open-interest basis). Omitted, it
    falls back to `rows`, but then the open-interest basis covers only the traded subset — callers should pass it explicitly.
    """
    scope_rows = all_rows if all_rows is not None else rows
    # ⚠️ Always sorted by notional; **"zero prior OI" is never a sort key**.
    # Measured (SPY 2026-07-25), sorting on it puts three deep out-of-the-money lottery tickets
    # worth a few thousand dollars each at the top (820C / 815C / 590P, notional $0.00M),
    # while the real move of the day — 739P, 86,279 contracts, $24M notional — is pushed to fifth.
    # "Zero prior OI" is an **attribute** (shown as a badge), not a measure of importance:
    # what has money in it floats up on its own, and what does not belongs at the bottom.
    unusual = sorted((r for r in rows if r.unusual),
                     key=lambda r: -(r.notional or 0.0))
    biggest = sorted(rows, key=lambda r: -(r.notional or 0.0))
    return {
        "ticker": chain.ticker,
        "spot": chain.spot,
        "timestamp": chain.timestamp,
        # ⚠️ session = the trading session the data belongs to; timestamp = when Cboe published it. They often differ by a day.
        "session": chain.session,
        "counts": {
            "traded_contracts": len(rows),
            "scope_contracts": len(scope_rows),
            "unusual": sum(1 for r in rows if r.unusual),
            "zero_prior_oi": sum(1 for r in rows if r.zero_prior_oi),
            "total_volume": sum(r.volume for r in rows),
            # ⚠️ Total open interest runs over the whole chain, consistent with by_oi
            "total_oi": sum(r.open_interest for r in scope_rows),
        },
        "ratios": ratios(rows, scope_rows),
        "exposure": exposure(rows, chain.spot),
        "unusual_rows": [to_dict(r) for r in unusual[:top]],
        "biggest_rows": [to_dict(r) for r in biggest[:top]],
        "by_expiry": by_expiry(rows),
        "by_strike": by_strike(rows, chain.spot),
        "thresholds": {"unusual_ratio": UNUSUAL_RATIO, "min_volume": MIN_VOLUME},
        "limits": LIMITS,
    }
