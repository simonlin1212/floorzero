"""Rule three: the rest of the places that go quietly wrong the moment they are touched.

- The four dark-pool record types arrive in one response, and adding them wrong doubles the figure
- The stock page has nine lanes, and one failing must not take the page down
- There are seven reason codes, and only `no_data` means "this ticker has no such activity"
- The greeks' signs and dimensions (checked against finite differences, not by copying the formula)
- An unconfigured contact must fail fast
"""
from __future__ import annotations

import math

import pytest

from modules import darkpool as dp
from modules import stock as st


# ─────────────────── Dark pools: never count the same volume twice ───────────────────

def _row(**kw):
    base = dict(summaryTypeCode="ATS_W_SMBL_FIRM", weekStartDate="2260-01-05",
                issueSymbolIdentifier="X", MPID="AAA",
                marketParticipantName="A", tierDescription="T1",
                totalWeeklyShareQuantity="1000", totalWeeklyTradeCount="10",
                totalNotionalSum="5000")
    base.update(kw)
    return base


def test_aggregate_and_detail_rows_must_not_be_added_together():
    """`*_SMBL` is the aggregate and `*_SMBL_FIRM` the same volume split by firm.
    SUM everything and the result is precisely twice the truth — as measured on NVDA that week."""
    rows = [
        _row(MPID="AAA", totalWeeklyShareQuantity="600"),
        _row(MPID="BBB", totalWeeklyShareQuantity="400"),
        _row(summaryTypeCode="ATS_W_SMBL", MPID="",
             totalWeeklyShareQuantity="1000"),          # the aggregate row
    ]
    w = dp.week_summary(dp.parse(rows), "2260-01-05")
    assert w["ats"]["shares"] == 1000.0, "use the detail total only; the aggregate row must not be added to it"
    assert w["ats"]["reconcile"]["matches"] is True


def test_the_reconciliation_tolerance_is_one_share_not_a_relative_error():
    """Share counts are integers. Written `max(1.0, ref*1e-6)` it takes **the larger** —
    at 268m shares that is 268 shares of tolerance, enough to let a genuinely missing or duplicated row through."""
    rows = [_row(totalWeeklyShareQuantity="268000000"),
            _row(summaryTypeCode="ATS_W_SMBL", MPID="",
                 totalWeeklyShareQuantity="268000200")]      # 200 shares out
    w = dp.week_summary(dp.parse(rows), "2260-01-05")
    assert w["ats"]["reconcile"]["matches"] is False, "200 shares out should report a mismatch"


def test_ats_and_non_ats_stay_apart_throughout():
    """"Dark pool" is not "off-exchange": non-ATS is wholesaler internalisation, measured at more than double ATS.
    The module **deliberately provides no** `dark_pool_shares` field, so callers are not tempted to add them."""
    rows = [_row(totalWeeklyShareQuantity="100"),
            _row(summaryTypeCode="OTC_W_SMBL_FIRM", MPID="",
                 totalWeeklyShareQuantity="240")]
    w = dp.week_summary(dp.parse(rows), "2260-01-05")
    assert w["ats"]["shares"] == 100.0
    assert w["otc"]["shares"] == 240.0
    assert "dark_pool_shares" not in w, "do not offer a field name that invites addition"
    assert w["ats_over_otc"] == pytest.approx(100 / 240)


def test_unknown_record_types_are_counted_rather_than_dropped_in_silence():
    """When FINRA adds a type, we would otherwise miss a whole class of trading without a sound."""
    parsed = dp.parse([_row(summaryTypeCode="BRAND_NEW_TYPE")])
    assert parsed["unknown_types"] == {"BRAND_NEW_TYPE": 1}
    assert parsed["parsed_any"] is False, "not one row of a known type → the parser is incompatible"


def test_mean_shares_per_trade_is_incomputable_at_zero_trades():
    rows = dp.parse([_row(totalWeeklyTradeCount="0")])["ats_firm"]
    assert rows[0].avg_trade_size is None, "\"nothing traded\" is not \"0 shares per trade\""


