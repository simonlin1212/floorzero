"""SEC 13F — the source for institutional holdings.

Same compliance tier as Form 4 (S: EDGAR public record, rate-limited but not restricted commercially).
Reuses `edgar.py`'s rate limiter and `contact.py`'s UA configuration.

━━━ ⭐ What a 13F actually covers: the most important thing about this section ━━━

The phrase "institutional holdings" misleads on its own. A 13F reports **long positions in
13(f) securities as of quarter-end**, and beyond that:

| Not included | Why |
|---|---|
| **Short positions** | the SEC created Rule 13f-2 / Form SHO in 2023 specifically to report shorts (effective 2025-01) — precisely because 13F does not cover them |
| Cash, bonds, commodities, FX | not on the 13(f) securities list |
| Stocks listed only outside the US | same |
| Unlisted and private holdings | same |
| Holdings granted confidential treatment | disclosure can be deferred on request (Rule 24b-2) |

⚠️ **Options appear as their underlying security** (Form 13F Special Instruction 10):
a put is marked `PUT` but listed under the underlying's name. Measured on the 2026Q1 window:
ordinary holdings $74.9tn / calls $2.95tn / **puts $3.66tn** —
so summing PUT rows in with holdings **counts $3.66tn of bearish exposure as bullish**.

━━━ The source: the quarterly structured dataset (no need to parse filings one by one) ━━━

⚠️ The naming is not by calendar quarter but by a **three-month window of filing dates**:
`01mar2026-31may2026_form13f.zip` (published 2026-06-01).
One window mixes several reporting periods — measured, that window holds
10,776 filings for the 2026-03-31 period, alongside late and amended filings going back to 2008.
**So it has to be filtered on `PERIODOFREPORT`**; a whole window is not one quarter.

⚠️ An order of magnitude larger than Form 345: 95MB compressed / INFOTABLE 396MB / 3.8m rows.

━━━ Two further traps ━━━
1. **13F-NT is a notice filing and carries no holdings at all** ("my holdings are reported by someone else").
   Measured at 2,045 filings in that window — 18%. Leave them in and you report two thousand managers holding nothing.
2. **There are CUSIPs and no tickers.** The SEC publishes no CUSIP→ticker mapping (that is commercial data).
   Matching issuer names against `company_tickers.json` was measured hitting only **42.8%**
   (most misses being ETFs and funds) — so a ticker is a best-effort auxiliary field and
   **the key must be the CUSIP**.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from typing import Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter

DATASET_INDEX = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
DATASET_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"

#: The tables we use
DATASET_TABLES = ("SUBMISSION", "COVERPAGE", "INFOTABLE")

#: The **SUBMISSIONTYPE** values (EDGAR filing types) that carry holdings. NT is a notice, and explicitly carries none.
#:
#: ⚠️ Do not add `13F COMBINATION REPORT` here — that is a value of **COVERPAGE.REPORTTYPE**,
#: not a filing type. Measured on the 2026Q1 window: all 441 combination reports have a SUBMISSIONTYPE
#: of `13F-HR`(410) or `13F-HR/A`(31), so **the two values below already cover them**.
#: It was once mixed into this constant, which led a code review to conclude combination reports were being dropped (not one was).
HOLDINGS_TYPES = frozenset({"13F-HR", "13F-HR/A"})

_WINDOW_RE = re.compile(
    r"(\d{2}[a-z]{3}\d{4}-\d{2}[a-z]{3}\d{4})_form13f\.zip", re.I)


def list_windows(limit: int = 12) -> list[str]:
    """List the available dataset windows (newest first), e.g. `01mar2026-31may2026`.

    ⚠️ Only obtainable by parsing the index page — the naming is a three-month window of filing
    dates and cannot be derived from a date (unlike Form 345's `2026q1`).
    """
    _limiter.wait()
    try:
        r = requests.get(DATASET_INDEX, headers={"User-Agent": user_agent()},
                         timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"The 13F dataset index request failed: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"The 13F dataset index returned HTTP {r.status_code}")
    seen: list[str] = []
    for m in _WINDOW_RE.finditer(r.text):
        w = m.group(1).lower()
        if w not in seen:
            seen.append(w)
    if not seen:
        raise RuntimeError("No windows parsed out of the 13F dataset index page (its structure may have changed)")
    return seen[:limit]


def download_window(window: str) -> zipfile.ZipFile:
    """Download one window's ZIP and return a handle (**without parsing INFOTABLE**).

    ⚠️ Streaming is mandatory: reading INFOTABLE whole into a list of dicts was measured peaking at
    **5.3 GB** (4.1GB after download, plus another 1.2GB once parsed into objects) —
    an 8GB machine swaps furiously or gets OOM-killed, which breaks "clone it and it runs" outright.
    So this only takes hold of the ZIP. The small tables (SUBMISSION/COVERPAGE, ~12k rows each) can be
    read whole; **INFOTABLE can only go through `iter_table()`, filtered as it is read**.
    """
    url = f"{DATASET_BASE}/{window}_form13f.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=600)
    except requests.RequestException as e:
        raise RuntimeError(f"The 13F dataset download failed: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"No such 13F dataset window: {window}")
    if r.status_code != 200:
        raise RuntimeError(f"The 13F dataset returned HTTP {r.status_code}: {window}")
    try:
        return zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"{window} did not return a ZIP (possibly an error page)") from e


def iter_table(zf: zipfile.ZipFile, table: str):
    """Iterate one TSV inside the ZIP row by row (**nothing stays in memory**)."""
    names = {n.upper(): n for n in zf.namelist()}
    real = names.get(f"{table.upper()}.TSV")
    if real is None:
        raise RuntimeError(f"The dataset is missing {table}.tsv (its structure may have changed)")
    with zf.open(real) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        header = next(text).rstrip("\n").split("\t")
        for line in text:
            vals = line.rstrip("\n").split("\t")
            if len(vals) < len(header):
                vals += [""] * (len(header) - len(vals))
            yield dict(zip(header, vals))


def read_table(zf: zipfile.ZipFile, table: str) -> list[dict]:
    """Read a table whole — **for the small tables only** (SUBMISSION / COVERPAGE, ~12k rows each).

    ⛔ Never point it at INFOTABLE (3.8m rows = 4GB of memory).
    """
    return list(iter_table(zf, table))


def window_dataset(window: str) -> dict[str, list[dict]]:
    """⚠️ **Deprecated** — it reads INFOTABLE whole into memory (measured peaking at 5.3GB).

    Kept only so existing callers keep working; new code should use
    `download_window()` + `read_table()` (small tables) + `iter_table()` (INFOTABLE).
    """
    url = f"{DATASET_BASE}/{window}_form13f.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=600)
    except requests.RequestException as e:
        raise RuntimeError(f"The 13F dataset download failed: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"No such 13F dataset window: {window}")
    if r.status_code != 200:
        raise RuntimeError(f"The 13F dataset returned HTTP {r.status_code}: {window}")

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"{window} did not return a ZIP (possibly an error page)") from e

    names = {n.upper(): n for n in zf.namelist()}
    out: dict[str, list[dict]] = {}
    for table in DATASET_TABLES:
        real = names.get(f"{table}.TSV")
        if real is None:
            raise RuntimeError(f"The {window} dataset is missing {table}.tsv (its structure may have changed)")
        with zf.open(real) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            header = next(text).rstrip("\n").split("\t")
            rows = []
            for line in text:
                vals = line.rstrip("\n").split("\t")
                if len(vals) < len(header):
                    vals += [""] * (len(header) - len(vals))
                rows.append(dict(zip(header, vals)))
            out[table] = rows
    return out


def periods_in(tables: dict[str, list[dict]]) -> list[tuple[str, int]]:
    """Filing counts per reporting period inside a window (most first).

    Lets the user see which period a window is mostly about —
    measured, `01mar2026-31may2026` holds 10,776 filings for 2026-03-31,
    mixed in with late filings going all the way back to 2008.
    """
    counts: dict[str, int] = {}
    for r in tables.get("SUBMISSION", []):
        p = (r.get("PERIODOFREPORT") or "").strip()
        if p:
            counts[p] = counts.get(p, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


def window_end(window: str) -> Optional[date]:
    """`01mar2026-31may2026` → date(2026, 5, 31) (the window's closing date)."""
    m = re.match(r"\d{2}[a-z]{3}\d{4}-(\d{2})([a-z]{3})(\d{4})", window, re.I)
    if not m:
        return None
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    try:
        return date(int(m.group(3)), months[m.group(2).lower()], int(m.group(1)))
    except (KeyError, ValueError):
        return None


# ─────────────────────── The official 13F securities list ───────────────────────

#: The SEC's quarterly "Official List of Section 13(f) Securities" —
#: the authoritative source for **CUSIP → canonical issuer name**.
SEC_LIST_BASE = "https://www.sec.gov/files/investment"


def securities_list(quarter: str) -> list[dict]:
    """Download the official 13(f) securities list (fixed-width text). `quarter` looks like `2026q2`.

    ⭐ Why it is needed: `NAMEOFISSUER` in INFOTABLE is **typed freely by the filer**, and Apple's
    CUSIP was measured carrying **61 different spellings** — among them outright errors such as
    `VANGUARD WHITEHALL FDS`, another company's name sitting on Apple's CUSIP.
    Display or group by those names and securities get attributed to the wrong company.

    Fixed-width layout (stated on the SEC's page):
        CUSIP 1-9 / option marker 10 / issuer name 11-40 / class description 41-67 / status 68-70
    """
    url = f"{SEC_LIST_BASE}/13flist{quarter}-txt.txt"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"The 13F securities list request failed: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"No 13F securities list for: {quarter}")
    if r.status_code != 200:
        raise RuntimeError(f"The 13F securities list returned HTTP {r.status_code}: {quarter}")

    out: list[dict] = []
    for line in r.text.splitlines():
        if len(line) < 40:
            continue
        cusip = line[0:9].strip().upper()
        if not cusip or not cusip[0].isalnum():
            continue
        out.append({
            "cusip": cusip,
            "has_option": line[9:10].strip() == "*",
            "issuer": line[10:40].strip(),
            "class": line[40:67].strip(),
            "status": line[67:70].strip(),
        })
    if not out:
        raise RuntimeError(f"No rows parsed out of the {quarter} 13F securities list (the format may have changed)")
    return out


def list_quarters_for(period: date, back: int = 4) -> list[str]:
    """Given a reporting period, return the list quarters it might correspond to (newest first, for fallback attempts)."""
    y, q = period.year, (period.month - 1) // 3 + 1
    out = []
    for _ in range(back):
        out.append(f"{y}q{q}")
        q += 1
        if q > 4:
            q, y = 1, y + 1
    return out
