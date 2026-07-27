"""Orchestration of the incremental congressional-disclosure sync.

A first sync fetches several hundred PDFs (313 for the House in 2026, about 100 seconds at our
rate limit); after that it only picks up new filings. It runs on a background thread with progress
you can query — because a two-minute request left spinning gives the user no way to tell slow from dead.

⚠️ **Failures have to be classified**:
- one filing fails to parse → record it (with the reason) and carry on to the next;
- the whole Senate lane is unavailable (curl_cffi missing, or blocked) → record it as an
  **environment problem** and show it as one, never as "senators did not trade".
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Optional

from sources import congress as src
from modules import congress as parse
from modules import congress_store as store


class SyncState:
    """Sync progress (a single instance is enough; this tool is designed for one self-hosting user)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.stage = "idle"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.senate_status: Optional[str] = None

    def _snapshot_locked(self) -> dict:
        """⚠️ The caller must **already hold** self.lock.

        Split out separately because `threading.Lock` is **not reentrant**:
        calling `snapshot()` again inside `with self.lock:` deadlocks permanently
        (measured: a second POST /api/congress/sync hangs and eats a worker).
        """
        return {
            "running": self.running, "done": self.done, "total": self.total,
            "stage": self.stage, "started_at": self.started_at,
            "finished_at": self.finished_at,
            "errors": self.errors[-12:], "error_count": len(self.errors),
            "senate_status": self.senate_status,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _sync_house(year: int, limit: Optional[int]) -> None:
    STATE.stage = f"House {year} index"
    filings = [f for f in src.house_filings(year) if f.is_ptr]
    # ⚠️ Skip what is **settled** = parsed successfully + scans (terminal; unreadable is unreadable).
    # Skipping only the successes will not do: the scans eat the whole limit and earlier filings never get a turn.
    # Nor can every failure count as done: then a transient fault never gets the chance to heal.
    known = store.settled_doc_ids("house")
    todo = [f for f in filings if f.doc_id not in known]
    todo.sort(key=lambda f: f.filing_date or date.min, reverse=True)
    if limit:
        todo = todo[:limit]
    with STATE.lock:
        STATE.total += len(todo)
    STATE.stage = f"House {year} ({len(todo)} new of {len(filings)})"

    for f in todo:
        try:
            res = parse.parse_house_ptr(src.house_ptr_pdf(f.year, f.doc_id), f)
            # A scan is terminal (without OCR it will never be readable); other failures wait for the next retry
            store.save_filing(f, [parse.to_dict(t) for t in res.trades],
                              res.unparsed_reason,
                              terminal=parse.is_terminal(res.unparsed_reason))
        except src.DataNotAvailable as e:
            store.save_filing(f, [], f"File unavailable: {e}")
        except Exception as e:                       # one failure should not sink the whole sync
            with STATE.lock:
                STATE.errors.append(f"House {f.doc_id} {f.name}: {type(e).__name__}: {e}")
        finally:
            with STATE.lock:
                STATE.done += 1


def _sync_senate(since: str, limit: Optional[int]) -> None:
    STATE.stage = "Senate index"
    ok, msg = src.senate_available()
    STATE.senate_status = msg
    if not ok:
        # ⚠️ An environment problem — record it as an error and show it, not as "senators did not trade"
        with STATE.lock:
            STATE.errors.append(f"Senate unavailable: {msg}")
        return

    # ⚠️ The order is what matters: **fetch the full index → filter out what is synced → only then apply the quota**.
    # Pass limit to the index instead (taking the newest N only) and, once those N are all cached,
    # every later sync fetches the same batch and discards all of it — earlier filings are **never synced**,
    # while it all looks like "it ran and nothing went wrong". The House path already works this way; this aligns with it.
    filings = src.senate_filings(since, limit=10000)
    known = store.settled_doc_ids("senate")
    todo = [f for f in filings if f.doc_id and f.doc_id not in known]
    todo.sort(key=lambda f: f.filing_date or date.min, reverse=True)
    if limit:
        todo = todo[:limit]
    with STATE.lock:
        STATE.total += len(todo)
    STATE.stage = f"Senate ({len(todo)} new of {len(filings)})"

    for f in todo:
        try:
            if f.is_paper:
                store.save_filing(f, [], parse.SENATE_PAPER_REASON,
                                  terminal=True)
            else:
                res = parse.parse_senate_ptr(src.senate_ptr_html(f.detail_url), f)
                store.save_filing(f, [parse.to_dict(t) for t in res.trades],
                                  res.unparsed_reason)
        except src.DataNotAvailable as e:
            store.save_filing(f, [], str(e))
        except src.SenateUnavailable as e:
            with STATE.lock:
                STATE.errors.append(f"Senate interrupted: {e}")
            break                                    # the session is gone, so the rest is pointless
        except Exception as e:
            with STATE.lock:
                STATE.errors.append(f"Senate {f.doc_id} {f.name}: {type(e).__name__}: {e}")
        finally:
            with STATE.lock:
                STATE.done += 1


def _run(years: list[int], since: str, limit: Optional[int],
         chambers: tuple[str, ...]) -> None:
    try:
        # ⚠️ limit is the **quota for this whole sync**, shared by both chambers.
        # Passing one to each makes limit=N process up to 2N filings — not what the parameter says.
        remaining = limit
        if "house" in chambers:
            for y in years:
                if remaining is not None and remaining <= 0:
                    break
                _sync_house(y, remaining)
                if remaining is not None:
                    remaining = max(0, limit - STATE.done)
        if "senate" in chambers and (remaining is None or remaining > 0):
            _sync_senate(since, remaining)
        STATE.stage = "done"
    except Exception as e:
        STATE.stage = f"interrupted: {type(e).__name__}: {e}"
        with STATE.lock:
            STATE.errors.append(f"Sync interrupted: {type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(year: Optional[int] = None, years_back: int = 0,
          since: Optional[str] = None, limit: Optional[int] = None,
          chambers: tuple[str, ...] = ("house", "senate")) -> dict:
    """Start an incremental sync (already running, it just returns current progress rather than stacking another).

    ⚠️ **The House archive is split by year**, so one run syncs one specified year.
    The default is the current year only — so the UI must not claim "one sync fills in all of history".
    To reach further back, pass `years_back` (3 = this year plus the previous 3, four volumes in all).
    Each extra year is several hundred more PDFs, and that cost is the user's to accept, not ours to spend quietly.
    """
    with STATE.lock:
        if STATE.running:
            # Lock already held — this must use _snapshot_locked; calling snapshot() would deadlock
            return {"started": False, "reason": "a sync is already running",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.done = 0
        STATE.total = 0
        STATE.errors = []
        STATE.stage = "starting"
        STATE.senate_status = None
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    base = year or date.today().year
    years = [base - i for i in range(max(0, years_back) + 1)]
    # ⚠️ The Senate start date has to follow the **year boundary**, not a rolling "N days back from today":
    # the House syncs by calendar year, so a 180-day rolling window on the Senate side means a July run
    # misses that year's January and February Senate filings — the two chambers cover different ground while the UI says "syncing year X".
    # Passing a year and having it not affect the Senate range contradicts the parameter outright.
    s = since or f"01/01/{min(years)}"
    threading.Thread(target=_run, args=(years, s, limit, chambers), daemon=True).start()
    return {"started": True, **STATE.snapshot()}
