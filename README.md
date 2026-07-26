<p align="center"><b>English</b> | <a href="README_zh.md">简体中文</a></p>

<h1 align="center">vibe-flow</h1>

<p align="center">
  <b>Self-hosted market data analysis. Ten sections, ten free public sources, your machine.</b><br>
  Options flow · GEX · Screener · Dark pools · Congress · Insiders · 13F · Short data · Macro · MCP
</p>

<p align="center">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3776AB">
  <img alt="React" src="https://img.shields.io/badge/react-19-61DAFB">
  <img alt="Sections" src="https://img.shields.io/badge/sections-10-ff5a1f">
  <img alt="MCP tools" src="https://img.shields.io/badge/MCP%20tools-17-ff5a1f">
</p>

<p align="center">
  <a href="#what-this-is">What this is</a> ·
  <a href="#why-you-run-it-yourself">Why you run it yourself</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#the-ten-sections">The ten sections</a> ·
  <a href="#data-sources-and-their-licences">Data sources</a> ·
  <a href="#what-it-refuses-to-do">What it refuses to do</a> ·
  <a href="#mcp">MCP</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

---

## Screenshots

There is no demo site — that constraint is the whole point — so these are the only way to
see it before running it. They illustrate the software; they are not a data service.

**Market** — Treasury and CFTC, the one lane with no licence attached at all. Two
inversion definitions side by side, because they cross zero months apart and "the curve
inverted" means nothing without saying which one.

![Market section](docs/screenshots/market.png)

**Stock** — nine lanes on one ticker, sorted by how old each one is. The banner reports
the span between newest and oldest; here, 126 days. Two lanes are empty and each says why
it is empty rather than showing a blank.

![Stock section](docs/screenshots/stock.png)

**Scanner** — the IV Rank column reads "59 more sessions" all the way down, because the
history it needs cannot be backfilled and had not accrued yet. That column is the whole
argument of this project in one screenshot: the honest output of an unavailable metric is
not a number.

![Scanner section](docs/screenshots/scanner.png)

## What this is

A local market-data workbench covering roughly what a paid options-flow service covers,
built entirely on free public sources. You clone it, you run it, the data lands on your disk.

It is **not** a data service, a SaaS, or a hosted dashboard. There is no demo site and
there will not be one — see below for why that is a design constraint rather than laziness.

The intended user is someone who wants to run their own analysis, build their own tools,
or feed the numbers to an LLM. It is not a "open it and watch the tape" app; brokers do
that better and for free.

## Why you run it yourself

The single decision that shapes this entire project: **we distribute code, never data.**

Cboe's delayed options feed carries OPRA data. OPRA's rule is blunt — *if you show OPRA
data externally in an app, tool, or website, you are a redistributor* — and that is
**$1,500/month**, with no exemption for free, open-source, or non-commercial use.

Ship a hosted dashboard and the project owes that fee. Ship code that each user runs on
their own machine, and each user is doing personal research. So:

- Backend binds to `127.0.0.1` by default.
- No demo site, ever. Promotion is screenshots and README.
- Every source is tagged with its compliance tier in the sidebar, not buried in a footnote.

## Quick start

```bash
git clone <this repo> && cd vibe-flow

# Backend
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env        # then set VF_CONTACT — see below
VF_CONTACT="Your Name you@example.com" python -m uvicorn app:app --host 127.0.0.1 --port 8920

# Frontend (another terminal)
cd frontend && npm install && npm run dev
```

`VF_CONTACT` is **required and has no default**. SEC and the congressional disclosure
sites ask for a User-Agent that identifies who is calling. Rather than ship a placeholder —
which would get you rate-limited without you ever knowing why — the program refuses to
start until you set it. It identifies *you* to those sites and is sent nowhere else.

Python 3.9 or newer (`zoneinfo`); a startup guard checks this and says so plainly.

## The ten sections

| Section | What it shows | Source |
|---|---|---|
| **Stock** | One ticker across all nine other lanes, each labelled with its own age | all |
| **Flow** | vol/OI outliers, put/call under three definitions, absolute delta exposure, local OI accrual | Cboe |
| **GEX** | Gamma exposure, flip, call/put walls, vanna and charm, expiry × strike surface | Cboe |
| **Scanner** | Market-wide screening on IV rank, volume multiple, price | Cboe |
| **Darkpool** | ATS venues and non-ATS internalisation, kept apart | FINRA |
| **Congress** | House and Senate PTR filings, disclosure delay | House/Senate |
| **Insiders** | Form 4, open-market trades separated from compensation | SEC EDGAR |
| **Institutions** | 13F holdings and quarter-over-quarter changes | SEC EDGAR |
| **Shorts** | SEC fails-to-deliver; FINRA off-exchange volume optional | SEC / FINRA |
| **Market** | Treasury yield curve with two inversion definitions, CFTC positioning | Treasury / CFTC |

## Data sources and their licences

Each source carries a tier, shown in the UI next to every section:

| Tier | Source | Commercial use | Redistribution |
|---|---|---|---|
| **S** | SEC EDGAR · Treasury · CFTC | Yes | Yes |
| **S−** | Congressional disclosures | **Prohibited by statute** | Yes (public record) |
| **B** | FINRA (Reg SHO, ATS) | Non-commercial only | No |
| **C** | Cboe | Requires authorisation | No |

Two of these deserve their own paragraph.

**Congressional filings are public record, but 5 U.S.C. §13107(c)(1)(B) makes it unlawful
to obtain or use them "for any commercial purpose"** (news media excepted), with penalties
to $10,000. Free, open-source, self-hosted personal research is fine. Any paid product or
commercial service must not include this lane. This is *not* the same as EDGAR, which
constrains request rate and User-Agent but not commercial use.

