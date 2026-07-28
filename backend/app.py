"""The FloorZero backend API.

⚠️ Compliance: this service **should only ever run on the user's own machine** (localhost).
FloorZero distributes code, not data — a user running it themselves is personal use.
⛔ It must never be deployed as a site serving options data to the public internet (= an OPRA redistributor, $1,500/month).
Binding to 127.0.0.1 by default is exactly why.

Start it with:
    cd backend && python -m uvicorn app:app --host 127.0.0.1 --port 8920
"""
from __future__ import annotations

# ⚠️ Immediately after `__future__` — which has to be the first statement in the file —
#    while the version gate has to run before the rest of the imports, so that too old a version
#    gets a sentence in plain words rather than dropping the user into a SyntaxError deep inside some module to puzzle over.
import pyversion  # noqa: F401

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from sources import cboe
from modules import greeks, history
from modules import congress_store, congress_sync
from modules import congress as congress_parse
from modules import insider_store, insider_sync
from modules import insider as insider_parse
from modules import institution_store, institution_sync
from modules import institution as institution_parse
from modules import shorts_store
from modules import shorts as shorts_parse
from sources import shorts as shorts_src
from sources import macro as macro_src
from modules import market as market_parse
from modules import market_store
from modules import flow as flow_parse
from modules import flow_store
from modules import scanner as scanner_parse
from modules import scanner_store, scanner_sync
from sources import darkpool as darkpool_src
from modules import darkpool as darkpool_parse
from modules import stock as stock_parse

app = FastAPI(
    title="FloorZero API",
    description="An open-source, self-hosted alternative to Unusual Whales — your data stays on your own machine",
    version="0.1.0",
)

# The frontend dev server; in production it is same-origin and needs no allowance
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5895", "http://127.0.0.1:5895"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "floorzero", "version": app.version}


@app.get("/api/gex/{ticker}")
def get_gex(
    ticker: str,
    expiry: Optional[str] = Query(None, description="A specific expiry, YYYY-MM-DD, or '0DTE'"),
    dte_max: Optional[int] = Query(None, ge=0, le=365, description="Only expiries within N days"),
    strike_pct: float = Query(0.05, gt=0, le=0.5, description="Strike range, ± this fraction"),
) -> dict:
    """One symbol's GEX profile.

    Neither expiry nor dte_max = the whole chain (far-dated contracts dilute the signal, so dte_max is usually more useful).
    """
    try:
        chain = cboe.cached_option_chain(ticker)
        profile = greeks.compute(chain, expiry=expiry, dte_max=dte_max,
                                 strike_pct=strike_pct)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:                              # a parameter problem (including cross-market symbols)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:                            # network / rate limit — must propagate, never swallowed
        raise HTTPException(status_code=502, detail=str(e)) from e

    out = greeks.to_dict(profile)
    out["timestamp"] = chain.timestamp
    out["expiries"] = chain.expiries()[:20]
    return out


@app.get("/api/gex/{ticker}/curve")
def get_gex_curve(
    ticker: str,
    expiry: Optional[str] = Query(None,
                                  description="A specific expiry, YYYY-MM-DD, or '0DTE' — "
                                              "must match the main /api/gex endpoint"),
    dte_max: Optional[int] = Query(None, ge=0, le=365,
                                   description="Omitted = the whole chain, consistent with the main /api/gex endpoint"),
    span_pct: float = Query(0.06, gt=0, le=0.3, description="Price range to sweep, ± this fraction"),
    strike_pct: float = Query(0.05, gt=0, le=0.5,
                              description="Strike range, ± this fraction — must match the main /api/gex endpoint, "
                                          "or the curve's zero and the returned gamma_flip come from different contract sets"),
    points: int = Query(40, ge=10, le=200),
) -> dict:
    """The GEX curve against share price — for seeing where the gamma flip is.

    Every point recomputes gamma with Black-Scholes (the gamma at the current spot cannot be reused).
    """
    try:
        chain = cboe.cached_option_chain(ticker)
        cs = chain.filter(expiry=expiry, dte_max=dte_max)
        # ⚠️ Use the caller's strike_pct; never hardcode ±15%:
        # the main endpoint computes the flip from strike_pct, and a different range here puts the curve's zero somewhere else.
        lo_k, hi_k = chain.spot * (1 - strike_pct), chain.spot * (1 + strike_pct)
        cs = [c for c in cs if lo_k <= c.strike <= hi_k]
        if not cs:
            raise ValueError(f"{ticker} has no contracts matching those conditions")
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    lo, hi = chain.spot * (1 - span_pct), chain.spot * (1 + span_pct)
    curve = []
    for i in range(points + 1):
        px = lo + (hi - lo) * i / points
        curve.append({"price": round(px, 2),
                      "gex_bn": round(greeks.total_gex_at(cs, px) / 1e9, 4)})
    return {
        "ticker": chain.ticker,
        "spot": round(chain.spot, 2),
        "curve": curve,
        "note": "Every point recomputes gamma with Black-Scholes; where the curve crosses 0 is the gamma flip",
    }

@app.get("/api/exposures/{ticker}")
def get_exposures(
    ticker: str,
    expiry: Optional[str] = Query(None),
    dte_max: Optional[int] = Query(None, ge=0, le=365),
    strike_pct: float = Query(0.05, gt=0, le=0.5),
) -> dict:
    """Vanna / charm exposure (Cboe gives no second-order greeks, so Black-Scholes computes them here)."""
    try:
        chain = cboe.cached_option_chain(ticker)
        prof = greeks.compute_exposures(chain, expiry=expiry, dte_max=dte_max,
                                        strike_pct=strike_pct)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return greeks.exposures_to_dict(prof)


@app.get("/api/gex/{ticker}/surface")
def get_gex_surface(
    ticker: str,
    dte_max: Optional[int] = Query(45, ge=0, le=365),
    strike_pct: float = Query(0.06, gt=0, le=0.5),
) -> dict:
    """A two-dimensional GEX surface over expiry × strike (ready for an ECharts heatmap)."""
    try:
        chain = cboe.cached_option_chain(ticker)
        return greeks.gex_surface(chain, dte_max=dte_max, strike_pct=strike_pct)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/history/snapshot/{ticker}")
def take_snapshot(
    ticker: str,
    expiry: Optional[str] = Query(None,
                                  description="A specific expiry, YYYY-MM-DD, or '0DTE' — "
                                              "must match the main /api/gex endpoint"),
    dte_max: Optional[int] = Query(None, ge=0, le=365),
    strike_pct: float = Query(0.05, gt=0, le=0.5),
) -> dict:
    """Capture one snapshot into local history (accrual starts the day it is installed).

    ⚠️ The filter parameters must match the main `/api/gex` endpoint **exactly**: a snapshot's scope is part of
    the storage key, so mismatched parameters file "the basis shown on the page" and "the basis stored in history"
    apart, and the series plotted later is not the thing that was being looked at.
    """
    try:
        chain = cboe.cached_option_chain(ticker)
        prof = greeks.to_dict(greeks.compute(
            chain, expiry=expiry, dte_max=dte_max, strike_pct=strike_pct))
        exp = greeks.exposures_to_dict(greeks.compute_exposures(
            chain, expiry=expiry, dte_max=dte_max, strike_pct=strike_pct))
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    # Use **the data's own time** as the instant, not the wall clock: Cboe's delayed quotes update only every so often,
    # and storing by now() records "one piece of data" as several observations (see history.record_gex).
    created = history.record_gex(prof, exp, captured_at=chain.timestamp)
    return {"ok": True, "created": created, "ticker": prof["ticker"],
            "scope": prof["meta"]["scope"],
            "scope_key": prof["meta"]["scope_key"], "captured_at": chain.timestamp,
            "data_stale": chain.timestamp is None}


