<p align="center"><b>English</b> | <a href="README_zh.md">简体中文</a></p><!-- cn-ok: language switcher -->

<h1 align="center">FloorZero</h1>

<p align="center">
  <b>Your own trading floor. Ten sections, zero hosted data.</b><br>
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
  <a href="#roadmap">Roadmap</a> ·
  <a href="#data-sources-and-their-licences">Data sources</a> ·
  <a href="#what-it-refuses-to-do">What it refuses to do</a> ·
  <a href="#use-it-from-an-ai-assistant">Use it from an AI assistant</a> ·
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
or feed the numbers to an LLM. It is not an "open it and watch the tape" app; brokers do
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
git clone <this repo> && cd floorzero

# Backend
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env        # then set FZ_CONTACT — see below
FZ_CONTACT="Your Name you@example.com" python -m uvicorn app:app --host 127.0.0.1 --port 8920

# Frontend (another terminal)
cd frontend && npm install && npm run dev
```

`FZ_CONTACT` is **required and has no default**. SEC and the congressional disclosure
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

## Roadmap

**This is 0.1.0 — a usable start, not a finished product.** Ten sections work today; the
version number will say how far along it is. Each release that adds a section moves the
minor version by one, and **1.0 is the complete one**.

| Version | Adds | Why it is not here yet |
|---|---|---|
| **0.1** ✅ | Stock · Flow · GEX · Scanner · Darkpool · Congress · Insiders · Institutions · Shorts · Macro | — |
| 0.2 | **Historical context for what is already here** — percentile over the last 60/252 sessions, 1-day and 5-day change, the symbol's own range; for GEX, IV and option volume | The commonest gap: `Total GEX −6.44B` on its own cannot be judged. Extreme, or an ordinary Tuesday for SPY? Waits on local history accruing |
| 0.3 | **Volatility term structure** — ATM IV by expiry, IV against realised, front/back spread, earnings date marked | Without it the options pages show positioning but not price. The realised leg needs daily closes kept locally |
| 0.4 | **Earnings and fundamentals** — statements, calendar, surprise history | EDGAR XBRL is a different shape from the filing index already wired up |
| 0.5 | **Saved screens, and a scanner useful on day one** — cross-sectional IV30 percentile, IV30/RV20, option volume against its own median | Ranking a symbol against the market can be had from one scan; ranking it against its own past cannot |
| 0.6 | **Sector-relative flow and IV** | Cheap per symbol, expensive across the market; wants the scanner's batch pass first |
| 0.7 | **Seasonality** — monthly and annual, per symbol | Wants years of local history rather than a fetch |
| 0.8 | **Prediction markets** — Polymarket and Kalshi as a macro overlay | Working elsewhere; needs porting, not inventing. Deliberately last: it is the loosest fit with the rest, and the easiest way for a data tool to drift into being a news feed |
| **1.0** | Complete | |

⭐ This order was revised after a review by a US options trader, who put it plainly: what is
needed next is not an eleventh data source, it is making the numbers already here comparable.
**"Is this figure unusual?" is a question about the data, not a view on the market** — so
percentiles, z-scores and change-over-time sit comfortably inside the no-conclusions rule.
What stays out is the composite: no score folding a 130-day-old Form 4 in with yesterday's
option volume.

Anything not on this list is not planned. That is deliberate, and it is the same promise as
the reason codes: **be clear about what this does not do.**

⚠️ Dates are absent on purpose. This is one person's side project, and a roadmap with dates
on it would be the first thing in this README to become untrue.

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
of up to $10,000. Free, open-source, self-hosted personal research is fine. Any paid product or
commercial service must not include this lane. This is *not* the same as EDGAR, which
constrains request rate and User-Agent but not commercial use.

**FINRA is off by default** (`FZ_ENABLE_FINRA=1` to enable). Their Terms of Use permit
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
and twenty-eight days old. Compressing that into one bullish-bearish number treats a
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

## Use it from an AI assistant

Seventeen tools over MCP, defined once in `backend/tools.py` and inherited by the MCP
server, so the HTTP API and the tool layer can never drift apart.

**The assistant runs where you already have it** — Claude Code, Claude Desktop, or any MCP
client — and pulls from the server on your machine. There is no chat panel inside this app,
and that is deliberate: a panel would mean posting your local data to somebody's API, and
the sidebar's promise that your data stays on your own machine would stop being true.

> 💡 You can hand this whole section to your assistant and ask it to set the thing up. It is
> written to be followed literally: every path is absolute, and the verification step is at
> the end.

### Claude Code

```bash
claude mcp add floorzero --env FZ_CONTACT="Your Name you@example.com" -- /absolute/path/to/python /absolute/path/to/FloorZero/backend/mcp_server.py
```

### Claude Desktop

Edit `claude_desktop_config.json` — on macOS at
`~/Library/Application Support/Claude/claude_desktop_config.json`, on Windows at
`%APPDATA%\Claude\claude_desktop_config.json` — and restart the app:

```json
{
  "mcpServers": {
    "floorzero": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/FloorZero/backend/mcp_server.py"],
      "env": { "FZ_CONTACT": "Your Name you@example.com" }
    }
  }
}
```

Three things that decide whether this works first try:

- **Absolute paths, both of them.** The client starts the server from its own working
  directory, not from the repository.
- **The Python must be the one with the dependencies installed** — the virtualenv's
  `bin/python`, not the system `python3`, unless you installed `requirements.txt` globally.
- **`FZ_CONTACT` has no default.** Reading what you have already synced works without it, so
  a missing contact does not fail at startup — it surfaces later as a fetch error that reads
  like a data problem. Set it here and it cannot bite you.

### Check that it worked

Ask the assistant:

> Which FloorZero tools do you have?

Seventeen names should come back. If none do, the server did not start: run the same command
by hand in a terminal, and the error will be on stderr rather than swallowed by the client.

### What it is actually good at

The tools are worth pointing at questions that need a *definition* to be answered correctly,
which is where most tools quietly get it wrong:

> Has anyone at NVDA bought on the open market recently — real purchases, not option
> exercises or grants?

> Where is SPY's gamma flip, and which strikes are the call and put walls?

> Which members of Congress filed more than 45 days after the trade this quarter?

> What did institutions do with NVDA quarter on quarter — new positions, added, exited?

Every response carries its own caveats: how old the data is, what the sample covers, and
where a figure is an estimate. An assistant asking for options flow is told, in the reply,
that direction cannot be inferred from this data — so the limits travel with the numbers
instead of being dropped on the way.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

52 tests, none of which touch the network. What they cover is the arithmetic and the
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
and "only no_data means absent" was enforced by looking for a particular word in the label
text. All four now assert against the stored rows or against a constant the code exports
(`MEANS_ABSENT`), which the UI and MCP layer read too rather than each guessing.

Both rounds were then mutation-checked — ten invariants broken on purpose, ten caught.

Tests use a throwaway `FZ_DATA_DIR`, and the fixture asserts the resolved database path
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
docs/               ARCHITECTURE.md · screenshots/
```

Python 3.9+ · FastAPI · React 19 · Vite · Tailwind · ECharts · SQLite.
Four backend dependencies. Data lives in `~/.floorzero/`, outside the repo, so updating
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
