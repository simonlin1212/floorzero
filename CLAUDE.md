# FloorZero — notes for Claude Code

Read `docs/ARCHITECTURE.md` before changing anything structural.

Most non-obvious decisions here were paid for rather than reasoned out: a source
behaved unexpectedly, a review found a defect, a fix introduced a regression. Where
that history matters it is written into the comment next to the code, not filed
somewhere else — so the comments are load-bearing. Read them before rewriting them.

## The constraint that shapes everything

**This project distributes code, never data.** Cboe's delayed feed carries OPRA data,
and OPRA treats showing that data externally in any app, tool or website as
redistribution — US$1,500/month, with no exemption for free, open-source or
non-commercial use.

So:

- The backend binds to `127.0.0.1`. Do not change the default.
- ⛔ **Never build a hosted demo.** Promotion is screenshots and README, nothing else.
- Every source keeps its compliance tier visible in the UI, not buried in docs.

## Compliance tiers

| Tier | Source | Commercial | Redistribution |
|---|---|---|---|
| **S** | SEC EDGAR · Treasury · CFTC | yes | yes |
| **S−** | Congressional disclosures | **unlawful** — 5 U.S.C. §13107(c)(1)(B) | yes (public record) |
| **B** | FINRA (Reg SHO, ATS) | non-commercial only | no |
| **C** | Cboe | needs authorisation | no |

- Congressional data must never appear in a paid product or commercial service.
  It is *not* equivalent to EDGAR, which limits request rate but not commercial use.
- FINRA is **off by default** (`FZ_ENABLE_FINRA`). Its terms are reproduced verbatim in
  the UI; we do not interpret them for the user. **No section may use FINRA as its only
  source.**
- Credentials are never embedded — and **contact details count as credentials**.
  `FZ_CONTACT` has no default and fails fast; hardcoding an address would attribute every
  user's upstream traffic to whoever wrote it.

## Rules the code follows

These recur throughout and are worth internalising before editing:

1. **"Could not fetch" must never be rendered as "does not exist."** Classify exceptions
   positively, never by elimination: `DataNotAvailable` means genuinely absent and may be
   fallen back on; `RuntimeError` means configuration, rate limiting or network and must
   propagate. A 403 from an S3-backed archive means both "missing" and "denied" — read
   the body to tell them apart.
2. **A value that cannot be computed is not zero.** Return null and, wherever the caller
   might act on it, a reason code. A null with no reason gets mistranslated: `iv_rank`
   could be null for three distinct causes and the UI once rendered all of them as
   "N days to go", which for a missing current IV read "0 days to go".
3. **Output data, never conclusions.** No buy/sell labels, no targets, no predictions,
   no cross-source scores. Where the data cannot support a claim — options direction
   without the tape — say so instead of inferring.
4. **One dataset, one set of filters.** When two views of the same data drift apart, the
   numbers disagree and neither is obviously wrong. Share the filter builder; aggregate in
   SQL, not over a truncated page.
5. **Local history is keyed by the data's own trading session**, never wall-clock date.
   Keyed by wall clock, opening the page twice over a weekend stores one Friday close as
   two observations.
6. **Guard where a value is evaluated, not where it is produced.** `lag_days` reads like
   a field and computes like a function; its exceptions surfaced outside every per-lane
   try block.
7. **Do not depend on the development machine.** SQLite `FULL OUTER JOIN` needs 3.39+;
   a self-hosting user may have 3.3x. Python floor is 3.9, enforced at startup.

## Versions

**0.1.0 is a usable start; 1.0 is the finished product.** Adding a section moves the minor
version by one (0.2, 0.3, …); patches fix what is already there. The roadmap in the README
lists what is still missing, and **anything not on it is not planned** — saying that plainly
is the same promise as the reason codes.

Not splitting this into several repositories was a decision, not an oversight: it was tried
on 2026-07-28 and reverted the same day. One repository whose version number shows how far
along it is carries a continuity that a scatter of repositories does not.

⚠️ The version appears in **four** places — `frontend/package.json`, `backend/app.py`,
`backend/mcp_server.py`, `CHANGELOG.md`. Bump all four; package.json is the one that gets
missed.

## Workflow

1. Compare against the published version before editing — a local copy may be behind.
2. Change, then **actually run** the code against real data, including edge cases.
3. ⛔ **Run `codex review` before pushing, not after.** Every section in this repo needed
   two to four passes, and roughly a third of each round's findings were regressions
   introduced by the previous round's fixes. Re-review until it returns no regressions.
4. Assert that each edit matched (`if old not in s: fail`) rather than letting a silent
   no-op through, and grep to confirm afterwards. A green build proves the file is valid,
   not that it changed.

## Tests

```bash
cd backend && pip install -r requirements-dev.txt && python -m pytest
```

`backend/tests/` — 52 cases, no network. They exist to pin the seven rules above rather
than to chase coverage, so when you add a rule, add the case that would catch its
violation. Two conventions:

- **Never touch `~/.floorzero`.** The `tmp_db` fixture redirects `FZ_DATA_DIR` *and
  reloads the modules* — `db.py` computes its path at import time, so setting the
  variable alone leaves tests writing into the user's real history, silently and while
  passing.
- **Verify the tests, not just the code.** Break an invariant on purpose and confirm the
  suite goes red. Ten were checked this way across two rounds — and the first round of
  tests contained four cases that passed against broken implementations, because they
  asserted on a function's own return value rather than on the stored rows, or on the
  wording of a label rather than on a constant. A test that passes against broken code
  is worse than no test.
- **Classify in code, not in prose.** `MEANS_ABSENT` exists because a test was checking
  whether a label contained a particular word. UI, MCP and tests now read the same
  constant instead of each inferring the category from the text.

## Stack

Python 3.9+ · FastAPI · React 19 · Vite · Tailwind · ECharts · SQLite.
Four backend dependencies; keep it that way. Tools are defined once in `backend/tools.py`
and inherited by the MCP server, so REST and MCP cannot diverge.

⛔ **No chat panel inside the app, and this is not an oversight.** An assistant reaches the
data through MCP, running in the client the user already trusts. A panel here would mean
either posting local data to somebody's API — which would make the sidebar's "your data
stays on your own machine" false — or spawning a CLI on the host, with SEC filing text and
issuer names flowing into the prompt as an injection surface. The MCP route has neither
problem, and it keeps rule three intact: the tool still states no conclusion, and whatever
the user's own assistant concludes is the assistant's, not this project's. Data lives in
`~/.floorzero/`, outside the repo, so updating code never destroys accrued history.