@app.get("/api/history/{ticker}")
def get_history(ticker: str,
                scope: Optional[str] = Query(
                    None, description="The storage key (= meta.scope_key from /api/gex). "
                                      "Required when the symbol has history under more than one basis"),
                limit: int = Query(200, ge=1, le=2000)) -> dict:
    """The local historical series.

    ⚠️ **GEX under different bases is not the same order of magnitude** (≤7DTE against the whole chain differ tenfold),
    and stirred into one array it is a meaningless sawtooth. So "omit scope and get everything" is not accepted here:
    - only one basis in the database → unambiguous, return it;
    - several bases and none specified → **400, listing the choices**, rather than a blended series that will be misread.
    """
    scopes = history.scopes_for(ticker)
    if scope is None and len(scopes) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"{ticker.upper()} has history under several bases, so scope is required (bases are not comparable): "
                   + " | ".join(scopes))
    if scope is None and len(scopes) == 1:
        scope = scopes[0]
    return {"ticker": ticker.upper(),
            "scope": scope,
            "scopes": scopes,
            "series": history.gex_series(ticker, scope, limit)}


@app.get("/api/history")
def history_stats() -> dict:
    """Storage overview: so the user can see how much they have accrued."""
    return history.stats()


# ═══════════════════════ Congressional trades (tier S source) ═══════════════════════
# ⚠️ Compliance: both chambers' disclosures are US government public record (freely obtainable),
#    but **5 U.S.C. §13107(c)(1)(B) prohibits any commercial purpose by statute**
#    (news media disseminating to the public excepted), with a maximum fine of $10,000, in both chambers.
#    → Free, open-source and self-hosted for personal research ✅; no paid product may include this lane ❌.
#    A **different tier** from SEC EDGAR (which does not restrict commercial use); see sources/congress.py.
#    Unlike Cboe options data it is not bound by OPRA — so prefer it for anything shown externally.

@app.get("/api/congress/trades")
def congress_trades(
    chamber: Optional[str] = Query(None, pattern="^(house|senate)$"),
    ticker: Optional[str] = Query(None),
    member: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="Earliest trade date, YYYY-MM-DD"),
    tx_type: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    limit: int = Query(300, ge=1, le=2000),
) -> dict:
    """Cached congressional trade detail (most recent trade date first).

    ⚠️ This reads the **local cache** and does not fetch live — the House files 313 PDFs in a year,
    and fetching them takes 100+ seconds. Call `/api/congress/sync` first to fill it.
    """
    rows = congress_store.query_trades(chamber=chamber, ticker=ticker, member=member,
                                       since=since, tx_type=tx_type, limit=limit)
    # ⚠️ Must go through **the same derivation path** as /summary (_row_to_trade → to_dict).
    # Returning raw DB rows loses derived fields such as date_anomaly —
    # so the summary card says "2 dates look wrong" while the detail table cheerfully shows -320 days.
    # "Two views of one dataset must share one derivation" — the third time this project has hit it.
    out = [congress_parse.to_dict(_row_to_trade(r)) for r in rows]
    st = congress_store.stats()
    return {
        "trades": out, "count": len(out), "stats": st,
        "disclaimer": {
            "amount": "Amounts are **ranges**, not exact figures — the STOCK Act only requires banded "
                      "disclosure (such as $1,001-$15,000). Any total is an estimate from range midpoints.",
            "delay": "Delay in days = filing date − trade date, a statement of fact and **not a finding of violation**: "
                     "the statutory deadline is 30 days after becoming aware and no later than 45 days after the trade, rolling over weekends and holidays.",
            "coverage": st["note"],
        },
    }


@app.get("/api/congress/summary")
def congress_summary(
    since: Optional[str] = Query(None, description="Earliest trade date, YYYY-MM-DD"),
    chamber: Optional[str] = Query(None, pattern="^(house|senate)$"),
    ticker: Optional[str] = Query(None),
    member: Optional[str] = Query(None),
    tx_type: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    limit: int = Query(2000, ge=1, le=20000),
) -> dict:
    """Aggregate by ticker and by member.

    ⚠️ The filter parameters must match `/api/congress/trades` **exactly**:
    miss one ticker or tx_type and you get "the detail table shows only NVDA while the cards and charts above are still market-wide" —
    two views on one screen contradicting each other. This class of drift between two views of one dataset has happened four times here.
    """
    rows = congress_store.query_trades(chamber=chamber, since=since, ticker=ticker,
                                       member=member, tx_type=tx_type, limit=limit)
    trades = [_row_to_trade(r) for r in rows]
    out = congress_parse.summarize(trades)
    out["stats"] = congress_store.stats()
    out["scope"] = {"chamber": chamber or "both chambers", "since": since, "ticker": ticker,
                    "member": member, "tx_type": tx_type, "sampled": len(rows),
                    "limit": limit,
                    # Say so when the limit was hit; do not let the user think they are seeing everything
                    "truncated": len(rows) >= limit}
    return out


def _row_to_trade(r: dict) -> congress_parse.Trade:
    """DB row → Trade (the aggregate functions reuse this same logic, so the two cannot drift)."""
    from datetime import date as _d

    def d(v):
        return _d.fromisoformat(v) if v else None

    return congress_parse.Trade(
        chamber=r["chamber"], member=r["member"],
        state_district=r["state_district"] or "", ticker=r["ticker"],
        asset_name=r["asset_name"] or "", asset_type=r["asset_type"],
        # ⚠️ Both labels are **derived on read** from the stored codes, never taken from the
        #    stored label. They were written into the database at sync time, so rows synced by
        #    an earlier build keep that build's wording for good — and re-deriving them is the
        #    only way to fix an existing database short of refetching every filing, which is
        #    hours of PDFs. Falls back to the stored value where there is no code to derive
        #    from (the Senate path gives free-text asset types with asset_type=None).
        asset_type_label=congress_parse.ASSET_TYPES.get(
            r["asset_type"] or "", r["asset_type_label"] or ""),
        tx_type=r["tx_type"] or "",
        tx_type_label=congress_parse.TX_TYPES.get(
            r["tx_type"] or "", r["tx_type_label"] or ""),
        tx_date=d(r["tx_date"]),
        notification_date=d(r["notification_date"]), filing_date=d(r["filing_date"]),
        amount_low=r["amount_low"], amount_high=r["amount_high"],
        amount_raw=r["amount_raw"] or "", owner=r["owner"] or "self",
        doc_id=r["doc_id"], source_url=r["source_url"] or "")


@app.get("/api/congress/unparsed")
def congress_unparsed(limit: int = Query(100, ge=1, le=500)) -> dict:
    """The list of filings that could not be read (mostly paper scans).

    ⭐ It gets its own endpoint to **make unreadability visible**:
    10% of House PTRs are whole scanned images, and hiding that leaves the user believing they see everything.
    """
    rows = congress_store.unparsed_filings(limit)
    return {"filings": rows, "count": len(rows),
            "note": "These filings do exist; their detail simply cannot be parsed automatically. Follow source_url to read the original."}


@app.post("/api/congress/sync")
def congress_sync_start(
    year: Optional[int] = Query(None, ge=2012, le=2100, description="Starting year; defaults to the current one"),
    years_back: int = Query(0, ge=0, le=10,
                            description="How many further years back to fill (0 = this year only). The House archive is "
                                        "split by year, and each extra year is several hundred more PDFs"),
    limit: Optional[int] = Query(None, ge=1, le=5000,
                                 description="Maximum filings this run (a quota shared by both chambers)"),
    chamber: Optional[str] = Query(None, pattern="^(house|senate)$"),
) -> dict:
    """Start an incremental sync (a background thread; poll GET /api/congress/sync for progress)."""
    ch = (chamber,) if chamber else ("house", "senate")
    return congress_sync.start(year=year, years_back=years_back, limit=limit, chambers=ch)


@app.get("/api/congress/sync")
def congress_sync_status() -> dict:
    """Sync progress."""
    return {**congress_sync.STATE.snapshot(), "stats": congress_store.stats()}


# ═══════════════════════ Insider trades, Form 4 (tier S source) ═══════════════════════
# ⭐ SEC EDGAR: the official terms limit the rate (10 requests/second) and require a declared UA,
#    stating plainly "Anyone can access and download this information for free" —
#    **no restriction on commercial use**. A different tier from the congressional lane (which forbids it); do not conflate them.
# ⚠️ This section is about **classification**: active open-market trading is only about a quarter of Form 4,
#    the rest being grants, exercises, tax withholding and the like. The default shows open market only, or the signal drowns.

