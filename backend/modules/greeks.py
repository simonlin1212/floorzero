"""GEX (gamma exposure) calculation.

The counterpart to Unusual Whales' Gex/Greeks category (11 endpoints).

━━━ What GEX is ━━━
Having sold options to the public, market makers must hedge: every move in the stock means buying or selling shares to stay neutral.
GEX measures precisely that — how much notional stock dealers have to buy or sell for each 1% move in the price.

· GEX **positive** → dealers are long gamma → sell into rallies, buy into dips → **volatility suppressed** (price gets "pinned")
· GEX **negative** → dealers are short gamma → chase rallies, sell into dips → **volatility amplified**
· **Gamma flip** = the price at which GEX turns from positive to negative, read as a watershed in market structure

━━━ ⚠️ One assumption that has to be stated ━━━
Computing GEX requires knowing whether dealers are long or short each contract, and **the market does not publish that**.
The industry's usual approach (proposed by SqueezeMetrics, followed by gex-tracker and other mainstream implementations) assumes:
    **dealers are long gamma in calls and short gamma in puts**
so calls count positive and puts negative. That is **an assumption, not a fact** — real positioning is unknowable.
This module makes it explicit through the `dealer_convention` parameter and labels it in the output,
rather than burying it in the code as though it were the truth.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from sources.cboe import Chain, Contract
from modules import bs

CONTRACT_SIZE = 100          # one US equity option = 100 shares

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
    """The full result of one GEX calculation."""
    ticker: str
    spot: float
    total_gex: float                  # notional dollars per 1% move
    by_strike: tuple[StrikeGex, ...]
    by_expiry: tuple[tuple[str, float], ...]
    gamma_flip: Optional[float]       # where GEX turns from positive to negative (None = no flip within the range)
    call_wall: Optional[float]        # the strike with the largest call GEX (often acts as resistance)
    put_wall: Optional[float]         # the strike with the most negative put GEX (often acts as support)
    convention: str
    scope: str                        # for display: carries the contract count, so the sample size is visible at a glance
    scope_key: str                    # ⭐ the stable key: no contract count, and history is filed under it


def contract_gex(c: Contract, spot: float,
                 convention: DealerConvention = "long_call_short_put") -> float:
    """One contract's GEX (notional dollars per 1% move in the underlying).

        GEX = gamma × OI × 100 × spot² × 0.01

    Where spot² comes from: gamma is "the second derivative of delta with respect to price"; one spot turns it into
    "how much delta changes per one unit of price", and a second spot turns that into notional dollars;
    ×0.01 converts "a $1 move" into "a 1% move".
    """
    if not c.gamma or not c.open_interest:
        return 0.0
    raw = c.gamma * c.open_interest * CONTRACT_SIZE * spot * spot * 0.01
    if convention == "long_call_short_put":
        return raw if c.type == "call" else -raw
    return -raw if c.type == "call" else raw


def total_gex_at(contracts: Iterable[Contract], hypo_spot: float, asof: str,
                 convention: DealerConvention = "long_call_short_put",
                 rate: float = 0.04) -> float:
    """Total GEX on the assumption that the share price becomes hypo_spot.

    ⚠️ Gamma has to be **recomputed** with Black-Scholes and never reused from Cboe —
    theirs is the gamma at the current price, and gamma varies violently with price (highest at the money, tending to zero at both ends).
    Applying the gamma to hand at a different price gives a wrong flip.
    """
    total = 0.0
    for c in contracts:
        if not c.open_interest or not c.iv or c.iv <= 0:
            continue
        g = bs.bs_gamma(hypo_spot, c.strike, bs.years_to_expiry(c.dte_from(asof)), c.iv, rate)
        if not g:
            continue
        raw = g * c.open_interest * CONTRACT_SIZE * hypo_spot * hypo_spot * 0.01
        if convention == "long_call_short_put":
            total += raw if c.type == "call" else -raw
        else:
            total += -raw if c.type == "call" else raw
    return total


def _find_gamma_flip(contracts: list[Contract], spot: float, asof: str,
                     convention: DealerConvention,
                     search_pct: float = 0.10, steps: int = 60,
                     rate: float = 0.04) -> Optional[float]:
    """Find the gamma flip: the price at which total GEX turns from positive to negative.

    The method: sweep hypothetical prices across spot ±search_pct, recompute the whole chain's GEX with BS at each,
    find where the sign changes and refine by bisection. No sign change means None —
    **no number is forced** (plenty of symbols genuinely do not flip within a sensible range).
    """
    lo, hi = spot * (1 - search_pct), spot * (1 + search_pct)
    xs = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    vals = [total_gex_at(contracts, x, asof, convention, rate) for x in xs]

    # ⚠️ The curve can cross zero **more than once** within the search range (common with mixed call/put OI).
    # Sweeping upwards from the low end and "taking the first" reports a crossing far from spot — which is no watershed in market structure.
    # The right way: collect every crossing and take **the one nearest spot**.
    crossings = []
    for i in range(1, len(xs)):
        a, b = vals[i - 1], vals[i]
        if (a > 0 >= b) or (a < 0 <= b):
            crossings.append((xs[i - 1], xs[i], a, b))
    if not crossings:
        return None
    cross = min(crossings, key=lambda c: abs((c[0] + c[1]) / 2 - spot))

    x0, x1, v0, _ = cross
    for _ in range(40):                       # bisect down to about 1e-4
        mid = (x0 + x1) / 2
        vm = total_gex_at(contracts, mid, asof, convention, rate)
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
    """Compute a GEX profile.

    expiry / dte_max: restrict the expiry ('0DTE' = expiring today). Neither given = the whole chain.
    strike_pct: count only strikes within this fraction of spot (default ±15%; far strikes' OI is negligible
                for GEX but contaminates the call/put wall identification).
    """
    spot = chain.spot
    lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)

    cs = chain.filter(expiry=expiry, dte_max=dte_max)
    cs = [c for c in cs if lo <= c.strike <= hi]
    if not cs:
        scope = expiry or (f"≤{dte_max}DTE" if dte_max is not None else "whole chain")
        raise ValueError(f"{chain.ticker} has no contracts within {scope} / ±{strike_pct:.0%}")

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
    # When call_gex is all zeros (or all negative) the "maximum" is meaningless; do not force a wall
    if call_wall is not None and max(r.call_gex for r in rows) <= 0:
        call_wall = None
    if put_wall is not None and min(r.put_gex for r in rows) >= 0:
        put_wall = None

    scope = expiry or (f"≤{dte_max}DTE" if dte_max is not None else "whole chain")
    return GexProfile(
        ticker=chain.ticker,
        spot=spot,
        total_gex=sum(r.net_gex for r in rows),
        by_strike=tuple(rows),
        by_expiry=tuple(sorted(by_exp.items())),
        gamma_flip=_find_gamma_flip(cs, spot, chain.asof, convention),
        call_wall=call_wall,
        put_wall=put_wall,
        convention=convention,
        # ⚠️ scope_key and scope have to stay separate:
        # scope carries the contract count (for people, so the sample size is visible), but that count **changes daily**
        # (expiries roll, new strikes get listed). With the history keyed partly on scope,
        # folding the count in files tomorrow's snapshot under a different scope and the series never accrues —
        # and it fails **silently**: the page just says "1 observation accrued", with nothing to show what went wrong.
        # The key carries the **exact value**: `.0%` flattens 0.051 and 0.054 both to "±5%",
        # merging snapshots of two different contract sets into one series. The rounded percentage is for display only.
        scope_key=f"{scope} · k±{strike_pct:.4f}",
        scope=f"{scope} · strikes ±{strike_pct:.0%} · {len(cs)} contracts",
    )


def to_dict(p: GexProfile) -> dict:
    """Convert to the JSON the API and frontend use (all in billions of dollars, so the frontend need not convert)."""
    B = 1e9
    return {
        "ticker": p.ticker,
        "spot": round(p.spot, 2),
        "total_gex_bn": round(p.total_gex / B, 4),
        "gamma_flip": round(p.gamma_flip, 2) if p.gamma_flip else None,
        "call_wall": p.call_wall,
        "put_wall": p.put_wall,
        # Zero exposure (gamma missing across the chain, or calls and puts cancelling exactly) is neither positive nor negative —
        # reporting it as negative makes the interface say "dealer hedging will amplify volatility", which is invented out of nothing.
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
                "Dealer positioning is not public, so the industry's usual assumption is used here: calls positive / puts negative. "
                "That is an assumption, not a fact."
            ),
            "scope": p.scope,
            "scope_key": p.scope_key,
            "unit": "billions of dollars per 1% move in the underlying",
        },
    }

# ══════════════════════════════════════════════════════════════
#  Vanna / charm exposure + the two-dimensional surface
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExposureProfile:
    """Vanna / charm exposure profile (on the same dealer assumption as GEX)."""
    ticker: str
    spot: float
    total_vanna: float                       # notional dollars per +1 percentage point of IV
    total_charm: float                       # notional dollars per day that passes
    vanna_by_strike: tuple[tuple[float, float], ...]
    charm_by_strike: tuple[tuple[float, float], ...]
    convention: str
    scope: str


def _dealer_sign(c: Contract, convention: DealerConvention) -> float:
    """The dealer positioning assumption — identical to GEX's (calls positive / puts negative)."""
    if convention == "long_call_short_put":
        return 1.0 if c.type == "call" else -1.0
    return -1.0 if c.type == "call" else 1.0


