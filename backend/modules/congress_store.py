"""Local cache and incremental sync for congressional trades.

━━━ Why a cache is unavoidable ━━━
The House filed 313 PTRs in 2026, each its own PDF.
At our self-imposed 3 requests/second that is 100+ seconds to fetch — **fetching live on every page request is out of the question**.
So: fetch once → parse → store locally → afterwards only pick up new filings.

━━━ The key difference from GEX history ━━━
GEX history **cannot be backfilled** (Cboe only gives the present);
congressional disclosures **come with all their history** (the annual ZIP holds the whole year), so this lane can be filled in completely.
That difference belongs in the README, spelled out for the user.

⚠️ **Filings that cannot be parsed go into the store too** (with `unparsed_reason` recording why):
10.5% of House PTRs are paper scans. Without a record of them, every sync refetches them,
and the user never gets to see that 33 filings exist and cannot be read.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

from modules import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS congress_filing (
    doc_id      TEXT NOT NULL,
    chamber     TEXT NOT NULL,
    member      TEXT NOT NULL,
    state_district TEXT,
    filing_date TEXT,
    year        TEXT,
    source_url  TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    unparsed_reason TEXT,                    -- NULL = parsed successfully
    terminal    INTEGER NOT NULL DEFAULT 0,  -- 1 = will never parse (a scan); do not retry
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (chamber, doc_id)
);
CREATE TABLE IF NOT EXISTS congress_trade (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chamber     TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,            -- index within the filing, for idempotence
    member      TEXT NOT NULL,
    state_district TEXT,
    ticker      TEXT,
    asset_name  TEXT,
    asset_type  TEXT,
    asset_type_label TEXT,
    tx_type     TEXT,
    tx_type_label TEXT,
    tx_date     TEXT,
    notification_date TEXT,
    filing_date TEXT,
    amount_low  INTEGER,
    amount_high INTEGER,
    amount_raw  TEXT,
    owner       TEXT,
    delay_days  INTEGER,
    source_url  TEXT
);
-- Idempotent: rerunning the sync will not insert the same trade twice
CREATE UNIQUE INDEX IF NOT EXISTS idx_ctrade_unique
    ON congress_trade (chamber, doc_id, seq);
CREATE INDEX IF NOT EXISTS idx_ctrade_ticker ON congress_trade (ticker);
CREATE INDEX IF NOT EXISTS idx_ctrade_date   ON congress_trade (tx_date);
CREATE INDEX IF NOT EXISTS idx_ctrade_member ON congress_trade (member);
"""


def _init() -> None:
    db.ensure_schema("congress", _SCHEMA)


def known_doc_ids(chamber: str) -> set[str]:
    """Every filing seen so far, including those that failed to parse."""
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT doc_id FROM congress_filing WHERE chamber = ?", (chamber,))}


def settled_doc_ids(chamber: str) -> set[str]:
    """Filings that **need no further work**: parsed successfully, plus those that will never parse (scans).

    ⚠️ The balance here matters, and it can be got wrong in both directions:
    - skip only the successes → the 33 scans are picked again every time and **eat the whole quota**,
      so earlier filings never get a turn (the smaller the limit, the faster they starve).
    - count every failure as done → once the parser improves, or a temporarily unreachable file
      comes back, **there is never another attempt**.
    So the split is terminal vs temporary: a scan is terminal (without OCR it cannot be read),
    while a network failure is temporary and gets tried again next time.
    """
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT doc_id FROM congress_filing "
            "WHERE chamber = ? AND (unparsed_reason IS NULL OR terminal = 1)",
            (chamber,))}