def _ins_row_to_trade(r: dict) -> insider_parse.InsiderTrade:
    """DB row → InsiderTrade. **Detail and summary share this one derivation path**,
    so the two views cannot disagree on fields or definitions (this project has come unstuck on that five times)."""
    from datetime import date as _d

    def d(v):
        return _d.fromisoformat(v) if v else None

    return insider_parse.InsiderTrade(
        accession=r["accession"], seq=r["seq"], ticker=r["ticker"],
        company=r["company"] or "", issuer_cik=r["issuer_cik"] or "",
        owner=r["owner"] or "", owner_cik=r["owner_cik"] or "",
        is_officer=bool(r["is_officer"]), is_director=bool(r["is_director"]),
        is_ten_pct=bool(r["is_ten_pct"]), officer_title=r["officer_title"] or "",
        security=r["security"] or "", tx_code=r["tx_code"] or "",
        tx_date=d(r["tx_date"]), filing_date=d(r["filing_date"]),
        shares=r["shares"], price=r["price"],
        acquired_disposed=r["acquired_disposed"] or "",
        shares_after=r["shares_after"], is_direct=bool(r["is_direct"]),
        is_10b5_1=None if r["is_10b5_1"] is None else bool(r["is_10b5_1"]),
        form_type=r["form_type"] or "4",
        source_url=r["source_url"] or "")


@app.get("/api/insider/trades")
def insider_trades(
    ticker: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    group: Optional[str] = Query("open_market",
                                 pattern="^(open_market|compensation|other|all)$",
                                 description="Defaults to open-market trades only (P/S); "
                                             "all = no filter (compensation is seven tenths of it and drowns the signal)"),
    direction: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    since: Optional[str] = Query(None, description="Earliest trade date, YYYY-MM-DD"),
    min_value: Optional[float] = Query(None, ge=0, description="Minimum trade value (dollars)"),
    role: Optional[str] = Query(None, pattern="^(officer|director|ten_pct)$"),
    plan: Optional[str] = Query(None, pattern="^(yes|no|unknown)$",
                                description="10b5-1 plan: yes / no = explicitly not / "
                                            "unknown = the filing did not mark it (no such field before 2023)"),
    include_amendments: bool = Query(False,
                                     description="Whether to include 4/A amendments. Default no — "
                                                 "an amendment usually restates the original's transactions, "
                                                 "so counting both double-counts"),
    limit: int = Query(300, ge=1, le=2000),
) -> dict:
    """Insider trade detail (from the local cache, most recent trade date first)."""
    rows = insider_store.query(
        ticker=ticker, owner=owner, group=None if group == "all" else group,
        direction=direction, since=since, min_value=min_value, role=role,
        plan=plan, include_amendments=include_amendments, limit=limit)
    out = [insider_parse.to_dict(_ins_row_to_trade(r)) for r in rows]
    return {
        "trades": out, "count": len(out), "stats": insider_store.stats(),
        "disclaimer": {
            "forms": "This page holds Form 4 only (plus 4/A amendments if asked for). The same SEC dataset also "
                     "carries Form 3 (an initial statement of holdings, not a transaction) and Form 5 (the annual "
                     "catch-up filing, median delay 274 days with three in ten over a year) — both excluded, "
                     "or they would inflate insider filing delay across the board.",
            "classification": "**Active open-market trading is only about a quarter of Form 4**, "
                              "the rest being grants, exercises, tax withholding and the like. "
                              "Counting bluntly by the SEC's acquired/disposed flag overstates insider buying several times over.",
            "plan": "A 10b5-1 is a pre-arranged trading plan, and a sale under one was often scheduled months earlier.",
            "disclaimer": "This presents facts already publicly filed and is not investment advice.",
        },
    }


