"""Local storage for insider trades.

━━━ Why it has to be stored ━━━
Neither source suits being fetched afresh on every request:
- the quarterly dataset is 100k transactions a quarter (~2.4s to download and parse, but wasteful on every request)
- the daily XML is fetched filing by filing: 645 filings in a day = 645 requests

Stored, one quarterly import covers a quarter and the daily increment only picks up new filings.

⚠️ **The two sources overlap** (the quarterly dataset reaches quarter-end, and the daily fetch may have taken the same day).
The `(accession, seq)` unique index deduplicates — one transaction is kept once, whichever route it arrived by.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from modules import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_trade (
    accession   TEXT NOT NULL,
    -- ⭐ A content fingerprint, not a row index: the two import paths are not guaranteed to iterate in
    --    the same order, and a row index as key makes different trades collide and drops data silently (see insider.trade_key)
    trade_key   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ticker      TEXT,
    company     TEXT,
    issuer_cik  TEXT,
    owner       TEXT,
    owner_cik   TEXT,
    is_officer  INTEGER,
    is_director INTEGER,
    is_ten_pct  INTEGER,
    officer_title TEXT,
    security    TEXT,
    tx_code     TEXT,
    tx_group    TEXT,               -- open_market / compensation / other
    direction   TEXT,               -- buy / sell / NULL (only open-market transactions have a direction)
    tx_date     TEXT,
    filing_date TEXT,
    shares      REAL,
    price       REAL,
    value       REAL,
    acquired_disposed TEXT,
    shares_after REAL,
    is_direct   INTEGER,
    is_10b5_1   INTEGER,            -- NULL = the filing left the box unticked, or predates the field
    form_type   TEXT NOT NULL DEFAULT '4',   -- 4 / 4/A
    delay_days  INTEGER,
    source_url  TEXT,
    source      TEXT NOT NULL,      -- dataset / daily
    PRIMARY KEY (accession, trade_key)
);
CREATE INDEX IF NOT EXISTS idx_ins_ticker ON insider_trade (ticker, tx_date);
CREATE INDEX IF NOT EXISTS idx_ins_date   ON insider_trade (tx_date);
CREATE INDEX IF NOT EXISTS idx_ins_group  ON insider_trade (tx_group, tx_date);
CREATE INDEX IF NOT EXISTS idx_ins_owner  ON insider_trade (owner);

-- Individual filings that will never parse (such as pre-2003 plain-text Form 4s, which have no XML).
-- ⚠️ Without recording them, one unreadable filing keeps **that entire day** from ever being marked complete,
-- so every sync redownloads all 600-700 of that day's filings — at unbounded cost.
-- The same problem as the Congress section's terminal vs temporary split (fixed there; identical in shape here).
CREATE TABLE IF NOT EXISTS insider_dead_filing (
    accession   TEXT PRIMARY KEY,
    day         TEXT,
    reason      TEXT,
    recorded_at TEXT NOT NULL
);

-- Source batches already imported (a quarter, or a day), for skipping on the next increment
CREATE TABLE IF NOT EXISTS insider_batch (
    kind        TEXT NOT NULL,      -- quarter / day
    key         TEXT NOT NULL,      -- '2026q1' / '2026-07-24'
    rows        INTEGER NOT NULL,
    filings     INTEGER,
    errors      INTEGER NOT NULL DEFAULT 0,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
"""

_COLS = ("accession", "trade_key", "seq", "ticker", "company", "issuer_cik", "owner", "owner_cik",
         "is_officer", "is_director", "is_ten_pct", "officer_title", "security",
         "tx_code", "tx_group", "direction", "tx_date", "filing_date", "shares",
         "price", "value", "acquired_disposed", "shares_after", "is_direct",
         "is_10b5_1", "form_type", "delay_days", "source_url", "source")


def _init() -> None:
    db.ensure_schema("insider", _SCHEMA)