# ─────────────────── The stock page: one lane failing does not take the page down ───────────────────

def test_a_broken_instant_on_one_lane_does_not_blow_up_the_page():
    """`lag_days` is a property: it reads like a field and executes a date parse —
    and it is evaluated inside `assemble()`, which is already outside every per-lane guard."""
    lanes = [st.lane("quote", as_of="not a date", data={}),
             st.lane("gex", as_of="2260-01-05", data={})]
    out = st.assemble(lanes)                       # must not raise
    got = {l["key"]: l["lag_days"] for l in out["lanes"]}
    assert got["quote"] is None
    assert isinstance(got["gex"], int)


def test_a_future_instant_does_not_sort_to_the_top():
    """An instant in the future can only be bad data or a timezone problem; a negative would take the "newest" slot."""
    out = st.assemble([st.lane("quote", as_of="2999-01-01", data={})])
    assert out["lanes"][0]["lag_days"] == 0


def test_only_no_data_means_this_ticker_has_no_such_activity():
    """Of the seven reason codes, the other six all say "we could not get it".
    Conflated, the reader concludes "this ticker has no insider trading".

    ⚠️ This case once decided by **looking for a particular word in the label** — a heuristic that any
    rewording defeats (`no_data: "the fetch failed"` would have passed just as well).
    The right way is for **the code itself** to carry the classification (`MEANS_ABSENT`),
    with UI, MCP and tests all reading that one constant rather than each guessing from the prose.
    """
    assert st.MEANS_ABSENT == {"no_data"}
    # The two sets must be disjoint and together cover every reason code — nothing may go unclassified
    assert not (st.MEANS_ABSENT & st.MEANS_UNAVAILABLE)
    assert st.MEANS_ABSENT | st.MEANS_UNAVAILABLE == set(st.REASON_LABEL)


def test_every_lane_carries_whether_it_is_genuinely_absent():
    """The frontend and tool layer use this field directly, without deciding for themselves which count as absent."""
    absent = st.to_dict(st.lane("insider", reason="no_data", detail=""))
    cant = st.to_dict(st.lane("darkpool", reason="disabled", detail=""))
    fine = st.to_dict(st.lane("quote", as_of="2020-01-01", data={}))
    assert absent["means_absent"] is True
    assert cant["means_absent"] is False
    assert fine["means_absent"] is None, "it does not apply to a block that has data"


def test_the_timeline_sorts_by_lag_not_by_a_hardcoded_order():
    # ⚠️ Use dates in the **past**: a future instant is clamped to 0, two blocks tie,
    #    and a stable sort keeps the original order — at which point this case is not testing the sort.
    lanes = [st.lane("institution", as_of="2020-01-01", data={}),
             st.lane("quote", as_of="2020-06-01", data={})]
    out = st.assemble(lanes)
    assert [l["key"] for l in out["lanes"]] == ["quote", "institution"]
    assert out["lag_spread_days"]["oldest"] > out["lag_spread_days"]["newest"]


def test_blocks_that_could_not_be_fetched_sink_to_the_end():
    lanes = [st.lane("darkpool", reason="disabled", detail="switched off"),
             st.lane("quote", as_of="2020-06-01", data={})]
    out = st.assemble(lanes)
    assert out["lanes"][-1]["key"] == "darkpool"
    assert out["available"] == 1 and out["unavailable"] == 1


# ─────────────────── The greeks: checked against finite differences ───────────────────

def _call_delta(S, K, t, sigma, r):
    """Compute delta from d1 here, as the finite-difference reference for gamma.

    ⚠️ It **deliberately does not reuse the module's implementation** — verifying code with its own
    intermediate values verifies "I copied it consistently", not "the formula is right".
    """
    from statistics import NormalDist
    d1 = ((math.log(S / K) + (r + 0.5 * sigma ** 2) * t)
          / (sigma * math.sqrt(t)))
    return NormalDist().cdf(d1)


