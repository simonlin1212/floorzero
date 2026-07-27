"""Sync orchestration for insider trades (quarterly bulk + daily incremental).

━━━ The two routes differ in cost by two orders of magnitude, and the user has to see that ━━━

| | Quarterly dataset | Daily XML |
|---|---|---|
| Yield per run | ~100k transactions (a whole quarter) | ~700 (one day) |
| Requests | **1** | **1 per filing** (645 in a day) |
| Time | ~3 seconds | ~90 seconds a day (rate-limited to 8/s) |
| Freshness | 7 to 49 days behind | live |

→ The default: **fill the last few quarters first (hundreds of thousands of rows in seconds), then close the gap day by day**.
   The gap currently runs to 117 days — closing all of it takes over 3 hours, so the default covers only the last 5 business days.
   Anything more is the user's choice; **we do not quietly spend hours on their behalf**.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Optional

from sources import edgar as src
from modules import insider as parse
from modules import insider_store as store

#: "No index" within this many days of today is not conclusive — the SEC usually publishes a day's index in the US evening.
#: Only when it is still absent beyond this is the day taken to be a non-trading day.
INDEX_SETTLE_DAYS = 3


class SyncState:
    """Sync progress (one self-hosting user, so one instance is enough)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.rows = 0
        self.stage = "idle"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.coverage: Optional[str] = None

    def _snapshot_locked(self) -> dict:
        """⚠️ The caller must **already hold** self.lock.

        `threading.Lock` is not reentrant, so calling `snapshot()` while holding it deadlocks —
        which is exactly what the Congress section hit (the second POST hung permanently).
        """
        return {
            "running": self.running, "done": self.done, "total": self.total,
            "rows": self.rows, "stage": self.stage,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "errors": self.errors[-12:], "error_count": len(self.errors),
            "coverage": self.coverage,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _bump(done: int = 0, rows: int = 0) -> None:
    with STATE.lock:
        STATE.done += done
        STATE.rows += rows


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)


def _sync_quarters(quarters: list[str]) -> None:
    known = store.known_batches("quarter")
    todo = [q for q in quarters if q not in known]
    with STATE.lock:
        STATE.total += len(todo)
    for q in todo:
        STATE.stage = f"quarterly dataset {q}"
        try:
            tables = src.quarter_dataset(q)
            trades = parse.parse_dataset(tables)
            n = store.save_trades(parse.to_dicts(trades), source="dataset")
            # ⚠️ The batch record stores **the rows parsed in that batch**, not "rows added":
            # the latter is the increment after deduplication, and falls badly short on an overlapping
            # import or a retry, putting figures like "2026q1 imported = 3 rows" into the inventory table.
            store.mark_batch("quarter", q, rows=len(trades))
            _bump(rows=n)
        except src.DataNotAvailable:
            # The quarter is not published yet — **not an error**, and not marked synced either (it gets tried again)
            _err(f"The {q} dataset is not published yet (the SEC runs 7 to 49 days behind); skipped")
        except Exception as e:
            _err(f"quarter {q}: {type(e).__name__}: {e}")
        finally:
            _bump(done=1)


def _sync_one_day(day: date) -> tuple[int, int, int]:
    """Fetch one day of Form 4s. Returns (transactions, filings, errors)."""
    refs = src.form4_filings(day)
    # Skip the ones already known to be unreadable (the old format with no XML): they are **terminal**, and redownloading changes nothing.
    # Without the skip, one bad filing keeps the day from ever being marked complete, and every sync redownloads 600-700 filings.
    dead = store.dead_accessions()
    total_refs = len(refs)
    refs = [r for r in refs if r.accession not in dead]
    rows: list[dict] = []
    errors = 0

    def one(r):
        # Keys are assigned per filing (the counting context is that one filing)
        return parse.to_dicts(parse.parse_form4_xml(src.filing_xml(r.txt_url), r))

    # The SEC allows 10 requests/second; the source layer's limiter already sets the pace, so this concurrency only fills the pipe
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(one, r): r for r in refs}
        for fu in as_completed(futs):
            r = futs[fu]
            try:
                rows.extend(fu.result())
            except src.NoXmlInFiling as e:
                # The document genuinely has no XML = terminal; record it and never retry
                store.mark_dead(r.accession, day.isoformat(), str(e)[:120])
            except src.DataNotAvailable as e:
                # ⚠️ "Could not fetch the object" is not terminal: a newly indexed filing's body may not have propagated yet.
                # Record it as dead and one propagation delay loses that filing permanently.
                # Counted as an error → the day is not marked complete → it is retried next time.
                errors += 1
                _err(f"{day} {r.company[:20]} {r.accession}: body not available yet ({e}); will retry")
            except Exception as e:
                errors += 1
                _err(f"{day} {r.company[:20]} {r.accession}: {type(e).__name__}: {e}")
    n = store.save_trades(rows, source="daily")
    # ⚠️ **The day is marked complete only when every filing succeeded.**
    # Mark it complete with failures outstanding and `known_batches("day")` makes every later sync skip it forever —
    # one network hiccup then leaves a permanent hole in that day, with no error at the end of the run and nothing to see.
    # Only **transient** failures (network and the like) hold the day open; terminal ones are recorded and should not block it
    if errors == 0:
        # Records len(rows) (the transactions actually parsed that day), not the post-deduplication increment —
        # after a partial failure and a successful retry, the increment is only "what came back this time", which writes the batch record wrong.
        store.mark_batch("day", day.isoformat(), rows=len(rows),
                         filings=total_refs, errors=0)
    return n, total_refs, errors