def save_trades(rows: list[dict], source: str) -> int:
    """Bulk write (idempotent). Returns the number of rows actually added.

    Uses `INSERT OR IGNORE`: where the two sources overlap, whichever arrived first is kept —
    the content of a given transaction is identical either way (both come from the same filing), so there is no "which is newer".
    The deduplication key is `(accession, trade_key)`, and **trade_key is a content fingerprint, not a row index** —
    row order is not guaranteed to agree across the two paths, and using it makes different trades collide and drops data silently.
    """
    _init()
    if not rows:
        return 0
    payload = [
        (r["accession"], r["trade_key"], r["seq"], r["ticker"], r["company"], r["issuer_cik"],
         r["owner"], r["owner_cik"], int(bool(r["is_officer"])),
         int(bool(r["is_director"])), int(bool(r["is_ten_pct"])),
         r["officer_title"], r["security"], r["tx_code"], r["group"], r["direction"],
         r["tx_date"], r["filing_date"], r["shares"], r["price"], r["value"],
         r["acquired_disposed"], r["shares_after"], int(bool(r["is_direct"])),
         None if r["is_10b5_1"] is None else int(r["is_10b5_1"]),
         r.get("form_type") or "4", r["delay_days"], r["source_url"], source)
        for r in rows
    ]
    sql = (f"INSERT OR IGNORE INTO insider_trade ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM insider_trade").fetchone()[0]
        conn.executemany(sql, payload)
        after = conn.execute("SELECT COUNT(*) FROM insider_trade").fetchone()[0]
    return after - before


def mark_batch(kind: str, key: str, rows: int,
               filings: Optional[int] = None, errors: int = 0) -> None:
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO insider_batch (kind, key, rows, filings, errors, synced_at) "
            "VALUES (?,?,?,?,?,?)",
            (kind, key, rows, filings, errors,
             datetime.now().isoformat(timespec="seconds")))


def mark_dead(accession: str, day: str, reason: str) -> None:
    """Record a filing that will **never parse** (the old format with no XML)."""
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO insider_dead_filing "
            "(accession, day, reason, recorded_at) VALUES (?,?,?,?)",
            (accession, day, reason, datetime.now().isoformat(timespec="seconds")))


def dead_accessions() -> set[str]:
    """Filings known to be unreadable — skipped on retry rather than downloaded again for nothing."""
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT accession FROM insider_dead_filing")}


def known_batches(kind: str) -> set[str]:
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT key FROM insider_batch WHERE kind = ?", (kind,))}


def query(ticker: Optional[str] = None, owner: Optional[str] = None,
          group: Optional[str] = None, direction: Optional[str] = None,
          since: Optional[str] = None, min_value: Optional[float] = None,
          role: Optional[str] = None, plan: Optional[str] = None,
          include_amendments: bool = False, limit: int = 300) -> list[dict]:
    """Query transaction detail (most recent trade date first).

    `group` is unrestricted by default; the UI shows `open_market` only —
    because compensation makes up seven tenths of the rows and drowns the real buying and selling.
    """
    _init()
    # ⚠️ NULL = **not marked** (filings before 2023 had no such field), and not "confirmed as not under a plan" —
    # mixing them distorts the comparison between planned and spur-of-the-moment. The three states are in _where().
    w, args = _where(ticker=ticker, owner=owner, group=group, direction=direction,
                     since=since, min_value=min_value, role=role, plan=plan,
                     include_amendments=include_amendments)
    sql = f"SELECT * FROM insider_trade {w}"
    sql += " ORDER BY tx_date IS NULL, tx_date DESC, value DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _where(ticker=None, owner=None, group=None, direction=None, since=None,
           min_value=None, role=None, plan=None,
           include_amendments: bool = False) -> tuple[str, list]:
    """The filter conditions — detail and aggregate **share this one place**, so the two cannot drift."""
    # ⚠️ Amendments (4/A) are excluded by default: an amendment usually **restates** the original's transactions,
    # so counting it alongside the original is double counting. We **do not pair originals with their amendments**
    # and substitute (that would need DATE_OF_ORIG_SUB matched filing by filing), so the conservative course is taken:
    # aggregates use original Form 4s only, and amendments remain queryable with include_amendments=True.
    sql, args = "WHERE 1=1", []
    if not include_amendments:
        sql += " AND form_type = '4'"
    if ticker:
        sql += " AND ticker = ?"; args.append(ticker.upper())
    if owner:
        sql += " AND owner LIKE ?"; args.append(f"%{owner}%")
    if group:
        sql += " AND tx_group = ?"; args.append(group)
    if direction:
        sql += " AND direction = ?"; args.append(direction)
    if since:
        sql += " AND tx_date >= ?"; args.append(since)
    if min_value:
        sql += " AND value >= ?"; args.append(min_value)
    if role == "officer":
        sql += " AND is_officer = 1"
    elif role == "director":
        sql += " AND is_director = 1"
    elif role == "ten_pct":
        sql += " AND is_ten_pct = 1"
    if plan == "yes":
        sql += " AND is_10b5_1 = 1"
    elif plan == "no":
        sql += " AND is_10b5_1 = 0"
    elif plan == "unknown":
        sql += " AND is_10b5_1 IS NULL"
    return sql, args


