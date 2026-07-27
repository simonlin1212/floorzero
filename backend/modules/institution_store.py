"""Local storage for 13F institutional holdings.

━━━ Scale ━━━
One reporting period was measured at **3.32m holdings** (8,741 managers × 31,464 CUSIPs).
Stored whole that is 600MB-1GB — heavy for a self-hosted tool meant to run straight from a clone.

⭐ Hence the default **$1m value threshold**: measured, it keeps 37.5% of the rows and
covers **99.37%** of the value. A good trade, but **what it drops has to be reported honestly**
(`stats()` returns both the threshold and the discarded amount) — truncating quietly makes queries
like "who holds this stock" miss the small positions with the user none the wiser. Set it to 0 to keep everything.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from modules import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS f13_holding (
    accession    TEXT NOT NULL,
    holding_key  TEXT NOT NULL,      -- cusip|kind|discretion#index (see institution.assign_keys)
    manager      TEXT NOT NULL,
    manager_cik  TEXT,
    period       TEXT NOT NULL,      -- reporting period (quarter-end)
    filing_date  TEXT,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    cusip        TEXT NOT NULL,      -- ⭐ the key for aggregation and cross-quarter comparison (not the ticker)
    issuer       TEXT,
    title_of_class TEXT,
    kind         TEXT NOT NULL,      -- share / call / put ⚠️ must be counted separately
    value        REAL,               -- dollars (thousands already converted, per filing period)
    shares       REAL,
    shares_type  TEXT,               -- SH / PRN
    discretion   TEXT,
    voting_sole  REAL,
    voting_shared REAL,
    voting_none  REAL,
    source_url   TEXT,
    PRIMARY KEY (accession, holding_key)
);
CREATE INDEX IF NOT EXISTS idx_f13_cusip   ON f13_holding (cusip, period, kind);
CREATE INDEX IF NOT EXISTS idx_f13_manager ON f13_holding (manager_cik, period);
CREATE INDEX IF NOT EXISTS idx_f13_period  ON f13_holding (period, kind);

-- Import staging table: same shape as f13_holding, but with an **independent key space**.
-- ⚠️ Staged rows cannot go into f13_holding and be told apart by period —
-- the key is (accession, holding_key) and carries no period, so staged rows collide with live ones
-- and INSERT OR IGNORE drops them silently (measured: 7 rows in, 4 stored).
CREATE TABLE IF NOT EXISTS f13_holding_staging (
    accession    TEXT NOT NULL,
    holding_key  TEXT NOT NULL,
    manager      TEXT NOT NULL,
    manager_cik  TEXT,
    period       TEXT NOT NULL,
    filing_date  TEXT,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    cusip        TEXT NOT NULL,
    issuer       TEXT,
    title_of_class TEXT,
    kind         TEXT NOT NULL,
    value        REAL,
    shares       REAL,
    shares_type  TEXT,
    discretion   TEXT,
    voting_sole  REAL,
    voting_shared REAL,
    voting_none  REAL,
    source_url   TEXT,
    PRIMARY KEY (accession, holding_key)
);

-- ⭐ CUSIP → canonical issuer name (from the SEC's official 13(f) securities list).
-- NAMEOFISSUER in a filing is free text: Apple's CUSIP was measured carrying 61 spellings,
-- some of them other companies' names. Display and grouping follow this table, falling back to the filings' modal spelling only when it holds no answer.
CREATE TABLE IF NOT EXISTS f13_security (
    cusip      TEXT PRIMARY KEY,
    issuer     TEXT NOT NULL,
    class      TEXT,
    has_option INTEGER NOT NULL DEFAULT 0,
    quarter    TEXT,
    synced_at  TEXT NOT NULL
);

-- Reporting periods already imported
CREATE TABLE IF NOT EXISTS f13_batch (
    period       TEXT PRIMARY KEY,
    window       TEXT,
    rows         INTEGER NOT NULL,   -- rows actually stored
    parsed_rows  INTEGER,            -- rows parsed in total (including those the threshold dropped)
    dropped_rows INTEGER,            -- rows dropped by the threshold
    dropped_value REAL,              -- value dropped
    min_value    REAL,               -- the threshold used this time
    managers     INTEGER,
    synced_at    TEXT NOT NULL
);
"""