**FINRA is off by default** (`VF_ENABLE_FINRA=1` to enable). Their Terms of Use permit
"ONLY your own non-commercial personal or professional use" and restriction (d) forbids
"develop or create a database of data using the FINRA Website" — which is precisely what
downloading into SQLite does. There is genuine ambiguity (the terms name FINRA.**org**
while the data files sit on `cdn.finra.org`). We do not interpret those terms for you: the
text is reproduced verbatim in the UI, the switch is yours. No section uses FINRA as its
only source.

## What it refuses to do

These are deliberate and load-bearing.

**No directional labels on options flow.** A bullish/bearish tag requires knowing whether
a trade hit the ask or the bid, which requires the per-trade tape, which requires OPRA.
The free feed is a chain *snapshot*: cumulative daily volume, open interest, quotes,
greeks. So sweep detection, block classification, aggressor side and open-versus-close are
all out of reach, and the section says so at the top rather than guessing.

**No number where a number cannot be computed.** IV Rank needs a year of history that Cboe
does not serve, so before sixty sessions have accrued the field is null and says how many
days remain — never a figure derived from twenty days, which would look equally
authoritative while measuring something else. The same rule holds throughout: missing
greeks, absent quotes, unfetched days and expired contracts all render as unavailable with
a reason, not as zero.

**No cross-source score.** The Stock page shows nine lanes whose ages differ by two orders
of magnitude — measured on NVDA, chain data two days old and the latest Form 4 a hundred
and twenty-eight. Compressing that into one bullish-bearish number treats a
quarter-old position as contemporary with yesterday's option volume.

**No conclusions at all**, in fact. The output is data and arithmetic. There are no
buy/sell labels, no price targets, no predictions.

## History you have to accrue

Two datasets cannot be backfilled, because Cboe serves only the present:

- **Open interest by contract** — its day-over-day change is the cleanest evidence of new
  positioning, and unlike the tape it needs no guess about direction.
- **iv30 by symbol** — without it there is no IV Rank.

Both start accumulating the day you install and are worth more the longer you run it.
EDGAR and FINRA lanes carry their own history and can be backfilled at any time.

## MCP

Seventeen tools, defined once in `backend/tools.py` and inherited by the MCP server, so
REST and MCP can never drift apart. Point any MCP client at `backend/mcp_server.py`.

Tool summaries carry the same caveats the UI does — an assistant asking for options flow
is told, in the response, that direction cannot be inferred from this data.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

48 tests, none of which touch the network. What they cover is the arithmetic and the
semantics — the invariants that adversarial review kept catching in the first place:

- a value that cannot be computed never renders as zero, in each of the shapes that
  failure took (missing greeks on one side only, absent quotes, a zero denominator,
  a zero-median volume history, a spread whose tenor is missing)
- the reason a value is absent survives to the caller, since `iv_rank` can be null for
  three distinct causes and only one of them is "wait a few more days"
- local history is keyed by trading session, so the same Friday close cannot become two
  observations; IV history is bounded by the requested session, so a past scan cannot
  borrow from the future
- expired contracts are not net closings, and contracts missing from an incomplete fetch
  are not positions gone to zero
- aggregate rows and per-firm rows are never summed together, which would double
- one lane failing does not take down the stock page

The suite was reviewed the same way the code was, and four cases came back as false
positives — they passed against deliberately broken implementations. Archiving under the
wall-clock date went undetected because both calls in the test happened on the same day;
a dropped row was checked by the function's own counter rather than by querying the
table; a full-segment replace was verified through a summary the same module produced;
and "only no_data means absent" was enforced by looking for a Chinese word in the label
text. All four now assert against the stored rows or against a constant the code exports
(`MEANS_ABSENT`), which the UI and MCP layer read too rather than each guessing.

Both rounds were then mutation-checked — ten invariants broken on purpose, ten caught.

Tests use a throwaway `VF_DATA_DIR`, and the fixture asserts the resolved database path
really is inside it before letting anything run. That guard is not ceremony: if `db.py`
ever stopped honouring the variable, the tests would silently write into history that
cannot be rebuilt.

## Architecture

```
backend/sources/    cboe · edgar · edgar13f · congress · shorts · darkpool · macro · contact
backend/modules/    greeks · bs · flow · scanner · darkpool · insider · institution ·
                    congress · shorts · market · stock · history · *_store · *_sync
backend/            app.py (FastAPI) · tools.py (single tool definition) · mcp_server.py
frontend/src/pages  ten sections
docs/               模块设计.md (architecture) · 开发日志.md (build log, Chinese)
```

Python 3.9+ · FastAPI · React 19 · Vite · Tailwind · ECharts · SQLite.
Four backend dependencies. Data lives in `~/.vibe-flow/`, outside the repo, so updating
the code never costs you the history you have accrued.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Disclaimer

This software presents publicly available data and arithmetic derived from it. It is not
investment advice, and the author is not a licensed financial adviser. Every source has
limits — delays, revisions, definitional quirks — and the software tries hard to state
them, but you are responsible for what you conclude and for complying with the terms of
each data source in your jurisdiction and use case.

## Support

<p align="center">
  <a href="https://buymeacoffee.com/simonlin1212"><img src="./assets/bmc-qr.png" width="180" alt="Buy Me a Coffee"></a>
</p>

## License

MIT — see [LICENSE](LICENSE).

**Author:** Simon Lin · X [@linsizhen](https://x.com/linsizhen) · Email: [simonlin0423@gmail.com](mailto:simonlin0423@gmail.com)
