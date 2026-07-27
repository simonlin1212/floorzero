"""The stock page: nine lanes converging on one ticker.

━━━━━━━━━━━━━━ ⚠️ The hard part is not aggregation, it is that the lanes disagree about "now" ━━━━━━━━━━━━━━

Put nine sources on one screen and the natural reading is that they describe the
**same moment**. They differ in age by **two orders of magnitude**:

| Lane | What the data is of | Lag |
|---|---|---|
| Chain / GEX / Scanner | the last trading session | ~15 min delayed |
| Insider Form 4 | trade date | filing due within 2 business days |
| Fails-to-deliver | settlement date | about 2 weeks |
| Congressional filings | trade date | 45 days by statute, often longer |
| Off-exchange / dark pools | week | about 4 weeks |
| Institutional 13F | **quarter-end** | **at least 45 days** |

**So "an overview of one ticker" is an illusion.** The 13F is a quarter-end three
months back; the chain is yesterday's close. Set side by side without their dates,
a reader will assemble them into one coherent story — and that story is fabricated.

What this module does instead: **every lane carries its own `as_of` and lag**, the
interface orders them newest to oldest, and ⛔ **no cross-source score is produced**.
Weighting different vintages into one number is the mistake this page is likeliest to
make and least likely to catch.

━━━━━━━━━━━━━━ ⚠️ A missing lane is not a lane reading zero ━━━━━━━━━━━━━━

Four of the nine depend on data **already synced or accrued locally** (insiders,
institutions, shorts, scanner) and one is **off by default** (darkpool). Whenever a
lane cannot be filled, this page gives **that lane's own reason**: not yet synced /
not enough accrued here / source switched off / fetch failed. ⛔ Never a blank or a
zero, which would read as "this ticker has no insider trading".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

#: Each lane's name and its inherent lag (for ordering and for the hint; not exact)
LANES = {
    "quote": ("Quote and option chain", "Cboe, delayed about 15 minutes"),
    "flow": ("Options flow", "same; open interest settles overnight, so it lags a day"),
    "gex": ("Gamma exposure", "same"),
    "scanner": ("IV rank", "accrued locally; IV Rank needs 60 sessions"),
    "insider": ("Insider Form 4", "filing due within 2 business days"),
    "shorts": ("Fails-to-deliver", "SEC publishes about two weeks later"),
    "congress": ("Congressional filings", "45 days by statute, often longer"),
    "darkpool": ("Off-exchange / dark pools", "FINRA, about four weeks"),
    "institution": ("Institutional 13F", "quarter-end, at least 45 days behind"),
}

NOTES = {
    "timeline": (
        "**These nine differ in age by two orders of magnitude**: the option chain is "
        "the last trading session, the 13F a quarter-end three months back. Side by side "
        "they read as one moment — so each carries its own date and lag, ordered newest first."),
    "no_score": (
        "⛔ **No cross-source score is produced here.** Weighting different vintages and "
        "definitions into a single bullish-bearish number is the mistake this page is "
        "likeliest to make and least likely to catch: it treats a quarter-old position as "
        "contemporary with yesterday's option volume. The lanes are laid out; the reading is yours."),
    "missing": (
        "When a lane is empty this page says **why that lane** is empty: not yet synced / "
        "not enough accrued / source switched off / fetch failed. ⛔ None of those mean \"this ticker has no such activity\"."),
}


@dataclass(frozen=True)
class Lane:
    """One lane on the stock page."""

    key: str
    title: str
    lag_note: str
    #: When **this lane's** data is from (YYYY-MM-DD or an ISO timestamp)
    as_of: Optional[str]
    ok: bool
    #: Reason code when it could not be filled:
    #: not_synced / not_enough (not accrued here yet) / disabled (source switched off) /
    #: fetch_failed / no_data (genuinely absent) / no_mapping (cannot be located) /
    #: bad_symbol
    #: ⚠️ All six must read differently in the UI — only no_data means "no such activity".
    reason: Optional[str]
    detail: Optional[str]
    data: Any

    @property
    def lag_days(self) -> Optional[int]:
        """How many days old.

        ⚠️ Measured against **today in US/Eastern**, not the server's local date. Every
        `as_of` here is a US market date — trading, settlement or filing. Against a local
        date, the same instant yields lags a day apart in Auckland and Los Angeles, and a
        "lag" that drifts with where you deployed means nothing.
        """
        if not self.as_of:
            return None
        try:
            d = datetime.fromisoformat(self.as_of.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                d = date.fromisoformat(self.as_of[:10])
            except ValueError:
                return None
        # ⚠️ Even fetching "today in US/Eastern" needs a guard: a missing tzdata will raise
        #    from here, and `lag_days` is evaluated inside `assemble()` — already outside
        #    every per-lane guard, so it would take the whole page down with a 500.
        try:
            from sources.cboe import et_today
            today = date.fromisoformat(et_today())
        except Exception:                            # noqa: BLE001
            today = date.today()
        # ⚠️ A future date yields a negative, which sorting lifts to the top as "the newest".
        #    A date in the future can only mean bad data or a timezone problem — clamp and record.
        return max(0, (today - d).days)


#: ⭐ **Only the codes in here mean "this ticker genuinely has no such activity".**
#: Everything else means "we could not get it". This is a constant rather than something
#: inferred from label text, so UI, MCP and tests all read the same thing.
MEANS_ABSENT = frozenset({"no_data"})

#: Could not be fetched, though the data may well exist.
MEANS_UNAVAILABLE = frozenset({
    "not_synced", "not_enough", "disabled", "fetch_failed",
    "no_mapping", "bad_symbol",
})

REASON_LABEL = {
    "not_synced": "not synced locally yet (not the same as the ticker having none)",
    "not_enough": "not enough history accrued here (it cannot be backfilled, only accrued)",
    "disabled": "this source is off by default (a configuration state, not absence of data)",
    "fetch_failed": "fetch failed (not absence of data)",
    "no_data": "this ticker genuinely has no record in that source",
    "bad_symbol": "invalid ticker, or outside the source's coverage",
    # ⚠️ Strictly distinct from no_data: the data exists, we just **cannot locate it from a ticker**
    "no_mapping": "the source keys on something else (13F gives only CUSIP), so a ticker cannot locate it",
}


def lane(key: str, *, as_of: Optional[str] = None, data: Any = None,
         reason: Optional[str] = None, detail: Optional[str] = None) -> Lane:
    title, lag_note = LANES.get(key, (key, ""))
    return Lane(key=key, title=title, lag_note=lag_note, as_of=as_of,
                ok=reason is None, reason=reason, detail=detail, data=data)


def to_dict(l: Lane, lag_days: Optional[int] = ...) -> dict:
    if lag_days is ...:
        try:
            lag_days = l.lag_days
        except Exception:                            # noqa: BLE001
            lag_days = None
    return {
        "key": l.key, "title": l.title, "lag_note": l.lag_note,
        "as_of": l.as_of, "lag_days": lag_days,
        "ok": l.ok, "reason": l.reason,
        "reason_label": REASON_LABEL.get(l.reason or "", None),
        # So the frontend and tool layer need not decide for themselves what counts as absent
        "means_absent": (l.reason in MEANS_ABSENT) if l.reason else None,
        "detail": l.detail, "data": l.data,
    }


def assemble(lanes: list[Lane]) -> dict:
    """Order newest to oldest by date.

    ⚠️ The sort key is **lag in days**, not an order we hardcoded — which lane is oldest
    is decided by the data. Lanes with no date (the ones that failed) sink to the bottom.
    """
    def _age(l: "Lane") -> Optional[int]:
        # ⚠️ `lag_days` is a property that parses dates — raising here would also take
        #    the whole page down (it is already outside every per-lane guard).
        try:
            return l.lag_days
        except Exception:                            # noqa: BLE001
            return None

    ages = {l.key: _age(l) for l in lanes}
    ordered = sorted(lanes, key=lambda l: (ages[l.key] is None, ages[l.key] or 0))
    ok = [l for l in ordered if l.ok]
    missing = [l for l in ordered if not l.ok]
    spread = None
    days = [ages[l.key] for l in ok if ages[l.key] is not None]
    if len(days) >= 2:
        spread = {"newest": min(days), "oldest": max(days)}
    return {
        "lanes": [to_dict(l, ages[l.key]) for l in ordered],
        "available": len(ok), "unavailable": len(missing),
        # Newest against oldest — that number is the honest caption for the word "overview"
        "lag_spread_days": spread,
        "notes": NOTES,
    }