_COLS = ("accession", "holding_key", "manager", "manager_cik", "period",
         "filing_date", "is_amendment", "cusip", "issuer", "title_of_class",
         "kind", "value", "shares", "shares_type", "discretion",
         "voting_sole", "voting_shared", "voting_none", "source_url")


def _init() -> None:
    db.ensure_schema("f13", _SCHEMA)


def _row_tuple(r: dict) -> tuple:
    """dict → a storage tuple (shared by the live and staging tables, so field order cannot drift)."""
    return (r["accession"], r["holding_key"], r["manager"], r["manager_cik"],
            r["period"], r["filing_date"], int(bool(r["is_amendment"])),
            r["cusip"], r["issuer"], r["title_of_class"], r["kind"], r["value"],
            r["shares"], r["shares_type"], r["discretion"], r["voting_sole"],
            r["voting_shared"], r["voting_none"], r["source_url"])


def clear_staging(period: Optional[str] = None) -> None:
    """Empty the staging table (an import that failed part-way leaves rows behind)."""
    _init()
    with db.connect() as conn:
        if period:
            conn.execute("DELETE FROM f13_holding_staging WHERE period = ?", (period,))
        else:
            conn.execute("DELETE FROM f13_holding_staging")


def save_staging(rows: list[dict]) -> int:
    """Write to the staging table."""
    _init()
    if not rows:
        return 0
    sql = (f"INSERT OR REPLACE INTO f13_holding_staging ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        conn.executemany(sql, [_row_tuple(r) for r in rows])
    return len(rows)


def commit_staging(period: str) -> int:
    """**Atomic switch**: drop the old, promote the staged, both in one transaction.

    ⚠️ "Delete then write" will not do: a streaming import failing part-way (network drop, process
    killed, crash) leaves nothing or half a period behind, while `f13_batch` still records the old
    row count — after which every query quietly serves incomplete data, entirely undetectably.
    Stage first, switch only once everything succeeded, and a failure leaves the old data untouched.
    """
    _init()
    cols = ",".join(_COLS)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM f13_holding_staging WHERE period = ?",
                         (period,)).fetchone()[0]
        # ⚠️ Switch **even when staging is empty**: a high enough threshold can leave no rows at all,
        # and returning early there leaves the old holdings in place while the batch metadata already
        # reads "0 rows at the new threshold" — the page says there is no data while queries still serve the old.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM f13_holding WHERE period = ?", (period,))
        conn.execute(f"INSERT OR REPLACE INTO f13_holding ({cols}) "
                     f"SELECT {cols} FROM f13_holding_staging WHERE period = ?",
                     (period,))
        conn.execute("DELETE FROM f13_holding_staging WHERE period = ?", (period,))
        conn.execute("COMMIT")
        return n


def save_holdings(rows: list[dict], replace_period: Optional[str] = None) -> int:
    """Bulk write (idempotent). Returns the number of rows actually added.

    `replace_period`: clear that reporting period's old data before writing.
    ⚠️ Using it directly is **destructive** (a failure destroys the data) —
    streaming imports should use `clear_staging()` + write to `<period>#staging` + `commit_staging()`.
    """
    _init()
    if replace_period:
        with db.connect() as conn:
            conn.execute("DELETE FROM f13_holding WHERE period = ?", (replace_period,))
    if not rows:
        return 0
    payload = [_row_tuple(r) for r in rows]
    sql = (f"INSERT OR IGNORE INTO f13_holding ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM f13_holding").fetchone()[0]
        conn.executemany(sql, payload)
        after = conn.execute("SELECT COUNT(*) FROM f13_holding").fetchone()[0]
    return after - before


