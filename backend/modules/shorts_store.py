"""Local storage and sync for short-sale data.

One half-month file is about 60k FTD rows, which is light — but it still streams into staging and switches over,
keeping the same habits as 13F (this project has been bitten once by keeping whole tables resident, and once by deleting before writing).
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any, Optional

from modules import db
from modules import shorts as parse
from sources import shorts as src

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ftd (
    settlement_date TEXT NOT NULL,
    cusip        TEXT NOT NULL,
    symbol       TEXT,
    description  TEXT,
    quantity     REAL,          -- ⚠️ a cumulative balance, not that day's additions (see shorts.OFFICIAL_NOTES)
    price        REAL,          -- the previous day's close; the SEC does not guarantee it matches other sources
    value        REAL,
    tag          TEXT NOT NULL, -- the half-month file's identifier, e.g. 202606b
    PRIMARY KEY (settlement_date, cusip)
);
CREATE INDEX IF NOT EXISTS idx_ftd_symbol ON ftd (symbol, settlement_date);
CREATE INDEX IF NOT EXISTS idx_ftd_date   ON ftd (settlement_date);

CREATE TABLE IF NOT EXISTS ftd_staging (
    settlement_date TEXT NOT NULL,
    cusip        TEXT NOT NULL,
    symbol       TEXT,
    description  TEXT,
    quantity     REAL,
    price        REAL,
    value        REAL,
    tag          TEXT NOT NULL,
    PRIMARY KEY (settlement_date, cusip)
);

CREATE TABLE IF NOT EXISTS ftd_batch (
    tag        TEXT PRIMARY KEY,
    rows       INTEGER NOT NULL,
    symbols    INTEGER,
    date_from  TEXT,
    date_to    TEXT,
    synced_at  TEXT NOT NULL
);
"""

_COLS = ("settlement_date", "cusip", "symbol", "description",
         "quantity", "price", "value", "tag")


def _init() -> None:
    db.ensure_schema("shorts", _SCHEMA)


def _tuple(r: dict, tag: str) -> tuple:
    return (r["settlement_date"], r["cusip"], r["symbol"], r["description"],
            r["quantity"], r["price"], r["value"], tag)


def clear_staging(tag: Optional[str] = None) -> None:
    _init()
    with db.connect() as conn:
        if tag:
            conn.execute("DELETE FROM ftd_staging WHERE tag = ?", (tag,))
        else:
            conn.execute("DELETE FROM ftd_staging")


def save_staging(rows: list[dict], tag: str) -> int:
    _init()
    if not rows:
        return 0
    sql = (f"INSERT OR REPLACE INTO ftd_staging ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        conn.executemany(sql, [_tuple(r, tag) for r in rows])
    return len(rows)


def commit_staging(tag: str) -> int:
    """Atomic switch: drop the old file's rows → promote the staged ones.

    ⚠️ The same approach as 13F: **never delete before writing** —
    an import failing part-way leaves nothing or half a file behind, while the batch metadata still records the old count.
    """
    _init()
    cols = ",".join(_COLS)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM ftd_staging WHERE tag = ?",
                         (tag,)).fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ftd WHERE tag = ?", (tag,))
        conn.execute(f"INSERT OR REPLACE INTO ftd ({cols}) "
                     f"SELECT {cols} FROM ftd_staging WHERE tag = ?", (tag,))
        conn.execute("DELETE FROM ftd_staging WHERE tag = ?", (tag,))
        conn.execute("COMMIT")
        return n


def mark_batch(tag: str, rows: int, symbols: int,
               date_from: Optional[str], date_to: Optional[str]) -> None:
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ftd_batch "
            "(tag, rows, symbols, date_from, date_to, synced_at) VALUES (?,?,?,?,?,?)",
            (tag, rows, symbols, date_from, date_to,
             datetime.now().isoformat(timespec="seconds")))


def known_tags() -> list[str]:
    _init()
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT tag FROM ftd_batch ORDER BY tag DESC")]


def _where(symbol: Optional[str] = None, since: Optional[str] = None,
           settlement_date: Optional[str] = None,
           min_quantity: Optional[float] = None,
           alias: str = "") -> tuple[str, list]:
    """The filter conditions — shared by detail and aggregate, so the two cannot drift."""
    p = f"{alias}." if alias else ""
    sql, args = "WHERE 1=1", []
    if symbol:
        sql += f" AND {p}symbol = ?"; args.append(symbol.upper())
    if settlement_date:
        sql += f" AND {p}settlement_date = ?"; args.append(settlement_date)
    if since:
        sql += f" AND {p}settlement_date >= ?"; args.append(since)
    if min_quantity:
        sql += f" AND {p}quantity >= ?"; args.append(min_quantity)
    return sql, args


def query(limit: int = 200, **filters) -> list[dict]:
    _init()
    w, args = _where(**filters)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM ftd {w} ORDER BY settlement_date DESC, "
            f"value IS NULL, value DESC LIMIT ?", args + [limit])]


