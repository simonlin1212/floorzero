"""Black-Scholes greeks (only the parts GEX needs).

Why gamma is computed here at all — Cboe already gives a gamma, but it is the value **at the current price**.
Computing the gamma flip means answering "**if** the price became X, what would total GEX be",
and gamma varies sharply with price (highest at the money, tending to zero at both ends), so it has to be recomputed at the hypothetical price.

Standard library only, no scipy (keeping the project's principle of no heavy dependencies).
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)
# ⚠️ **Calendar days** (365), not trading days (252): options expire on the calendar, and decay runs through weekends.
# Do not name it TRADING_DAYS — that leads people (and code review) to assume 252,
# and from there to misjudge the /365 conversions elsewhere as inconsistent. A Codex review misread it here once.
DAYS_PER_YEAR = 365.0


def norm_pdf(x: float) -> float:
    """The standard normal density φ(x)."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_gamma(spot: float, strike: float, t_years: float,
             sigma: float, rate: float = 0.04) -> float:
    """Black-Scholes gamma.

    gamma = φ(d1) / (S · σ · √T)

    Calls and puts have **the same** gamma (a direct consequence of put-call parity), so type does not enter into it.
    Degenerate cases (expired, zero volatility, invalid price) return 0 rather than raising —
    they occur daily in a real options chain (expired contracts, deep out-of-the-money contracts with no quote).
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
    """Days → years (0DTE counts as half a trading day, so T=0 cannot blow gamma up to inf)."""
    return max(dte_days, 0.5) / DAYS_PER_YEAR

def _d1_d2(spot: float, strike: float, t_years: float,
           sigma: float, rate: float) -> tuple[float, float] | None:
    """BS d1/d2; degenerate input returns None (on which the caller returns 0)."""
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
    """Vanna = ∂delta/∂σ = ∂vega/∂S.

        vanna = -φ(d1) · d2 / σ

    What it means: how delta moves when implied volatility moves — dealers holding delta neutral
    buy and sell stock as IV rises and falls. One of the main drivers behind a slow grinding rally.

    ⚠️ Calls and puts have **the same** vanna (absent dividends). Verified numerically by finite differences.
    """
    dd = _d1_d2(spot, strike, t_years, sigma, rate)
    if dd is None:
        return 0.0
    d1, d2 = dd
    return -norm_pdf(d1) * d2 / sigma


def bs_charm(spot: float, strike: float, t_years: float,
             sigma: float, rate: float = 0.04) -> float:
    """Charm = ∂delta/∂t, where **t is elapsed time** (not time remaining, τ).

        charm = -φ(d1) · (2rT - d2·σ√T) / (2T·σ√T)

    What it means: delta changes as time passes even with price and volatility perfectly still,
    and dealers must adjust accordingly — one source of the late-day pinning effect.

    ⚠️ **Sign convention: the leading minus sign in the formula already flips τ→t**,
    so the return value is directly "how much delta changes per unit of time elapsed", and callers **must not negate it again**.
    (This docstring once read ∂delta/∂τ, on which a code review concluded the sign was inverted —
     numerical verification: a 30-day out-of-the-money call's delta actually moves −0.003911 over one day,
     and this function after /365 gives −0.003832: same sign, in agreement. Negated, it is simply wrong.
     A finite difference at `h=1e-7` differs from the analytic value by <1e-3.)

    ⚠️ Calls and puts have **the same** charm (absent dividends: put delta = call delta − 1,
    and the derivative of a constant with respect to time is 0). The widely repeated claim that
    puts take the opposite sign is wrong — verified by finite differences: the analytic value agrees
    with the numerical ∂delta/∂τ for both calls and puts, and disagrees once negated.
    """
    dd = _d1_d2(spot, strike, t_years, sigma, rate)
    if dd is None:
        return 0.0
    d1, d2 = dd
    sq = sigma * math.sqrt(t_years)
    return -norm_pdf(d1) * (2 * rate * t_years - d2 * sq) / (2 * t_years * sq)