def aggregate(top: int = 20, **filters) -> dict:
    """Aggregate **in SQL over every matching row**.

    ⚠️ Never "take the newest N rows and aggregate in Python": when the filter matches more than N rows,
    the totals, the cluster-buy table and the ticker table describe only **those newest N rows**
    while the label speaks of the whole period — and adding a "truncated" hint does not make the numbers right.

    Classification (tx_group / direction) is computed in Python **at write time** and stored as columns,
    so this only groups by column — the classification logic still exists in exactly one place, not one in SQL and one in Python.
    """
    _init()
    w, a = _where(**filters)
    with db.connect() as conn:
        tot = conn.execute(
            f"SELECT COUNT(*) n, "
            f" SUM(tx_group='open_market') om, "
            f" SUM(tx_group='compensation') comp, "
            f" SUM(tx_group='other') other, "
            f" SUM(direction='buy') buys, SUM(direction='sell') sells, "
            f" SUM(CASE WHEN direction='buy' THEN value ELSE 0 END) bv, "
            f" SUM(CASE WHEN direction='sell' THEN value ELSE 0 END) sv, "
            f" SUM(direction='sell' AND is_10b5_1=1) plan_sells "
            f"FROM insider_trade {w}", a).fetchone()
        by_ticker = [dict(r) for r in conn.execute(
            f"SELECT ticker, MAX(company) company, "
            f" SUM(direction='buy') buys, SUM(direction='sell') sells, "
            f" SUM(CASE WHEN direction='buy' THEN value ELSE 0 END) buy_value, "
            f" SUM(CASE WHEN direction='sell' THEN value ELSE 0 END) sell_value, "
            f" COUNT(DISTINCT CASE WHEN direction='buy' THEN owner END) insider_count, "
            f" GROUP_CONCAT(DISTINCT CASE WHEN direction='buy' THEN owner END) buyers "
            f"FROM insider_trade {w} AND ticker IS NOT NULL "
            f"GROUP BY ticker", a)]
        by_owner = [dict(r) for r in conn.execute(
            f"SELECT owner, ticker, MAX(officer_title) title, "
            f" SUM(direction='buy') buys, SUM(direction='sell') sells, "
            f" SUM(CASE WHEN direction='buy' THEN value ELSE 0 END) buy_value, "
            f" SUM(CASE WHEN direction='sell' THEN value ELSE 0 END) sell_value "
            f"FROM insider_trade {w} "
            f"GROUP BY owner, ticker "
            # ⚠️ The ordering does not reference the aggregate alias but rewrites the expression:
            # whether `MAX(alias, alias)` parses as the scalar max or the aggregate MAX depends on the SQLite version,
            # and older ones raise "misuse of aliased aggregate". This machine runs 3.53.2 and is fine,
            # but a self-hosting user's version is out of our hands — so use the spelling that works everywhere, at zero cost.
            f"ORDER BY MAX(SUM(CASE WHEN direction='buy' THEN value ELSE 0 END), "
            f"           SUM(CASE WHEN direction LIKE 'sell' THEN value ELSE 0 END)) DESC "
            f"LIMIT ?", a + [top])]
        delays = [r[0] for r in conn.execute(
            f"SELECT delay_days FROM insider_trade {w} AND delay_days >= 0", a)]
        anomalies = conn.execute(
            f"SELECT COUNT(*) FROM insider_trade {w} AND tx_date > filing_date",
            a).fetchone()[0]
        # The count of mis-entered prices: value was already nulled at write time (keeping it out of the money totals),
        # but **the count has to be reported** — dropping something from the statistics is not the same as pretending it does not exist.
        # The condition matches InsiderTrade.price_implausible.
        implausible = conn.execute(
            f"SELECT COUNT(*) FROM insider_trade {w} AND "
            f"(price > 1000000 OR (shares IS NOT NULL AND price IS NOT NULL "
            f" AND shares * price > 200000000000))", a).fetchone()[0]
    for e in by_ticker:
        e["buy_value"] = round(e["buy_value"] or 0)
        e["sell_value"] = round(e["sell_value"] or 0)
        e["net_value"] = e["buy_value"] - e["sell_value"]
        # The list of buyers: the UI tooltip shows it. It used to be hardcoded to an empty list —
        # so the interface only ever had a count and never the names, which hid half the information.
        e["insiders"] = sorted((e.pop("buyers", None) or "").split(","))[:10]
    for m in by_owner:
        m["buy_value"] = round(m["buy_value"] or 0)
        m["sell_value"] = round(m["sell_value"] or 0)
    return {
        "counts": dict(tot), "delays": delays, "anomalies": anomalies,
        "implausible": implausible,
        "by_ticker": sorted(by_ticker, key=lambda x: abs(x["net_value"]),
                            reverse=True)[:top],
        "cluster_buys": sorted([e for e in by_ticker if e["buys"]],
                               key=lambda x: (x["insider_count"], x["buy_value"]),
                               reverse=True)[:top],
        "by_owner": by_owner,
    }