def test_gamma_is_the_derivative_of_delta_with_respect_to_spot():
    """Checked by finite differences, rather than one formula checking another."""
    from modules import bs
    S, K, t, v, r = 100.0, 100.0, 0.25, 0.30, 0.04
    h = 1e-3
    numeric = (_call_delta(S + h, K, t, v, r)
               - _call_delta(S - h, K, t, v, r)) / (2 * h)
    assert bs.bs_gamma(S, K, t, v, r) == pytest.approx(numeric, rel=1e-5)


def test_gamma_peaks_near_the_money_and_decays_monotonically_either_side():
    """With the shape wrong, a correct sign is worth nothing.

    ⚠️ Comparing only "at the money vs two distant points" is a **false positive**: a peak that wandered to 110 still beats 70 and 130.
    So this sweeps a grid of strikes and checks two things —
    ① the peak really does land near spot (with rates it need not be **exactly** at K=S, hence a narrow band)
    ② it decreases monotonically away from the peak in both directions
    """
    from modules import bs
    S = 100.0
    ks = [70 + 2 * i for i in range(31)]              # 70 … 130
    gs = [bs.bs_gamma(S, float(k), 0.25, 0.3) for k in ks]
    peak = ks[gs.index(max(gs))]
    assert abs(peak - S) <= 6, f"the peak landed at {peak}, not near spot"
    top = gs.index(max(gs))
    assert all(gs[i] < gs[i + 1] for i in range(top)), "it should rise to the left of the peak"
    assert all(gs[i] > gs[i + 1] for i in range(top, len(gs) - 1)), "it should fall to the right of the peak"


def test_degenerate_input_returns_zero_rather_than_raising_or_nan():
    """Expired contracts and deep out-of-the-money contracts with no quote occur daily in a real chain."""
    from modules import bs
    for args in ((100.0, 100.0, 0.0, 0.3),      # expired
                 (100.0, 100.0, 0.25, 0.0),     # zero volatility
                 (0.0, 100.0, 0.25, 0.3),       # spot of 0
                 (100.0, 0.0, 0.25, 0.3)):      # strike of 0
        g = bs.bs_gamma(*args)
        assert g == 0.0 and math.isfinite(g), args


def test_vanna_and_charm_survive_degenerate_input_too():
    from modules import bs
    for fn in (bs.bs_vanna, bs.bs_charm):
        v = fn(100.0, 100.0, 0.0, 0.3)
        assert math.isfinite(v), f"{fn.__name__} returned {v} at t=0"


def test_zero_dte_does_not_blow_gamma_up_to_infinity():
    """`years_to_expiry` counting 0DTE as half a trading day exists for exactly this."""
    from modules import bs
    t = bs.years_to_expiry(0)
    assert t > 0
    assert math.isfinite(bs.bs_gamma(100.0, 100.0, t, 0.3))


# ─────────────────── The contact: must fail fast ───────────────────

def test_an_unconfigured_contact_raises_outright(monkeypatch):
    """A built-in placeholder would send every user's upstream traffic under the author's name,
    and leave no clue at all when throttled."""
    from sources import contact
    monkeypatch.delenv("FZ_CONTACT", raising=False)
    with pytest.raises(contact.ContactNotConfigured):
        contact.user_agent()


def test_the_contact_has_to_look_like_an_email(monkeypatch):
    from sources import contact
    monkeypatch.setenv("FZ_CONTACT", "just a name")
    with pytest.raises(contact.ContactNotConfigured):
        contact.user_agent()


def test_once_configured_the_ua_carries_the_contact(monkeypatch):
    from sources import contact
    monkeypatch.setenv("FZ_CONTACT", "Someone one@example.com")
    assert "one@example.com" in contact.user_agent()


