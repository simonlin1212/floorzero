"""What the scanner accrues locally: a light quote snapshot per day.

━━━ Why it has to be accrued ━━━
The scanner's central metric is **IV Rank**, which by definition is "where current IV sits within the past year's range" —
no history, no metric. Cboe gives only the present `iv30`, and **the past cannot be fetched**.

So this table is the same kind of thing as the OI in `flow_store`: **it starts when you install, and cannot be backfilled**.
The difference is that it is far cheaper — six numbers a row, so 6,000 symbols over a year is on the order of a million rows.

━━━ ⚠️ Keyed by trading session, not wall-clock date ━━━
As in `flow_store`: Cboe's `last_trade_time` is the session the data belongs to.
Open the page twice over a weekend and a wall-clock key stores it as "two days", flooding the IV samples with duplicates —
the sample count is inflated, and IV Rank is built directly on the sample count.
"""
from __future__ import annotations

from typing import Iterable, Optional

from modules import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_snapshot (
    symbol        TEXT NOT NULL,
    session       TEXT NOT NULL,        -- YYYY-MM-DD, the trading session the data belongs to
    price         REAL,
    change_pct    REAL,
    volume        REAL,
    iv30          REAL,
    security_type TEXT,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, session)
);
CREATE INDEX IF NOT EXISTS ix_q_session ON quote_snapshot(session);
CREATE INDEX IF NOT EXISTS ix_q_symbol  ON quote_snapshot(symbol, session);

