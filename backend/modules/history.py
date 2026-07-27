"""Locally accrued history.

━━━ Why this exists ━━━
Unusual Whales' real moat is not exclusive data (the sources are nearly all free and public)
but that **they have been running for years and have accrued the history** — which they sell separately.

A user self-hosting FloorZero starts on day one with **no history at all**, and that has to be said honestly.
What this module does: **start accruing the moment it is installed**, and grow more valuable with use.

⚠️ What cannot be backfilled: historical snapshots of the option chain. Cboe gives only the present.
    What can: EDGAR and FINRA carry their own history (those two lanes can be filled in later).

━━━ Storage ━━━
SQLite, no configuration, defaulting to `~/.floorzero/history.db` in the user's home
(not inside the repo: updating the code or re-cloning must not lose the history a user has accrued —
 VibeResearch's issue #12 was exactly this trap).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from modules import db
from modules.db import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gex_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    captured_at TEXT    NOT NULL,          -- ISO8601 UTC
    scope       TEXT    NOT NULL,          -- the basis: snapshots of different scopes are not comparable
    spot        REAL    NOT NULL,
    total_gex   REAL    NOT NULL,          -- notional dollars per 1%
    gamma_flip  REAL,
    call_wall   REAL,
    put_wall    REAL,
    regime      TEXT    NOT NULL,
    total_vanna REAL,
    total_charm REAL
);
-- Querying by time within one symbol and scope is by far the main read
CREATE INDEX IF NOT EXISTS idx_gex_ticker_time
    ON gex_snapshot (ticker, scope, captured_at);
-- Idempotent: one row per symbol/scope/instant, so repeated collection cannot duplicate
CREATE UNIQUE INDEX IF NOT EXISTS idx_gex_unique
    ON gex_snapshot (ticker, scope, captured_at);
"""


def _ensure_db() -> None:
    db.ensure_schema("gex_snapshot", _SCHEMA)


def _conn():
    _ensure_db()
    return db.connect()


def record_gex(profile_dict: dict, exposures: Optional[dict] = None,
               captured_at: Optional[str] = None) -> bool:
    """Record one GEX snapshot. Returns True for a new row, False if that data instant is already stored.

    ⚠️ **`captured_at` takes the data's own time (`chain.timestamp`), never the wall clock.**
    Cboe's delayed quotes update only every so often, so with `now()` as part of the key,
    two presses of the button store two rows of **identical data under different timestamps**,
    and plotted as a series that is an "observation" conjured out of nothing — it looks like the market moved when nothing did.
    Keyed by the data's own instant, the (ticker, scope, captured_at) unique index finally does block duplicates:
    a row is added only when upstream really did publish something new.

    The degenerate case: only when upstream gives no timestamp does it fall back to the UTC wall clock (and idempotence is then not guaranteed).
    """
    ts = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = profile_dict["meta"]
    # ⭐ It must use scope_key (which carries no contract count) rather than the display scope:
    # the contract count changes daily, and folded into the key it files tomorrow's snapshot under a different scope, so the series never accrues.
    scope_key = meta.get("scope_key") or meta["scope"]
    row = (
        profile_dict["ticker"], ts, scope_key,
        profile_dict["spot"], profile_dict["total_gex_bn"] * 1e9,
        profile_dict.get("gamma_flip"), profile_dict.get("call_wall"),
        profile_dict.get("put_wall"), profile_dict["regime"],
        (exposures or {}).get("total_vanna_mm"),
        (exposures or {}).get("total_charm_mm"),
    )
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO gex_snapshot "
            "(ticker, captured_at, scope, spot, total_gex, gamma_flip, call_wall, "
            " put_wall, regime, total_vanna, total_charm) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        return cur.rowcount > 0


def gex_series(ticker: str, scope: Optional[str] = None,
               limit: int = 200) -> list[dict[str, Any]]:
    """Fetch a symbol's historical series (newest first).

    ⚠️ scope has to be part of the filter: the GEX of `≤7DTE` and of the `whole chain` are not the same order of magnitude,
    and mixed into one chart they draw a meaningless sawtooth.
    """
    sql = "SELECT * FROM gex_snapshot WHERE ticker = ?"
    args: list[Any] = [ticker.upper()]
    if scope:
        sql += " AND scope = ?"
        args.append(scope)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def scopes_for(ticker: str) -> list[str]:
    """Which scopes this symbol has accrued history for (so the frontend can choose, and avoid comparing across them)."""
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT scope FROM gex_snapshot WHERE ticker = ? ORDER BY scope",
            (ticker.upper(),))]


def stats() -> dict:
    """Storage overview — so the user can see how much they have accrued."""
    with _conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, "
            "MIN(captured_at) lo, MAX(captured_at) hi FROM gex_snapshot").fetchone()
    return {
        "snapshots": r["n"], "tickers": r["t"],
        "earliest": r["lo"], "latest": r["hi"],
        "db_path": DB_PATH,
        "note": "Option chain history cannot be backfilled (Cboe gives only the present); it begins accruing once installed.",
    }