# ─────────────────── The tool layer: declaration and implementation must not drift ───────────────────

def test_every_declared_tool_has_an_implementation():
    """Tools are defined in `tools.py` alone — the MCP server inherits them automatically.
    A declaration without an implementation gives "a tool in MCP that cannot be called"."""
    import tools
    assert sorted(t["name"] for t in tools.TOOLS) == sorted(tools._IMPL)


def test_tool_schemas_carry_the_required_fields():
    import tools
    for t in tools.TOOLS:
        assert t.get("name") and t.get("description"), t
        assert t["inputSchema"]["type"] == "object", t["name"]


def test_an_unknown_tool_returns_a_structured_error_rather_than_raising():
    """An MCP caller needs the error, not a dropped connection."""
    import tools
    assert "error" in tools.exec_tool("no_such_tool", {})


# ─────────────────── Congress: terminal classification must not rest on prose ───────────────────

def test_the_scan_reason_and_the_terminal_check_are_one_constant():
    """A PDF that is pure image cannot be parsed, and that is **terminal** — without OCR it never will be, so retrying is pointless.

    The sync layer once decided this by looking for a word inside that sentence. Reword it in translation and the check fails:
    33 scans go back to being retried every time, eating the quota so earlier filings never get a turn,
    while the sync reports success as usual. So what this asserts is that **the producer and the classifier share one constant**,
    not that the sentence contains some particular word.
    """
    from modules import congress as parse

    writer = pytest.importorskip("pypdf").PdfWriter()
    writer.add_blank_page(width=612, height=792)      # a valid PDF with zero extractable characters
    buf = __import__("io").BytesIO()
    writer.write(buf)

    res = parse.parse_house_ptr(buf.getvalue(), _filing())
    assert res.trades == ()
    assert res.unparsed_reason is not None
    assert parse.is_terminal(res.unparsed_reason), res.unparsed_reason


def test_retryable_failures_are_not_classified_as_terminal():
    """Network faults and temporary unavailability — classified terminal, they would never be retried again."""
    from modules import congress as parse
    for reason in ("File unavailable: timeout", "No transaction lines recognised in the PDF",
                   None):
        assert not parse.is_terminal(reason), reason


def _filing():
    from datetime import date
    from sources.congress import Filing
    return Filing(chamber="house", name="X", last="X", first="X",
                  state_district="IN02", filing_type="P",
                  filing_date=date(2260, 1, 5), year="2260", doc_id="1",
                  detail_url="")


def test_an_unsupported_filter_fails_loudly_rather_than_being_ignored(tmp_db):
    """FastAPI ignores query parameters it does not declare, so `?ticker=NVDA` on the 13F
    endpoint returned HTTP 200 with the whole unfiltered table — the caller believes they
    filtered and they did not.

    That is the silent wrong answer this project exists to avoid, and it is worse here than
    elsewhere: the stock page already tells the user in prose that a holding cannot be located
    from a symbol, while the API quietly accepted one.
    """
    from fastapi.testclient import TestClient
    import app

    c = TestClient(app.app)
    bad = c.get("/api/institution/holdings?ticker=NVDA")
    assert bad.status_code == 400, "an unsupported filter must not be silently ignored"
    assert "cusip" in bad.json()["detail"].lower(), "the error has to say what to use instead"
    # the supported filters must keep working
    # tmp_db keeps this off the real database — an empty store still answers 200
    assert c.get("/api/institution/holdings").status_code == 200
    assert c.get("/api/institution/holdings?cusip=037833100").status_code == 200


# ─────────── Rule five: a chain is dated by its own session, not by the wall clock ───────────