def save_filing(filing: Any, trades: list[dict],
                unparsed_reason: Optional[str] = None,
                terminal: bool = False) -> None:
    """Write one filing and its trades (idempotent, and **a rerun replaces the entire filing**).

    ⚠️ Trades must be deleted and reinserted rather than `INSERT OR IGNORE`:
    when a filing is reparsed (the parser improved, or upstream issued an amendment),
    IGNORE keeps the old rows, discards the new ones, and leaves surplus rows on the end —
    so `trade_count` no longer matches the cache, and a bad old parse can never be washed out.
    Delete and insert sit in one transaction, so a failure part-way leaves no half a filing.
    """
    _init()
    now = datetime.now().isoformat(timespec="seconds")
    fd = filing.filing_date.isoformat() if filing.filing_date else None
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO congress_filing "
            "(doc_id, chamber, member, state_district, filing_date, year, "
            " source_url, trade_count, unparsed_reason, terminal, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (filing.doc_id, filing.chamber, filing.name, filing.state_district,
             fd, filing.year, filing.detail_url, len(trades), unparsed_reason,
             int(terminal), now))
        # Full replacement: clear the old rows first (a unique index blocks duplicates, not leftover rows)
        conn.execute("DELETE FROM congress_trade WHERE chamber = ? AND doc_id = ?",
                     (filing.chamber, filing.doc_id))
        for i, t in enumerate(trades):
            conn.execute(
                "INSERT OR IGNORE INTO congress_trade "
                "(chamber, doc_id, seq, member, state_district, ticker, asset_name, "
                " asset_type, asset_type_label, tx_type, tx_type_label, tx_date, "
                " notification_date, filing_date, amount_low, amount_high, amount_raw, "
                " owner, delay_days, source_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (filing.chamber, filing.doc_id, i, t["member"], t["state_district"],
                 t["ticker"], t["asset_name"], t["asset_type"], t["asset_type_label"],
                 t["tx_type"], t["tx_type_label"], t["tx_date"], t["notification_date"],
                 t["filing_date"], t["amount_low"], t["amount_high"], t["amount_raw"],
                 t["owner"], t["delay_days"], t["source_url"]))


def query_trades(chamber: Optional[str] = None, ticker: Optional[str] = None,
                 member: Optional[str] = None, since: Optional[str] = None,
                 tx_type: Optional[str] = None,
                 limit: Optional[int] = 500) -> list[dict]:
    """Query cached trades (most recent trade date first). `limit=None` returns every match.

    ⚠️ `None` exists for the aggregates, which have to describe the whole filtered set rather
    than its newest page — a page-bounded summary labels itself as covering the period and does
    not. The detail listings keep a limit; they are a page and say so.
    """
    _init()
    sql = "SELECT * FROM congress_trade WHERE 1=1"
    args: list[Any] = []
    if chamber:
        sql += " AND chamber = ?"; args.append(chamber)
    if ticker:
        sql += " AND ticker = ?"; args.append(ticker.upper())
    if member:
        sql += " AND member LIKE ?"; args.append(f"%{member}%")
    if since:
        sql += " AND tx_date >= ?"; args.append(since)
    if tx_type == "buy":
        sql += " AND tx_type = 'P'"
    elif tx_type == "sell":
        sql += " AND tx_type LIKE 'S%'"
    # tx_date is an ISO string, so lexical order is chronological order; NULLs sort last
    sql += " ORDER BY tx_date IS NULL, tx_date DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def stats() -> dict:
    """Cache overview — **it has to report how many could not be read, too**.

    Reporting "N trades parsed" alone leaves the user thinking that is everything,
    when in reality 10% of filings are paper scans and never entered that count.
    """
    _init()
    with db.connect() as conn:
        t = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) tk, COUNT(DISTINCT member) mb, "
            "MIN(tx_date) lo, MAX(tx_date) hi FROM congress_trade").fetchone()
        f = conn.execute(
            "SELECT chamber, COUNT(*) n, SUM(unparsed_reason IS NOT NULL) bad "
            "FROM congress_filing GROUP BY chamber").fetchall()
        last = conn.execute(
            "SELECT MAX(synced_at) FROM congress_filing").fetchone()[0]
    chambers = {r["chamber"]: {"filings": r["n"], "unparsed": r["bad"] or 0} for r in f}
    total_f = sum(c["filings"] for c in chambers.values())
    total_bad = sum(c["unparsed"] for c in chambers.values())
    return {
        "trades": t["n"], "tickers": t["tk"], "members": t["mb"],
        "earliest_trade": t["lo"], "latest_trade": t["hi"],
        "filings": total_f, "unparsed_filings": total_bad,
        "unparsed_pct": round(total_bad / total_f * 100, 1) if total_f else 0.0,
        "by_chamber": chambers, "last_sync": last, "db_path": db.DB_PATH,
        "note": "Almost all unparsed filings are paper scans (the whole filing is an image), "
                "whose detail cannot be read without OCR — they do exist, they are simply not in the trade count above.",
    }


def unparsed_filings(limit: int = 100) -> list[dict]:
    """List the filings that could not be read (giving the user a link straight to the original, rather than a bare \"no data\")."""
    _init()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT chamber, doc_id, member, state_district, filing_date, "
            "       source_url, unparsed_reason "
            "FROM congress_filing WHERE unparsed_reason IS NOT NULL "
            "ORDER BY filing_date DESC LIMIT ?", (limit,))]
