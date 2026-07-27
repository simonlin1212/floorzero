"""Open interest, accrued day by day.

━━━ Why this one is worth accruing separately ━━━
The tape tells you **what traded**; open interest tells you **what settled into position**.
Today's OI minus yesterday's = **net new positioning** — the hardest evidence there is that
"someone is building", and it **needs no guess about direction** (unlike UW's bullish/bearish labels, which rest on the aggressor side).

⚠️ **This history cannot be backfilled.** Cboe gives the chain as it is now; yesterday's is gone.
That makes it unlike the EDGAR and FINRA lanes entirely: those can be fetched retrospectively, this one **can only be accrued**,
starting the day FloorZero is installed. It is also exactly what UW sells separately.

━━━ ⚠️ Two definitions to keep in mind ━━━

1. **Open interest settles overnight**, so it reflects positions at **yesterday's close** and excludes anything opened today.
   The OI inside "today's snapshot" is therefore the state of the **previous trading session**.
   This table is keyed by `snapshot_date` (the day it was pulled); read it knowing about that lag.

2. **Two adjacent records are not necessarily one trading day apart.** A user may install on Monday and next open the page on Friday.
   So a difference must carry **how many days it spans** and **must never be assumed to be a daily change** —
   calling a week's movement "added today" invents a number that does not exist.
"""
from __future__ import annotations

from typing import Iterable, Optional

