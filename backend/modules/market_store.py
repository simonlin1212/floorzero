"""Local cache for the yield curve.

━━━ Why this lane is stored, when other "just read it" sources are not ━━━
Treasury's annual XML is **one file per year and 8 seconds a fetch** (measured), so three years is 29 seconds.
And this data has a property the others lack: **past years never change again** —
the 2024 yield curve read today and read in ten years is the same thing.
Paying 29 seconds afresh on every page load buys back numbers that are identical.

So freshness here is judged **per year** rather than by a single TTL:

- **Past years**: once that year's record count looks complete (≥200 trading days), it is **never fetched again**.
- **This year**: still growing, judged on `MAX(date)` — behind "four days before today" and it is refetched.
  Four days covers weekends plus holidays: opening the page on a Sunday, the newest row is Friday's,
  which is entirely normal and no reason to refetch every time.

⚠️ **What is cached is the data itself, not the fact that "I fetched this year".**
The difference: if Treasury revises a historical value (revisions do happen),
a refetch overwrites it; whereas recording only "already fetched" means never finding out.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from modules import db
from sources.macro import TENORS

SCHEMA = """
CREATE TABLE IF NOT EXISTS treasury_yield (
    date        TEXT PRIMARY KEY,      -- YYYY-MM-DD
    year        INTEGER NOT NULL,
    yields      TEXT NOT NULL,         -- JSON: {BC_1MONTH: 4.3, ...}
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ty_year ON treasury_yield(year);
"""

#: The minimum trading days for a year to "look complete". A US year runs about 250 trading days,
#: and 200 leaves ample room for holidays and the occasional missing report — better to fetch
#: once more than to pin a half-fetched, truncated year into the database as though it were whole.
_YEAR_COMPLETE = 200

#: How many days the current year may lag. Weekends plus holidays can run 4 days with no new data.
_STALE_DAYS = 4


def _ensure() -> None:
    db.ensure_schema("market", SCHEMA)


def year_status(years: Iterable[int], today: Optional[date] = None) -> dict[int, dict]:
    """Each year's state in the database: how many days, the newest one, and whether it still needs fetching."""
    _ensure()
    today = today or date.today()
    ys = sorted(set(years))
    if not ys:
        return {}
    out: dict[int, dict] = {
        y: {"rows": 0, "latest": None, "stale": True} for y in ys}
    with db.connect() as conn:
        q = ",".join("?" * len(ys))
        for r in conn.execute(
                f"SELECT year, COUNT(*) n, MAX(date) mx FROM treasury_yield "
                f"WHERE year IN ({q}) GROUP BY year", ys):
            out[r["year"]] = {"rows": r["n"], "latest": r["mx"], "stale": True}
    for y, st in out.items():
        if y < today.year:
            # A past year: complete enough, so never fetched again
            st["stale"] = st["rows"] < _YEAR_COMPLETE
        else:
            # This year: check how far behind the newest row is
            st["stale"] = (st["latest"] or "") < str(today - timedelta(days=_STALE_DAYS))
    return out


def save_year(year: int, rows: Iterable[dict]) -> int:
    """Write a whole year of daily curves (idempotent, overwriting by date)."""
    import json

    _ensure()
    now = _now()
    payload = []
    for r in rows:
        d = (r.get("date") or "")[:10]
        if not d:
            continue
        ys = {k: r.get(k) for k in TENORS if r.get(k) is not None}
        if not ys:
            continue
        payload.append((d, year, json.dumps(ys, separators=(",", ":")), now))
    if not payload:
        return 0
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO treasury_yield(date, year, yields, fetched_at) "
            "VALUES (?,?,?,?)", payload)
    return len(payload)


def load_years(years: Iterable[int]) -> list[dict]:
    """Read every curve for these years, ascending by date. Returns the row shape of `sources.macro.yield_curve`."""
    import json

    _ensure()
    ys = sorted(set(years))
    if not ys:
        return []
    q = ",".join("?" * len(ys))
    out = []
    with db.connect() as conn:
        for r in conn.execute(
                f"SELECT date, yields FROM treasury_yield WHERE year IN ({q}) "
                f"ORDER BY date", ys):
            row: dict = {"date": r["date"]}
            stored = json.loads(r["yields"])
            for k in TENORS:
                row[k] = stored.get(k)
            out.append(row)
    return out


def stats() -> dict:
    """How much has accrued."""
    _ensure()
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, MIN(date) lo, MAX(date) hi, "
            "COUNT(DISTINCT year) ys, MAX(fetched_at) f FROM treasury_yield"
        ).fetchone()
    return {"rows": r["n"], "earliest": r["lo"], "latest": r["hi"],
            "years": r["ys"], "last_fetch": r["f"]}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
