"""Rule one: **what cannot be computed must not be rendered as 0.**

This is the defect codex caught most often across the project — nine times over ten sections. The shapes vary:
an accumulator starting at 0, an `or 0.0`, an `if v` folding `None` in with `False`, an empty `catch` —
but the consequence is one: an "we do not know" written out as a definite number, with nothing in the number to show it.

These cases pin every one of those traps in place.
"""
from __future__ import annotations

import pytest

from modules import darkpool as dp
from modules import flow
from modules import market as mkt
from modules import scanner as sc


# ─────────────────────── Options flow ───────────────────────

def _contract(**kw):
    """Build a Cboe contract (field names matching sources.cboe.Contract)."""
    from sources.cboe import Contract
    base = dict(symbol="X260130C00100000", expiry="2260-01-30", type="call",
                strike=100.0, bid=1.0, ask=1.2, volume=100.0,
                open_interest=50.0, iv=0.3, delta=0.5, gamma=0.01,
                vega=0.1, theta=-0.05, rho=0.01, last=1.1)
    base.update(kw)
    return Contract(**base)


def _chain(contracts, spot=100.0):
    from sources.cboe import Chain
    return Chain(ticker="X", spot=spot, timestamp="2260-01-01 00:00:00",
                 session="2260-01-01", contracts=tuple(contracts))


def test_vol_oi_with_zero_open_interest_is_neither_infinity_nor_zero():
    """OI=0 → the ratio is **not computable**. A 999 disguises it as an extreme ratio; a 0 buries it."""
    rows = flow.parse(_chain([_contract(open_interest=0.0, volume=100.0)]))
    assert rows[0].vol_oi is None
    assert rows[0].zero_prior_oi is True


def test_delta_exposure_is_null_not_zero_when_one_side_is_wholly_missing():
    """Puts have deltas, calls have none → the call side must be None.

    Counting "how many deltas there are in total" is not enough: the call side would still show 0,
    turning "this side cannot be computed" into "this side has no exposure".
    """
    ch = _chain([
        _contract(type="call", delta=None),
        _contract(type="put", strike=90.0, delta=-0.4,
                  symbol="X260130P00090000"),
    ])
    exp = flow.exposure(flow.parse(ch), spot=100.0)
    assert exp["call_delta_shares"] is None, "the call side should be None"
    assert exp["put_delta_shares"] is not None
    # With one side missing, the total is half a picture — it too must be None
    assert exp["total_delta_shares"] is None
    assert exp["counted_delta_call"] == 0 and exp["missing_delta_call"] == 1


def test_premium_is_null_not_zero_when_one_side_has_no_quotes():
    ch = _chain([
        _contract(type="call", bid=None, ask=None),
        _contract(type="put", strike=90.0, bid=2.0, ask=2.2,
                  symbol="X260130P00090000"),
    ])
    rows = flow.parse(ch)
    r = flow.ratios(rows, rows)
    assert r["by_notional"]["call"] is None, "the call side is not computable and must not be 0"
    assert r["by_notional"]["put"] is not None
    assert r["by_notional"]["pc"] is None, "with one side incomputable, the ratio is too"


def test_put_call_ratio_is_null_when_the_denominator_is_zero():
    """"No call volume" and "put/call = 0" are opposite statements."""
    ch = _chain([_contract(type="put", strike=90.0, symbol="X260130P00090000")])
    rows = flow.parse(ch)
    assert flow.ratios(rows, rows)["by_volume"]["pc"] is None


def test_mid_does_not_fall_back_to_last_when_one_side_of_the_quote_is_missing():
    """`last` may be days old, and multiplying it by today's volume goes wrong invisibly."""
    rows = flow.parse(_chain([_contract(bid=None, ask=None, last=99.0)]))
    assert rows[0].mid is None
    assert rows[0].notional is None, "last must not stand in for the mid"


def test_open_interest_basis_must_cover_contracts_that_did_not_trade_today():
    """Not trading today ≠ holding zero open interest.

    The first version computed all three bases on the traded subset, so "the structure of settled positions"
    was really "positions in the contracts touched today" — an order of magnitude out on a thin symbol.
    """
    ch = _chain([
        _contract(volume=100.0, open_interest=10.0),
        _contract(strike=110.0, volume=0.0, open_interest=999.0,
                  symbol="X260130C00110000"),
    ])
    scope = flow.parse(ch, traded_only=False)
    traded = [r for r in scope if r.volume > 0]
    r = flow.ratios(traded, scope)
    assert r["by_oi"]["call"] == 1009.0, "open interest must include the 999 that did not trade today"
    assert r["by_volume"]["call"] == 100.0, "volume counts only what traded"


# ─────────────────────── Scanner ───────────────────────

