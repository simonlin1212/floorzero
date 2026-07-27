"""The scanner's background job.

One market-wide scan = 6,049 light quote requests. At our self-imposed 4 requests/second that is about **26 minutes** —
so it has to be a background task reporting progress, not an endpoint that spins for 26 minutes.

━━━ ⚠️ Three deliberate choices ━━━

1. **More than one worker thread, all sharing one rate limiter.**
   The limiter sets the real rate (4/s); the threads only cover the network round-trip latency.
   Going around the limiter for speed trades a banned IP for a few minutes.

2. **One symbol failing does not stop the round**, but **every failure is recorded** and summarised.
   If 800 symbols could not be fetched in a round, the results table merely looks like "a few symbols missing";
   without the trace, "could not fetch" has disguised itself as "these symbols have no data".

3. **A subset can be scanned on its own** (the `symbols` parameter).
   Most people care about the few dozen they follow — that is a matter of seconds,
   and there is no reason to wait 26 minutes to see the IV Rank of three symbols.
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
from typing import Optional

from sources import cboe
from modules import scanner_store as store

#: Worker threads. The real rate is set by `cboe._limiter` (4/s);
#: the threads only cover network round-trip latency and are not there to go faster.
WORKERS = 8


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.total = 0
        self.done = 0
        self.stored = 0
        self.failed = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.dropped = 0
        self.cancel = False

    def _snapshot_locked(self) -> dict:
        """⚠️ The caller must **already hold the lock** — `threading.Lock` is not reentrant,
        so calling `snapshot()` while holding it deadlocks (as the Congress section found out)."""
        pct = (self.done / self.total * 100.0) if self.total else 0.0
        # Time remaining is extrapolated from the rate actually measured so far, **never guessed at a fixed value**
        return {
            "running": self.running, "stage": self.stage,
            "total": self.total, "done": self.done, "percent": round(pct, 1),
            "stored": self.stored, "failed": self.failed,
            "dropped_no_session": self.dropped,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "errors": self.errors[-10:], "error_count": len(self.errors),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = ScanState()


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)
        STATE.failed += 1


def _run(symbols: Optional[list[str]]) -> None:
    batch_id = 0
    try:
        with STATE.lock:
            STATE.stage = "fetching the symbol universe"
        universe = symbols or cboe.option_roots()
        with STATE.lock:
            STATE.total = len(universe)
            STATE.stage = f"scanning {len(universe)} symbols"
        batch_id = store.start_batch(len(universe))

        results: list[dict] = []
        res_lock = threading.Lock()

        attempted = 0
        att_lock = threading.Lock()

        def one(sym: str) -> None:
            nonlocal attempted
            # ⚠️ On cancellation, **still increment done**, or the progress bar stops halfway forever —
            #    the job has actually finished while the interface looks stuck.
            with STATE.lock:
                cancelled = STATE.cancel
            if cancelled:
                with STATE.lock:
                    STATE.done += 1
                return
            with att_lock:
                attempted += 1
            try:
                q = cboe.quote(sym)
            except cboe.DataNotAvailable:
                # This symbol genuinely has no quote — an absence, not a failed fetch, and not counted as a failure
                with STATE.lock:
                    STATE.done += 1
                return
            except ValueError as e:                 # the symbol itself is not valid
                _err(f"{sym}: {e}")
                with STATE.lock:
                    STATE.done += 1
                return
            except RuntimeError as e:               # network / rate limit / upstream fault — must leave a trace
                _err(f"{sym}: {e}")
                with STATE.lock:
                    STATE.done += 1
                return
            except Exception as e:                  # noqa: BLE001
                # ⚠️ **Catch everything unexpected.** Should upstream return a structure we did not anticipate,
                #    the AttributeError/TypeError raised here escapes through `ex.map()` and takes the whole
                #    round down — so even the **several thousand already fetched successfully** never get stored.
                #    One symbol's surprise should not destroy the round: record it and carry on.
                _err(f"{sym}: unexpected exception {type(e).__name__}: {e}")
                with STATE.lock:
                    STATE.done += 1
                return
            with res_lock:
                results.append({
                    "symbol": q.symbol, "session": q.session, "price": q.price,
                    "change_pct": q.change_pct, "volume": q.volume,
                    "iv30": q.iv30, "security_type": q.security_type,
                })
            with STATE.lock:
                STATE.done += 1

        with cf.ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, universe))

        with STATE.lock:
            STATE.stage = "writing local history"
        rec = store.record_quotes(results)
        sess = max((r["session"] for r in results if r.get("session")), default=None)
        # ⚠️ Symbols where upstream gave a quote but no trading session are dropped — **they have to count as failures**,
        #    or the interface reports "all succeeded" while those symbols never entered the store.
        dropped = rec["dropped_no_session"]
        with STATE.lock:
            STATE.stored = rec["stored"]
            STATE.dropped = dropped
            if dropped:
                STATE.errors.append(
                    f"{dropped} had quotes but **no trading session from upstream** and were dropped "
                    f"(the wrong key would contaminate the IV samples)")
                STATE.failed += dropped
            STATE.stage = "finished" if not STATE.cancel else "cancelled"
            failed = STATE.failed
        note = []
        if STATE.cancel:
            note.append("cancelled by the user")
        if dropped:
            note.append(f"{dropped} dropped for want of a trading session")
        # ⚠️ `scanned` records **how many were actually attempted**, not `len(results)` —
        #    the latter leaves out "attempted but unfetchable", which then looks as though they were never scanned at all.
        store.finish_batch(batch_id, attempted, rec["stored"], failed, sess,
                           note="; ".join(note))
    except Exception as e:                          # noqa: BLE001 — background-thread backstop
        _err(f"Scan interrupted: {type(e).__name__}: {e}")
        with STATE.lock:
            STATE.stage = "interrupted"
        if batch_id:
            store.finish_batch(batch_id, STATE.done, STATE.stored, STATE.failed,
                               None, note=f"interrupted: {type(e).__name__}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = _now()


def start(symbols: Optional[list[str]] = None) -> dict:
    """Start a scan (already running, it returns the current state unchanged rather than queueing a second)."""
    with STATE.lock:
        if STATE.running:
            return {**STATE._snapshot_locked(), "started": False,
                    "note": "A scan is already running; wait for it to finish, or cancel it."}
        STATE.running = True
        STATE.cancel = False
        STATE.stage = "starting"
        STATE.total = len(symbols) if symbols else 0
        STATE.done = STATE.stored = STATE.failed = STATE.dropped = 0
        STATE.errors = []
        STATE.started_at = _now()
        STATE.finished_at = None
        snap = STATE._snapshot_locked()
    threading.Thread(target=_run, args=(symbols,), daemon=True).start()
    return {**snap, "started": True}


def cancel() -> dict:
    with STATE.lock:
        STATE.cancel = True
        STATE.stage = "cancelling"
        return STATE._snapshot_locked()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