def aggregate(top: int = 20, **filters) -> dict:
    """Aggregate in SQL over everything.

    ⚠️ The per-symbol total is **the mean of the balances across settlement dates**, never their sum —
    an FTD is a cumulative balance at a point in time, and adding several days' balances together means nothing
    (one undelivered trade reappears on consecutive days).
    """
    _init()
    w, a = _where(**filters)
    with db.connect() as conn:
        tot = conn.execute(
            f"SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            f" COUNT(DISTINCT settlement_date) days, "
            f" MIN(settlement_date) lo, MAX(settlement_date) hi "
            f"FROM ftd {w}", a).fetchone()
        by_symbol = [dict(r) for r in conn.execute(
            f"SELECT symbol, MAX(description) description, MAX(cusip) cusip, "
            f" COUNT(*) days, "
            f" AVG(quantity) avg_quantity, MAX(quantity) max_quantity, "
            f" AVG(value) avg_value, MAX(value) max_value "
            f"FROM ftd {w} AND symbol IS NOT NULL AND symbol != '' "
            # ⚠️ Ordered by **share count**, because the bar lengths on the chart and the phrase "largest balance" both mean shares.
            # It once sorted by avg_value (money), which put GOOG's 17.84m shares above XOM's 26.68m
            # — an ordering at odds with what is on screen. Value is shown as an annotation and is not the primary sort key.
            f"GROUP BY symbol ORDER BY avg_quantity IS NULL, avg_quantity DESC LIMIT ?",
            a + [top])]
        by_date = [dict(r) for r in conn.execute(
            f"SELECT settlement_date, COUNT(*) symbols, SUM(quantity) total_quantity, "
            f" SUM(value) total_value FROM ftd {w} "
            f"GROUP BY settlement_date ORDER BY settlement_date", a)]
    return {"counts": dict(tot), "by_symbol": by_symbol, "by_date": by_date}


def stats() -> dict:
    _init()
    with db.connect() as conn:
        t = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            "COUNT(DISTINCT settlement_date) days, "
            "MIN(settlement_date) lo, MAX(settlement_date) hi FROM ftd").fetchone()
        b = [dict(r) for r in conn.execute(
            "SELECT * FROM ftd_batch ORDER BY tag DESC")]
    return {
        "rows": t["n"] or 0, "symbols": t["syms"] or 0, "days": t["days"] or 0,
        "earliest": t["lo"], "latest": t["hi"],
        "tags": [x["tag"] for x in b], "batches": b,
        "last_sync": b[0]["synced_at"] if b else None,
        "db_path": db.DB_PATH,
        "finra_enabled": src.finra_enabled(),
        "note": "An FTD is **the cumulative balance on a settlement date** (not that day's additions), "
                "and the SEC states plainly that it is not evidence of naked shorting — see the definitions on the page.",
    }


# ─────────────────────────── Sync ───────────────────────────

class SyncState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.rows = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []

    def _snapshot_locked(self) -> dict:
        """⚠️ The caller must already hold the lock (Lock is not reentrant, so calling snapshot() while holding it deadlocks)."""
        return {"running": self.running, "stage": self.stage, "rows": self.rows,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "errors": self.errors[-10:], "error_count": len(self.errors)}

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)


def _sync_tag(tag: str) -> int:
    clear_staging(tag)
    batch, total, symbols = [], 0, set()
    lo = hi = None
    for raw in src.ftd_rows(tag):
        f = parse.parse_ftd(raw)
        if f is None:
            continue
        d = parse.to_dict(f)
        batch.append(d)
        if f.symbol:
            symbols.add(f.symbol)
        ds = d["settlement_date"]
        lo = ds if lo is None or ds < lo else lo
        hi = ds if hi is None or ds > hi else hi
        if len(batch) >= 20_000:
            total += len(batch)
            save_staging(batch, tag)
            batch = []
    if batch:
        total += len(batch)
        save_staging(batch, tag)
    if not total:
        clear_staging(tag)
        return 0
    commit_staging(tag)                    # only now is the old data touched
    mark_batch(tag, rows=total, symbols=len(symbols), date_from=lo, date_to=hi)
    return total


def _run(candidates: list[str], want: int) -> None:
    """Try candidate files newest to oldest, **until `want` of them have actually been imported**.

    ⚠️ "Take the last N files and be done" will not do: the SEC often has not published the latest one or two
    (the first half of a month appears at month end, the second half around the 15th of the next).
    Written that way, the default back=2 imports **nothing at all** early in a month,
    and every retry hits the same 404s — the user presses the button, gets nothing, and cannot see why.
    """
    got = 0
    try:
        for tag in candidates:
            if got >= want:
                break
            STATE.stage = f"importing FTD {tag}"
            try:
                n = _sync_tag(tag)
                with STATE.lock:
                    STATE.rows += n
                if n:
                    got += 1
                else:
                    _err(f"{tag}: the file holds no parseable records")
            except src.DataNotAvailable as e:
                # The SEC has not published this file yet — not an error; carry on to earlier ones
                _err(f"{tag}: {e} (falling back to earlier files)")
            except Exception as e:
                _err(f"{tag}: {type(e).__name__}: {e}")
        STATE.stage = ("done" if got else
                       "done (nothing imported — within the candidate range the SEC has published nothing new, or it is all imported already)")
    except Exception as e:
        STATE.stage = f"interrupted: {type(e).__name__}: {e}"
        _err(f"Sync interrupted: {type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(back: int = 2) -> dict:
    """Import the last N half-month files (skipping those already imported)."""
    with STATE.lock:
        if STATE.running:
            return {"started": False, "reason": "a sync is already running",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.rows = 0
        STATE.errors = []
        STATE.stage = "starting"
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    want = max(1, min(back, 12))
    known = set(known_tags())
    # The candidate range widens to want + 6: the newest few are often unpublished, so leave room to fall back
    tags = [t for t in src.ftd_files(want + 6) if t not in known]
    if not tags:
        with STATE.lock:
            STATE.running = False
            STATE.stage = "already up to date (no new files to import)"
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")
        return {"started": False, "reason": "the most recent files are all imported", **STATE.snapshot()}

    threading.Thread(target=_run, args=(tags, want), daemon=True).start()
    return {"started": True, **STATE.snapshot()}
