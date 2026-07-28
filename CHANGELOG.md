# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

Build notes for each section — sources probed, traps measured, defects found and
fixed — are kept in the module docstrings, beside the code they explain.

## [0.1.0] — 2026-07-27

First complete version. Ten sections, seventeen MCP tools, ten public data sources.

### Sections

- **Stock** — one ticker across the other nine lanes, each carrying its own `as_of`
  and lag; the span between newest and oldest is printed at the top. No cross-source
  score is produced.
- **Flow** — vol/OI outliers, put/call under three definitions, absolute delta
  exposure, day-over-day open-interest accrual. No directional labels.
- **GEX** — gamma exposure, flip, call/put walls, vanna and charm (validated against
  finite differences), expiry × strike surface, local snapshot history.
- **Scanner** — market-wide screening as a background job with progress; IV Rank and
  IV percentile once sixty sessions have accrued.
- **Darkpool** — ATS venues and non-ATS internalisation reported separately, with
  per-firm rows reconciled against aggregate rows.
- **Congress** — House and Senate PTR filings, disclosure delay, scanned-PDF accounting.
- **Insiders** — Form 4 with open-market trades separated from compensation.
- **Institutions** — 13F holdings and quarter-over-quarter changes; issuer names taken
  from the official 13(f) list.
- **Shorts** — SEC fails-to-deliver, with FINRA off-exchange volume behind a switch.
- **Market** — Treasury yield curve under both inversion definitions, CFTC positioning.

### Design decisions worth stating

- **Code is distributed, data is not.** Backend binds to localhost; there is no demo
  site. Showing OPRA data externally would make the project a redistributor
  (US$1,500/month, no exemption for free or open-source use).
- **`FZ_CONTACT` is required with no default.** SEC and congressional sites want a
  User-Agent identifying the caller; shipping a placeholder would attribute every
  user's traffic to the author and get people rate-limited without explanation.
- **FINRA is off by default** (`FZ_ENABLE_FINRA=1`), with its terms reproduced verbatim
  in the UI. No section uses FINRA as its only source.
- **Congressional data is tagged S− and excluded from any commercial use**, per
  5 U.S.C. §13107(c)(1)(B).
- **A value that cannot be computed is never rendered as zero.** Missing greeks, absent
  quotes, unscanned days, expired contracts, insufficient history and disabled sources
  each carry their own reason code.
- **Open interest and iv30 accrue locally** and cannot be backfilled — Cboe serves only
  the present. Both are keyed by the data's own trading session, not wall-clock date.
- Data lives in `~/.floorzero/`, outside the repository.

### Tests

52 tests, no network. They pin the invariants that review kept finding: unavailable
never rendering as zero (in each shape it took), reason codes surviving to the caller,
history keyed by trading session, IV history bounded by the requested session, expired
contracts distinguished from closings, aggregate rows never summed with per-firm rows,
and one failing lane not taking down the stock page.

The suite was itself reviewed and four cases came back as false positives — passing
against deliberately broken implementations. Those now assert against stored rows or
against `MEANS_ABSENT`, a constant the code exports and the UI and MCP layer both read.

Ten invariants were broken on purpose across two rounds; ten were caught. The tmp_db
fixture asserts the resolved database path is inside the temporary directory before
allowing a write, so a regression in `db.py` cannot silently corrupt accrued history.

### Verified

- Vanna and charm checked against finite-difference derivatives; a formula from a
  widely-referenced GitHub issue was disproven in the process.
- 13F INFOTABLE streamed rather than materialised: 5.3GB peak reduced to 598MB.
- Senate eFD reached via TLS-fingerprint impersonation; plain `requests` is refused by
  Akamai regardless of headers.
- Every section passed repeated adversarial review to convergence.
- The English itself was reviewed in two independent passes: once against the source it
  was translated from, to catch dropped sentences and altered numbers, and once by
  readers given only the English, to catch what reads translated. The second pass found
  a different class of defect from the first, including two sentences that did not
  parse — one of them in a banner shown whenever a scan fails.
- `plan` accepts the same three values through MCP as through REST. The tool schema had
  offered two while the store implemented three, so an agent could not reach a state the
  HTTP API exposed.
