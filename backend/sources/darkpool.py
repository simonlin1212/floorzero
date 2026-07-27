"""FINRA off-exchange transparency (ATS dark pools + non-ATS off-exchange).

━━━━━━━━━━━━━━ ⚠️ Compliance: tier B, **off by default** ━━━━━━━━━━━━━━

Same terms and same switch (`FZ_ENABLE_FINRA`) as the FINRA part of `sources/shorts.py`.
The terms verbatim, their ambiguities, and our position of not interpreting them for you, are in `shorts.FINRA_TERMS`.

⚠️ **A project rule: no section may use FINRA as its only source.**
This section says so honestly: **ATS volume has no second public source** — FINRA is the sole publisher.
So the approach is:
- FINRA supplies the **numerator** (ATS / non-ATS off-exchange volume), and when it is off, that part shows **nothing at all**;
  ⛔ no plausible-looking answer assembled from other figures;
- the **denominator** (total volume over the same period) comes from locally accrued Cboe quotes (`scanner_store`),
  so the "off-exchange share" metric genuinely **needs both sides** to exist.
- Hence, with FINRA off, this section holds only the terms and how to enable it, and that is **deliberate**.

━━━━━━━━━━━━━━ ⚠️ Three traps that make people compute it wrong (confirmed by measurement 2026-07-26) ━━━━━━━━━━━━━━

**① "Dark pool" ≠ "off-exchange".** One endpoint mixes two entirely different kinds of trading:

| Type | What it is | NVDA, week of 2026-06-29 |
|---|---|---|
| **ATS** | a genuine dark pool (an alternative trading system displaying no quotes) | 79,525,315 shares |
| **Non-ATS off-exchange** | wholesaler **internalisation** (retail order flow sold to market makers and filled there) | **188,388,580 shares** |

The second is **2.4× the first**, and it **is not a dark pool**. A great deal of talk about "dark pool volume"
conflates the two, and the figure comes out more than double. This module **always returns them apart**.

**② The response mixes aggregate and detail rows, and summing everything double-counts exactly once.**
`summaryTypeCode` takes four values: `ATS_W_SMBL` (that symbol's ATS total),
`ATS_W_SMBL_FIRM` (split by individual ATS), `OTC_W_SMBL`, `OTC_W_SMBL_FIRM`.
The per-firm rows were measured summing **exactly** to the aggregate row (a difference of 0) — they are two cuts of the same volume,
and an undiscriminating SUM of 535,827,790 shares is precisely twice the truth.

**③ Non-ATS off-exchange trades do not disclose the firm** at the symbol level (`MPID` is always blank).
Measured on NVDA that week: 32 non-ATS records, every one with a blank MPID — so
"which wholesaler took how much" is a question **this data cannot answer**. On the ATS side the firms are named.

**④ The data runs about four weeks behind.** Measured on 2026-07-26, the newest week available was 2026-06-29 (27 days).
FINRA publishes Tier 1 symbols two weeks late and Tier 2 four weeks late, plus its own release cadence.
This is not "live dark-pool monitoring"; it is **after-the-fact statistics**.
"""
from __future__ import annotations

import csv
import io
from typing import Iterator, Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter
from sources.shorts import FINRA_TERMS, FinraDisabled, finra_enabled  # noqa: F401

FINRA_API = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"

#: The four record types. **ATS and non-ATS are different things; aggregate and detail are two cuts of the same volume.**
TYPE_ATS_TOTAL = "ATS_W_SMBL"
TYPE_ATS_FIRM = "ATS_W_SMBL_FIRM"
TYPE_OTC_TOTAL = "OTC_W_SMBL"
TYPE_OTC_FIRM = "OTC_W_SMBL_FIRM"

#: The per-request limit. FINRA publishes no hard cap; 5000 was measured working.
MAX_LIMIT = 5000


def _post(body: dict) -> list[dict]:
    """Make one call to the FINRA data API.

    ⚠️ It **returns CSV** (`Content-Type: text/plain`), even though the request body is JSON.
    Parsing it as JSON raises `JSONDecodeError`, which reads like "the endpoint is broken" when the format simply did not match.
    ⚠️ And **do not pass `sortFields`** — it returns 400 unless every partition key is specified
    ("Sorting is allowed only if all partitions keys are specified").
    """
    if not finra_enabled():
        raise FinraDisabled(
            "The FINRA source is off by default. Set the environment variable FZ_ENABLE_FINRA=1 to enable it.\n"
            "Off is deliberate: FINRA's Terms of Use limit it to non-commercial personal or professional use, "
            "and explicitly forbid using the site's data to build a database — which is exactly what this project does, downloading into SQLite.\n"
            "The terms verbatim and their ambiguities are shown in the interface — we do not interpret them for you; the judgement is yours.")
    _limiter.wait()
    try:
        r = requests.post(FINRA_API, json=body, timeout=90,
                          headers={"User-Agent": user_agent(),
                                   "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise RuntimeError(f"FINRA network failure: {type(e).__name__}: {e}") from e
    if r.status_code == 400:
        raise RuntimeError(f"FINRA refused the query (400): {(r.text or '')[:200]}")
    if r.status_code == 404:
        raise RuntimeError(
            "The FINRA endpoint does not exist (404) — the dataset path may have changed. "
            "That is a configuration problem, not an absence of data.")
    if r.status_code != 200:
        raise RuntimeError(f"FINRA HTTP {r.status_code}")
    text = r.text or ""
    if not text.strip():
        raise DataNotAvailable("FINRA returned empty content")
    return list(csv.DictReader(io.StringIO(text)))


def weekly(symbol: Optional[str] = None, limit: int = MAX_LIMIT) -> list[dict]:
    """One symbol's (or the whole market's) weekly off-exchange volume.

    ⚠️ The **raw rows mix all four `summaryTypeCode` values**,
    so a plain SUM counts the same volume twice — classification is `modules/darkpool.py`'s job.
    """
    body: dict = {"limit": max(1, min(limit, MAX_LIMIT))}
    if symbol:
        body["domainFilters"] = [
            {"fieldName": "issueSymbolIdentifier", "values": [symbol.strip().upper()]}]
    rows = _post(body)
    if len(rows) >= body["limit"]:
        # ⚠️ Hitting the limit = **it was probably truncated**, and we **cannot sort**
        #    (passing sortFields returns 400), so which weeks got cut is unknowable.
        #    Treating the weekly series as "complete history" is then wrong, and the caller has to be told.
        rows.append({"summaryTypeCode": "__TRUNCATED__",
                     "weekStartDate": "", "issueSymbolIdentifier": "",
                     "totalWeeklyShareQuantity": ""})
    if not rows:
        raise DataNotAvailable(
            f"FINRA has no off-exchange records for {symbol}"
            if symbol else "FINRA has no off-exchange records")
    return rows


def iter_weekly(symbol: Optional[str] = None) -> Iterator[dict]:
    yield from weekly(symbol)
