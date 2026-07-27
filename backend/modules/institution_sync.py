"""13F sync orchestration.

One window is 95MB compressed with 3.8m INFOTABLE rows, about 35 seconds to download and parse,
and roughly 3.32m rows for a single quarter once filtered by reporting period. So:
- import **one reporting period** at a time (the user picks), never all of them by default
- default to a $1m value threshold (keeping 37.5% of rows, covering 99.37% of value)
- record what was discarded in the batch table, and show it
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from sources import edgar13f as src
from modules import institution as parse
from modules import institution_store as store

#: The default value threshold. Measured: keeps 37.5% of rows, covers 99.37% of value.
DEFAULT_MIN_VALUE = 1_000_000.0


class SyncState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.rows = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.windows: list[str] = []
        self.periods: list[list] = []

    def _snapshot_locked(self) -> dict:
        """⚠️ The caller must **already hold the lock** (`threading.Lock` is not reentrant,
        so calling snapshot() while holding it deadlocks — as the Congress section found out)."""
        return {
            "running": self.running, "stage": self.stage, "rows": self.rows,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "errors": self.errors[-10:], "error_count": len(self.errors),
            "windows": self.windows, "periods": self.periods,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)


def _run(window: Optional[str], period: Optional[str], min_value: float) -> None:
    try:
        STATE.stage = "probing for available windows"
        windows = src.list_windows()
        with STATE.lock:
            STATE.windows = windows
        w = window or windows[0]

        STATE.stage = f"downloading dataset {w} (about 95MB)"
        # ⚠️ Take the ZIP handle only, **do not parse it whole** — reading all 3.8m INFOTABLE rows
        # into memory was measured peaking at 5.3GB, which gets an 8GB machine OOM-killed.
        zf = src.download_window(w)

        periods = {}
        for r in src.read_table(zf, "SUBMISSION"):
            p_ = (r.get("PERIODOFREPORT") or "").strip()
            if p_:
                periods[p_] = periods.get(p_, 0) + 1
        ordered = sorted(periods.items(), key=lambda x: -x[1])
        with STATE.lock:
            STATE.periods = [[p_, n] for p_, n in ordered[:8]]
        if not ordered:
            _err(f"The {w} window contains no filings at all")
            return

        target = period
        if not target:
            d0 = parse._parse_date(ordered[0][0])
            target = d0.isoformat() if d0 else None
        if not target:
            _err(f"Could not determine a reporting period for {w}")
            return

        # The official securities list: CUSIP → canonical issuer name.
        # Without syncing it, display falls back to the free-text names in the filings —
        # Apple's CUSIP was measured carrying 61 spellings, some of them other companies' names.
        STATE.stage = "syncing the official 13(f) securities list"
        d = parse._parse_date(target)
        for q in (src.list_quarters_for(d) if d else []):
            try:
                n = store.save_securities(src.securities_list(q), q)
                STATE.stage = f"securities list {q}: {n:,} rows"
                break
            except src.DataNotAvailable:
                continue                      # that quarter's list is not published yet; try the next
            except Exception as e:
                _err(f"securities list {q}: {type(e).__name__}: {e}")
                break

        STATE.stage = f"streaming {target} holdings in"
        # ⚠️ Write to **staging** first and switch atomically once everything succeeded —
        # with a straight "delete then write", a failure part-way leaves nothing or half a period,
        # while the batch metadata still records the old row count and every query quietly serves incomplete data.
        store.clear_staging(target)

        batch, total_kept, dropped_rows, dropped_value, parsed = [], 0, 0, 0.0, 0
        managers: set[str] = set()
        for h, dropped, val in parse.iter_holdings(zf, src, target, min_value):
            parsed += 1
            if dropped:
                dropped_rows += 1
                dropped_value += val
                continue
            batch.append(parse.to_dict(h))     # into the staging table (its own key space)
            managers.add(h.manager_cik)
            if len(batch) >= 50_000:          # flush in batches, so memory stays constant
                total_kept += len(batch)
                store.save_staging(batch)
                batch = []
                with STATE.lock:
                    STATE.rows = total_kept
                STATE.stage = f"streaming {target} in: {total_kept:,} rows written"
        if batch:
            total_kept += len(batch)
            store.save_staging(batch)
        with STATE.lock:
            STATE.rows = total_kept

        if not total_kept and not dropped_rows:
            store.clear_staging(target)         # nothing was filed for that period → leave the old data alone
            _err(f"{w} holds no {target} holdings filings (the window may be mostly another reporting period)")
            return
        if not total_kept:
            # Filings exist, but the threshold dropped every one — this is a **successful empty import**,
            # so the old data has to go with it, or the metadata says 0 rows while queries still return the old holdings
            _err(f"{target}: all {dropped_rows:,} rows fall below the ${min_value:,.0f} threshold, "
                 f"so the period has been cleared (lower the threshold and reimport to keep them)")

        # ⭐ Only now does it switch: the old data stayed whole and usable throughout the import
        STATE.stage = f"switching {target} over ({total_kept:,} rows)"
        store.commit_staging(target)

        store.mark_batch(
            period=target, window=w, rows=total_kept, parsed_rows=parsed,
            dropped_rows=dropped_rows, dropped_value=dropped_value,
            min_value=min_value, managers=len(managers))
        STATE.stage = "done"
    except src.DataNotAvailable as e:
        _err(f"Data unavailable: {e}")
        STATE.stage = "done (no data)"
    except Exception as e:
        STATE.stage = f"interrupted: {type(e).__name__}: {e}"
        _err(f"Sync interrupted: {type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(window: Optional[str] = None, period: Optional[str] = None,
          min_value: float = DEFAULT_MIN_VALUE) -> dict:
    """Start an import (one reporting period per run)."""
    with STATE.lock:
        if STATE.running:
            # Lock already held — this must use _snapshot_locked; calling snapshot() would deadlock
            return {"started": False, "reason": "a sync is already running",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.rows = 0
        STATE.errors = []
        STATE.stage = "starting"
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    threading.Thread(target=_run, args=(window, period, min_value),
                     daemon=True).start()
    return {"started": True, **STATE.snapshot()}
