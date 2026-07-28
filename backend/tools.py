"""The AI tool layer — **the single place tools are defined**.

The MCP server inherits these definitions automatically; a system AI or multi-agent setup later reuses the same file.
A new tool changes only this file; do not write a second set in mcp_server.py (a trap VibeResearch fell into:
tool definitions scattered about → three interfaces whose capabilities disagree).

⚠️ Compliance: tool output gives **data and computed results only** — no buy or sell advice, and no undervalued/overvalued labels.
"""
from __future__ import annotations

from typing import Any, Callable

from sources import cboe
# ⚠️ Imported for exec_tool's exception handling: each source package defines its own
#    `DataNotAvailable`, and one left out gets reported as a fetch failure.
from sources import edgar as edgar_src
from sources import congress as congress_src
from modules import greeks
from modules import congress as congress_parse
from modules import congress_store
from modules import insider as insider_parse
from modules import insider_store
from modules import institution_store
from modules import shorts_store
from modules import market as market_parse
from modules import market_store
from modules import flow as flow_parse
from modules import flow_store
from modules import scanner as scanner_parse
from modules import scanner_store
from sources import darkpool as darkpool_src
from modules import darkpool as darkpool_parse
from modules import stock as stock_parse
from sources import macro as macro_src
from modules import shorts as shorts_parse