def mark_batch(period: str, window: str, rows: int, parsed_rows: int,
               dropped_rows: int, dropped_value: float, min_value: float,
               managers: int) -> None:
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO f13_batch (period, window, rows, parsed_rows, "
            " dropped_rows, dropped_value, min_value, managers, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (period, window, rows, parsed_rows, dropped_rows, dropped_value,
             min_value, managers, datetime.now().isoformat(timespec="seconds")))


def save_securities(rows: list[dict], quarter: str) -> int:
    """Write the official securities list (CUSIP → canonical name)."""
    _init()
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO f13_security "
            "(cusip, issuer, class, has_option, quarter, synced_at) VALUES (?,?,?,?,?,?)",
            [(r["cusip"], r["issuer"], r.get("class"), int(bool(r.get("has_option"))),
              quarter, now) for r in rows])
        return conn.execute("SELECT COUNT(*) FROM f13_security").fetchone()[0]


def security_count() -> int:
    _init()
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM f13_security").fetchone()[0]


def known_periods() -> list[str]:
    _init()
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT period FROM f13_batch ORDER BY period DESC")]


def _where(period: Optional[str] = None, cusip: Optional[str] = None,
           manager: Optional[str] = None, kind: Optional[str] = "share",
           min_value: Optional[float] = None,
           include_amendments: bool = False, alias: str = "") -> tuple[str, list]:
    """The filter conditions — detail and aggregate **share this one place**, so the two cannot drift.

    ⚠️ `kind` defaults to `share`: mixing puts (bearish) into the holdings totals
    counts bearish exposure as bullish (puts ran to $2.66tn that quarter).
    """
    # `alias` lets one set of conditions serve JOIN queries that use table aliases —
    # this used to rewrite the alias by string replacement, which fails silently the moment a column is renamed.
    p = f"{alias}." if alias else ""
    sql, args = "WHERE 1=1", []
    if not include_amendments:
        # An amendment restates the filing whole (Form 13F Instruction 3 requires a full restatement),
        # so counting it alongside the original is double counting
        sql += f" AND {p}is_amendment = 0"
    if period:
        sql += f" AND {p}period = ?"; args.append(period)
    if cusip:
        sql += f" AND {p}cusip = ?"; args.append(cusip.upper())
    if manager:
        sql += f" AND {p}manager LIKE ?"; args.append(f"%{manager}%")
    if kind and kind != "all":
        sql += f" AND {p}kind = ?"; args.append(kind)
    if min_value:
        sql += f" AND {p}value >= ?"; args.append(min_value)
    return sql, args


def query(limit: int = 200, **filters) -> list[dict]:
    """Holding detail (largest value first).

    ⚠️ Issuer names come from the official list here too: use it for the aggregate view but not the
    detail, and one CUSIP is called "APPLE INC" in the chart and another company's name in the table below it.
    """
    _init()
    wh, args = _where(**filters, alias="h")
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT h.*, COALESCE(s.issuer, h.issuer) issuer, s.class class "
            f"FROM f13_holding h LEFT JOIN f13_security s ON s.cusip = h.cusip "
            f"{wh} ORDER BY h.value IS NULL, h.value DESC LIMIT ?", args + [limit])]