def _session_chain():
    """A chain whose session is 2260-01-05 while "today" has already rolled to 2260-01-06.

    That is the ordinary state of things for most of the day: Cboe's file keeps carrying the
    last completed session until the next one starts printing, so between ET midnight and the
    open — and all weekend — `session` is behind the wall clock.
    """
    from sources.cboe import Chain, Contract
    def c(expiry, volume):
        return Contract(symbol="X", expiry=expiry, type="call", strike=100.0,
                        bid=1.0, ask=1.2, volume=volume, open_interest=50.0,
                        iv=0.3, delta=0.5, gamma=0.01, vega=0.1, theta=-0.05,
                        rho=0.01, last=1.1)
    return Chain(ticker="X", spot=100.0, timestamp="2260-01-06 03:57:10",
                 session="2260-01-05",
                 contracts=(c("2260-01-05", 900.0),      # the session's own 0DTE
                            c("2260-01-06", 100.0),      # the next expiry
                            c("2260-02-20", 10.0)))      # far out


def test_zero_dte_means_the_sessions_own_expiry_not_the_wall_clocks(monkeypatch):
    """`0DTE` measured against the wall clock selects **the wrong expiry** once the ET date rolls.

    Measured live on 2026-07-28: session was 2026-07-27, and `?expiry=0DTE` returned the
    2026-07-28 expiry (−0.745bn of GEX) while the session's actual 0DTE was 2026-07-27
    (−1.390bn). Not a rounding difference — a different expiry, roughly half the magnitude.
    """
    from sources import cboe
    monkeypatch.setattr(cboe, "et_today", lambda: "2260-01-06")
    got = {c.expiry for c in _session_chain().filter(expiry="0DTE")}
    assert got == {"2260-01-05"}, f"0DTE picked {got}, but the session's own expiry is 2260-01-05"


def test_a_dte_window_keeps_the_sessions_own_expiry(monkeypatch):
    """`dte_max` drops anything with a negative dte, so against the wall clock the session's
    own 0DTE — the single largest bucket of the day — silently leaves the sample.

    Measured live: the flow page defaults to `dte_max=7` and showed 3,169,494 of the session's
    13,835,063 contracts. The missing 66.2% was one expiry: the session's own 0DTE.
    """
    from sources import cboe
    monkeypatch.setattr(cboe, "et_today", lambda: "2260-01-06")
    cs = _session_chain().filter(dte_max=7)
    assert "2260-01-05" in {c.expiry for c in cs}, "the session's own 0DTE was filtered out"
    assert sum(c.volume for c in cs) == 1000.0, "a dte window must not lose the session's volume"


def test_the_whole_chain_equals_the_sum_of_its_dte_slices(monkeypatch):
    """Rule four in its sharpest form: two views of one dataset must not disagree.

    Unfiltered took every contract while any `dte_max` slice dropped the negative ones, so
    "whole chain" was not the sum of its own parts and neither number looked wrong on its own.
    """
    from sources import cboe
    monkeypatch.setattr(cboe, "et_today", lambda: "2260-01-06")
    ch = _session_chain()
    whole = sum(c.volume for c in ch.filter())
    sliced = sum(c.volume for c in ch.filter(dte_max=10_000))
    assert whole == sliced == 1010.0


def test_dte_needs_an_explicit_reference_date(monkeypatch):
    """The defect was structural: `Contract` holds no reference to its `Chain`, so a zero-argument
    `dte` could only reach for the wall clock. Requiring the reference is what stops it coming back —
    a call site that forgets one now fails loudly instead of quietly measuring against today.
    """
    from sources import cboe
    monkeypatch.setattr(cboe, "et_today", lambda: "2260-01-06")
    ch = _session_chain()
    assert ch.asof == "2260-01-05", "a chain dates itself by its session"
    c0 = next(c for c in ch.contracts if c.expiry == "2260-01-05")
    assert c0.dte_from(ch.asof) == 0
    assert c0.dte_from("2260-01-06") == -1          # against the wall clock, expired
    assert not hasattr(c0, "dte"), "a zero-argument dte would silently reintroduce the wall clock"


