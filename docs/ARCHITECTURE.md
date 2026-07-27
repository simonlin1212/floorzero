# FloorZero module design (measured against Unusual Whales, in full)

> Written 2026-07-26 · named **FloorZero** on 2026-07-27 (working name Vibe-Flow until then).
> The idea: **an open-source Unusual Whales** — giving away in code what UW charges $29-99 a month for.
> The compliance foundation: **distribute code only, and the user runs it themselves** (the data lands on their machine) → we are never an OPRA redistributor.

---

## 1. What we are measuring against (api.unusualwhales.com/docs, read 2026-07-26)

UW's public API: **35 categories, 197+ endpoints**. The spread:

| Category | Endpoints | Category | Endpoints |
|---|---|---|---|
| Stock | **37** | Predictions | 9 |
| WebSocket | 14 | Private Markets | 9 |
| Market | 12 | Institution | 7 |
| Gex/Greeks | **11** | Short | **7** |
| Option-Contract | 7 | Volatility | 6 |
| Alerts / Companies / ETFs / Intel / Option-Trade / Politician Portfolios | 5 each | Congress / Crypto / Insiders / Seasonality / Unusual Trades | 4 each |
| Earnings / Forex / Screener | 3 each | Darkpool / Group Flow / Lit-Flow / Option Trades / Digital Currencies / POTUS | 2 each |
| Commodities / Economy / News / Stock-Directory | 1 each | | |

---

## 2. ⭐ The central judgement: UW's moat is not exclusive data

**Measured 2026-07-26, the sources behind UW's headline features are nearly all free and public:**

| UW's selling point | Source | Result |
|---|---|---|
| Options flow / GEX / greeks | **Cboe official delayed** | ✅ already working in global-stock-data |
| Darkpool | **FINRA ATS API** | ✅ works (returns symbol, weekly trade count, volume) |
| Congressional trades | **House disclosure ZIP** | ✅ works (52KB, downloadable directly) |
| Insiders | **SEC EDGAR Form 4** | ✅ working (547 filings in a day) |
| Institutional holdings | **SEC EDGAR 13F** | ✅ working (261 filings in a day) |
| Shorts | **FINRA Reg SHO + SEC FTD** | ✅ working (12,112 symbols market-wide) |
| Earnings / fundamentals | **SEC EDGAR XBRL** | ✅ working |
| Predictions | **Polymarket + Kalshi** | ✅ already covered by globalpercent |
| Economy | **Treasury / CFTC** | ✅ working |

**→ UW's real moat is integration, processing, interface and accrued history — and open source is at its best against the first three.**

---

## 3. Module design (organised by data source, rather than copying UW's categories)

UW's categories are cut by business function, so several of them hit the same source. This is layered **by data source**, with **feature modules** on top — one dataset feeding several features, which avoids fetching the same thing twice (and falls naturally in line with the rate limits).

```
┌────────────────────────────────────────────────────────────┐
│  L4  exits        Web UI · MCP · REST · alerts             │
├────────────────────────────────────────────────────────────┤
│  L3  features     scanner/GEX/darkpool/congress/insiders…  │
├────────────────────────────────────────────────────────────┤
│  L2  processing   compute · aggregate · flag · accrue      │
├────────────────────────────────────────────────────────────┤
│  L1  sources      Cboe/FINRA/SEC/House/Treasury/CFTC/PM    │
└────────────────────────────────────────────────────────────┘
```

### L1 sources `backend/sources/`

| File | Source | Tier | Provides |
|---|---|---|---|
| `cboe.py` | Cboe delayed options | C (personal research) | option chain · greeks · IV · OI |
| `finra.py` | FINRA Reg SHO + ATS | B | short volume · dark pools |
| `edgar.py` | SEC EDGAR | **S** | Form 4 · 13F · 13D/G · XBRL · full-text search |
| `congress.py` | House Clerk + Senate eFD | **S⁻** ⚠️no commercial use | congressional trade disclosures (5 USC §13107(c)) |
| `macro.py` | Treasury + CFTC + Nasdaq | **S/C** | yields · COT · earnings calendar |
| `quotes.py` | multi-source quotes | C | quotes · bars |
| `predmkt.py` | Polymarket + Kalshi | — | prediction markets (reusing globalpercent) |

> ⭐ Reuse `global-stock-data` V2.0's already-verified code directly; do not rewrite it.

### L2 processing `backend/modules/`

This is **where UW actually earns its money**, and where the work is here:

| Module | What it does | UW counterpart |
|---|---|---|
| `greeks.py` | **GEX gamma exposure**: Σ(gamma×OI×100×spot²×0.01), aggregated by strike/expiry; vanna/charm | Gex/Greeks (11) |
| `flow.py` | **Unusual activity**: vol/OI>1, sweep detection, block tiering, P/C ratio, net delta exposure | Option-Trade + Flow (12) |
| `scanner.py` | **Market-wide scanning**: screening across symbols (UW's headline feature) | Screener + Hottest Chains (3) |
| `darkpool.py` | ATS volume aggregation, off-exchange share, unusual volume | Darkpool + Lit-Flow (4) |
| `insider.py` | Form 4 parsing, buy/sell classification, sector flow | Insiders (4) |
| ✅ `institution.py` | 13F parsing, position changes, manager profiles | Institution (7) |
| `congress.py` | Congressional trade parsing, late-disclosure detection, member portfolios | Congress + Politician (9) |
| `shorts.py` | Short volume ratio, FTD, trend | Short (7) |
| `vol.py` | IV rank/percentile, term structure, skew, variance risk premium | Volatility (6) |
| `seasonality.py` | Monthly and annual seasonality | Seasonality (4) |
| `fundamental.py` | Financial statements, earnings calendar, earnings history | Companies + Stock financials (12) |
| `tide.py` | Market tide, sector net flow, OI change | Market (12) |
| `history.py` | **Locally accrued history** (accruing from installation) | UW's Data Shop |

### L3 exits

| Exit | Notes |
|---|---|
| **Web UI** | React 19 + Vite + Tailwind + ECharts (reusing Vibe-Trading's visual language) |
| ⭐ **MCP server** | Lets anyone's Claude or GPT ask directly — **UW's MCP is behind their paywall; ours is free** |
| **REST API** | FastAPI, local |
| **Alerts** | A local rule engine (against UW's 5 Alerts endpoints) |

---

## 4. Frontend sections (against UW's own navigation)

Reusing Vibe-Trading's sidebar pattern, with 10 main sections:

| # | Section | Contents | Source |
|---|---|---|---|
| 1 | **Flow** | live unusual activity, sweeps, blocks, filtered by symbol/sector | Cboe |
| 2 | ✅ **GEX** | GEX levels, distribution by strike/expiry, spot GEX | Cboe, computed here |
| 3 | **Scanner** | market-wide screening (IV rank / OI change / unusual ratio) | Cboe + quotes |
| 4 | **Darkpool** | ATS volume, off-exchange share, anomalies | FINRA |
| 5 | ✅ **Congress** ⚠️no commercial use | member trades, late disclosure, portfolios | House/Senate |
| 6 | ✅ **Insiders** | Form 4 flow, buy/sell classification, cluster buying | EDGAR |
| 7 | ✅ **Institutions** | 13F holdings, quarter-on-quarter change, manager profiles | EDGAR |
| 8 | ✅ **Shorts** | SEC FTD (main) · FINRA off-exchange short volume (optional, off by default) | SEC + FINRA |
| 9 | **Market** | tide, sectors, earnings calendar, macro | several |
| 10 | **Stock** | one symbol in full (chain / GEX / financials / filings / shorts) | all |

---

## 5. ⚠️ Compliance rules (fixed, not negotiable)

1. **Distribute code, never host data.** The user clones it, runs it, and the data lands on their machine → they are personal use, and we are not a redistributor.
2. ⛔ **Never build an online demo.** The moment we host a site showing options data we become an OPRA redistributor (**$1,500/month**). Promotion is screenshots, screen recordings and the README, nothing else.
3. **Output data, not conclusions.** Show GEX values and unusual-activity rankings; **attach no buy/sell label, give no levels, produce no subjective score**.
4. **Each source's compliance tier goes in the UI and the docs** (S/B/C, with the terms verbatim) — that is our differentiator and our own protection.
5. **Users supply their own API keys** where a source needs one; none is ever embedded.

---

## 6. ⚠️ Three known hard problems (recorded honestly)

1. **History is the weak point.** UW has been running for years and has accrued it; a self-hosting user starts on **day one with none**.
   → Mitigation: `history.py` accrues from installation; EDGAR and FINRA carry their own history and can be backfilled; option chain history genuinely cannot be backfilled.
2. **Real-time streaming is out of reach.** UW has 14 WebSocket endpoints on the live tape, which needs the OPRA real-time feed (paid).
   → We do **delayed** only (Cboe's free delayed data is enough for research, not for chasing fills).
3. **The sheer volume.** Matching 197 endpoints in one go is unrealistic — **deliver section by section**, each one a milestone that can be accepted on its own.

## 7. What we are not doing

| UW has | We skip | Why |
|---|---|---|
| Crypto (4) + Digital Currencies (2) | ❌ | a hard line here: no crypto |
| Private Markets (9) | ❌ | the sources are paid or hard to obtain |
| Forex (3) / Commodities (1) | later | not the core audience |
| WebSocket real-time (14) | ❌ | needs the OPRA real-time feed (paid) |
| POTUS (2) | later | peripheral, and political (a hard line) |

**Leaving ≈ 165 endpoints across 30 categories.**

---

## 8. Stack (carrying over what is already proven)

- **Backend**: Python + FastAPI (following VibeResearch's open-source modular pattern)
- **Frontend**: React 19 + Vite 6 + TS + Tailwind 3.4 + ECharts 6 + zustand + react-router 7
- **Visual**: the same as Vibe-Trading (dark, with vermilion `#F35D2B`)
- **Storage**: SQLite (locally accrued history, no configuration)
- **AI exit**: MCP (stdlib JSON-RPC, following the `mcp_server.py` pattern)
- **Dependency principle**: as few as possible, `requests` first (continuing global-stock-data's "no auth, works out of the box")