def aggregate(top: int = 20, **filters) -> dict:
    """Aggregate in SQL over **every matching row** (not over the first N rows fetched)."""
    _init()
    w, a = _where(**filters)
    wh, ah = _where(**filters, alias="h")          # the aliased version, for JOIN queries
    with db.connect() as conn:
        tot = conn.execute(
            f"SELECT COUNT(*) n, COUNT(DISTINCT manager_cik) mgrs, "
            f" COUNT(DISTINCT cusip) cusips, SUM(value) val "
            f"FROM f13_holding {w}", a).fetchone()
        # ⚠️ The issuer name comes from the **official list**, never from MAX(issuer):
        # names in filings are free text, and MAX takes the alphabetically largest —
        # measured, Apple's CUSIP then displays as "VANGUARD WHITEHALL FDS" (another company)
        # and Amazon's as "JOHNSON & JOHNSON COM". Wrongly attributed, and invisibly so.
        # Only when the official list has no answer does it fall back to the **most frequent** spelling in the filings (the mode, not MAX).
        # ⚠️ Carry the official list's class along: one issuer often has several share classes
        # (Alphabet's CL A and CL C are two CUSIPs and two distinct securities).
        # Without the class, the table shows two rows of "ALPHABET INC" and reads like duplicated data.
        by_issuer = [dict(r) for r in conn.execute(
            f"SELECT h.cusip, COALESCE(s.issuer, "
            f"  (SELECT issuer FROM f13_holding x WHERE x.cusip = h.cusip "
            f"   GROUP BY x.issuer ORDER BY COUNT(*) DESC LIMIT 1)) issuer, "
            f" s.class class, "
            f" COUNT(DISTINCT h.manager_cik) holders, "
            f" SUM(h.value) value, SUM(h.shares) shares "
            f"FROM f13_holding h LEFT JOIN f13_security s ON s.cusip = h.cusip "
            f"{wh} GROUP BY h.cusip ORDER BY value DESC LIMIT ?", ah + [top])]
        by_manager = [dict(r) for r in conn.execute(
            f"SELECT manager_cik, MAX(manager) manager, COUNT(DISTINCT cusip) positions, "
            f" SUM(value) value "
            f"FROM f13_holding {w} GROUP BY manager_cik "
            f"ORDER BY value DESC LIMIT ?", a + [top])]
        # ⭐ The size of each of the three position kinds — so the scale of puts sits in plain sight
        by_kind = {r["kind"]: {"rows": r["n"], "value": r["v"]}
                   for r in conn.execute(
                       f"SELECT kind, COUNT(*) n, SUM(value) v FROM f13_holding "
                       f"{_where(**{**filters, 'kind': 'all'})[0]} GROUP BY kind",
                       _where(**{**filters, "kind": "all"})[1])}
    return {"counts": dict(tot), "by_issuer": by_issuer,
            "by_manager": by_manager, "by_kind": by_kind}