# ── Tool schemas (shared by MCP and function-calling) ──
TOOLS: list[dict] = [
    {
        "name": "get_gex",
        "description": (
            "A US stock's GEX (gamma exposure) profile: total GEX, the gamma flip price, "
            "call and put walls, and the distribution across strikes and expiries. "
            "Positive GEX means dealer hedging suppresses volatility; negative means it amplifies volatility."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "US ticker, e.g. SPY / NVDA"},
                "dte_max": {"type": "integer",
                            "description": "Count only contracts expiring within N days; 0 means same-day expiry (0DTE). Omitted = the whole chain"},
                "strike_pct": {"type": "number",
                               "description": "Strike range, ± this fraction; default 0.05 (±5%)"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_short_fails",
        "description": (
            "Query SEC fails-to-deliver data. "
            "⚠️ **Three points that have to be read together**: (1) it is **a cumulative balance on a settlement date**, not that day's additions; "
            "the SEC states that consecutive days 'may have little or no relationship' and that "
            "'the age of fails cannot be determined'; "
            "(2) the SEC states that a failure to deliver **can arise from either a long or a short sale** "
            "and is **not evidence of naked shorting**; (3) the per-symbol figure is the **mean** of the balances across settlement dates, never their sum "
            "(one undelivered trade reappears on consecutive days)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker"},
                "settlement_date": {"type": "string", "description": "Settlement date, YYYY-MM-DD"},
                "since": {"type": "string", "description": "Earliest settlement date, YYYY-MM-DD"},
                "min_quantity": {"type": "number", "description": "Minimum balance (shares)"},
                "top": {"type": "integer", "description": "How many to list, default 10"},
            },
        },
    },
    {
        "name": "get_institution_holdings",
        "description": (
            "Query institutional 13F holdings (the SEC's quarterly filing). "
            "⚠️ **13F reports long positions in 13(f) securities as of quarter-end** only — no shorts (the SEC created Form SHO for those), "
            "no cash, bonds, stocks listed only outside the US or private holdings. Puts are listed under their underlying and are **bearish**, "
            "so they are excluded from the holdings totals by default. Filings run at least 45 days behind."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cusip": {"type": "string", "description": "CUSIP (not a ticker — 13F gives CUSIPs only)"},
                "manager": {"type": "string", "description": "Manager name (fuzzy match), e.g. Berkshire"},
                "period": {"type": "string", "description": "Reporting period, YYYY-MM-DD (quarter-end)"},
                "kind": {"type": "string", "enum": ["share", "call", "put", "all"],
                         "description": "Position kind, default share"},
                "top": {"type": "integer", "description": "How many per table, default 10"},
            },
        },
    },
    {
        "name": "get_institution_changes",
        "description": (
            "Quarter-on-quarter change in institutional holdings: new positions / added / trimmed / exited. "
            "⭐ 13F's value is in the change; a single quarter is only a static snapshot. "
            "⚠️ An exit means only that the symbol no longer appears among 13(f) long holdings, not that the manager turned bearish."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "This reporting period, YYYY-MM-DD"},
                "prev_period": {"type": "string", "description": "The previous reporting period, YYYY-MM-DD"},
                "manager": {"type": "string", "description": "Restrict to one manager"},
                "top": {"type": "integer", "description": "How many per table, default 10"},
            },
        },
    },
    {
        "name": "get_insider_trades",
        "description": (
            "Query trades by insiders of US listed companies (officers, directors, 10% holders) as filed on SEC Form 4. "
            "⚠️ **By default it returns active open-market trading only (codes P/S)** — about seven tenths of Form 4 is "
            "compensation: grants, option exercises, tax withholding. Counted as insider buying, they overstate it several times over. "
            "It reads the locally synced cache."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker, e.g. NVDA"},
                "owner": {"type": "string", "description": "Insider name (fuzzy match)"},
                "direction": {"type": "string", "enum": ["buy", "sell"]},
                "role": {"type": "string", "enum": ["officer", "director", "ten_pct"],
                         "description": "Role: officer / director / 10% holder"},
                "group": {"type": "string",
                          "enum": ["open_market", "compensation", "other", "all"],
                          "description": "Transaction group, default open_market"},
                "plan": {"type": "string", "enum": ["yes", "no", "unknown"],
                         "description": "10b5-1 plan: yes / no = explicitly marked not under a plan / unknown = the filing did not mark it (no such field before 2023)"},
                "since": {"type": "string", "description": "Earliest trade date, YYYY-MM-DD"},
                "min_value": {"type": "number", "description": "Minimum trade value (dollars)"},
                "limit": {"type": "integer", "description": "Maximum trades to return, default 50"},
            },
        },
    },
    {
        "name": "get_insider_summary",
        "description": (
            "Insider trades aggregated: net buy and sell value, a cluster-buying table (how many distinct insiders bought the same name), "
            "and the most active insiders. Open-market transactions only. ⚠️ It presents facts and gives no buy or sell advice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "since": {"type": "string", "description": "Earliest trade date, YYYY-MM-DD"},
                "role": {"type": "string", "enum": ["officer", "director", "ten_pct"]},
                "min_value": {"type": "number"},
                "top": {"type": "integer", "description": "How many per table, default 10"},
            },
        },
    },
    {
        "name": "get_congress_trades",
        "description": (
            "Query US congressional stock trades disclosed under the STOCK Act (House + Senate). "
            "Filterable by ticker, member, chamber, direction and earliest trade date. "
            "⚠️ Amounts are **ranges**, not exact figures; disclosure inherently lags by tens of days; "
            "it reads the locally synced cache, and is empty if nothing has been synced."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker, e.g. NVDA"},
                "member": {"type": "string", "description": "Member name (fuzzy match)"},
                "chamber": {"type": "string", "enum": ["house", "senate"],
                            "description": "Chamber; omitted = both"},
                "tx_type": {"type": "string", "enum": ["buy", "sell"],
                            "description": "Buy or sell; omitted = all"},
                "since": {"type": "string", "description": "Earliest trade date, YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Maximum trades to return, default 50"},
            },
        },
    },
    {
        "name": "get_congress_summary",
        "description": (
            "Congressional trades aggregated: the most active tickers, the members trading most, the buy/sell ratio, and disclosure delay statistics. "
            "⚠️ Amounts are sums of range midpoints, good for comparison between rows and not real trade sizes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Earliest trade date, YYYY-MM-DD"},
                "chamber": {"type": "string", "enum": ["house", "senate"]},
                "top": {"type": "integer", "description": "How many per table, default 10"},
            },
        },
    },
    {
        "name": "get_gex_curve",
        "description": (
            "The GEX curve against hypothetical share prices, for locating the gamma flip. "
            "Every price recomputes gamma with Black-Scholes (rather than reusing the current gamma)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "dte_max": {"type": "integer",
                            "description": "Count only expiries within N days; omitted = the whole chain (matching get_gex's default)"},
                "span_pct": {"type": "number", "description": "Price range to sweep, ± this fraction; default 0.06"},
                "strike_pct": {"type": "number",
                               "description": "Strike range, ± this fraction; default 0.05 (must match get_gex)"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_option_chain_summary",
        "description": (
            "An overview of a US stock's options chain: spot, total contracts, available expiries, "
            "and volume bucketed by days to expiry (showing how short-dated the market is)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_option_flow",
        "description": (
            "A US stock's unusual options activity and positioning for the day: the vol/OI table, "
            "the put/call ratio (on volume, open interest and premium), absolute delta exposure, and the expiry distribution. "
            "⚠️ The data is a **chain snapshot**, not the print-by-print tape — buyer/seller direction **cannot** be determined, "
            "and sweep detection and block-size tiering are impossible, so this tool **attaches no bullish or bearish label**."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "dte_max": {"type": "integer", "description": "Only expiries within N days; omitted = the whole chain"},
                "top": {"type": "integer", "description": "Rows in the unusual table, default 15"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_oi_change",
        "description": (
            "The change in locally accrued options open interest — who was opening or closing between two snapshot days. "
            "⚠️ This history **cannot be backfilled** and exists only where this machine has accrued it; without enough it returns enough=false "
            "(meaning 'not enough accrued yet', not 'open interest did not change')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD; omitted = the second-newest snapshot"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD; omitted = the newest snapshot"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "scan_market",
        "description": (
            "Screen symbols within the quote snapshots **already scanned locally**: IV Rank / IV percentile / volume multiple / "
            "price / change. "
            "⚠️ **IV Rank needs history by definition**, and the field is null where this machine has not accrued enough — "
            "which means 'not enough accrued yet', not 'a low rank'. The two must never be conflated. "
            "⚠️ It reads local snapshots and fetches nothing live; if nothing has been scanned it says so plainly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_iv_rank": {"type": "number", "description": "Minimum IV Rank, 0-100"},
                "min_volume": {"type": "number", "description": "Minimum volume (shares)"},
                "min_volume_x": {"type": "number",
                                 "description": "Volume as at least this multiple of the local historical median"},
                "min_price": {"type": "number"},
                "sort": {"type": "string",
                         "description": "iv_rank/iv_percentile/iv30/volume/volume_x/change_pct"},
                "top": {"type": "integer", "description": "How many symbols to return, default 20"},
            },
        },
    },
    {
        "name": "get_darkpool",
        "description": (
            "Query a US stock's off-exchange volume (FINRA, weekly). "
            "⚠️ **ATS (a genuine dark pool) and non-ATS off-exchange (wholesaler internalisation) are two different kinds of trading; "
            "this tool returns them apart and never adds them into a 'dark pool volume'** — measured, non-ATS is often more than double ATS. "
            "⚠️ The data runs **about four weeks behind**, and non-ATS off-exchange **does not disclose the firm**. "
            "⚠️ This source is off by default (FINRA's terms); when it is off, the tool says so plainly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "week": {"type": "string", "description": "Week start, YYYY-MM-DD; omitted = the newest"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_stock",
        "description": (
            "A US stock's full profile across nine data lanes: quote and options chain, GEX, options flow, IV ranking, "
            "insider Form 4, fails to deliver, congressional filings, institutional 13F, and off-exchange / dark pools. "
            "⚠️ **These nine differ in freshness by two orders of magnitude** (the option chain is the last trading session, "
            "the 13F a quarter-end three months back), so each carries its own instant and lag in days — "
            "⛔ **do not treat them as contemporaneous**, and this tool **produces no cross-source score**. "
            "⚠️ A missing block gives **its own reason** (not yet synced / not enough accrued / source switched off / "
            "fetch failed / cannot be located), and **none of those mean** 'this symbol has no such activity'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_yield_curve",
        "description": (
            "The Treasury yield curve: the latest day's full term structure plus the history of both spreads "
            "(10Y-2Y and 10Y-3M). "
            "⚠️ 'Inversion' has two common definitions that can invert months apart; this tool gives both and picks neither."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "years": {"type": "integer",
                          "description": "How many years back, default 3 (one year often never shows the crossing of zero)"},
            },
        },
    },
    {
        "name": "get_cot",
        "description": (
            "The CFTC financial futures positioning report (TFF): long, short and net positions for "
            "leveraged funds, asset managers and dealers. ⚠️ It runs three days behind (Tuesday's positions, published Friday). "
            "Omit market to get the list of available contracts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "market": {"type": "string",
                           "description": "Contract name keyword, e.g. 'E-MINI S&P 500' / 'TREASURY'"},
                "periods": {"type": "integer", "description": "How many recent reports, default 12"},
            },
        },
    },
]


# ── Implementations ──
def _tool_get_gex(ticker: str, dte_max: int | None = None,
                  strike_pct: float = 0.05) -> dict:
    chain = cboe.cached_option_chain(ticker)
    profile = greeks.compute(chain, dte_max=dte_max, strike_pct=strike_pct)
    out = greeks.to_dict(profile)
    # A sentence of plain words for the AI, so it need not work out what the sign means
    flip = out["gamma_flip"]
    # ⚠️ All three regimes have to be described honestly. Folding neutral into "positive" would tell the AI
    # "dealer hedging suppresses volatility" out of nothing at all.
    regime_txt = {
        "positive": "positive gamma; dealer hedging suppresses volatility",
        "negative": "negative gamma; dealer hedging amplifies volatility",
        "neutral": "zero exposure, no measurable gamma (the selected contracts may lack gamma data, or longs and shorts cancel exactly)",
    }[out["regime"]]
    out["summary"] = (
        f"{out['ticker']} spot ${out['spot']}, total GEX {out['total_gex_bn']:+.2f}B ({regime_txt}). "
        + (f"The gamma flip is at ${flip}, with spot "
           f"{'below' if out['spot'] < flip else 'above'} it." if flip else "No gamma flip occurs within the range.")
    )
    return out


def _tool_get_gex_curve(ticker: str, dte_max: int | None = None,
                        span_pct: float = 0.06, strike_pct: float = 0.05) -> dict:
    """⚠️ The defaults must match get_gex (dte_max=None for the whole chain, strike_pct=0.05).
    Otherwise, when the AI calls both tools, the curve's zero will not agree with the gamma_flip get_gex returned."""
    chain = cboe.cached_option_chain(ticker)
    lo_k, hi_k = chain.spot * (1 - strike_pct), chain.spot * (1 + strike_pct)
    cs = [c for c in chain.filter(dte_max=dte_max) if lo_k <= c.strike <= hi_k]
    if not cs:
        raise ValueError(f"{ticker} has no contracts matching those conditions")
    lo, hi = chain.spot * (1 - span_pct), chain.spot * (1 + span_pct)
    curve = [{"price": round(lo + (hi - lo) * i / 40, 2),
              "gex_bn": round(greeks.total_gex_at(cs, lo + (hi - lo) * i / 40) / 1e9, 4)}
             for i in range(41)]
    return {"ticker": chain.ticker, "spot": round(chain.spot, 2), "curve": curve}


def _tool_get_option_chain_summary(ticker: str) -> dict:
    from collections import defaultdict
    chain = cboe.cached_option_chain(ticker)
    buckets: dict[str, float] = defaultdict(float)
    for c in chain.contracts:
        if not c.volume:
            continue
        d = c.dte_from(chain.asof)
        key = "0-1d" if d <= 1 else "2-7d" if d <= 7 else "8-30d" if d <= 30 else "over 30d"
        buckets[key] += c.volume
    total = sum(buckets.values()) or 1
    return {
        "ticker": chain.ticker,
        "spot": round(chain.spot, 2),
        "contracts": len(chain.contracts),
        "expiries": chain.expiries()[:12],
        "volume_by_dte": {k: {"volume": v, "pct": round(v / total * 100, 1)}
                          for k, v in buckets.items()},
        "note": "A large share in 0-1d means this symbol's options trading is extremely short-dated (a 0DTE ecosystem)",
    }


def _row_to_trade(r: dict):
    """DB row → Trade. The same derivation as the REST layer, so the two exits cannot disagree on fields."""
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


def _tool_get_congress_trades(ticker: str | None = None, member: str | None = None,
                              chamber: str | None = None, tx_type: str | None = None,
                              since: str | None = None, limit: int = 50) -> dict:
    rows = congress_store.query_trades(chamber=chamber, ticker=ticker, member=member,
                                       since=since, tx_type=tx_type,
                                       limit=max(1, min(limit, 500)))
    trades = [congress_parse.to_dict(_row_to_trade(r)) for r in rows]
    st = congress_store.stats()
    if not trades and st["trades"] == 0:
        # An empty result has two causes, and which one has to be said
        return {"trades": [], "count": 0,
                "summary": "No congressional filings have been synced locally yet — "
                           "which means **nothing has been synced**, not that there were no trades. Call POST /api/congress/sync first."}
    # ⚠️ Transaction types are not only buys and sells: there is E (exchange) too. Using len-buys as the sell count counts exchanges as sells.
    # The rule matches summarize(): P = buy, anything starting with S = sell (including partial sales), and the rest is listed apart.
    buys = sum(1 for t in trades if t["tx_type"] == "P")
    sells = sum(1 for t in trades if str(t["tx_type"]).startswith("S"))
    others = len(trades) - buys - sells
    return {
        "trades": trades, "count": len(trades),
        "buys": buys, "sells": sells, "other_types": others,
        "coverage": {"cached_trades": st["trades"],
                     "unparsed_filings": st["unparsed_filings"],
                     "last_sync": st["last_sync"]},
        "summary": (f"Returned {len(trades)} trades ({buys} buys / {sells} sells"
                    + (f" / {others} of other types, such as exchanges" if others else "") + "). "
                    f"{st['trades']} are cached locally in total, with a further {st['unparsed_filings']} filings "
                    f"unparsed as paper scans. Amounts are ranges rather than exact figures, "
                    f"and disclosure lags by tens of days, so it does not represent current holdings."),
    }


def _tool_get_congress_summary(since: str | None = None, chamber: str | None = None,
                               top: int = 10) -> dict:
    # ⚠️ No limit: the statistics have to describe every matching filing. This used to fetch the
    #    newest 5,000 and warn when it hit the ceiling, but a warning attached to a wrong number
    #    is still a wrong number, and a model reading the summary carries the number, not the
    #    caveat. The REST endpoint made the same mistake at 2,000 and understated the filings
    #    past the 45-day deadline by 27%.
    rows = congress_store.query_trades(chamber=chamber, since=since, limit=None)
    out = congress_parse.summarize([_row_to_trade(r) for r in rows])
    n = max(1, min(top, 50))
    out["by_ticker"] = out["by_ticker"][:n]
    out["by_member"] = out["by_member"][:n]
    if not rows:
        out["summary"] = "Nothing local matches those conditions (possibly not synced yet, or genuinely nothing filed in that period)."
        return out
    hot = ", ".join(f"{t['ticker']} ({t['trades']} trades)" for t in out["by_ticker"][:5])
    out["scope"] = {"chamber": chamber, "since": since, "sampled": len(rows),
                    "limit": None, "truncated": False}
    out["summary"] = (
        f"{out['total_trades']} trades in total ({out['buys']} buys / {out['sells']} sells). "
        f"Most active tickers: {hot}. The median disclosure delay is {out['delay']['median_days']} days, "
        f"with {out['delay']['over_45d_count']} beyond 45 days — "
        f"a statement of fact, not a finding of violation (the deadline rolls over weekends and so on). "
        f"Amounts are sums of range midpoints, for comparison between rows only.")
    return out


def _ins_row(r: dict):
    """DB row → InsiderTrade. The same derivation path as the REST layer."""
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


def _tool_get_insider_trades(ticker: str | None = None, owner: str | None = None,
                             direction: str | None = None, role: str | None = None,
                             group: str = "open_market", plan: str | None = None,
                             since: str | None = None, min_value: float | None = None,
                             limit: int = 50) -> dict:
    rows = insider_store.query(
        ticker=ticker, owner=owner, group=None if group == "all" else group,
        direction=direction, role=role, plan=plan, since=since,
        min_value=min_value, limit=max(1, min(limit, 500)))
    st = insider_store.stats()
    if not rows and st["trades"] == 0:
        return {"trades": [], "count": 0,
                "summary": "No Form 4 data has been synced locally yet — which means **nothing has been synced**, "
                           "not that there were no insider trades. Call POST /api/insider/sync first."}
    out = [insider_parse.to_dict(_ins_row(r)) for r in rows]
    buys = sum(1 for t in out if t["direction"] == "buy")
    sells = sum(1 for t in out if t["direction"] == "sell")
    bv = sum(t["value"] or 0 for t in out if t["direction"] == "buy")
    sv = sum(t["value"] or 0 for t in out if t["direction"] == "sell")
    scope = ("active open-market trading" if group == "open_market"
             else "compensation (grants, exercises, tax withholding)" if group == "compensation"
             else "other types" if group == "other" else "all types")
    return {
        "trades": out, "count": len(out), "buys": buys, "sells": sells,
        "coverage": {"cached_trades": st["trades"],
                     "open_market_pct": st["open_market_pct"],
                     "range": [st["earliest"], st["latest"]],
                     "last_sync": st["last_sync"]},
        "summary": (f"Returned {len(out)} trades ({scope}): {buys} buys worth ${bv:,.0f} "
                    f"and {sells} sells worth ${sv:,.0f}. There are {st['trades']:,} rows locally, "
                    f"of which open market is only {st['open_market_pct']}% — "
                    f"the rest being grants, exercises and tax withholding, which represent no decision to trade."),
    }


def _tool_get_insider_summary(ticker: str | None = None, since: str | None = None,
                              role: str | None = None, min_value: float | None = None,
                              top: int = 10) -> dict:
    # ⚠️ It runs the same full SQL aggregation as REST, not "take the newest N rows and compute" —
    # otherwise the totals MCP reports describe the newest N rows while the AI takes them for the whole period.
    agg = insider_store.aggregate(top=max(1, min(top, 50)), ticker=ticker,
                                  since=since, role=role, group="open_market",
                                  min_value=min_value)
    lib = insider_store.stats()
    c = agg["counts"]
    out = {
        "open_market": {"count": c["n"] or 0, "buys": c["buys"] or 0,
                        "sells": c["sells"] or 0,
                        "buy_value": round(c["bv"] or 0),
                        "sell_value": round(c["sv"] or 0)},
        "by_ticker": agg["by_ticker"], "cluster_buys": agg["cluster_buys"],
        "by_owner": agg["by_owner"], "plan_sells": c["plan_sells"] or 0,
        "notes": insider_parse.summary_notes(lib), "stats": lib,
    }
    if not c["n"]:
        out["summary"] = "No open-market trades locally under those conditions (possibly not synced yet, or genuinely none in that period)."
        return out
    om = out["open_market"]
    cluster = ", ".join(f"{c['ticker']} ({c['insider_count']} insiders)"
                        for c in out["cluster_buys"][:5]) or "none"
    out["summary"] = (
        f"Open-market buying: {om['buys']} trades worth ${om['buy_value']:,}; "
        f"selling: {om['sells']} trades worth ${om['sell_value']:,}. "
        f"Symbols bought by several insiders: {cluster}. Of the sales, {out['plan_sells']} were under 10b5-1 plans "
        f"(arranged months earlier, not decided on the day). All of this is statistics on filed facts and is not investment advice.")
    return out


def _tool_get_institution_holdings(cusip: str | None = None,
                                   manager: str | None = None,
                                   period: str | None = None,
                                   kind: str = "share", top: int = 10) -> dict:
    st = institution_store.stats()
    if not st["holdings"]:
        return {"holdings": [], "count": 0,
                "summary": "No 13F data has been imported locally yet — which means **nothing has been imported**, "
                           "not that institutions hold nothing. Call POST /api/institution/sync first."}
    # Same table the aggregate reads — `stats` reports periods from the import log, which
    # can be empty while holdings are present, and an empty period sums every quarter at once.
    p = period or institution_store.newest_period()
    agg = institution_store.aggregate(top=max(1, min(top, 50)), cusip=cusip,
                                      manager=manager, period=p, kind=kind)
    c = agg["counts"]
    puts = (agg["by_kind"].get("put") or {}).get("value") or 0
    hot = ", ".join(f"{x['issuer'][:22]} ({_money(x['value'])}, {x['holders']} holders)"
                    for x in agg["by_issuer"][:5]) or "none"
    return {
        **agg, "period": p, "stats": st,
        "summary": (f"Reporting period {p}: {c['n']:,} holdings across {c['mgrs']:,} managers, "
                    f"totalling {_money(c['val'] or 0)}. Largest holdings: {hot}. "
                    f"⚠️ 13F carries **long** positions in 13(f) securities only — no shorts, cash, bonds or "
                    f"stocks listed only outside the US; puts ({_money(puts)} this period) are listed under their "
                    f"underlying, classified separately and excluded from the holdings; and the data lags by at least 45 days."),
    }


def _tool_get_institution_changes(period: str | None = None,
                                  prev_period: str | None = None,
                                  manager: str | None = None,
                                  top: int = 10) -> dict:
    have = institution_store.known_periods()
    if len(have) < 2:
        return {"summary": f"Comparing needs at least two reporting periods, and there {'is' if len(have) == 1 else 'are'} currently {len(have)} — "
                           f"import more quarters first (13F's value is in the change, not the static snapshot)."}
    p = period or have[0]
    pp = prev_period or next((x for x in have if x < p), None)
    if not pp:
        return {"summary": f"No imported reporting period precedes {p}, so there is nothing to compare against. Available: {', '.join(have)}"}
    out = institution_store.changes(period=p, prev_period=pp,
                                    top=max(1, min(top, 50)), manager=manager)
    fmt = lambda rows: ", ".join(f"{r['issuer'][:20]} ({_money(r['delta_value'])})"
                                 for r in rows[:4]) or "none"
    out["summary"] = (
        f"{pp} → {p}: {out['counts']['new']} new / {out['counts']['increased']} added / "
        f"{out['counts']['decreased']} trimmed / {out['counts']['exited']} exited. "
        f"Largest additions: {fmt(out['increased'])}. Largest reductions: {fmt(out['decreased'])}. "
        f"⚠️ An exit means only that it no longer appears among 13(f) long holdings, not that the manager turned bearish. {out['floor_note']}")
    return out


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    a = abs(n)
    sign = "-" if (n or 0) < 0 else ""
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.1f}{unit}"
    return f"{sign}${a:.0f}"


def _tool_get_short_fails(symbol: str | None = None,
                          settlement_date: str | None = None,
                          since: str | None = None,
                          min_quantity: float | None = None,
                          top: int = 10) -> dict:
    st = shorts_store.stats()
    if not st["rows"]:
        return {"fails": [], "count": 0,
                "summary": "No FTD data has been imported locally yet — which means **nothing has been imported**, "
                           "not that the market has no failures to deliver. Call POST /api/shorts/sync first."}
    agg = shorts_store.aggregate(top=max(1, min(top, 50)), symbol=symbol,
                                 settlement_date=settlement_date, since=since,
                                 min_quantity=min_quantity)
    c = agg["counts"]
    hot = ", ".join(
        f"{x['symbol']} (mean {(x['avg_quantity'] or 0):,.0f} shares)"
        for x in agg["by_symbol"][:5]) or "none"
    return {
        **agg, "stats": st, "notes": shorts_parse.OFFICIAL_NOTES,
        "summary": (
            f"{c['lo']} to {c['hi']}: {c['n']:,} records across {c['syms']:,} symbols and "
            f"{c['days']} settlement dates. Largest balances: {hot}. "
            f"⚠️ This is **a cumulative balance on a settlement date**, not that day's additions (per the SEC, consecutive days "
            f"'may have little or no relationship', and the age of a fail cannot be determined); "
            f"⚠️ the SEC states plainly that a failure to deliver **can arise from either a long or a short sale and is not evidence of naked shorting**; "
            f"⚠️ the table uses the **mean** of the balances across settlement dates, never their sum. "
            f"All of this is statistics on public data and is not investment advice."),
    }


def _tool_get_option_flow(ticker: str, dte_max: int | None = None,
                          top: int = 15) -> dict:
    chain = cboe.cached_option_chain(ticker)
    scope = flow_parse.parse(chain, dte_max=dte_max, traded_only=False)
    traded = [r for r in scope if r.volume > 0]
    out = flow_parse.summarize(chain, traded, all_rows=scope,
                               top=max(1, min(top, 60)))
    c, rt = out["counts"], out["ratios"]
    hot = "; ".join(
        f"{x['expiry']} {'put' if x['type'] == 'put' else 'call'} {x['strike']:g}"
        f" (volume {x['volume']:,.0f}"
        + (", prior open interest 0 (**which does not mean today is all new positions** — it may equally be "
           "a cold strike, or an intraday round trip opened and closed)" if x["zero_prior_oi"]
           else f", vol/OI {x['vol_oi']:.1f}")
        # ⚠️ Never write `x['notional'] or 0` — the structured field is null (not computable)
        #    while the summary says "$0": two accounts of the same thing, contradicting each other in one response.
        + (f", premium estimated at {_money(x['notional'])})" if x["notional"] is not None
           else ", premium **not computable** (no two-sided quote))")
        for x in out["unusual_rows"][:3]) or "none"

    def _pc(k: str) -> str:
        """⚠️ `pc=None` has **two** causes, and neither can be reported as a bare "zero denominator"."""
        d = rt[k]
        if d["pc"] is not None:
            return f"{d['pc']:.2f}"
        if k == "by_notional":
            # ⚠️ counted=0 has **two** causes: that side did not trade at all, or it traded and every quote is missing.
            #    Reporting both as "no two-sided quote" misdiagnoses "no call volume" as a data problem.
            bad = []
            for side, cnt, vol in (("call", d.get("counted_call"), rt["by_volume"]["call"]),
                                   ("put", d.get("counted_put"), rt["by_volume"]["put"])):
                if cnt:
                    continue
                bad.append(f"the {side} side " + ("did not trade today" if not vol else "traded but has no quotes at all"))
            if bad:
                return "not computable (" + ", ".join(bad) + ")"
        return "not computable (zero denominator: that side has no volume or open interest at all)"

    return {
        **out,
        "summary": (
            f"{chain.ticker} ${chain.spot:.2f} (trading session {chain.session or 'unknown'}): "
            f"scope {'the whole chain' if dte_max is None else f'expiring within {dte_max} days'} "
            f"(⚠️ the web page defaults to 7 days, and a different scope changes the contract count and every ratio): "
            f"{c['scope_contracts']:,} contracts in range, of which {c['traded_contracts']:,} traded today, "
            f"totalling {c['total_volume']:,.0f} contracts traded against {c['total_oi']:,.0f} open. "
            f"{c['unusual']} are unusual (including {c['zero_prior_oi']} with zero prior open interest). "
            f"Put/call ratio: on volume {_pc('by_volume')}, "
            f"on open interest {_pc('by_oi')}, on estimated premium {_pc('by_notional')} "
            f"— **the three measure different things, and disagreeing is normal**. "
            f"Largest unusual activity: {hot}. "
            f"⛔ **This data is a chain snapshot, not the print-by-print tape**: whether these trades were buyer- or seller-initiated cannot be determined, "
            f"so it **cannot** be called bullish or bearish, and sweep detection and block-size tiering are impossible. "
            f"Premium is an **estimate** of cumulative volume × the mid at capture time, and may be several times the amount actually traded. "
            f"The above presents public delayed data and is not investment advice."),
    }


def _tool_get_oi_change(ticker: str, date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    tk = (ticker or "").strip().upper()
    out = flow_store.oi_change(tk, date_from=date_from, date_to=date_to, top=15)
    if not out.get("enough"):
        # ⚠️ `enough=false` has **three** causes and cannot all be put down to "not enough local history":
        #    ① only 0-1 days really have accrued ② the date given is not in the database ③ the start and end dates are the wrong way round.
        #    ② and ③ are **parameter problems**, and calling them "not enough history" sends the AI to the wrong next step.
        have = out.get("have", 0)
        if date_from or date_to:
            why = ("This is **a problem with the dates given** (the database has no such day, or the order is reversed) — "
                   f"the snapshot days held locally: {', '.join(out.get('dates', [])[:8]) or 'none'}.")
        elif have < 2:
            why = ("This is **not enough local history** (this data cannot be backfilled and only accrues daily), "
                   "not an absence of change in the market's positions.")
        else:
            why = "This does not mean open interest did not change; see note for the specific reason."
        return {**out, "summary": f"Cannot compute the open-interest change for {tk}: {out['note']} {why}"}
    t = out["totals"]
    top3 = "; ".join(
        f"{x['expiry']} {'put' if x['type'] == 'put' else 'call'} {x['strike']:g} "
        f"{x['change']:+,.0f}" for x in out["gained"][:3]) or "none"
    span = ("two adjacent snapshots" if out["is_consecutive"]
            else f"{out['span_days']} days apart with "
                 f"{out['snapshots_between']} observations in between, so the change is **cumulative**")
    return {**out, "summary": (
        f"{tk} {out['date_from']} → {out['date_to']} ({span}): "
        f"call open interest changed by {t['call_change']:+,.0f} and put by {t['put_change']:+,.0f}, "
        f"across {t['contracts']:,} contracts. Largest increases: {top3}. "
        + (f"Excluded {out['expired_excluded']} contracts that expired during the period "
           f"({out['expired_oi']:,.0f} of open interest) — leaving the chain at expiry is not closing out. "
           if out.get("expired_excluded") else "")
        # ⚠️ The web page raises a warning banner for these two, and saying nothing here would leave "two views
        #    describing the same data's reliability differently" — the class of trap this project keeps hitting.
        + (f"⚠️ **This comparison's reliability is in doubt**: {out['incomplete_excluded']} unexpired contracts "
           f"are absent from the end snapshot ({out['incomplete_oi']:,.0f} of open interest), which means that pull was incomplete. They are excluded. "
           if out.get("incomplete_excluded") else "")
        + (f"⚠️ {out['new_listings']} contracts appear only in the end snapshot and are counted as rising from 0 — "
           f"**listed during the period** and **missed by the start pull** cannot be told apart in the data "
           f"(contract counts {out.get('contracts_from')} → {out.get('contracts_to')}). "
           if out.get("new_listings") else "")
        + f"⚠️ A rise or fall in open interest **indicates no direction**: every contract has a buyer and a seller, "
          f"so net new longs and shorts are equal. None of the above is investment advice.")}


def _tool_scan_market(min_iv_rank: float | None = None,
                      min_volume: float | None = None,
                      min_volume_x: float | None = None,
                      min_price: float | None = None,
                      sort: str = "iv30", top: int = 20) -> dict:
    sess = scanner_store.latest_session()
    st = scanner_store.stats()
    if not sess:
        return {"rows": [], "count": 0, "stats": st,
                "summary": ("There are no scan results here yet — which means **nothing has been scanned**, "
                            "not that no symbol in the market matches. "
                            "Run a scan first (POST /api/scanner/scan).")}
    quotes = scanner_store.quotes_at(sess)
    hist = scanner_store.history([q["symbol"] for q in quotes], as_of=sess)
    rows = [scanner_parse.build_row(
                q, hist.get(q["symbol"], {}).get("iv", []),
                hist.get(q["symbol"], {}).get("volume", []))
            for q in quotes]
    kept, excluded = scanner_parse.apply_filters(
        rows, min_iv_rank=min_iv_rank, min_volume=min_volume,
        min_volume_x=min_volume_x, min_price=min_price)
    kept = scanner_parse.sort_rows(kept, sort)
    total = len(kept)                      # ⚠️ The count **before** truncation
    kept = kept[:max(1, min(top, 100))]
    hot = "; ".join(
        f"{r.symbol} (IV30 " + ("—" if r.iv30 is None else f"{r.iv30:.1f}")
        + (f", IV Rank {r.iv_rank:.0f}" if r.iv_rank is not None
           else ", IV Rank null ("
                + scanner_parse.REASON_LABEL.get(r.iv_reason or "unknown", "reason unknown")
                + (f", {r.iv_days_needed} more trading days needed"
                   if r.iv_reason == "insufficient_history" else "") + ")")
        + ")" for r in kept[:5]) or "none"
    # ⚠️ "Excluded because it could not be computed" has to be said apart from "failed the condition",
    #    or the AI reads "not enough local history" as "only this handful in the whole market qualifies".
    exc = ""
    def _why(d: dict) -> str:
        return ", ".join(
            f"{scanner_parse.REASON_LABEL.get(k, k)}: {v}" for k, v in d.items())
    if excluded["excluded_no_iv_rank"]:
        exc += (f"⚠️ A further {excluded['excluded_no_iv_rank']} were filtered out by that condition because "
                f"**IV Rank could not be computed** ({_why(excluded['iv_reasons'])}) — "
                f"they are **not computable**, not failing the condition. ")
    if excluded["excluded_no_volume_x"]:
        exc += (f"⚠️ A further {excluded['excluded_no_volume_x']} were filtered out because "
                f"**the volume multiple could not be computed** ({_why(excluded['volume_reasons'])}). ")
    return {
        "rows": [scanner_parse.to_dict(r) for r in kept],
        # ⚠️ `count` matches REST = the total matching **before** truncation.
        #    Returning the post-truncation count makes one dataset report two different totals in two views.
        "count": total, "returned": len(kept),
        "session": sess, "excluded": excluded, "stats": st,
        "summary": (
            f"Trading session {sess}: {len(rows):,} symbols scanned locally, {total} matching "
            f"(the first {len(kept)} returned here). "
            f"Leading: {hot}. {exc}"
            f"{st['sessions']} trading sessions have accrued locally, "
            f"of which {st['iv_ready_symbols']:,} symbols have the history IV Rank requires "
            f"({scanner_parse.IV_MIN_SAMPLE} trading days; this history **cannot be backfilled** "
            f"and only accrues daily). The above is statistics on public delayed data and is not investment advice."),
    }


def _tool_get_darkpool(ticker: str, week: str | None = None) -> dict:
    tk = (ticker or "").strip().upper()
    try:
        raw = darkpool_src.weekly(tk)
    except darkpool_src.FinraDisabled as e:
        # ⚠️ This is a **configuration state**, not "this symbol has no off-exchange volume"
        return {"enabled": False, "error": str(e),
                "summary": (f"The dark pool / off-exchange source is **currently off**, so no data for {tk} could be fetched. "
                            f"This is a **configuration state**, not 'this symbol has no off-exchange volume'. "
                            f"Set FZ_ENABLE_FINRA=1 to enable it; off is deliberate, "
                            f"because FINRA's terms restrict it to non-commercial use and forbid building a database from their data.")}
    parsed = darkpool_parse.parse(raw)
    weeks = darkpool_parse.weeks_of(parsed)
    if not weeks:
        # ⚠️ Same semantics as REST: not one row of a known type, yet unknown types present = **a parser incompatibility**,
        #    which must not be reported as "this symbol has no off-exchange volume".
        if parsed["unknown_types"]:
            return {"error": "parser incompatibility", "unknown_types": parsed["unknown_types"],
                    "summary": (f"FINRA returned record types this program does not recognise "
                                f"{parsed['unknown_types']} — the parsing rules may be out of date. "
                                f"This is a **parser incompatibility**, "
                                f"and **not** '{tk} has no off-exchange volume'.")}
        return {"rows": [], "summary": f"No off-exchange records for {tk}."}
    wk = week or weeks[-1]
    if wk not in weeks:
        return {"weeks": weeks[-8:],
                "summary": f"No such week as {wk}. Available: {', '.join(weeks[-8:])}."}
    # ⚠️ **It goes through the same function as REST** (local denominator included) — the previous MCP version did not read the denominator,
    #    so for one symbol in one week the web page could give a share while the tool layer always returned null: two views, disagreeing.
    from app import _consolidated
    out = _consolidated(tk, wk, parsed)
    a, o = out["ats"], out["otc"]
    # ⚠️ `shares` may be null (an upstream field missing) — an unconditional `:,.0f` raises TypeError and
    #    loses the entire tool result, while REST and the UI display "—" quite happily.
    def _v(x: dict) -> str:
        sh = "—" if x["shares"] is None else f"{x['shares']:,.0f} shares"
        avg = ("" if x["avg_trade_size"] is None
               else f" (mean {x['avg_trade_size']:,.0f} shares/trade)")
        return f"{x['mpid'] or '(not disclosed)'} {(x['name'] or '')[:24]} {sh}{avg}"
    top = "; ".join(_v(v) for v in out["venues"]["ats"][:3]) or "none"
    return {
        **out, "ticker": tk, "weeks": weeks[-12:],
        "summary": (
            f"{tk}, week beginning {wk}: "
            f"**ATS (genuine dark pools) {a['shares']:,.0f} shares** ({a['firms']} firms, "
            f"{a['trades']:,.0f} trades); "
            f"**non-ATS off-exchange (wholesaler internalisation) {o['shares']:,.0f} shares** "
            f"({o['records']} records, of which {o['firms']} can be named). "
            + (f"⚠️ {a['null_share_records'] + o['null_share_records']} records "
               f"have a null volume and were excluded (not counted as 0), so the totals are correspondingly small. "
               if (a["null_share_records"] + o["null_share_records"]) else "")
            + "⛔ **These two numbers must not be added into a 'dark pool volume'** — internalisation is not a dark pool, "
            + f"and adding them more than doubles the figure. Leading ATS venues: {top}. "
            + f"⚠️ The data runs **about four weeks behind** (newest week {weeks[-1]}); it is after-the-fact statistics, not live monitoring. "
            + (f"Off-exchange share: ATS {out['share']['ats_pct']:.2f}%, "
               f"non-ATS {out['share']['otc_pct']:.2f}% "
               f"(the denominator being the sum of that week's daily volume accrued locally). "
               if out.get("share") else
               f"⚠️ **The off-exchange share cannot be computed**: {out['share_note']} ")
            + (f"⚠️ This fetch hit the {darkpool_src.MAX_LIMIT}-row limit, so the weekly series may be incomplete, "
               f"and since the endpoint cannot sort, which weeks were cut is unknowable. "
               if parsed.get("truncated") else "")
            + "The above presents public data and is not investment advice."),
    }


def _tool_get_stock(ticker: str) -> dict:
    from app import get_stock as _get
    out = _get(ticker)
    ok = [l for l in out["lanes"] if l["ok"]]
    miss = [l for l in out["lanes"] if not l["ok"]]
    have = "; ".join(
        f"{l['title']} ({l['as_of'] or 'instant unknown'}"
        + (f", {l['lag_days']} days ago)" if l["lag_days"] is not None else ")")
        for l in ok) or "none"
    # ⚠️ The missing ones must each give **their own reason**, and **must not be lumped together as "absent"** —
    #    `disabled` / `fetch_failed` / `no_mapping` all say "we cannot get it",
    #    and only `no_data` says "this symbol genuinely has no such record". The previous version mixed both into one
    #    "X lanes have none … none of which means no activity", which contradicted itself and was wrong at both ends.
    # ⚠️ Use `stock_parse.MEANS_ABSENT` rather than hardcoding "no_data" —
    #    add another "genuinely absent" reason code later and this follows automatically.
    truly_none = [l for l in miss if l["reason"] in stock_parse.MEANS_ABSENT]
    cant_get = [l for l in miss if l["reason"] not in stock_parse.MEANS_ABSENT]
    gone = ""
    if truly_none:
        gone += ("**Genuinely no record**: " + ", ".join(l["title"] for l in truly_none)
                 + " (these lanes really do have no such activity). ")
    if cant_get:
        gone += ("**We could not get**: " + "; ".join(
            f"{l['title']} ({l['reason_label'] or l['reason']})" for l in cant_get)
            + " — ⛔ these **do not mean** 'this symbol has no such activity'. ")
    spread = out.get("lag_spread_days")
    spread_txt = (
        f"⚠️ These data **span {spread['oldest'] - spread['newest']} days** "
        f"(the newest {spread['newest']} days old, the oldest {spread['oldest']}) — "
        f"**they are not contemporaneous**, so read each one's instant before stringing them into a story. "
        if spread else "")
    return {
        **out,
        "summary": (
            # ⚠️ The opening sentence must not say "X lanes have none" either — most of them are "we cannot get it".
            f"{out['ticker']}: {out['available']} lanes have data and "
            f"{out['unavailable']} are empty (reasons below; most are not absence). {spread_txt}"
            f"With data: {have}. "
            f"{gone}"
            f"⛔ This tool **produces no cross-source score**: weighting data of different instants and "
            f"definitions into one bullish-bearish number treats a position from three months ago and yesterday's "
            f"option volume as the same thing. The above presents public data and is not investment advice."),
    }


def _n(v: float | None, spec: str = ",.0f") -> str:
    """Null-safe number formatting.

    ⚠️ The parsing layer declares these fields `Optional[float]`, and both the API and the frontend render null as "—";
    only the tool layer wrote `f"{v:,.0f}"` directly — so one dataset displays under REST and crashes under MCP,
    which is precisely the "two views of one dataset disagreeing" this project keeps hitting.
    (A sample of 1,000 TFF periods showed no null fields, so this is an inconsistency **not yet triggered** rather than a certain crash.)
    """
    return "—" if v is None else format(v, spec)


def _tool_get_yield_curve(years: int = 3) -> dict:
    from datetime import date as _d
    y = _d.today().year
    wanted = list(range(y - max(1, min(years, 15)) + 1, y + 1))
    status = market_store.year_status(wanted)
    failed: dict[int, str] = {}
    missing: list[int] = []
    for yy in wanted:
        if not status[yy]["stale"]:
            continue
        try:
            if not market_store.save_year(yy, macro_src.yield_curve(yy)):
                missing.append(yy)
        except macro_src.DataNotAvailable:
            # ⚠️ "That year genuinely has none" has to be **carried out**: the REST view has missing_years,
            #    and swallowing it in the tool view silently shortens the window the AI thinks it has.
            missing.append(yy)
        except RuntimeError as e:
            failed[yy] = str(e)
    pts = [p for p in (market_parse.parse_curve(r)
                       for r in market_store.load_years(wanted)) if p]
    if not pts:
        return {"error": "could not fetch yield data" + (f": {failed}" if failed else ""),
                "note": "This is **a failed fetch**, not 'there are no yields'."}
    out = market_parse.curve_series(pts)
    L = out["latest"]
    # ⚠️ Three states, not two: inverted / not inverted / **not computable** (a tenor missing that day).
    #    Flattened to two, a missing value outputs "all three spreads are positive" — stating an unknown as a fact.
    inv = [k for k, v in L["inverted"].items() if v is True]
    pos = [k for k, v in L["inverted"].items() if v is False]
    unk = [k for k, v in L["inverted"].items() if v is None]
    parts = []
    if inv:
        parts.append(", ".join(f"{k}={_n(L['spreads'][k], '+.2f')}%" for k in inv) + " negative")
    if pos and not inv:
        parts.append(f"{len(pos)} spreads positive")
    elif pos:
        parts.append(f"the other {len(pos)} positive")
    if unk:
        parts.append(f"{', '.join(unk)} **missing a tenor that day, so not computable**")
    inv_txt = "; ".join(parts) if parts else "no spread available"
    return {
        **out, "failed": failed, "missing_years": missing,
        "summary": (
            f"As of {L['date']}: 10Y {L['yields'].get('10Y')}%, "
            f"2Y {L['yields'].get('2Y')}%, 3M {L['yields'].get('3M')}%. "
            f"Spreads: 10Y-2Y {_n(L['spreads']['10Y-2Y'], '+.2f')}%, "
            f"10Y-3M {_n(L['spreads']['10Y-3M'], '+.2f')}% ({inv_txt}). "
            + (f"⚠️ Treasury has no data for {', '.join(map(str, missing))} "
               f"(which is different from a failed fetch). " if missing else "")
            + (f"⚠️ {', '.join(map(str, failed))} **failed to fetch**, "
               f"so what follows uses local data that may not be current: {failed}. " if failed else "")
            + "⚠️ The two definitions of inversion can invert months apart, so any conclusion has to say which one it used. "
              "The above presents public US Treasury data and is not investment advice."),
    }


def _tool_get_cot(market: str | None = None, periods: int = 12) -> dict:
    """CFTC positioning.

    ⚠️ **Pin the contract down first, then take the time series.** Going straight to a keyword LIKE has two traps:
    ① one keyword can match several contracts ("E-MINI S&P 500" matches both E-MINI and MICRO E-MINI),
       and taking N rows by descending date leaves which contract comes first on a given day **arbitrary** —
       answering a question about E-MINI with MICRO's numbers is worse than an error.
    ② limit is a **total row count**, so with several contracts matched, N rows scatter across them
       and never assemble into a time series.
    """
    periods = max(1, min(periods, 200))
    ms = macro_src.cot_markets()
    latest = max((m["last_date"] or "" for m in ms), default="")
    live = [m for m in ms if m["last_date"] == latest]

    if not market:
        return {"markets": [m["market"] for m in live], "count": len(live),
                "latest_report": latest, "notes": market_parse.NOTES,
                "summary": (f"TFF currently reports {len(live)} contracts (out of {len(ms)} recorded; "
                            f"the rest have stopped updating). The newest report is {latest}. "
                            f"Pass the market parameter for a specific contract's positioning.")}

    key = market.strip().upper()
    matches = [m for m in ms if key in m["market"].upper()]
    exact = [m for m in matches if m["market"].upper() == key]
    if exact:
        chosen = exact[0]
    elif len(matches) == 1:
        chosen = matches[0]
    elif matches:
        # Several matches → **do not choose for the caller**; hand back the candidates with each one's last report
        return {"candidates": [{"market": m["market"], "last_date": m["last_date"],
                                "reports": m["reports"]} for m in matches[:25]],
                "count": 0, "rows": [], "notes": market_parse.NOTES,
                "summary": (
                    f"'{market}' matches {len(matches)} contracts, "
                    f"and **none was chosen for you** (they are different contracts and their numbers do not mix): "
                    + ", ".join(m["market"] for m in matches[:6])
                    + ("…" if len(matches) > 6 else "")
                    + ". Call again with the full contract name.")}
    else:
        return {"rows": [], "count": 0, "candidates": [],
                "summary": f"No contract matches '{market}'. Omit market to get the full list."}

    name = chosen["market"]
    stale = chosen["last_date"] != latest
    raw = macro_src.cot_rows(limit=periods, market_contains=name, exact=True)
    rows = [market_parse.cot_to_dict(c)
            for c in (market_parse.parse_cot(r) for r in raw) if c]
    if not rows:
        return {"rows": [], "count": 0, "market": name,
                "summary": f"{name} has no records."}
    r0 = rows[0]
    # ⚠️ A contract that has stopped updating has to be flagged — or the user takes a 2022 figure for a current position
    stale_txt = (f"⚠️ This contract **has stopped updating**; its last report is {chosen['last_date']} "
                 f"(the newest in the whole dataset being {latest}). " if stale else "")
    return {
        "rows": rows, "count": len(rows), "market": name,
        "last_date": chosen["last_date"], "stale": stale,
        "notes": market_parse.NOTES,
        "summary": (
            f"{stale_txt}{name} as of {r0['report_date']} (Tuesday's close): "
            f"leveraged funds net {_n(r0['lev_net'])} contracts "
            f"({_n(r0['lev_long'])} long / {_n(r0['lev_short'])} short), "
            f"asset managers net {_n(r0['asset_net'])} contracts, "
            f"and total open interest {_n(r0['open_interest'])} contracts. "
            f"⚠️ CFTC runs **three days behind**: this is Tuesday's state, published on Friday, and not the present. "
            f"The above presents public CFTC data and is not investment advice."),
    }


_IMPL: dict[str, Callable[..., dict]] = {
    "get_short_fails": _tool_get_short_fails,
    "get_institution_holdings": _tool_get_institution_holdings,
    "get_institution_changes": _tool_get_institution_changes,
    "get_insider_trades": _tool_get_insider_trades,
    "get_insider_summary": _tool_get_insider_summary,
    "get_congress_trades": _tool_get_congress_trades,
    "get_congress_summary": _tool_get_congress_summary,
    "get_gex": _tool_get_gex,
    "get_gex_curve": _tool_get_gex_curve,
    "get_option_chain_summary": _tool_get_option_chain_summary,
    "get_option_flow": _tool_get_option_flow,
    "get_oi_change": _tool_get_oi_change,
    "scan_market": _tool_scan_market,
    "get_darkpool": _tool_get_darkpool,
    "get_stock": _tool_get_stock,
    "get_yield_curve": _tool_get_yield_curve,
    "get_cot": _tool_get_cot,
}


def exec_tool(name: str, args: dict[str, Any]) -> dict:
    """The single execution entry point. Exceptions become {"error": ...} rather than being raised —
    an MCP or function-calling caller needs a structured error, not a dropped connection.

    ⚠️ **Every** "genuinely absent" class has to be listed here, and listed first. There is one per
    source package rather than one shared class, and all of them subclass `RuntimeError`, so any that
    is left out does not go uncaught — it falls through to the network branch and is reported as
    "Fetch failed". A caller told that retries, and a model told that says the fetch broke, when in
    fact the Treasury simply never published that year. That is rule one running backwards: the thing
    that does not exist, dressed up as the thing we could not reach.
    """
    fn = _IMPL.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except (cboe.DataNotAvailable, edgar_src.DataNotAvailable,
            congress_src.DataNotAvailable) as e:
        return {"error": f"No data: {e}"}
    except (ValueError, TypeError) as e:
        return {"error": f"Bad parameter: {e}"}
    except RuntimeError as e:
        return {"error": f"Fetch failed: {e}"}
