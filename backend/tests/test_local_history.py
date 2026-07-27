"""Rule two: the semantics of locally accrued history.

These two histories (open interest per contract, iv30 per symbol) **cannot be backfilled** — Cboe gives only the present.
So getting them wrong costs differently from everywhere else: other lanes can be refetched, and a mistake here is permanent.

Four specific traps, every one of them hit for real:
1. Keying on the **wall-clock date** → open the page twice over a weekend and one Friday's data is stored as "two days of observation"
2. Not truncating history to the target session when computing IV Rank → **lookahead bias** (judging a day with quotes that had not happened)
3. An expiring contract leaving the chain → read as "the whole position closed out"
4. A bare `INSERT OR REPLACE` on re-recording a session → one field missing upstream wipes an already-accrued value to NULL
"""
from __future__ import annotations

import pytest


def _q(symbol, session, iv30=20.0, volume=100.0):
    return {"symbol": symbol, "session": session, "price": 1.0,
            "change_pct": 0.0, "volume": volume, "iv30": iv30,
            "security_type": "stock"}


def _oi(expiry, typ, strike, oi, vol=0.0):
    return {"expiry": expiry, "type": typ, "strike": strike,
            "open_interest": oi, "volume": vol}


# ─────────────────── The key is the trading session, not the wall-clock date ───────────────────

def test_the_key_is_the_session_passed_in_not_the_wall_clock_date(tmp_db):
    """Open the page twice over a weekend and both times you get the same Friday close.

    Stored by wall clock, the database shows "two days" and every OI difference is 0 —
    which looks like "positions did not change" when there was **no new data at all**.

    ⚠️ Asserting only "there is one day" is a **false positive**: both calls happen on the same wall-clock
    date, so even an implementation that ignored the session entirely and used `date.today()` would pass.
    So this asserts that **the key equals the value passed in**.
    """
    from modules import flow_store as fs
    rows = [_oi("2260-02-20", "call", 100.0, 500.0, 10.0)]
    fs.record("X", "2260-01-05", 100.0, rows)
    fs.record("X", "2260-01-05", 100.0, rows)      # the same session again
    got = [d["snapshot_date"] for d in fs.dates("X")]
    assert got == ["2260-01-05"], "the key must be the session passed in, not today"


def test_re_recording_a_session_replaces_it_whole_rather_than_merging_rows(tmp_db):
    """With `INSERT OR REPLACE` alone, old rows not included this time stay —
    so one session holds two pulls of different scope, and the difference is computed across the stitch."""
    from modules import flow_store as fs
    from modules import db
    fs.record("X", "2260-01-05", 100.0,
              [_oi("2260-02-20", "call", 100.0, 500.0),
               _oi("2260-02-20", "call", 110.0, 300.0)])
    fs.record("X", "2260-01-05", 100.0,
              [_oi("2260-02-20", "call", 100.0, 500.0)])   # only one row this time
    # ⚠️ Query the detail rows directly rather than reading `dates()`'s summary fields —
    #    otherwise the write logic under test and the summary logic under test vouch for each other.
    with db.connect() as conn:
        strikes = [r["strike"] for r in conn.execute(
            "SELECT strike FROM oi_snapshot WHERE ticker='X' "
            "AND snapshot_date='2260-01-05'")]
    assert strikes == [100.0], "the 110 row should not have stayed"


def test_a_null_open_interest_raises_on_the_spot_rather_than_storing_a_zero(tmp_db):
    """Upstream really did give None, which means "could not fetch" — stored as 0 it becomes a phantom change tomorrow."""
    from modules import flow_store as fs
    with pytest.raises(ValueError, match="failed fetch"):
        fs.record("X", "2260-01-05", 100.0,
                  [{"expiry": "2260-02-20", "type": "call", "strike": 100.0,
                    "open_interest": None, "volume": 1.0}])


def test_a_quote_with_no_session_really_does_not_reach_the_database(tmp_db):
    """Get the key wrong and the IV samples are scrambled, in a way the data itself will not show.
    So dropping is preferable — but **how many were dropped has to be reported**.

    ⚠️ Checking only the returned count is a **false positive**: an implementation could return `dropped=1`
    and still write B in under a wall-clock date or a NULL — the count right, the history already dirty.
    So this **queries the database directly**.
    """
    from modules import db, scanner_store as ss
    rec = ss.record_quotes([_q("A", "2260-01-05"), _q("B", None)])
    assert rec == {"stored": 1, "dropped_no_session": 1}
    with db.connect() as conn:
        syms = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM quote_snapshot")]
    assert syms == ["A"], "B should not have been stored in any form"


# ─────────────────── Lookahead bias ───────────────────

def test_history_is_truncated_at_the_target_session_to_avoid_lookahead_bias(tmp_db):
    """Viewing the 01-10 scan results with 01-28 data in the sample
    is judging that day's IV with quotes that had not happened yet."""
    from modules import scanner_store as ss
    ss.record_quotes([_q("X", f"2260-01-{d:02d}", iv30=float(d))
                      for d in range(1, 29)])
    full = ss.history(["X"])["X"]["iv"]
    cut = ss.history(["X"], as_of="2260-01-10")["X"]["iv"]
    assert max(full) == 28.0
    assert max(cut) == 10.0, "data after as_of must not enter the sample"
    assert len(cut) == 10