def test_a_chain_without_a_session_falls_back_to_the_wall_clock(monkeypatch):
    """Session is optional in the dataclass, and a fallback that raised would take the page down."""
    from sources import cboe
    monkeypatch.setattr(cboe, "et_today", lambda: "2260-01-06")
    from sources.cboe import Chain
    assert Chain(ticker="X", spot=1.0, timestamp=None, contracts=()).asof == "2260-01-06"


# ─────────── Rule four: a summary describes every matching row, not the newest page ───────────

def _seed_congress(n: int):
    """n trades whose filing delay alternates 90 / 10 days, the slow half being the older half.

    Ordered that way on purpose: take only the newest page and the late filings are exactly
    what falls off the end, so "over 45 days" reads far lower than the truth.
    """
    from datetime import date, timedelta
    from modules import congress_store
    trades = []
    for i in range(n):
        slow = i < n // 2                       # older half filed late
        tx = date(2260, 1, 1) + timedelta(days=i)
        delay = 90 if slow else 10
        trades.append(dict(
            member=f"Member {i % 7}", state_district="IN02", ticker="NVDA",
            asset_name="NVIDIA", asset_type="ST", asset_type_label="Stock",
            tx_type="P", tx_type_label="Purchase", tx_date=tx.isoformat(),
            notification_date=None, filing_date=(tx + timedelta(days=delay)).isoformat(),
            amount_low=1001.0, amount_high=15000.0, amount_raw="$1,001 - $15,000",
            owner="self", delay_days=delay, source_url=""))
    congress_store.save_filing(_filing(), trades)


def test_congress_summary_describes_every_matching_row_not_the_newest_page(tmp_db):
    """The cards read as statistics for the whole period, so they have to be computed over it.

    Measured on the real database before the fix: with 3,766 filings the cards were built from
    the newest 2,000 and reported 721 filings past the 45-day deadline. The true figure was 992 —
    **27% of the late filings missing**, on the one metric this section exists to show. The API
    did return `truncated: true`, but only the detail table rendered it, and a footnote under a
    table cannot repair a headline number above it.
    """
    from fastapi.testclient import TestClient
    import app

    _seed_congress(400)
    c = TestClient(app.app)
    # a limit far below the row count must not change what the statistics describe
    small = c.get("/api/congress/summary?limit=10").json()
    whole = c.get("/api/congress/summary?limit=20000").json()

    assert small["delay"]["over_45d_count"] == 200, "the late half has to be counted in full"
    assert small["delay"]["over_45d_count"] == whole["delay"]["over_45d_count"]
    assert small["delay"]["median_days"] == whole["delay"]["median_days"]
    assert small["total_trades"] == whole["total_trades"] == 400, \
        "a count of trades must be the count, never the row limit"


def test_congress_summary_scope_reports_the_aggregate_as_complete(tmp_db):
    """`truncated` meant "these numbers are partial". Now that they never are, it has to say so —
    a stale true left there would be a second wrong answer in place of the first."""
    from fastapi.testclient import TestClient
    import app

    _seed_congress(400)
    body = TestClient(app.app).get("/api/congress/summary?limit=10").json()
    assert body["scope"]["sampled"] == 400
    assert body["scope"]["truncated"] is False


def test_a_detail_listing_reports_its_own_truncation_not_the_summarys(tmp_db):
    """The two tables captioned themselves from `summary.scope.truncated` — a flag describing a
    different query. Insider's was hardcoded false, so that caption could never appear however
    many rows were cut; and making the congress aggregate whole would have silently retired the
    congress one the same way. A listing has to carry its own scope.
    """
    from fastapi.testclient import TestClient
    import app

    _seed_congress(400)
    c = TestClient(app.app)
    cut = c.get("/api/congress/trades?limit=50").json()
    assert cut["count"] == 50
    assert cut["scope"]["truncated"] is True, "a capped listing has to say it was capped"
    assert cut["scope"]["limit"] == 50

    whole = c.get("/api/congress/trades?limit=2000").json()
    assert whole["scope"]["truncated"] is False

    # the insider listing carries the same contract, on an empty store as much as a full one
    assert c.get("/api/insider/trades?limit=50").json()["scope"]["truncated"] is False


