"""Cboe official delayed options data.

⚠️ Compliance (tier C): Cboe's Use of Content policy requires written approval and a licence.
This module is **for personal research on your own machine only**. FloorZero ships code,
never data, so the person running it is doing personal use and we are not an OPRA redistributor.
⛔ Never turn this module's output into a publicly visible service (= $1,500/month redistributor fee).

Based on the verified implementation in global-stock-data V2.0 (three Codex review passes).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

# OCC symbol: root + YYMMDD + C/P + 8-digit strike (in thousandths of a dollar)
_OSI = re.compile(
    r"^(?P<root>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
    r"(?P<cp>[CP])(?P<strike>\d{8})$"
)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                    # Windows may lack tzdata
    _ET = None


class DataNotAvailable(RuntimeError):
    """This ticker/day genuinely has no data — callers may safely skip it.

    Kept distinct from configuration errors, rate limiting and network failure, which
    must propagate. Otherwise "could not fetch" masquerades as "does not exist".
    """


class _RateLimiter:
    """Thread-safe minimum-interval throttle (locked, so concurrency cannot punch through)."""

    def __init__(self, max_per_sec: float) -> None:
        self._interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_limiter = _RateLimiter(4)          # self-imposed ceiling for Cboe


@dataclass(frozen=True)
class Contract:
    """A single option contract (immutable, so it is safe to pass around and cache)."""
    symbol: str
    expiry: str            # YYYY-MM-DD
    type: str              # "call" | "put"
    strike: float
    bid: Optional[float]
    ask: Optional[float]
    volume: float
    open_interest: float
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    rho: Optional[float]
    last: Optional[float]

    @property
    def dte(self) -> int:
        """Days to expiry (by US/Eastern date)."""
        return (datetime.strptime(self.expiry, "%Y-%m-%d").date()
                - datetime.strptime(et_today(), "%Y-%m-%d").date()).days


@dataclass(frozen=True)
class Chain:
    ticker: str
    spot: float
    timestamp: Optional[str]
    contracts: tuple[Contract, ...]
    #: The **trading session** this data belongs to (YYYY-MM-DD, from Cboe's `last_trade_time`).
    #: ⚠️ Not the same thing as `timestamp`, which is when Cboe **published** the file
    #: (measured: Friday's 16:00 ET close carries a publish stamp of `2026-07-25 03:44:48`).
    #: Local history **must be keyed by session** — keyed by wall clock, opening the page
    #: once on Saturday and again on Sunday stores one Friday close as "two observations",
    #: and the diff comes out all zeros: it looks like "nothing moved" but there was no new data.
    session: Optional[str] = None

    def expiries(self) -> list[str]:
        return sorted({c.expiry for c in self.contracts})

    def filter(self, expiry: Optional[str] = None, dte_max: Optional[int] = None,
               traded_only: bool = False) -> list[Contract]:
        """expiry='0DTE' takes today's expiry; dte_max takes N days out; traded_only keeps today's trades."""
        cs = list(self.contracts)
        if expiry == "0DTE":
            cs = [c for c in cs if c.expiry == et_today()]
        elif expiry:
            cs = [c for c in cs if c.expiry == expiry]
        if dte_max is not None:
            cs = [c for c in cs if 0 <= c.dte <= dte_max]
        if traded_only:
            cs = [c for c in cs if c.volume > 0]
        return cs


def et_today() -> str:
    """Today's date in US/Eastern, YYYY-MM-DD.

    ⚠️ EDT (UTC-4) and EST (UTC-5) must be told apart: hardcoding UTC-4 pushes the
    04:00–05:00 UTC hour into the next day in winter, picking the wrong 0DTE expiry.
    """
    now = datetime.now(timezone.utc)
    if _ET is not None:
        return now.astimezone(_ET).strftime("%Y-%m-%d")
    # Fallback without tzdata: apply US DST rules directly (switch at local 2:00 = 07:00/06:00 UTC)
    y = now.year
    mar8 = datetime(y, 3, 8, tzinfo=timezone.utc)
    dst_start = (mar8 + timedelta(days=(6 - mar8.weekday()) % 7)).replace(hour=7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = (nov1 + timedelta(days=(6 - nov1.weekday()) % 7)).replace(hour=6)
    offset = 4 if dst_start <= now < dst_end else 5
    return (now - timedelta(hours=offset)).strftime("%Y-%m-%d")


def parse_osi(symbol: str) -> Optional[dict]:
    """Parse an OCC symbol → {expiry, type, strike}; None when it cannot be parsed."""
    m = _OSI.match(symbol)
    if not m:
        return None
    g = m.groupdict()
    return {
        "expiry": f"20{g['y']}-{g['m']}-{g['d']}",
        "type": "call" if g["cp"] == "C" else "put",
        "strike": int(g["strike"]) / 1000.0,
    }


def assert_us_ticker(ticker: str) -> str:
    """Cboe covers US equities only; a HK or A-share code gets a clear message, not an empty result."""
    t = str(ticker).upper().strip()
    if t.endswith(".HK") or (t.isdigit() and len(t) in (4, 5, 6)):
        raise ValueError(f"'{ticker}' is not a US ticker; Cboe options cover US equities only.")
    if not t or not t.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"invalid ticker: '{ticker}'")
    return t


def _get(url: str, timeout: int = 30) -> dict:
    """Fetch one Cboe JSON endpoint.

    ⚠️ Exceptions must be classified **positively**, never by elimination:
    · 404             = this ticker really has no data → DataNotAvailable (caller may skip)
    · 403 / 429 / 5xx = refused, throttled, upstream broken → RuntimeError (must propagate)
    Filing a 403 under "no data" lets a blocked IP silently masquerade as "this ticker has
    no options", and the real cause becomes undiscoverable.
    """
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()                    # inside the try: upstream can return 200 with HTML
    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 404:
            raise DataNotAvailable(f"Cboe has no data for this ticker (404): {url[:70]}") from e
        hint = {403: "refused (throttled or blocked)", 429: "too many requests"}.get(code, "")
        raise RuntimeError(f"CBOE HTTP {code} {hint}: {url[:70]}") from e
    except ValueError as e:                # r.json() failed (an HTML error page, etc.)
        raise RuntimeError(f"Cboe returned non-JSON (possibly an error page): {url[:70]}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"Cboe request failed: {type(e).__name__}: {e}") from e


def option_chain(ticker: str) -> Chain:
    """Fetch the full option chain for one US ticker (delayed)."""
    tk = assert_us_ticker(ticker)
    raw = _get(f"{CBOE_BASE}/options/{tk}.json")
    data = raw.get("data") or {}
    out: list[Contract] = []
    for o in data.get("options") or []:
        meta = parse_osi(o.get("option", ""))
        if not meta:
            continue
        out.append(Contract(
            symbol=o["option"],
            expiry=meta["expiry"], type=meta["type"], strike=meta["strike"],
            bid=o.get("bid"), ask=o.get("ask"),
            volume=o.get("volume") or 0.0,
            open_interest=o.get("open_interest") or 0.0,
            iv=o.get("iv"), delta=o.get("delta"), gamma=o.get("gamma"),
            vega=o.get("vega"), theta=o.get("theta"), rho=o.get("rho"),
            last=o.get("last_trade_price"),
        ))
    if not out:
        raise DataNotAvailable(f"{tk} returned no contracts (no options, or outside Cboe's coverage)")
    spot = data.get("current_price")
    if not spot:
        raise DataNotAvailable(f"{tk} returned no spot price")
    # Trading session: `last_trade_time` looks like "2026-07-24T16:00:00" (US/Eastern close)
    ltt = data.get("last_trade_time") or ""
    session = ltt[:10] if len(ltt) >= 10 and ltt[4] == "-" else None
    return Chain(ticker=tk, spot=float(spot),
                 timestamp=raw.get("timestamp"), session=session,
                 contracts=tuple(out))


# ── Short-lived snapshot cache (**shared** by REST and MCP) ──
# It lives in the source layer rather than app.py: otherwise the MCP path bypasses it and
# get_gex and get_gex_curve see two different snapshots inside one AI session, so the
_CACHE: dict[str, tuple[float, "Chain"]] = {}
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_GUARD = threading.Lock()
CACHE_TTL = 60.0


def _cache_lock(key: str) -> threading.Lock:
    with _CACHE_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def cached_option_chain(ticker: str) -> Chain:
    """Cached chain read; concurrent requests for one ticker collapse into a single upstream call."""
    key = assert_us_ticker(ticker)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    with _cache_lock(key):
        hit = _CACHE.get(key)              # double-check: it may have been filled while we waited
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        chain = option_chain(key)
        _CACHE[key] = (time.monotonic(), chain)
        return chain


def quote(ticker: str) -> dict:
    """Delayed equity snapshot (carries spot, useful for pinning ATM against the chain)."""
    return _get(f"{CBOE_BASE}/quotes/{assert_us_ticker(ticker)}.json")["data"]

# ── Two light endpoints the scanner needs ──

def option_roots() -> list[str]:
    """Every ticker that **has options** (Cboe's official list; measured at 6,300 / 217KB).

    This is the scanner's universe. ⚠️ It means "has options listed", not "traded today" —
    it holds a great many names with years of zero volume.
    """
    raw = _get(f"{CBOE_BASE}/symbol_book/option-roots.json")
    data = raw.get("data") or []
    out, seen = [], set()
    for row in data:
        sym = (row.get("symbol") or "").strip().upper()
        # One ticker can carry several roots (adjusted contracts after splits, etc.); dedupe by symbol
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    if not out:
        raise RuntimeError("Cboe's optionable-ticker list came back empty (the endpoint shape may have changed)")
    return out


@dataclass(frozen=True)
class Quote:
    """Light quote snapshot — **underlying only, no option chain**.

    The scanner's first pass runs on this: 0.4KB per name against 1.5MB for a full chain (**3,750x**).
    Full chains market-wide would take about 3.7 hours and 9.5GB; light quotes take about 26 minutes (4/s).
    """

    symbol: str
    price: Optional[float]
    change_pct: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    prev_close: Optional[float]
    volume: Optional[float]
    #: 30-day implied volatility (%). ⚠️ **IV Rank needs history**; one iv30 reading ranks nothing.
    iv30: Optional[float]
    iv30_change: Optional[float]
    #: The trading session this data belongs to (YYYY-MM-DD)
    session: Optional[str]
    security_type: Optional[str]


def quote(ticker: str) -> Quote:
    """Light quote for one ticker (no option chain)."""
    tk = assert_us_ticker(ticker)
    raw = _get(f"{CBOE_BASE}/quotes/{tk}.json")
    d = raw.get("data") or {}
    if not d.get("symbol"):
        raise DataNotAvailable(f"{tk} has no quote data")
    ltt = d.get("last_trade_time") or ""
    return Quote(
        symbol=d["symbol"],
        price=d.get("current_price"),
        change_pct=d.get("price_change_percent"),
        open=d.get("open"), high=d.get("high"), low=d.get("low"),
        prev_close=d.get("prev_day_close"),
        volume=d.get("volume"),
        iv30=d.get("iv30"),
        iv30_change=d.get("iv30_change"),
        session=ltt[:10] if len(ltt) >= 10 and ltt[4] == "-" else None,
        security_type=d.get("security_type"),
    )