def test_the_three_causes_of_a_null_iv_rank_stay_distinguishable():
    """`None` has three causes, and calling them all "N days to go" lies about the last two —
    a missing current IV would read "**0** days to go", contradicting itself."""
    cases = [
        ({"symbol": "A", "iv30": 30.0}, [20.0] * 70, "flat_history"),
        ({"symbol": "B", "iv30": None}, [20.0, 25.0] * 40, "no_current_iv"),
        ({"symbol": "C", "iv30": 30.0}, [20.0] * 5, "insufficient_history"),
    ]
    for q, hist, want in cases:
        row = sc.build_row(q, hist, [100.0] * 10)
        assert row.iv_rank is None
        assert row.iv_reason == want, f"{q['symbol']} should be {want}"


def test_median_of_an_even_sample_averages_the_middle_two():
    """`srt[n//2]` takes the **upper** median: [10,20,30,40] gives 30 rather than 25,
    20% out, and the volume-multiple screen shifts with it."""
    assert sc._median([10, 20, 30, 40]) == 25.0
    assert sc._median([10, 20, 30]) == 20.0
    assert sc._median([]) is None


def test_volume_multiple_with_a_zero_median_is_incomputable_not_zero_or_infinity():
    row = sc.build_row({"symbol": "Z", "iv30": 30.0, "volume": 100.0},
                       [], [0.0] * 10)
    assert row.volume_x_median is None
    assert row.volume_x_reason == "zero_median", "a different thing from 'not enough history'"


def test_incomputable_rows_sink_to_the_bottom_rather_than_counting_as_the_lowest():
    """`or 0` sorts "absent" alongside "genuinely 0", with no way for the reader to tell which is which."""
    def mk(sym, rank):
        return sc.ScanRow(symbol=sym, session="2260-01-01", price=1.0,
                          change_pct=0.0, volume=1.0, iv30=1.0, iv30_change=0.0,
                          security_type="stock", iv_samples=99, iv_rank=rank,
                          iv_percentile=rank, iv_reason=None,
                          volume_x_median=None, volume_samples=0,
                          volume_x_reason=None)
    out = sc.sort_rows([mk("NONE", None), mk("ZERO", 0.0), mk("HIGH", 90.0)],
                       "iv_rank")
    assert [r.symbol for r in out] == ["HIGH", "ZERO", "NONE"]


def test_filtering_distinguishes_incomputable_rows_from_rows_that_failed_the_condition():
    """Among the rows `min_iv_rank=80` filters out, "not enough history accrued" is not "a rank below 80"."""
    rows = [sc.build_row({"symbol": "A", "iv30": 30.0}, [20.0] * 5, [])]
    kept, exc = sc.apply_filters(rows, min_iv_rank=80)
    assert kept == []
    assert exc["excluded_no_iv_rank"] == 1
    assert exc["iv_reasons"] == {"insufficient_history": 1}


# ─────────────────────── Macro ───────────────────────

def test_inversion_has_three_states_not_two():
    """A missing tenor → that spread is **not computable**, and must not be folded into "all positive"."""
    p = mkt.parse_curve({"date": "2260-01-01", "BC_3MONTH": 3.9,
                         "BC_2YEAR": 4.3, "BC_10YEAR": 4.7, "BC_30YEAR": None})
    inv = p.is_inverted
    assert inv["10Y-2Y"] is False
    assert inv["30Y-10Y"] is None, "30Y is missing → not computable, which is not 'not inverted'"
    assert p.spread("30Y-10Y") is None


# ─────────────────────── Dark pools ───────────────────────

def _dp_row(**kw):
    base = dict(summaryTypeCode="ATS_W_SMBL_FIRM", weekStartDate="2260-01-05",
                issueSymbolIdentifier="X", MPID="AAA",
                marketParticipantName="A", tierDescription="T1",
                totalWeeklyShareQuantity="1000", totalWeeklyTradeCount="10",
                totalNotionalSum="5000")
    base.update(kw)
    return base


def test_records_with_a_null_volume_are_excluded_rather_than_counted_as_zero():
    parsed = dp.parse([_dp_row(MPID="AAA", totalWeeklyShareQuantity=""),
                       _dp_row(MPID="BBB")])
    w = dp.week_summary(parsed, "2260-01-05")
    assert w["ats"]["shares"] == 1000.0, "only the row that has a value counts"
    assert w["ats"]["null_share_records"] == 1
    assert w["share"] is None, "a numerator that is too small → no share either"


def test_a_row_count_is_not_a_firm_count():
    """Non-ATS off-exchange has a blank MPID throughout — 32 records name not one firm."""
    parsed = dp.parse([_dp_row(summaryTypeCode="OTC_W_SMBL_FIRM", MPID=""),
                       _dp_row(summaryTypeCode="OTC_W_SMBL_FIRM", MPID="")])
    w = dp.week_summary(parsed, "2260-01-05")
    assert w["otc"]["records"] == 2
    assert w["otc"]["firms"] == 0, "not one can be named, so it must not read as 2"
    assert w["otc"]["anonymous_records"] == 2