@app.get("/api/insider/summary")
def insider_summary(
    ticker: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    group: Optional[str] = Query("open_market",
                                 pattern="^(open_market|compensation|other|all)$"),
    direction: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    since: Optional[str] = Query(None),
    min_value: Optional[float] = Query(None, ge=0),
    role: Optional[str] = Query(None, pattern="^(officer|director|ten_pct)$"),
    plan: Optional[str] = Query(None, pattern="^(yes|no|unknown)$"),
    include_amendments: bool = Query(False),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """Aggregate by ticker and by insider, with a cluster-buying table.

    ⚠️ The aggregation runs **in SQL over every matching row**, not "take the newest N and compute" —
    the latter labels "statistics over the newest N rows" as "statistics over the whole period" whenever more than N match,
    and adding a "truncated" hint does not rescue the numbers themselves.

    ⚠️ The filter parameters **must match** `/api/insider/trades` exactly (they share store._where).
    """
    import statistics as _st

    filters = dict(ticker=ticker, owner=owner,
                   group=None if group == "all" else group,
                   direction=direction, since=since, min_value=min_value,
                   role=role, plan=plan, include_amendments=include_amendments)
    agg = insider_store.aggregate(top=top, **filters)
    lib = insider_store.stats()
    c = agg["counts"]
    delays = agg["delays"]
    n = c["n"] or 0
    om = c["om"] or 0
    return {
        "total_rows": n,
        "open_market": {
            "count": om, "buys": c["buys"] or 0, "sells": c["sells"] or 0,
            "buy_value": round(c["bv"] or 0), "sell_value": round(c["sv"] or 0),
        },
        "compensation_count": c["comp"] or 0,
        "other_count": c["other"] or 0,
        "open_market_pct": round(om / n * 100, 1) if n else 0.0,
        "by_ticker": agg["by_ticker"],
        "cluster_buys": agg["cluster_buys"],
        "by_owner": agg["by_owner"],
        "plan_sells": c["plan_sells"] or 0,
        # Dropped from the value totals ≠ hidden: the count is still reported, and the UI says "N flagged with a doubtful price"
        "implausible_price": agg["implausible"],
        "date_anomaly_count": agg["anomalies"],
        "delay": {
            "median_days": round(_st.median(delays), 1) if delays else None,
            "over_2d": sum(1 for d in delays if d > 2),
            "note": "Section 16(a) requires filing within two business days of the trade. Counted here in calendar "
                    "days without deducting weekends and holidays: a statement of fact, not a finding of violation.",
        },
        "notes": insider_parse.summary_notes(lib),
        "stats": lib,
        "scope": {**filters, "group": group, "aggregated_rows": n,
                  "truncated": False},
    }


@app.post("/api/insider/sync")
def insider_sync_start(
    quarters_back: int = Query(2, ge=0, le=12,
                               description="Fill the last few **published** quarters (cheap: about 3 seconds a quarter, "
                                           "100k transactions each)"),
    days: int = Query(5, ge=0, le=120,
                      description="How many **outstanding** business days to fetch one by one this run (costly, about 90 seconds a day). "
                                  "It walks back from today skipping those already done, so calling it repeatedly fills "
                                  "the gap between the quarterly dataset and today a stretch at a time (currently about 117 days)"),
) -> dict:
    """Start a sync (a background thread; poll GET /api/insider/sync for progress)."""
    return insider_sync.start(quarters_back=quarters_back, days=days)


@app.get("/api/insider/sync")
def insider_sync_status() -> dict:
    return {**insider_sync.STATE.snapshot(), "stats": insider_store.stats()}


@app.get("/api/insider/codes")
def insider_codes() -> dict:
    """The SEC's Form 4 transaction codes — laid out for the user, so the classification is not a black box."""
    return {
        "codes": [{"code": c, "label": l, "group": insider_parse.code_group(c)}
                  for c, l in insider_parse.TX_CODES.items()],
        "open_market": sorted(insider_parse.OPEN_MARKET),
        "compensation": sorted(insider_parse.COMPENSATION),
        "source": "https://www.sec.gov/files/form4.pdf §8 Transaction Codes",
    }


# ═══════════════════════ Institutional holdings, 13F (tier S source) ═══════════════════════
# ⚠️ "Institutional holdings" misleads as a phrase: 13F reports **long positions in 13(f) securities as of quarter-end** only,
#    excluding shorts (the SEC created Form SHO in 2023 precisely because 13F does not cover them), cash, bonds,
#    stocks listed only outside the US and private holdings. Puts are listed under their underlying and must be classified apart —
#    folded into "holdings" they count bearish exposure as bullish.

@app.get("/api/institution/holdings")
def institution_holdings(
    period: Optional[str] = Query(None, description="Reporting period, YYYY-MM-DD (quarter-end)"),
    cusip: Optional[str] = Query(None, description="Filter by CUSIP (not by ticker)"),
    manager: Optional[str] = Query(None, description="Manager name (fuzzy match)"),
    kind: str = Query("share", pattern="^(share|call|put|all)$",
                      description="Position kind. Defaults to share — a put is bearish, "
                                  "and folded into the holdings totals it counts bearish as bullish"),
    min_value: Optional[float] = Query(None, ge=0),
    include_amendments: bool = Query(False,
                                     description="Whether to include amendments. Default no — "
                                                 "a 13F amendment must restate the filing whole, so counting it "
                                                 "alongside the original double-counts"),
    limit: int = Query(200, ge=1, le=2000),
    ticker: Optional[str] = Query(None,
                                  description="⛔ Not supported — 13F keys on CUSIP and the SEC "
                                              "publishes no ticker→CUSIP mapping. Passing one is an "
                                              "error rather than a silent no-op; search by issuer name"),
) -> dict:
    """Holding detail (from the local cache, largest value first).

    ⚠️ `ticker` exists only so that passing it **fails loudly**. FastAPI ignores query
    parameters it does not declare, so `?ticker=NVDA` used to return HTTP 200 with the
    whole unfiltered table — the caller believes they filtered and they did not, which is
    the silent-wrong-answer this project exists to avoid. The stock page already tells the
    user a holding cannot be located from a symbol; the API has to say the same thing.
    """
    if ticker:
        raise HTTPException(
            status_code=400,
            detail=(f"13F cannot be filtered by ticker ({ticker!r}). It keys on CUSIP, and the SEC "
                    f"publishes no ticker→CUSIP mapping — matching issuer names was measured hitting "
                    f"only 42.8%. Use `cusip`, or search `manager`/issuer name instead. This is a "
                    f"mapping we cannot do, not an absence of holdings."))
    rows = institution_store.query(
        period=period, cusip=cusip, manager=manager, kind=kind,
        min_value=min_value, include_amendments=include_amendments, limit=limit)
    # ⚠️ kind_label has to be filled in: the UI's "type" column reads it. Without it the whole column is blank,
    # and **telling shares from calls from puts is the entire point of this section** (a put is bearish).
    for r in rows:
        r["kind_label"] = institution_parse.POSITION_KINDS.get(r["kind"], r["kind"])
    return {
        "holdings": rows, "count": len(rows),
        "stats": institution_store.stats(),
        "disclaimer": {
            "coverage": "13F holds **long positions in 13(f) securities as of quarter-end** only. "
                        "It excludes short positions, cash, bonds, commodities, stocks listed only outside the US, "
                        "private holdings, and holdings granted confidential treatment. "
                        "So \"this manager holds $X bn\" is neither their total assets nor their net exposure.",
            "options": "Puts and calls are listed under the **underlying security** (Form 13F Special Instruction 10); "
                       "this page counts share / call / put separately and shows share by default.",
            "lag": "13F's statutory deadline is 45 days after quarter-end, so what you see is a position **at least six weeks old**, "
                   "and the manager may have moved a long way since.",
            "cusip": "13F gives CUSIPs and no tickers, and the SEC publishes no CUSIP→ticker mapping, "
                     "so this page keys on issuer name plus CUSIP.",
        },
    }


@app.get("/api/institution/summary")
def institution_summary(
    period: Optional[str] = Query(None),
    cusip: Optional[str] = Query(None),
    manager: Optional[str] = Query(None),
    kind: str = Query("share", pattern="^(share|call|put|all)$"),
    min_value: Optional[float] = Query(None, ge=0),
    include_amendments: bool = Query(False),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """Aggregate by ticker and by manager (in SQL over everything, not over the first N rows)."""
    agg = institution_store.aggregate(
        top=top, period=period, cusip=cusip, manager=manager, kind=kind,
        min_value=min_value, include_amendments=include_amendments)
    st = institution_store.stats()
    return {**agg, "stats": st,
            "scope": {"period": period, "cusip": cusip, "manager": manager,
                      "kind": kind, "min_value": min_value,
                      "include_amendments": include_amendments}}


@app.get("/api/institution/changes")
def institution_changes(
    period: str = Query(..., description="This reporting period, YYYY-MM-DD"),
    prev_period: str = Query(..., description="The previous reporting period, YYYY-MM-DD"),
    kind: str = Query("share", pattern="^(share|call|put)$"),
    manager: Optional[str] = Query(None),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """Quarter on quarter: new positions / added / trimmed / exited.

    ⭐ This is where 13F's value actually is — a single quarter is a static snapshot; the change carries the information.
    ⚠️ The result carries `floor_note`: the value threshold contaminates the new-position and exit calls, and must be read alongside.
    """
    have = institution_store.known_periods()
    missing = [p for p in (period, prev_period) if p not in have]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"These reporting periods are not imported: {', '.join(missing)}. Available: {', '.join(have) or 'none'}")
    return institution_store.changes(period=period, prev_period=prev_period,
                                     kind=kind, manager=manager, top=top)


@app.post("/api/institution/sync")
def institution_sync_start(
    window: Optional[str] = Query(None,
                                  description="The dataset window, e.g. 01mar2026-31may2026. "
                                              "Omitted = the newest"),
    period: Optional[str] = Query(None,
                                  description="Reporting period, YYYY-MM-DD. Omitted = whichever period "
                                              "has the most filings in that window"),
    min_value: float = Query(institution_sync.DEFAULT_MIN_VALUE, ge=0,
                             description="Value threshold (dollars). Default $1m — "
                                         "measured, it keeps 37.5% of rows and covers 99.37% of value. "
                                         "Set 0 to keep everything (3.32m rows a quarter, about 580MB)"),
) -> dict:
    """Import one reporting period (a background thread; poll GET /api/institution/sync for progress)."""
    return institution_sync.start(window=window, period=period, min_value=min_value)


@app.get("/api/institution/sync")
def institution_sync_status() -> dict:
    return {**institution_sync.STATE.snapshot(),
            "stats": institution_store.stats()}


# ═══════════════════════ Short-sale data (tier S for FTD, optional tier B FINRA) ═══════════════════════
# ⚠️ This section's data is the easiest in the project to read backwards:
#    an FTD is **a cumulative balance at a point in time** (not that day's additions), and the SEC states plainly it is **not evidence of naked shorting**.
#    FINRA's off-exchange short volume ≠ short interest, and covers only the off-exchange part.
#    All three official quotations are in shorts.OFFICIAL_NOTES, and the UI must show them verbatim.

@app.get("/api/shorts/ftd")
def shorts_ftd(
    symbol: Optional[str] = Query(None, description="Ticker"),
    settlement_date: Optional[str] = Query(None, description="Settlement date, YYYY-MM-DD"),
    since: Optional[str] = Query(None, description="Earliest settlement date, YYYY-MM-DD"),
    min_quantity: Optional[float] = Query(None, ge=0, description="Minimum balance (shares)"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """Fail-to-deliver detail (SEC, tier S)."""
    rows = shorts_store.query(symbol=symbol, settlement_date=settlement_date,
                              since=since, min_quantity=min_quantity, limit=limit)
    return {"fails": rows, "count": len(rows), "stats": shorts_store.stats(),
            "notes": shorts_parse.OFFICIAL_NOTES}


@app.get("/api/shorts/summary")
def shorts_summary(
    symbol: Optional[str] = Query(None),
    settlement_date: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    min_quantity: Optional[float] = Query(None, ge=0),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """Aggregate by symbol and by settlement date (in SQL over everything).

    ⚠️ The per-symbol figure is **the mean of the balances across settlement dates**, never their sum —
    an FTD is a balance at a point in time, one undelivered trade reappears on consecutive days, and adding them means nothing.
    """
    agg = shorts_store.aggregate(top=top, symbol=symbol,
                                 settlement_date=settlement_date, since=since,
                                 min_quantity=min_quantity)
    return {**agg, "stats": shorts_store.stats(),
            "notes": shorts_parse.OFFICIAL_NOTES,
            "scope": {"symbol": symbol, "settlement_date": settlement_date,
                      "since": since, "min_quantity": min_quantity,
                      "aggregation": "avg_per_settlement_date"}}


@app.post("/api/shorts/sync")
def shorts_sync_start(
    back: int = Query(2, ge=1, le=12,
                      description="How many recent half-month files to import (two a month: first half, second half)"),
) -> dict:
    """Import SEC FTD data (a background thread; poll GET /api/shorts/sync for progress)."""
    return shorts_store.start(back=back)


@app.get("/api/shorts/sync")
def shorts_sync_status() -> dict:
    return {**shorts_store.STATE.snapshot(), "stats": shorts_store.stats()}


@app.get("/api/shorts/finra-status")
def shorts_finra_status() -> dict:
    """Whether the FINRA source is on, and **its terms verbatim**.

    ⭐ It gets its own endpoint because the compliance judgement on this lane has to be **the user's own**:
    we lay out FINRA's Terms of Use as written, ambiguities and all, and do not interpret them for them.
    """
    return {
        "enabled": shorts_src.finra_enabled(),
        "env_var": "FZ_ENABLE_FINRA",
        "terms": shorts_src.FINRA_TERMS,
    }


# ═══════════════════════ Macro (tier S: Treasury + CFTC) ═══════════════════════
# The cleanest lane in the project: government works, no restriction on commercial use, redistributable.
# ⚠️ Two definitions: inversion has to say whether it is 10Y-2Y or 10Y-3M; COT runs three days behind.

@app.get("/api/market/curve")
def market_curve(
    year: Optional[int] = Query(None, ge=1990, le=2100, description="Omitted = this year"),
    years: int = Query(1, ge=1, le=15, description="How many years back to take (including `year` itself)"),
    refresh: bool = Query(False, description="Force a refetch (Treasury does revise historical values)"),
) -> dict:
    """The Treasury yield curve plus both spreads as a time series.

    ⚠️ `years` defaults to 1, but **seeing an inversion takes several years** — within a one-year window
    the spreads often keep one sign throughout, and the crossing of zero never shows.

    The data is stored locally: past years are never refetched (their values cannot change), and the current year's freshness is judged on its newest day.
    """
    from datetime import date as _d
    y = year or _d.today().year
    wanted = list(range(y - years + 1, y + 1))

    status = market_store.year_status(wanted)
    todo = wanted if refresh else [yy for yy in wanted if status[yy]["stale"]]
    fetched, missing, failed = {}, [], {}
    for yy in todo:
        try:
            n = market_store.save_year(yy, macro_src.yield_curve(yy))
            fetched[yy] = n
            if not n:
                missing.append(yy)
        except macro_src.DataNotAvailable:
            # One year missing ≠ the whole request failing: gaps in the early years are normal
            missing.append(yy)
        except RuntimeError as e:
            # ⚠️ A failed fetch ≠ an absence of data. Older rows already in the database are still served,
            #    but **the failure has to be reported as it stands**, so the user does not take it for the latest.
            failed[yy] = str(e)

    pts = [p for p in (market_parse.parse_curve(r)
                       for r in market_store.load_years(wanted)) if p]
    if not pts:
        if failed:
            raise HTTPException(
                status_code=502,
                detail="; ".join(f"{k}: {v}" for k, v in failed.items()))
        raise HTTPException(status_code=404,
                            detail=f"No yield data for {wanted[0]}-{y}")
    return {"year": y, "years": wanted, "missing_years": missing,
            "fetched": fetched, "failed": failed,
            "cache": market_store.stats(),
            **market_parse.curve_series(pts)}


@app.get("/api/market/cot/markets")
def market_cot_markets(
    active_only: bool = Query(True, description="Only contracts still being updated"),
) -> dict:
    """The markets TFF covers (each with its own most recent report date).

    ⚠️ The default gives only those **still being updated**: the dataset holds a great many contracts that stopped
    reporting years ago, and selecting one shows a blank that means "this contract no longer reports", not "the fetch failed".
    """
    try:
        rows = macro_src.cot_markets()
    except macro_src.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    latest = max((r["last_date"] or "" for r in rows), default="")
    # "Still being updated" = its last report is the newest in the whole dataset (dates agree across contracts within a report)
    live = [r for r in rows if r["last_date"] == latest] if latest else []
    return {"markets": live if active_only else rows,
            "total": len(rows), "active": len(live),
            "latest_report": latest or None,
            "active_only": active_only, "notes": market_parse.NOTES}


@app.get("/api/market/cot")
def market_cot(
    market: Optional[str] = Query(None, description="Market name keyword, e.g. S&P 500 / TREASURY"),
    exact: bool = Query(False, description="Exact match on market name (required for a time series)"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """CFTC financial futures positioning (TFF).

    ⚠️ It runs **three days behind**: positions as of Tuesday's close, published on Friday.
    """
    try:
        raw = macro_src.cot_rows(limit=limit, market_contains=market, exact=exact)
    except macro_src.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    rows = [market_parse.cot_to_dict(c)
            for c in (market_parse.parse_cot(r) for r in raw) if c]
    return {"rows": rows, "count": len(rows), "notes": market_parse.NOTES,
            "markets": sorted({r["market"] for r in rows}),
            "scope": {"market": market, "exact": exact, "limit": limit,
                      "truncated": len(raw) >= limit}}


# ═══════════════════════ Options flow (tier C: Cboe delayed, local only) ═══════════════════════
# ⚠️ The data is a **chain snapshot**, not the print-by-print tape — sweeps, block-size tiering and buy/sell direction are all out of reach.
#    See modules/flow.py for the limits. This section produces no directional label of any kind.

def _session_of(chain) -> str:
    """The storage key is the data's own trading session, **not the wall-clock date**.

    ⚠️ Keyed by wall clock, opening the page twice over a weekend stores one Friday close
    as "two days of observation", and every OI difference is 0 — which looks like "positions did not change"
    when in fact there was no new data at all. Only when Cboe gives no session does this fall back to today in US/Eastern.
    """
    return chain.session or cboe.et_today()


@app.get("/api/flow/{ticker}")
def get_flow(
    ticker: str,
    expiry: Optional[str] = Query(None, description="A specific expiry, YYYY-MM-DD, or '0DTE'"),
    dte_max: Optional[int] = Query(None, ge=0, le=365),
    top: int = Query(40, ge=1, le=200),
    record: bool = Query(False, description="Write this snapshot into local OI history"),
) -> dict:
    """One symbol's options flow profile for the day (from the chain snapshot)."""
    try:
        chain = cboe.cached_option_chain(ticker)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    # ⚠️ Two sets: "those that traded" for display, "every contract in scope" for the open-interest basis.
    #    Merged into one, "the structure of settled positions" silently becomes
    #    "positions in the contracts touched today", which on a thin symbol is an order of magnitude out.
    scope_rows = flow_parse.parse(chain, dte_max=dte_max, expiry=expiry,
                                  traded_only=False)
    rows = [r for r in scope_rows if r.volume > 0]
    out = flow_parse.summarize(chain, rows, all_rows=scope_rows, top=top)
    out["expiries"] = chain.expiries()[:20]
    out["session"] = chain.session
    out["scope"] = {"expiry": expiry, "dte_max": dte_max, "top": top}
    if record:
        # ⚠️ What is accrued is **the whole chain, including contracts that did not trade today** (`traded_only=False`).
        #    ① Changing dte_max makes the stored set disagree, and the difference records "the filter changed" as "positions changed";
        #    ② worse, missing the contracts that did not trade today means one vanishes from the store tomorrow,
        #       and the difference records it as "the whole position closed out" when not one contract moved.
        allrows = flow_parse.parse(chain, traded_only=False)
        out["recorded"] = flow_store.record(
            chain.ticker, _session_of(chain), chain.spot,
            [flow_parse.to_dict(r) for r in allrows])
    out["history"] = flow_store.dates(chain.ticker)[:30]
    return out


@app.post("/api/flow/{ticker}/record")
def record_flow(ticker: str) -> dict:
    """Write the current chain snapshot into local OI history (this history cannot be backfilled; it only accrues)."""
    try:
        chain = cboe.cached_option_chain(ticker)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    rows = flow_parse.parse(chain, traded_only=False)
    sess = _session_of(chain)
    n = flow_store.record(chain.ticker, sess, chain.spot,
                          [flow_parse.to_dict(r) for r in rows])
    return {"ticker": chain.ticker, "snapshot_date": sess,
            "recorded": n, "history": flow_store.dates(chain.ticker)[:30],
            "stats": flow_store.stats()}


@app.get("/api/flow/{ticker}/oi-change")
def get_oi_change(
    ticker: str,
    date_to: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    top: int = Query(40, ge=1, le=200),
) -> dict:
    """The change in open interest between two snapshot days.

    ⚠️ With only one day accrued it returns `enough=false` — meaning **not enough accrued yet**, not "positions did not change".
    """
    return flow_store.oi_change(ticker.strip().upper(), date_to=date_to,
                                date_from=date_from, top=top)


# ═══════════════════════ Scanner (tier C: Cboe delayed, local only) ═══════════════════════
# ⚠️ There is no market-wide endpoint, only symbol by symbol → one full pass takes about 26 minutes, so it is a **background job** by nature.
#    IV Rank needs history by definition; without enough it returns null and **never forces a number out of a short sample**.

@app.get("/api/scanner")
def get_scanner(
    session: Optional[str] = Query(None, description="Which trading session to view; omitted = the newest"),
    sort: str = Query("iv_rank"),
    limit: int = Query(100, ge=1, le=1000),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    min_volume: Optional[float] = Query(None),
    min_iv: Optional[float] = Query(None),
    max_iv: Optional[float] = Query(None),
    min_iv_rank: Optional[float] = Query(None, ge=0, le=100),
    min_volume_x: Optional[float] = Query(None),
    security_type: Optional[str] = Query(None),
) -> dict:
    """Scan results (read from what has already been scanned locally; nothing is fetched live)."""
    sess = session or scanner_store.latest_session()
    st = scanner_store.stats()
    if not sess:
        return {"rows": [], "count": 0, "session": None, "stats": st,
                "batches": scanner_store.batches(5),
                "notes": scanner_parse.NOTES,
                "note": ("There are no scan results here yet — that means **nothing has been scanned**, "
                         "not that no symbol in the market matches. Run a scan first.")}
    quotes = scanner_store.quotes_at(sess)
    # ⚠️ `as_of=sess` cannot be omitted — viewing a past day's results with later quotes counted into
    #    the IV Rank sample is **lookahead bias** (judging that day's IV with data that had not happened).
    hist = scanner_store.history([q["symbol"] for q in quotes], as_of=sess)
    rows = [scanner_parse.build_row(
                q, hist.get(q["symbol"], {}).get("iv", []),
                hist.get(q["symbol"], {}).get("volume", []))
            for q in quotes]
    kept, excluded = scanner_parse.apply_filters(
        rows, min_price=min_price, max_price=max_price, min_volume=min_volume,
        min_iv=min_iv, max_iv=max_iv, min_iv_rank=min_iv_rank,
        min_volume_x=min_volume_x, security_type=security_type)
    kept = scanner_parse.sort_rows(kept, sort)
    return {
        "rows": [scanner_parse.to_dict(r) for r in kept[:limit]],
        "count": len(kept), "scanned": len(rows), "session": sess,
        "truncated": len(kept) > limit,
        # ⚠️ "Excluded because it could not be computed" has to be reported apart from "failed the condition"
        "excluded": excluded,
        "stats": st, "batches": scanner_store.batches(5),
        "sorts": sorted(scanner_parse.SORTS),
        "notes": scanner_parse.NOTES,
        "thresholds": {"iv_lookback": scanner_parse.IV_LOOKBACK,
                       "iv_min_sample": scanner_parse.IV_MIN_SAMPLE},
    }


@app.get("/api/scanner/scan")
def get_scan_state() -> dict:
    """Current scan progress."""
    return {**scanner_sync.STATE.snapshot(), "stats": scanner_store.stats()}


@app.post("/api/scanner/scan")
def start_scan(
    symbols: Optional[str] = Query(
        None, description="Comma-separated symbols; omitted = the whole market (about 26 minutes)"),
) -> dict:
    """Start a scan.

    ⚠️ The whole market takes about **26 minutes** (6,049 symbols at a self-imposed 4 requests/second).
    To see only the few dozen you follow, pass `symbols`; that is a matter of seconds.
    """
    syms = None
    # ⚠️ An empty `symbols` string is a **parameter error** and must not fall through to "omitted = the whole market" —
    #    a user who clears the box and presses "scan these" would accidentally start a 26-minute market-wide job.
    if symbols is not None and not symbols.strip():
        raise HTTPException(
            status_code=400,
            detail="symbols is empty. To scan the whole market, **omit** the parameter (about 26 minutes).")
    if symbols:
        # Deduplicate but keep the order: repeated symbols only hit upstream again for nothing and inflate the progress denominator
        seen: set = set()
        syms = []
        for raw in symbols.split(","):
            t = raw.strip().upper()
            if t and t not in seen:
                seen.add(t)
                syms.append(t)
        if not syms:
            raise HTTPException(status_code=400, detail="symbols parsed to nothing")
    return scanner_sync.start(syms)


@app.post("/api/scanner/scan/cancel")
def cancel_scan() -> dict:
    return scanner_sync.cancel()


@app.get("/api/scanner/universe")
def get_universe() -> dict:
    """Cboe's official universe of symbols with options."""
    try:
        roots = cboe.option_roots()
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"count": len(roots), "symbols": roots[:200],
            "note": scanner_parse.NOTES["universe"]}


# ═══════════════════════ Dark pools / off-exchange (tier B FINRA, **off by default**) ═══════════════════════
# ⚠️ A project rule: no section may use FINRA as its only source.
#    Here FINRA provides the **numerator** (ATS / non-ATS off-exchange volume),
#    and locally accrued Cboe quotes provide the **denominator** (total volume over the same period) — the share genuinely needs both.
#    With FINRA off, this section holds only the terms, and that is **deliberate**: no plausible-looking answer is assembled from other figures.

def _week_days(week: str) -> list[str]:
    """Week start → that week's five calendar weekdays (FINRA's week starts on Monday)."""
    from datetime import date, timedelta
    try:
        y, m, d = (int(x) for x in week.split("-"))
    except ValueError:
        return []
    start = date(y, m, d)
    return [(start + timedelta(days=i)).isoformat() for i in range(5)]


def _trading_days(days: list[str]) -> list[str]:
    """Which of those days the market was actually **open**.

    ⚠️ Monday-to-Friday cannot serve as a trading calendar: US markets close a dozen or so days a year,
    and taking a holiday for "missing locally" means the weeks containing one **never** get a share computed.
    We have no holiday table, but there is a harder test — **whether the locally accrued quotes hold that day**:
    if **any** symbol has a snapshot that day, the market was open.
    That infers the calendar from the data, needs no extra dependency, and cannot go stale with the years.

    The cost: with no symbol scanned locally that week, 0 trading days are detected → no share is computed.
    Which is the right answer (there genuinely is no denominator), not a misjudgement.
    """
    if not days:
        return []
    from modules import db
    q = ",".join("?" * len(days))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT session FROM quote_snapshot "
            f"WHERE session IN ({q})", tuple(days)).fetchall()
    have = {r["session"] for r in rows}
    return [d for d in days if d in have]


def _calendar_note(days: list[str], observed: list[str]) -> Optional[str]:
    """The explanation for a week that could not be observed in full locally.

    ⚠️ **Two levels of uncertainty, and the second must not be read as absence:**
    ① a local snapshot exists for that day → the market was certainly open;
    ② **no** local snapshot for that day → **it cannot be told** whether the market was shut or we simply never scanned.
    Without a trading calendar there is no way to separate them, so unless all five weekdays are accounted for, no share is given —
    including for the weeks that contain a holiday. Better to leave it unanswered than to answer wrongly.
    """
    if len(observed) >= len(days):
        return None
    miss = [d for d in days if d not in observed]
    return (f"Only {len(observed)}/{len(days)} weekdays of that week were observed locally"
            f" (missing {', '.join(miss)}). **With no trading calendar**, "
            f"there is no telling whether those days were holidays or simply never scanned — so no share is given. "
            f"(Weeks containing a US market holiday will always read this way; that is a deliberate trade-off.)")


@app.get("/api/darkpool/{ticker}")
def get_darkpool(
    ticker: str,
    week: Optional[str] = Query(None, description="Week start, YYYY-MM-DD; omitted = the newest"),
) -> dict:
    """One symbol's off-exchange volume (ATS and non-ATS kept **apart**)."""
    tk = ticker.strip().upper()
    try:
        raw = darkpool_src.weekly(tk)
    except darkpool_src.FinraDisabled as e:
        # ⚠️ This is a **configuration state**, not an absence of data — 409, not 404
        raise HTTPException(status_code=409, detail=str(e)) from e
    except darkpool_src.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    parsed = darkpool_parse.parse(raw)
    weeks = darkpool_parse.weeks_of(parsed)
    if not weeks:
        if parsed["unknown_types"]:
            # ⚠️ Not one row of a known type, yet unknown types present = **the parser is no longer compatible**,
            #    not "this symbol has no off-exchange volume". Let a 502 propagate; do not report it as a 404.
            raise HTTPException(
                status_code=502,
                detail=(f"FINRA returned record types this program does not recognise "
                        f"{parsed['unknown_types']} — the parsing rules may be out of date. "
                        f"This is a **parser incompatibility**, not \"{tk} has no off-exchange volume\"."))
        raise HTTPException(status_code=404, detail=f"No off-exchange records for {tk}")
    wk = week or weeks[-1]
    if wk not in weeks:
        raise HTTPException(
            status_code=404,
            detail=f"No such week as {wk} (available: {', '.join(weeks[-8:])})")

    # The denominator: the sum of Cboe daily volume accrued locally for that week.
    # ⚠️ Taken day by day, exactly, with **no interpolation and no extrapolation** — a missing day stays missing, and is listed for the user.
    #    A small denominator makes the share too high, and the direction of that bias has to be known.
    out = _consolidated(tk, wk, parsed)
    out["ticker"] = tk
    out["weeks"] = weeks[-52:]
    out["series"] = darkpool_parse.series(parsed)[-52:]
    out["unknown_types"] = parsed["unknown_types"]
    out["null_shares"] = parsed["null_shares"]
    out["truncated"] = parsed["truncated"]
    out["finra"] = {"enabled": True, "terms": darkpool_src.FINRA_TERMS}
    return out


def _sessions_for(symbol: str, days: list[str]) -> list[tuple]:
    """Each of those days' volume from local accrual (absent is absent; **nothing is interpolated or extrapolated**).

    The table's key is (symbol, session), so one day cannot have two rows and the denominator cannot double-count.
    """
    if not days:
        return []
    from modules import db
    q = ",".join("?" * len(days))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT session, volume FROM quote_snapshot "
            f"WHERE symbol = ? AND session IN ({q})", (symbol, *days)).fetchall()
    return [(r["session"], r["volume"]) for r in rows]


def _consolidated(tk: str, wk: str, parsed: dict) -> dict:
    """Compute that week's summary, denominator included. **REST and MCP share this one function**.

    ⚠️ It was extracted because the previous MCP version never read the local denominator — for one symbol in one week,
    the web page could give a share while the tool layer always returned null: two views of one dataset, disagreeing.
    """
    weekdays = _week_days(wk)
    observed = _trading_days(weekdays)          # the days locally confirmed open
    cal_note = _calendar_note(weekdays, observed)
    covered, total = [], 0.0
    for d, v in _sessions_for(tk, observed):
        if v is not None:
            covered.append(d)
            total += v
    missing = [d for d in observed if d not in covered]
    # ⚠️ A share is given only when all five days of the week were observed and this symbol has volume on all five.
    #    One day short and it is systematically too high, by an amount nothing on screen reveals.
    full = cal_note is None and not missing
    out = darkpool_parse.week_summary(
        parsed, wk,
        consolidated_shares=total if full else None,
        covered_days=covered,
        missing_days=missing if not cal_note else weekdays)
    out["weekdays"] = weekdays
    out["locally_observed_days"] = observed
    out["calendar_note"] = cal_note
    if cal_note:
        out["share_note"] = cal_note
    return out


@app.get("/api/darkpool-status")
def darkpool_status() -> dict:
    """Whether this section is currently on or off, and why."""
    return {
        "enabled": darkpool_src.finra_enabled(),
        "env_var": "FZ_ENABLE_FINRA",
        "terms": darkpool_src.FINRA_TERMS,
        "notes": darkpool_parse.NOTES,
        "why_gated": (
            "This section's core data (ATS and non-ATS off-exchange volume) is **published by FINRA alone**, "
            "with no second public source. And FINRA's terms limit it to non-commercial personal or professional "
            "use and explicitly forbid using the site's data to build a database — which is exactly what this project does, downloading into SQLite. "
            "So it is off by default, its terms are reproduced here as written, and **the judgement is yours**. "
            "Switched on, FINRA supplies the numerator only; the denominator (total volume over the same period) comes from locally accrued Cboe quotes."),
    }


# ═══════════════════════ The stock page (nine lanes converging) ═══════════════════════
# ⚠️ The hard part is not aggregation but that **the timelines do not line up**: the option chain is yesterday's close, the 13F a quarter-end three months back.
#    Every block carries its own instant and lag, and ⛔ no cross-source score is produced.

@app.get("/api/stock/{ticker}")
def get_stock(ticker: str) -> dict:
    """One symbol's full profile across the nine lanes.

    ⚠️ **Each lane guards itself, and guards against *any* exception**: the whole point of this page is
    that one lane failing does not take the page down with it. Catching `RuntimeError` alone is not enough —
    a change in an upstream structure raises `KeyError` / `IndexError` / `AttributeError`,
    and those should equally turn **that one block** into "could not fetch" rather than 500 the whole page.
    """
    tk = ticker.strip().upper()
    L = stock_parse.lane
    lanes: list = []

    # ⚠️ An invalid symbol has to be caught **at the entrance**. Otherwise the three Cboe lanes report bad_symbol
    #    while the rest each go and look and each report no_data — one invalid symbol
    #    receiving mutually contradictory explanations on one page.
    try:
        tk = cboe.assert_us_ticker(tk)
    except ValueError as e:
        return {**stock_parse.assemble(
            [L(k, reason="bad_symbol", detail=str(e)) for k in stock_parse.LANES]),
            "ticker": tk}

    def run(key: str, fn, *, as_of_on_error: Optional[str] = None) -> None:
        """Run one lane; **any** exception affects only this lane.

        `as_of_on_error`: when the instant of this block's data is **already known** at failure time (the chain
        was fetched and only the computation afterwards blew up), carry it along — discarding what is known
        sinks the block to the end of the timeline and makes it look as though there was never any data.
        """
        try:
            lanes.append(fn())
        except cboe.DataNotAvailable as e:
            lanes.append(L(key, as_of=as_of_on_error, reason="no_data", detail=str(e)))
        except ValueError as e:
            # ⚠️ Symbol validity **was already checked at the entrance**, so a ValueError reaching here
            #    is dirty data downstream, not "invalid symbol" — labelling it bad_symbol sends the user
            #    off to fix a symbol that was fine.
            lanes.append(L(key, as_of=as_of_on_error, reason="fetch_failed",
                           detail=f"ValueError: {e}"))
        except Exception as e:                       # noqa: BLE001 — deliberately catching everything
            lanes.append(L(key, as_of=as_of_on_error, reason="fetch_failed",
                           detail=f"{type(e).__name__}: {e}"))

    # ── 1. Quote / GEX / options flow (one snapshot, fetched once) ──
    chain = None
    try:
        chain = cboe.cached_option_chain(tk)
    except cboe.DataNotAvailable as e:
        for k in ("quote", "gex", "flow"):
            lanes.append(L(k, reason="no_data", detail=str(e)))
    except Exception as e:                           # noqa: BLE001
        for k in ("quote", "gex", "flow"):
            lanes.append(L(k, reason="fetch_failed", detail=f"{type(e).__name__}: {e}"))

    if chain is not None:
        # ⚠️ Even reading `chain.session` needs a guard: a drift in the snapshot structure raises AttributeError here,
        #    and it sits **outside** run(), so it would 500 the whole page outright.
        try:
            sess = chain.session
        except Exception:                            # noqa: BLE001
            sess = None
        run("quote", lambda: L("quote", as_of=sess, data={
            "spot": chain.spot, "timestamp": chain.timestamp,
            "contracts": len(chain.contracts),
            "expiries": chain.expiries()[:12]}), as_of_on_error=sess)
        run("gex", lambda: L("gex", as_of=sess,
                             data=greeks.to_dict(greeks.compute(chain, dte_max=30))),
            as_of_on_error=sess)

        def _flow():
            scope = flow_parse.parse(chain, dte_max=30, traded_only=False)
            traded = [r for r in scope if r.volume > 0]
            f = flow_parse.summarize(chain, traded, all_rows=scope, top=8)
            return L("flow", as_of=sess, data={
                "counts": f["counts"], "ratios": f["ratios"],
                "unusual_rows": f["unusual_rows"], "limits": f["limits"]})
        run("flow", _flow, as_of_on_error=sess)

    # ── 2. IV ranking (locally accrued) ──
    def _scanner():
        sess2 = scanner_store.latest_session()
        if not sess2:
            return L("scanner", reason="not_enough",
                     detail="Nothing has been scanned locally yet — run a pass in the scanner.")
        q = [x for x in scanner_store.quotes_at(sess2) if x.get("symbol") == tk]
        if not q:
            # ⚠️ The newest local session is known, so carry it even on failure — this block is not "never had data",
            #    it is "we scanned other symbols and not this one".
            return L("scanner", as_of=sess2, reason="not_synced",
                     detail=f"{tk} is not among the symbols scanned locally (newest session {sess2}).")
        h = scanner_store.history([tk], as_of=sess2).get(tk, {})
        row = scanner_parse.build_row(q[0], h.get("iv", []), h.get("volume", []))
        return L("scanner", as_of=q[0].get("session") or sess2,
                 data=scanner_parse.to_dict(row))
    # The newest local session is known, so carry it even on failure — this block is not "never had any data"
    try:
        _sess2 = scanner_store.latest_session()
    except Exception:                                # noqa: BLE001
        _sess2 = None
    run("scanner", _scanner, as_of_on_error=_sess2)

    # ── 3. Insider Form 4 (local cache) ──
    def _insider():
        st = insider_store.stats()
        if not st.get("trades"):
            return L("insider", reason="not_synced", detail="Form 4 has not been synced locally yet.")
        agg = insider_store.aggregate(ticker=tk, top=8)
        if not (agg.get("counts") or {}).get("n"):
            return L("insider", reason="no_data",
                     detail=f"The Form 4 data synced so far holds no open-market trades for {tk}.")
        # Use **this symbol's own most recent** trade date as the instant, not the newest date in the database —
        # the latter shows a symbol that has not moved in three months as "yesterday's data".
        recent = insider_store.query(ticker=tk, limit=1) or []
        latest = (recent[0].get("tx_date") if recent else None) or None
        return L("insider", as_of=latest, data=agg)
    run("insider", _insider)

    # ── 4. Fails to deliver (local cache) ──
    def _shorts():
        st = shorts_store.stats()
        if not st.get("rows"):
            return L("shorts", reason="not_synced", detail="FTD data has not been imported locally yet.")
        agg = shorts_store.aggregate(top=8, symbol=tk)
        c = agg.get("counts") or {}
        if not c.get("n"):
            return L("shorts", reason="no_data", detail=f"The imported FTD data holds no {tk}.")
        return L("shorts", as_of=c.get("hi"), data=agg)
    run("shorts", _shorts)

    # ── 5. Congressional filings (local cache) ──
    def _congress():
        st = congress_store.stats()
        if not st.get("trades"):
            return L("congress", reason="not_synced", detail="Congressional filings have not been synced locally yet.")
        tr = congress_store.query_trades(ticker=tk, limit=12)
        if not tr:
            return L("congress", reason="no_data", detail=f"The filings synced so far hold no {tk}.")
        return L("congress", as_of=tr[0].get("tx_date"),
                 data={"trades": tr, "count": len(tr)})
    run("congress", _congress)

    # ── 6. Institutional 13F (local cache; keyed on CUSIP, not on ticker) ──
    def _institution():
        st = institution_store.stats()
        if not st.get("holdings"):
            return L("institution", reason="not_synced", detail="13F has not been imported locally yet.")
        # ⚠️ **This page deliberately does not map ticker → CUSIP.**
        #    13F gives CUSIPs only and the SEC publishes no mapping (that is commercial data);
        #    matching on issuer name was measured hitting just 42.8%, and matching a ticker ("NVDA")
        #    against an issuer name ("NVIDIA CORP") is near certain to miss —
        #    which would make this lane read "no institution holds it" for the vast majority of symbols,
        #    dressing "we cannot do the mapping" up as "no institution holds it".
        return L("institution", reason="no_mapping", detail=(
            f"{st.get('holdings', 0):,} 13F holdings have been imported locally"
            f" ({st.get('cusips', 0):,} CUSIPs, {st.get('managers', 0):,} managers). "
            f"But 13F **gives CUSIPs only**, the SEC publishes no ticker→CUSIP mapping, "
            f"and so a holding cannot be located reliably from the symbol {tk} — "
            f"which is **a mapping we cannot do**, not \"no institution holds it\". "
            f"Search the Institutions section by **issuer name** instead."))
    run("institution", _institution)

    # ── 7. Off-exchange / dark pools (off by default) ──
    def _darkpool():
        raw = darkpool_src.weekly(tk)
        parsed = darkpool_parse.parse(raw)
        weeks = darkpool_parse.weeks_of(parsed)
        if not weeks:
            if parsed["unknown_types"]:
                return L("darkpool", reason="fetch_failed", detail=(
                    f"FINRA returned record types not recognised here, {parsed['unknown_types']} — "
                    f"the parsing rules may be out of date. This is a **parser incompatibility**, not an absence of data."))
            return L("darkpool", reason="no_data",
                     detail=f"FINRA holds no off-exchange records for {tk}.")
        out = _consolidated(tk, weeks[-1], parsed)
        return L("darkpool", as_of=weeks[-1], data={
            "week": out["week"], "ats": out["ats"], "otc": out["otc"],
            "ats_over_otc": out["ats_over_otc"],
            "share": out["share"], "share_note": out["share_note"]})
    try:
        lanes.append(_darkpool())
    except darkpool_src.FinraDisabled as e:
        lanes.append(L("darkpool", reason="disabled", detail=str(e)))
    except darkpool_src.DataNotAvailable as e:
        lanes.append(L("darkpool", reason="no_data", detail=str(e)))
    except Exception as e:                           # noqa: BLE001
        lanes.append(L("darkpool", reason="fetch_failed",
                       detail=f"{type(e).__name__}: {e}"))

    out = stock_parse.assemble(lanes)
    out["ticker"] = tk
    return out