def changes(period: str, prev_period: str, top: int = 20,
            kind: str = "share", manager: Optional[str] = None) -> dict:
    """Quarter-on-quarter change: new positions / added / trimmed / exited.

    ⭐ This is where 13F's value actually is — a single quarter is a static snapshot; the change carries the information.

    ⚠️ Compared on **CUSIP**, not ticker (only four in ten tickers match at all).
    ⚠️ "Exited" means only that it **no longer appears among 13(f) holdings**, and is not the manager turning bearish —
    it may have moved into options, into an account that need not be reported, or the security may have left the 13(f) list.
    """
    _init()
    # ⚠️ **The value threshold contaminates the new-position and exit calls**:
    # a holding of $900k last quarter (dropped by the threshold) and $1.1m this quarter (kept)
    # counts as a "new position" when it was only an increase; the reverse counts as an exit.
    # So both periods' thresholds are read out, and the distorted band is stated honestly in the result.
    with db.connect() as conn:
        floors = {r[0]: r[1] for r in conn.execute(
            "SELECT period, min_value FROM f13_batch WHERE period IN (?,?)",
            (period, prev_period))}
    floor = max([v or 0 for v in floors.values()] or [0])
    m_sql, m_args = ("", [])
    if manager:
        m_sql, m_args = " AND manager LIKE ?", [f"%{manager}%"]
    # Issuer names come from the official list here too (see aggregate), falling back to the mode rather than MAX
    base = (f"SELECT h.cusip cusip, COALESCE(s.issuer, "
            f"  (SELECT issuer FROM f13_holding x WHERE x.cusip = h.cusip "
            f"   GROUP BY x.issuer ORDER BY COUNT(*) DESC LIMIT 1)) issuer, "
            f"s.class class, SUM(h.value) value, SUM(h.shares) shares, "
            f"COUNT(DISTINCT h.manager_cik) holders "
            f"FROM f13_holding h LEFT JOIN f13_security s ON s.cusip = h.cusip "
            f"WHERE h.is_amendment = 0 AND h.kind = ? AND h.period = ?"
            f"{m_sql.replace(' manager ', ' h.manager ')} GROUP BY h.cusip")
    with db.connect() as conn:
        cur = {r["cusip"]: dict(r) for r in conn.execute(base, [kind, period] + m_args)}
        prv = {r["cusip"]: dict(r) for r in conn.execute(base, [kind, prev_period] + m_args)}

    new, exited, inc, dec, unchanged = [], [], [], [], []
    for c, r in cur.items():
        p = prv.get(c)
        if p is None:
            new.append({**r, "prev_value": 0.0, "delta_value": r["value"] or 0.0})
            continue
        d = (r["value"] or 0) - (p["value"] or 0)
        row = {**r, "prev_value": p["value"], "delta_value": d,
               "prev_shares": p["shares"],
               "delta_shares": (r["shares"] or 0) - (p["shares"] or 0)}
        # ⚠️ d == 0 means **the holding did not change**, which is neither an increase nor a decrease.
        # This was once written `inc if d > 0 else dec`, which put 36 completely unchanged positions
        # into the decreases — inflating the number and showing "did not move" as "is selling".
        if d > 0:
            inc.append(row)
        elif d < 0:
            dec.append(row)
        else:
            unchanged.append(row)
    for c, p in prv.items():
        if c not in cur:
            exited.append({**p, "prev_value": p["value"], "value": 0.0,
                           "delta_value": -(p["value"] or 0.0)})

    k = lambda rows, rev: sorted(rows, key=lambda x: x["delta_value"],
                                 reverse=rev)[:top]
    return {
        "period": period, "prev_period": prev_period, "kind": kind,
        "new": k(new, True), "increased": k(inc, True),
        "decreased": k(dec, False), "exited": k(exited, False),
        "counts": {"new": len(new), "increased": len(inc),
                   "decreased": len(dec), "exited": len(exited),
                   "unchanged": len(unchanged)},
        "min_value": floor,
        "note": "An exit means only that this CUSIP no longer appears among 13(f) long holdings — "
                "not that the manager turned bearish: it may have moved into options, into an account "
                "that need not be reported, or the security may have left the 13(f) list.",
        "floor_note": (
            f"⚠️ Both periods carry a ${floor:,.0f} value threshold, below which holdings were never stored. "
            f"So **new positions and exits close to the threshold cannot be trusted** — "
            f"${floor*0.9:,.0f} last quarter (dropped) and ${floor*1.1:,.0f} this quarter (kept) "
            f"shows as a new position when it is only an increase. For an exact comparison, reimport both periods at a threshold of 0."
            if floor else "Both periods were imported in full (no value threshold), so new positions and exits can be trusted."),
    }


def stats() -> dict:
    """Storage overview — **the threshold and what it dropped have to be reported**."""
    _init()
    with db.connect() as conn:
        t = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT manager_cik) mgrs, "
            "COUNT(DISTINCT cusip) cusips, SUM(value) val FROM f13_holding "
            "WHERE is_amendment = 0").fetchone()
        b = [dict(r) for r in conn.execute(
            "SELECT * FROM f13_batch ORDER BY period DESC")]
        kinds = {r[0]: r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM f13_holding GROUP BY kind")}
    return {
        "holdings": t["n"] or 0, "managers": t["mgrs"] or 0,
        "cusips": t["cusips"] or 0, "total_value": t["val"] or 0.0,
        "by_kind": kinds,
        "periods": [x["period"] for x in b],
        "batches": b,
        "last_sync": b[0]["synced_at"] if b else None,
        "db_path": db.DB_PATH,
        "note": "13F carries **long positions in 13(f) securities as of quarter-end** only — no shorts, "
                "cash, bonds, stocks listed only outside the US, or private holdings. Puts are listed under "
                "their underlying; they are classified separately and left out of the default holdings view.",
    }