def compute_exposures(chain: Chain,
                      expiry: Optional[str] = None,
                      dte_max: Optional[int] = None,
                      strike_pct: float = 0.05,
                      convention: DealerConvention = "long_call_short_put",
                      rate: float = 0.04) -> ExposureProfile:
    """Vanna / charm exposure.

    Normalisation (matching GEX's "per 1% move in the share price"):
      · vanna exposure = vanna × OI × 100 × spot × **0.01**  → per +1 percentage point of IV
      · charm exposure = charm × OI × 100 × spot × **(1/365)** → per day that passes

    ⚠️ Cboe gives delta/gamma/vega/theta/rho and **no second-order cross greeks**,
    so these are computed here with Black-Scholes (using each contract's own IV).
    """
    spot = chain.spot
    lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)
    cs = [c for c in chain.filter(expiry=expiry, dte_max=dte_max)
          if lo <= c.strike <= hi]
    if not cs:
        scope = expiry or (f"≤{dte_max}DTE" if dte_max is not None else "whole chain")
        raise ValueError(f"{chain.ticker} has no contracts within {scope} / ±{strike_pct:.0%}")

    v_agg: dict[float, float] = defaultdict(float)
    c_agg: dict[float, float] = defaultdict(float)
    for c in cs:
        if not c.open_interest or not c.iv or c.iv <= 0:
            continue
        t = bs.years_to_expiry(c.dte_from(chain.asof))
        sign = _dealer_sign(c, convention)
        notional = c.open_interest * CONTRACT_SIZE * spot
        v_agg[c.strike] += sign * bs.bs_vanna(spot, c.strike, t, c.iv, rate) * notional * 0.01
        c_agg[c.strike] += sign * bs.bs_charm(spot, c.strike, t, c.iv, rate) * notional / bs.DAYS_PER_YEAR

    scope = expiry or (f"≤{dte_max}DTE" if dte_max is not None else "whole chain")
    return ExposureProfile(
        ticker=chain.ticker, spot=spot,
        total_vanna=sum(v_agg.values()),
        total_charm=sum(c_agg.values()),
        vanna_by_strike=tuple(sorted(v_agg.items())),
        charm_by_strike=tuple(sorted(c_agg.items())),
        convention=convention,
        scope=f"{scope} · strikes ±{strike_pct:.0%} · {len(cs)} contracts",
    )