def test_a_13f_aggregate_never_sums_two_reporting_periods(tmp_db):
    """13F is a quarter-end snapshot, so two quarters added together describe no moment that existed.

    Measured on the real database: with two periods imported, `/api/institution/summary` with no
    period returned NVIDIA at 31.84B shares — 15.53B and 16.30B added — while deduplicating the
    holder count, so it read as "4,844 holders between them hold 31.84B shares". The web page and
    the MCP tool both default to the newest period and never saw it; only a direct REST caller did,
    and on an open-source tool that caller is the point.
    """
    from fastapi.testclient import TestClient
    from modules import institution_store
    import app

    def hold(period, shares, value):
        return dict(accession=f"a-{period}", holding_key=f"k-{period}", manager="M",
                    manager_cik="1", period=period, filing_date=period, is_amendment=0,
                    cusip="67066G104", issuer="NVIDIA", title_of_class="COM", kind="share",
                    value=value, shares=shares, shares_type="SH", discretion="SOLE",
                    voting_sole=shares, voting_shared=0.0, voting_none=0.0, source_url="")

    institution_store.save_holdings([hold("2260-03-31", 100.0, 1000.0),
                                     hold("2259-12-31", 200.0, 2000.0)])

    c = TestClient(app.app)
    bare = c.get("/api/institution/summary").json()
    newest = c.get("/api/institution/summary?period=2260-03-31").json()
    assert bare["scope"]["period"] == "2260-03-31", "no period given must mean the newest, not all of them"
    assert bare["counts"]["val"] == newest["counts"]["val"] == 1000.0
    assert bare["by_issuer"][0]["shares"] == 100.0, "two quarters must never be added together"


def test_a_malformed_session_falls_back_rather_than_taking_the_page_down(monkeypatch):
    """Dating the chain by its session introduced a failure path that dating it by the clock
    never had: `session` is sliced out of Cboe's `last_trade_time` on a shape check alone, so a
    string that looks like a date but is not one reaches `strptime` — and it is evaluated inside
    every per-contract loop, well outside the guards. Rule six, and the same shape as `lag_days`.
    """
    from sources import cboe
    from sources.cboe import Chain
    monkeypatch.setattr(cboe, "et_today", lambda: "2260-01-06")
    for bad in ("2260-99-99", "not-a-date", "", None):
        ch = Chain(ticker="X", spot=1.0, timestamp=None, contracts=(), session=bad)
        assert ch.asof == "2260-01-06", f"{bad!r} should have fallen back to the wall clock"
    # a well-formed session still wins
    assert Chain(ticker="X", spot=1.0, timestamp=None, contracts=(),
                 session="2260-01-05").asof == "2260-01-05"


def test_every_genuinely_absent_exception_is_caught_as_absent(monkeypatch):
    """There is one `DataNotAvailable` per source package and all of them subclass `RuntimeError`,
    so one left out of `exec_tool` does not go uncaught — it falls through to the network branch
    and is reported as a fetch failure. A caller told that retries; a model told that says the
    fetch broke, when the Treasury simply never published that year. Rule one, running backwards.

    This asserts against the classes the packages actually export rather than a list written out
    here, so adding a source that defines its own cannot slip through unnoticed.
    """
    import tools
    from sources import cboe, congress, edgar

    for exc in (cboe.DataNotAvailable, edgar.DataNotAvailable, congress.DataNotAvailable):
        monkeypatch.setitem(tools._IMPL, "_probe",
                            lambda **_: (_ for _ in ()).throw(exc("not published for that year")))
        out = tools.exec_tool("_probe", {})
        assert out["error"].startswith("No data:"), f"{exc.__module__}.{exc.__name__} → {out['error']}"

    # and a real failure still has to read as one
    monkeypatch.setitem(tools._IMPL, "_probe",
                        lambda **_: (_ for _ in ()).throw(RuntimeError("connection reset")))
    assert tools.exec_tool("_probe", {})["error"].startswith("Fetch failed:")