def stats() -> dict:
    """Storage overview — **how small the open-market share is has to be in plain sight**."""
    _init()
    with db.connect() as conn:
        t = conn.execute(
            # ⚠️ Coverage is measured on the **filing date**, not the trade date:
            # the filing date is assigned by EDGAR (reliable), while the trade date is typed by the filer (with year typos) —
            # on trade dates, "2 quarters imported" displays as "covering 2002 to 2028".
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) tk, COUNT(DISTINCT owner) ow, "
            "MIN(filing_date) lo, MAX(filing_date) hi FROM insider_trade").fetchone()
        g = {r["tx_group"]: r["n"] for r in conn.execute(
            "SELECT tx_group, COUNT(*) n FROM insider_trade GROUP BY tx_group")}
        b = [dict(r) for r in conn.execute(
            "SELECT kind, key, rows, filings, errors, synced_at FROM insider_batch "
            "ORDER BY kind, key DESC")]
        last = conn.execute("SELECT MAX(synced_at) FROM insider_batch").fetchone()[0]
    total = t["n"] or 0
    om = g.get("open_market", 0)
    return {
        "trades": total, "tickers": t["tk"], "owners": t["ow"],
        "earliest": t["lo"], "latest": t["hi"],   # by filing date
        "range_basis": "filing_date",
        "by_group": g,
        "open_market_pct": round(om / total * 100, 1) if total else 0.0,
        "quarters": [x["key"] for x in b if x["kind"] == "quarter"],
        "days": [x["key"] for x in b if x["kind"] == "day"],
        "batches": b[:40], "last_sync": last, "db_path": db.DB_PATH,
        "note": "Open market = transaction codes P/S, the only part carrying an intent to buy or sell; "
                "the rest is compensation (grants, exercises, tax withholding) and other exempt transactions.",
    }