def _sync_days(days: int) -> None:
    # Walk backwards skipping the days already done, rather than fixating on the last N days — otherwise the gap never closes.
    # With the floor set at the cutoff of the quarters already imported: everything before it is in the quarterly ZIPs already,
    # and downloading it filing by filing is pure duplication (600-700 filings a day, about 90 seconds a day).
    floor = None
    quarters = sorted(store.known_batches("quarter"))
    if quarters:
        floor = src.quarter_end(quarters[-1])
    todo = src.pending_form4_days(days, store.known_batches("day"), floor=floor)
    if not todo and floor:
        STATE.stage = f"the daily portion is filled in (anything before {floor} is covered by the quarterly dataset)"
    with STATE.lock:
        STATE.total += len(todo)
    for d in todo:
        STATE.stage = f"fetching {d} day by day (about 600-700 filings)"
        try:
            n, filings, errors = _sync_one_day(d)
            _bump(rows=n)
            if errors:
                _err(f"{d}: {errors} of {filings} filings could not be fetched or parsed, "
                     f"so the day is **not marked complete** and the next sync will retry it")
        except src.DataNotAvailable:
            # ⚠️ "The index could not be fetched" has two causes and they must be told apart:
            #   · a non-trading day (weekend or holiday) → there will never be one, so mark it complete and stop retrying
            #   · **the SEC has not published today's or yesterday's index yet** (usually it lands in the US evening)
            #     → mark that complete and any filing added afterwards is **never picked up**
            if (date.today() - d).days >= INDEX_SETTLE_DAYS:
                store.mark_batch("day", d.isoformat(), rows=0, filings=0)
            else:
                _err(f"The index for {d} is not published yet (the SEC usually publishes in the US evening), "
                     f"so the day is not marked complete and the next sync will retry it")
        except Exception as e:
            _err(f"{d}: {type(e).__name__}: {e}")
        finally:
            _bump(done=1)


def _update_coverage() -> None:
    """Record honestly how far the dataset reaches and how much the daily fetch has added beyond it."""
    try:
        latest, _ = src.latest_available_quarter()
        if latest:
            end = src.quarter_end(latest)
            gap = (date.today() - end).days
            STATE.coverage = (f"The SEC's quarterly dataset only reaches {latest} (covering up to {end}), "
                              f"and the {gap} days since can only be filled in day by day")
    except Exception as e:
        STATE.coverage = f"Probing quarterly coverage failed: {type(e).__name__}: {e}"


def _run(quarters_back: int, days: int) -> None:
    try:
        _update_coverage()
        if quarters_back:
            # ⚠️ Count back from the **newest published** quarter: using calendar quarters directly spends
            # the budget on quarters not yet published (measured, quarters_back=2 imported nothing at all).
            STATE.stage = "probing for available quarters"
            quarters, missing = src.available_quarters(quarters_back)
            if missing:
                _err(f"The SEC has not published these quarters yet (it runs 7 to 49 days behind); skipped: {', '.join(missing)}")
            _sync_quarters(quarters)
        if days:
            _sync_days(days)
        STATE.stage = "done"
    except Exception as e:
        STATE.stage = f"interrupted: {type(e).__name__}: {e}"
        _err(f"Sync interrupted: {type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(quarters_back: int = 2, days: int = 5) -> dict:
    """Start a sync.

    `quarters_back` = how many recent quarters to fill (cheap, seconds per quarter)
    `days`          = how many recent business days to fetch one by one (dear, about 90 seconds a day)

    ⚠️ The default is (2, 5) rather than "fill everything": the gap currently runs to 117 days, over 3 hours of work.
    Anything that expensive is the user's decision, never something run quietly on their behalf.
    """
    with STATE.lock:
        if STATE.running:
            # Lock already held; this must use _snapshot_locked (calling snapshot() would deadlock)
            return {"started": False, "reason": "a sync is already running",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.done = STATE.total = STATE.rows = 0
        STATE.errors = []
        STATE.stage = "starting"
        STATE.coverage = None
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    threading.Thread(target=_run,
                     args=(max(0, quarters_back), max(0, days)), daemon=True).start()
    return {"started": True, **STATE.snapshot()}