from modules import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS oi_snapshot (
    ticker        TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,          -- YYYY-MM-DD (the day it was pulled, US/Eastern)
    expiry        TEXT NOT NULL,
    type          TEXT NOT NULL,          -- call | put
    strike        REAL NOT NULL,
    open_interest REAL NOT NULL,
    volume        REAL NOT NULL,
    spot          REAL,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (ticker, snapshot_date, expiry, type, strike)
);
CREATE INDEX IF NOT EXISTS ix_oi_ticker_date ON oi_snapshot(ticker, snapshot_date);
"""


def _ensure() -> None:
    db.ensure_schema("oi_snapshot", SCHEMA)


def record(ticker: str, snapshot_date: str, spot: float,
           rows: Iterable[dict]) -> int:
    """Record one day's snapshot (pulling twice in a day overwrites; it never doubles up).

    `rows` takes the shape of `flow.to_dict()`.
    """
    _ensure()
    now = _now()
    # ⚠️ Not `r["open_interest"] or 0` — if upstream really does hand back None,
    #    that means "could not fetch", and storing it permanently as 0 turns into a
    #    phantom position change tomorrow. Both fields are non-nullable floats on
    #    `Contract`, so a None means upstream parsing broke and it **should blow up here**, not quietly record a 0.
    payload = []
    for r in rows:
        oi, vol = r["open_interest"], r["volume"]
        if oi is None or vol is None:
            raise ValueError(
                f"{ticker} {r['expiry']} {r['type']} {r['strike']} has null open interest/volume — "
                f"that is a **failed fetch** and must not go into history as 0. Check the source parsing.")
        payload.append((ticker, snapshot_date, r["expiry"], r["type"],
                        float(r["strike"]), float(oi), float(vol), spot, now))
    if not payload:
        return 0
    # ⚠️ **Replace the whole day, do not merge row by row.**
    # With `INSERT OR REPLACE` alone, old rows not included this time simply stay —
    # so one trading session ends up holding the results of two different pulls (say a
    # dte_max=7 pull stored first, then the full chain), and the difference is computed
    # across two different scopes stitched together. Done in one transaction, a failure part-way leaves no empty table.
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM oi_snapshot WHERE ticker=? AND snapshot_date=?",
                     (ticker, snapshot_date))
        conn.executemany(
            "INSERT OR REPLACE INTO oi_snapshot"
            "(ticker, snapshot_date, expiry, type, strike, open_interest,"
            " volume, spot, captured_at) VALUES (?,?,?,?,?,?,?,?,?)", payload)
        conn.execute("COMMIT")
    return len(payload)


def dates(ticker: str) -> list[dict]:
    """Snapshot dates accrued for this ticker (newest first)."""
    _ensure()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT snapshot_date, COUNT(*) contracts, MAX(spot) spot, "
            "MAX(captured_at) captured_at FROM oi_snapshot WHERE ticker = ? "
            "GROUP BY snapshot_date ORDER BY snapshot_date DESC", (ticker,))]


def oi_change(ticker: str, date_to: Optional[str] = None,
              date_from: Optional[str] = None, top: int = 40) -> dict:
    """Change in open interest between two snapshot dates.

    With no dates given, takes the **two most recent** snapshot days.
    ⚠️ With only one day accrued it returns `enough=False` — meaning "not enough accrued yet",
    **not "open interest did not change"**. Those two must never look alike in the interface.
    """
    _ensure()
    ds = [d["snapshot_date"] for d in dates(ticker)]
    if len(ds) < 2:
        return {"enough": False, "have": len(ds), "dates": ds,
                "note": ("An open-interest change needs **at least two** snapshot days. "
                         "This history cannot be backfilled (Cboe only gives the present), "
                         "but it accrues on its own if the page is opened once a day from installation onwards.")}
    to_d = date_to or ds[0]
    # ⚠️ **A date passed in has to actually be in the database.** `_load()` returns an empty
    #    dict when it finds nothing, and the difference then reads "this day was never stored"
    #    as "open interest was zero that day", producing a fake report of a wholesale exit or entry.
    if to_d not in ds:
        return {"enough": False, "have": len(ds), "dates": ds,
                "note": (f"No local snapshot for {to_d} (held: {', '.join(ds[:8])}). "
                         f"That means **this day was never stored**, not that open interest was zero on it.")}
    if date_from:
        from_d = date_from
        if from_d not in ds:
            return {"enough": False, "have": len(ds), "dates": ds,
                    "note": (f"No local snapshot for {from_d} (held: {', '.join(ds[:8])}). "
                             f"That means **this day was never stored**, not that open interest was zero on it.")}
        if from_d >= to_d:
            return {"enough": False, "have": len(ds), "dates": ds,
                    "note": f"The start date {from_d} must be earlier than the end date {to_d}."}
    else:
        earlier = [d for d in ds if d < to_d]
        if not earlier:
            return {"enough": False, "have": len(ds), "dates": ds,
                    "note": f"There is no earlier snapshot before {to_d}."}
        from_d = earlier[0]

    # ⚠️ **The full outer join is done in Python on purpose, rather than as `FULL OUTER JOIN`.**
    # SQLite only supports it from 3.39 (2022). This machine has 3.53, but a self-hosting user
    # gets whatever sqlite3 their own Python carries — and macOS system Python still ships 3.3x.
    # One "works on my machine" is enough to void the whole self-hosting promise, while the volume
    # here (a few thousand rows per ticker per day) merges in memory without strain.
    #
    # ⚠️ Both days are read in **a single SQL statement**, not two queries one after the other.
    #    Split in two (even sharing a connection), an archiving commit can still land in between,
    #    and the comparison is then between two different versions of the database — a difference
    #    that corresponds to no real moment. A single statement is an atomic read in SQLite; no explicit transaction needed.
    cur: dict = {}
    prev: dict = {}
    with db.connect() as conn:
        for r in conn.execute(
                "SELECT snapshot_date, expiry, type, strike, open_interest, volume "
                "FROM oi_snapshot WHERE ticker=? AND snapshot_date IN (?, ?)",
                (ticker, to_d, from_d)):
            bucket = cur if r["snapshot_date"] == to_d else prev
            bucket[(r["expiry"], r["type"], r["strike"])] = dict(r)
    rows = []
    expired = 0
    expired_oi = 0.0
    incomplete = 0
    incomplete_oi = 0.0
    new_listings = 0
    for key in cur.keys() | prev.keys():
        expiry, typ, strike = key
        a, b = cur.get(key), prev.get(key)
        if a is None:
            # ⚠️ **Expiring out of the chain ≠ closing out.** A contract expiring Friday is gone
            #    from the chain next week, and counting that as "open interest went to zero"
            #    conjures up a huge net reduction when nobody closed anything — it merely expired.
            if expiry < to_d:
                expired += 1
                expired_oi += float(b["open_interest"]) if b else 0.0
                continue
            # ⚠️ **Not yet expired but absent from the end snapshot = that snapshot is incomplete.**
            #    Cboe lists contracts right through to expiry, and keeps listing them at OI=0.
            #    So "not expired and absent" can only mean that pull missed it —
            #    treating it as OI=0 would fabricate a full close-out that never happened.
            #    This is "could not fetch", not "is not there", and it must be counted apart and excluded.
            incomplete += 1
            incomplete_oi += float(b["open_interest"]) if b else 0.0
            continue
        if b is None:
            # ⚠️ Present in the end snapshot and absent from the start one — here there is **real ambiguity**:
            #    ① a strike listed during the period (genuinely from 0, so counting it as an increase is right)
            #    ② the start pull missed it (then it is no increase at all, and counting it inflates the number)
            #    The two look **exactly alike** in the data and cannot be told apart.
            #    The end side can be resolved (not expired and still absent can only be a missed pull, since Cboe lists to expiry),
            #    the start side cannot — so it is counted as normal, but the count is reported separately
            #    and the wording states the uncertainty rather than pretending these are certainly new listings.
            new_listings += 1
        oi_to = float(a["open_interest"]) if a else 0.0
        oi_from = float(b["open_interest"]) if b else 0.0
        change = oi_to - oi_from
        rows.append({
            "expiry": expiry, "type": typ, "strike": strike,
            "oi_from": oi_from, "oi_to": oi_to,
            "volume_to": float(a["volume"]) if a else 0.0,
            "change": change,
            # A rise from 0 has no percentage — return None rather than a fake 100%
            "change_pct": None if oi_from <= 0 else change / oi_from * 100.0,
        })
    gained = sorted((r for r in rows if r["change"] > 0),
                    key=lambda r: -r["change"])[:top]
    lost = sorted((r for r in rows if r["change"] < 0),
                  key=lambda r: r["change"])[:top]
    span = _daydiff(from_d, to_d)
    # ⚠️ "Adjacent" has to mean **no snapshot was missed in between**, not just a small calendar gap.
    #    Tuesday → Friday is 3 days with two missed observations; Friday → Monday is also 3 days
    #    and genuinely two consecutive sessions. The order in the database is the truth here.
    idx_from, idx_to = ds.index(from_d), ds.index(to_d)
    adjacent = (idx_from - idx_to) == 1        # ds runs newest to oldest
    return {
        "enough": True, "dates": ds, "date_from": from_d, "date_to": to_d,
        "span_days": span,
        # The span must be stated — a user may install on Monday and open the page on Friday,
        # and writing those days' accumulated change as "added today" invents a number.
        "is_consecutive": adjacent,
        "snapshots_between": max(0, idx_from - idx_to - 1),
        "expired_excluded": expired,
        "expired_oi": expired_oi,
        # Not expired yet absent = that snapshot is incomplete. A large count means this comparison cannot be trusted.
        "incomplete_excluded": incomplete,
        "incomplete_oi": incomplete_oi,
        "new_listings": new_listings,
        # Contracts absent from the start snapshot and present in the end one. **A new listing and a missed
        # pull are indistinguishable in the data**; a count too large for normal new listings suggests the start pull was incomplete.
        "new_listings_ambiguous": True,
        # Contract counts of the two snapshots: a wild difference means one of the pulls missed rows
        "contracts_from": len(prev),
        "contracts_to": len(cur),
        "totals": {
            "call_change": sum(r["change"] for r in rows if r["type"] == "call"),
            "put_change": sum(r["change"] for r in rows if r["type"] == "put"),
            "contracts": len(rows),
        },
        "gained": gained, "lost": lost,
        "note": ("Open interest is an **overnight settlement figure**: what is compared here is the "
                 "prior session's closing open interest as each snapshot saw it. A rise = net opening, "
                 "a fall = net closing, **but neither indicates direction** (every contract has a buyer "
                 "and a seller, so net new longs and shorts are equal). "
                 + (f"Excluded as **expired during the period**: {expired} "
                    f"({expired_oi:,.0f} of open interest between them) — they left the chain because "
                    f"they expired, not because anyone closed, and counting them conjures up a large net reduction. "
                    if expired else "")
                 + (f"⚠️ Also excluded, **not yet expired but absent from the end snapshot**: {incomplete} "
                    f"({incomplete_oi:,.0f} of open interest) — Cboe lists contracts through to expiry, "
                    f"so unexpired and absent can only mean that pull was incomplete: **could not fetch**, not **went to zero**. "
                    if incomplete else "")
                 + (f"⚠️ Appearing only in the end snapshot and counted as rising from 0: {new_listings}. "
                    f"**Listed during the period** and **missed by the start pull** cannot be told apart in the data, "
                    f"so an unusually large number here is reason to suspect the start snapshot was incomplete "
                    f"(contract counts {len(prev):,} → {len(cur):,}). "
                    if new_listings else "")),
    }


def stats() -> dict:
    _ensure()
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, "
            "COUNT(DISTINCT snapshot_date) d, MIN(snapshot_date) lo, "
            "MAX(snapshot_date) hi FROM oi_snapshot").fetchone()
    return {"rows": r["n"], "tickers": r["t"], "days": r["d"],
            "earliest": r["lo"], "latest": r["hi"]}


def _daydiff(a: str, b: str) -> Optional[int]:
    from datetime import date
    try:
        pa = date(*map(int, a.split("-")))
        pb = date(*map(int, b.split("-")))
    except (ValueError, TypeError):
        return None
    return (pb - pa).days


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