def test_each_symbol_takes_its_own_newest_row_not_only_the_newest_session(tmp_db):
    """Symbols within one scan need not share a `last_trade_time` (a halt, a delayed upstream update).
    Taking only those equal to the newest session hides another set of **successfully stored** symbols entirely."""
    from modules import scanner_store as ss
    ss.record_quotes([_q("A", "2260-01-28"), _q("B", "2260-01-27")])
    got = {r["symbol"] for r in ss.quotes_at("2260-01-28")}
    assert got == {"A", "B"}, "B is merely a session older and should not be hidden"


def test_a_rescan_missing_a_field_upstream_does_not_wipe_an_accrued_value(tmp_db):
    """A bare `INSERT OR REPLACE` overwrites an already-accrued iv30 with NULL —
    the sample count falls instead of rising, and this history cannot be backfilled."""
    from modules import scanner_store as ss
    ss.record_quotes([_q("A", "2260-01-05", iv30=42.0)])
    ss.record_quotes([{"symbol": "A", "session": "2260-01-05", "price": 9.0,
                       "change_pct": 0.0, "volume": None, "iv30": None,
                       "security_type": "stock"}])
    row = ss.quotes_at("2260-01-05")[0]
    assert row["iv30"] == 42.0, "the existing iv30 has to survive"
    assert row["price"] == 9.0, "fields that do have a new value update as normal"


# ─────────────────── Open interest differences ───────────────────

def _seed_oi(fs):
    fs.record("T", "2260-01-05", 100.0, [
        _oi("2260-01-06", "call", 100.0, 1000.0, 50.0),   # expires during the period
        _oi("2260-03-19", "put", 90.0, 500.0, 0.0),       # no volume, open interest unchanged
        _oi("2260-03-19", "call", 110.0, 200.0, 10.0),
        _oi("2260-06-18", "call", 120.0, 777.0, 5.0),     # will be absent from the end snapshot
    ])
    fs.record("T", "2260-01-07", 100.0, [
        _oi("2260-03-19", "put", 90.0, 500.0, 0.0),
        _oi("2260-03-19", "call", 110.0, 400.0, 140.0),
    ])


def test_an_expiring_contract_leaving_the_chain_is_not_a_close_out(tmp_db):
    from modules import flow_store as fs
    _seed_oi(fs)
    r = fs.oi_change("T")
    assert r["expired_excluded"] == 1
    assert r["expired_oi"] == 1000.0
    assert all(x["expiry"] != "2260-01-06" for x in r["lost"]), \
        "expiring is expiring, and not anyone closing out"


def test_unexpired_but_absent_means_an_incomplete_pull_not_a_zeroed_position(tmp_db):
    """Cboe lists contracts through to expiry and keeps listing them at zero —
    so "not expired and absent" can only mean that pull missed it."""
    from modules import flow_store as fs
    _seed_oi(fs)
    r = fs.oi_change("T")
    assert r["incomplete_excluded"] == 1
    assert r["incomplete_oi"] == 777.0
    assert all(x["expiry"] != "2260-06-18" for x in r["lost"]), \
        "a -777 net reduction must not be fabricated"


def test_contracts_with_no_volume_and_unchanged_open_interest_stay_out_of_both_tables(tmp_db):
    from modules import flow_store as fs
    _seed_oi(fs)
    r = fs.oi_change("T")
    moved = {(x["expiry"], x["type"]) for x in r["gained"] + r["lost"]}
    assert ("2260-03-19", "put") not in moved


def test_one_day_accrued_says_not_enough_yet_rather_than_no_change(tmp_db):
    from modules import flow_store as fs
    fs.record("T", "2260-01-05", 100.0, [_oi("2260-03-19", "call", 100.0, 1.0)])
    r = fs.oi_change("T")
    assert r["enough"] is False
    assert "at least two" in r["note"]


def test_invented_or_reversed_dates_are_rejected(tmp_db):
    """`_load()` returns an empty dict when it finds nothing — and the difference then reads
    "this day was never stored" as "open interest was zero that day", producing a fake wholesale-exit report."""
    from modules import flow_store as fs
    _seed_oi(fs)
    for kw in ({"date_to": "1999-01-01"},
               {"date_from": "2099-01-01"},
               {"date_from": "2260-01-07", "date_to": "2260-01-05"}):
        assert fs.oi_change("T", **kw)["enough"] is False, kw


def test_adjacency_follows_snapshot_order_not_the_calendar_gap(tmp_db):
    """Tuesday→Friday is also 3 days but misses two observations;
    Friday→Monday is 3 days too and genuinely adjacent. The order in the database is the truth."""
    from modules import flow_store as fs
    _seed_oi(fs)
    fs.record("T", "2260-01-09", 100.0, [_oi("2260-03-19", "call", 110.0, 500.0)])
    near = fs.oi_change("T")                                     # 01-07 → 01-09
    assert near["is_consecutive"] is True
    far = fs.oi_change("T", date_from="2260-01-05", date_to="2260-01-09")
    assert far["is_consecutive"] is False
    assert far["snapshots_between"] == 1