def test_the_app_imports_and_serves_every_declared_route():
    """Cheap, and it is the first thing a user hits. Worth pinning explicitly: most of the
    TestClient coverage sits inside individual section tests, so a broken import in `app.py`
    can leave the suite green while nothing serves at all.
    """
    import app
    paths = {r.path for r in app.app.routes if getattr(r, "path", "").startswith("/api")}
    assert "/api/health" in paths
    for p in ("/api/gex/{ticker}", "/api/flow/{ticker}", "/api/scanner", "/api/shorts/ftd",
              "/api/darkpool/{ticker}", "/api/stock/{ticker}", "/api/congress/trades",
              "/api/insider/trades", "/api/institution/holdings", "/api/market/curve"):
        assert p in paths, p


def test_every_caller_of_the_greeks_passes_a_reference_date():
    """Adding the `asof` parameter fixed the dating and broke two callers the suite never ran:
    the GEX curve endpoint and the `get_gex_curve` tool both still called `total_gex_at` with
    two arguments, so both raised TypeError on every request while 62 tests stayed green.

    The lesson is in what got grepped. The change was motivated by `.dte`, so `.dte` is what was
    swept — but what actually broke was **the signature**, and its callers sit in files that have
    nothing to do with dates. So this checks every call against the real signature, by parsing
    the modules rather than by matching text: a first attempt split on the closing bracket and
    cut `total_gex_at(cs, lo + (hi - lo)` in half, reporting the fixed code as broken.
    """
    import ast
    import inspect
    from modules import greeks

    checked = 0
    for mod_name in ("app", "tools"):
        mod = __import__(mod_name)
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in ("total_gex_at", "_find_gamma_flip"):
                continue
            checked += 1
            sig = inspect.signature(getattr(greeks, name))
            required = [n for n, p in sig.parameters.items()
                        if p.default is inspect.Parameter.empty]
            given = len(node.args) + {k.arg for k in node.keywords}.__len__()
            assert given >= len(required), (
                f"{mod_name}.py line {node.lineno}: {name}() takes {required}, "
                f"but is called with {given} arguments")
    assert checked >= 2, "the callers moved — this test is no longer watching anything"


def test_a_filing_dated_before_its_own_trade_does_not_lead_the_listing(tmp_db):
    """A few congressional filings carry a trade date years in the future — an error in the
    original, not in the parsing. Ordered by trade date alone they sort to the very top, so the
    first row anyone reads is the one row that is certainly wrong.

    They stay in the listing and stay flagged; the project does not drop rows it cannot explain.
    They simply do not get to lead.
    """
    from datetime import date
    from modules import congress_store

    def t(tx, filed):
        return dict(member="M", state_district="IN02", ticker="NVDA", asset_name="NVIDIA",
                    asset_type="ST", asset_type_label="Stock", tx_type="P",
                    tx_type_label="Purchase", tx_date=tx, notification_date=None,
                    filing_date=filed, amount_low=1.0, amount_high=2.0, amount_raw="",
                    owner="self",
                    delay_days=(date.fromisoformat(filed) - date.fromisoformat(tx)).days,
                    source_url="")

    congress_store.save_filing(_filing(), [
        t("2260-01-10", "2260-01-20"),
        t("2299-12-26", "2260-02-09"),     # trade date after the filing date
        t("2260-03-01", "2260-03-10"),
    ])
    rows = congress_store.query_trades(limit=None)
    assert len(rows) == 3, "the bad row must still be listed, not filtered away"
    assert rows[0]["tx_date"] == "2260-03-01", "the newest sound row leads"
    assert rows[-1]["tx_date"] == "2299-12-26", "the impossible one goes last"