-- A batch record per scan: how long it ran, how many symbols, how many failed.
-- ⚠️ The failure count has to leave a trace: if 800 symbols could not be fetched in one round,
--    the results table merely looks like "a few symbols missing", and without the trace it is never noticed.
CREATE TABLE IF NOT EXISTS scan_batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    universe    INTEGER NOT NULL,
    scanned     INTEGER NOT NULL,
    stored      INTEGER NOT NULL,
    failed      INTEGER NOT NULL,
    session     TEXT,
    note        TEXT
);
"""


def _ensure() -> None:
    db.ensure_schema("quote_snapshot", SCHEMA)


def record_quotes(rows: Iterable[dict]) -> dict:
    """Write a batch of light quotes (idempotent on symbol+session).

    ⚠️ Rows with no `session` are **dropped and counted**, never given today's date instead:
    get the key wrong and the IV samples are scrambled, in a way the data itself will not show.
    **How many were dropped must be returned** — the previous version computed `dropped` and never returned it,
    so "upstream gave no trading session" was entirely invisible in the interface,
    presenting instead as "those symbols are not in the scan results": exactly a failed fetch dressed up as an absence.

    ⚠️ **No bare `INSERT OR REPLACE`.** Rescanning the same session when upstream happens not to
    return `iv30` this time, a bare overwrite wipes an already-accrued valid value to NULL —
    the sample count falls instead of rising, and this history **cannot be backfilled**. So the three
    nullable fields use `COALESCE(new, old)`: update when there is a new value, keep the old when there is not.
    """
    _ensure()
    now = _now()
    payload = []
    dropped = 0
    for r in rows:
        sess = r.get("session")
        if not sess:
            dropped += 1
            continue
        payload.append((r["symbol"], sess, r.get("price"), r.get("change_pct"),
                        r.get("volume"), r.get("iv30"),
                        r.get("security_type"), now))
    if payload:
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO quote_snapshot"
                "(symbol, session, price, change_pct, volume, iv30,"
                " security_type, captured_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol, session) DO UPDATE SET "
                "  price         = COALESCE(excluded.price,         price),"
                "  change_pct    = COALESCE(excluded.change_pct,    change_pct),"
                "  volume        = COALESCE(excluded.volume,        volume),"
                "  iv30          = COALESCE(excluded.iv30,          iv30),"
                "  security_type = COALESCE(excluded.security_type, security_type),"
                "  captured_at   = excluded.captured_at", payload)
    return {"stored": len(payload), "dropped_no_session": dropped}


def history(symbols: Optional[list[str]] = None,
            lookback: int = 252,
            as_of: Optional[str] = None) -> dict[str, dict]:
    """Fetch each symbol's iv30 / volume history (ascending by session).

    Returns `{symbol: {"iv": [...], "volume": [...]}}`.

    ⚠️ **`as_of` is mandatory.** Viewing a past day's scan results with later data counted into the
    IV Rank sample is **lookahead bias** — judging whether that day's IV was high or low using
    quotes that had not happened yet. In the extreme the current value falls outside the sample range
    and the resulting rank goes beyond 0-100 altogether.
    ⚠️ One query, grouped in memory afterwards, and **never one query per symbol** — 6,000 symbols
    one at a time is 6,000 round trips, and the scanner page becomes unusable.
    """
    _ensure()
    out: dict[str, dict] = {}
    where = []
    args_list: list = []
    if symbols:
        where.append(f"symbol IN ({','.join('?' * len(symbols))})")
        args_list += list(symbols)
    if as_of:
        where.append("session <= ?")
        args_list.append(as_of)
    sql = "SELECT symbol, session, iv30, volume FROM quote_snapshot"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY symbol, session"
    args = tuple(args_list)
    with db.connect() as conn:
        for r in conn.execute(sql, args):
            d = out.setdefault(r["symbol"], {"iv": [], "volume": []})
            # ⚠️ Nulls are **skipped**, not filled with 0: a 0 drags the bottom of the IV range down to 0,
            #    after which IV Rank reads high forever.
            if r["iv30"] is not None:
                d["iv"].append(float(r["iv30"]))
            if r["volume"] is not None:
                d["volume"].append(float(r["volume"]))
    for d in out.values():
        d["iv"] = d["iv"][-lookback:]
        d["volume"] = d["volume"][-lookback:]
    return out


def latest_session() -> Optional[str]:
    _ensure()
    with db.connect() as conn:
        r = conn.execute("SELECT MAX(session) s FROM quote_snapshot").fetchone()
    return r["s"] if r and r["s"] else None


def quotes_at(session: str) -> list[dict]:
    """As of a given trading session, **each symbol's own most recent** quote.

    ⚠️ **Not `WHERE session = ?`.** Symbols within one scan need not share a `last_trade_time`
    (a halt, a delayed upstream update, or a scan spanning two sessions all cause it).
    Take only those equal to the newest session and another set of **successfully stored** symbols vanishes whole —
    presenting as "they are not in the scan results" when they are merely a session older.

    So this takes "the most recent row at or before the target session" and returns each row's own session
    alongside, so a mismatch is visible to the user at a glance.
    """
    _ensure()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT q.symbol, q.session, q.price, q.change_pct, q.volume, "
            "       q.iv30, q.security_type "
            "FROM quote_snapshot q "
            "JOIN (SELECT symbol, MAX(session) AS mx FROM quote_snapshot "
            "      WHERE session <= ? GROUP BY symbol) m "
            "  ON q.symbol = m.symbol AND q.session = m.mx", (session,))]


def start_batch(universe: int) -> int:
    _ensure()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO scan_batch(started_at, universe, scanned, stored, failed)"
            " VALUES (?,?,0,0,0)", (_now(), universe))
        return int(cur.lastrowid or 0)


def finish_batch(batch_id: int, scanned: int, stored: int, failed: int,
                 session: Optional[str], note: str = "") -> None:
    _ensure()
    with db.connect() as conn:
        conn.execute(
            "UPDATE scan_batch SET finished_at=?, scanned=?, stored=?, failed=?,"
            " session=?, note=? WHERE id=?",
            (_now(), scanned, stored, failed, session, note, batch_id))


def batches(limit: int = 10) -> list[dict]:
    _ensure()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scan_batch ORDER BY id DESC LIMIT ?", (limit,))]


def stats() -> dict:
    _ensure()
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            "COUNT(DISTINCT session) sessions, MIN(session) lo, MAX(session) hi "
            "FROM quote_snapshot").fetchone()
        # How many symbols have accrued enough for IV Rank — the direct answer to "how usable is this section right now"
        ready = conn.execute(
            "SELECT COUNT(*) n FROM (SELECT symbol FROM quote_snapshot "
            "WHERE iv30 IS NOT NULL GROUP BY symbol HAVING COUNT(*) >= ?)",
            (60,)).fetchone()
    return {"rows": r["n"], "symbols": r["syms"], "sessions": r["sessions"],
            "earliest": r["lo"], "latest": r["hi"],
            "iv_ready_symbols": ready["n"]}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