def exposures_to_dict(p: ExposureProfile) -> dict:
    M = 1e6
    return {
        "ticker": p.ticker,
        "spot": round(p.spot, 2),
        "total_vanna_mm": round(p.total_vanna / M, 3),
        "total_charm_mm": round(p.total_charm / M, 3),
        "vanna_by_strike": [{"strike": k, "vanna_mm": round(v / M, 3)}
                            for k, v in p.vanna_by_strike],
        "charm_by_strike": [{"strike": k, "charm_mm": round(v / M, 3)}
                            for k, v in p.charm_by_strike],
        "meta": {
            "convention": p.convention,
            "scope": p.scope,
            "vanna_unit": "millions of dollars per +1 percentage point of IV",
            "charm_unit": "millions of dollars per day that passes",
            "note": ("Vanna and charm are computed here with Black-Scholes (Cboe provides first-order greeks only). "
                     "Dealer positioning follows the same assumption as GEX: calls positive / puts negative — an assumption, not a fact."),
        },
    }


def gex_surface(chain: Chain,
                dte_max: Optional[int] = 45,
                strike_pct: float = 0.06,
                convention: DealerConvention = "long_call_short_put") -> dict:
    """A two-dimensional GEX surface over expiry × strike.

    The counterpart to UW's "Greek Exposure By Strike And Expiry".
    Returns the structure an ECharts heatmap takes directly: [[x_index, y_index, value], ...]
    """
    spot = chain.spot
    lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)
    cs = [c for c in chain.filter(dte_max=dte_max) if lo <= c.strike <= hi]
    if not cs:
        raise ValueError(f"{chain.ticker} has no contracts within ≤{dte_max}DTE / ±{strike_pct:.0%}")

    expiries = sorted({c.expiry for c in cs})
    strikes = sorted({c.strike for c in cs})
    xi = {e: i for i, e in enumerate(expiries)}
    yi = {k: i for i, k in enumerate(strikes)}

    grid: dict[tuple[int, int], float] = defaultdict(float)
    for c in cs:
        grid[(xi[c.expiry], yi[c.strike])] += contract_gex(c, spot, convention)

    B = 1e9
    data = [[x, y, round(v / B, 4)] for (x, y), v in grid.items() if v]
    vals = [d[2] for d in data] or [0.0]
    return {
        "ticker": chain.ticker,
        "spot": round(spot, 2),
        "expiries": expiries,
        "strikes": strikes,
        "data": data,
        "min": min(vals),
        "max": max(vals),
        "unit": "billions of dollars per 1% move in the underlying",
        "scope": f"≤{dte_max}DTE · strikes ±{strike_pct:.0%} · {len(cs)} contracts",
    }
