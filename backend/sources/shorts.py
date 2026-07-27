"""Short-side sources — SEC fails-to-deliver (primary) and FINRA off-exchange short volume (optional).

━━━ ⚠️ These two sit at **completely different** compliance tiers. Do not conflate them ━━━

| Source | Tier | Terms as measured (read verbatim 2026-07-26) |
|---|---|---|
| **SEC FTD** | **S** | Same as EDGAR: rate limit (10/s) plus a declared UA. **Commercial use unrestricted** |
| **FINRA Reg SHO** | **B ⚠️** | See below. **Off by default** |

FINRA Terms of Use (https://www.finra.org/terms-of-use, 2023-11-09 revision), verbatim:

    Permitted Uses: "the content and material provided through the FINRA Website
    shall be used ONLY for your own non-commercial personal or professional use."

    Restrictions (d): "develop or create a database of data using the FINRA
    Website, except as expressly permitted by any other terms of use..."

    Restrictions (e): "use any process to monitor or copy the FINRA Website in
    bulk, or use any data mining, scraping or harvesting tools (including robots)"

⚠️ **There is genuine ambiguity here, and this project does not resolve it for the user**:
- The terms scope themselves to "the use of the FINRA.**ORG** site", while the Reg SHO
  files sit on `cdn.finra.org` (a different host) — whether they are covered is unstated.
- Restriction (d) forbids "creating a database", and every section here is download → local SQLite.
- FINRA also has a separate **API Terms of Service** (developer.finra.org), a click-through
  licence each user must register for and accept — we cannot accept it on anyone's behalf.

→ Therefore: **this source is off by default** and must be switched on explicitly
  (`FZ_ENABLE_FINRA=1`), with the text above shown at the point of switching. The judgement
  is the user's; our job is only to make sure the terms are in front of them. The section's
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date, timedelta
from typing import Iterator, Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter

FTD_BASE = "https://www.sec.gov/files/data/fails-deliver-data"
FINRA_CDN = "https://cdn.finra.org/equity/regsho/daily"

#: FINRA Terms of Use, verbatim (shown to the user; do not paraphrase)
FINRA_TERMS = {
    "url": "https://www.finra.org/terms-of-use",
    "last_modified": "2023-11-09",
    "permitted": ("the content and material provided through the FINRA Website "
                  "shall be used ONLY for your own non-commercial personal or "
                  "professional use."),
    "restriction_d": ("develop or create a database of data using the FINRA "
                      "Website, except as expressly permitted by any other terms "
                      "of use on the FINRA Website"),
    "restriction_e": ("use any process to monitor or copy the FINRA Website in "
                      "bulk, or use any data mining, scraping or harvesting tools "
                      "(including robots), or any similar data-gathering or "
                      "extraction tools"),
    "ambiguity": ("The terms scope themselves to 'the use of the FINRA.ORG site', while "
                  "the Reg SHO files sit on cdn.finra.org, a different host — whether they "
                  "are covered is unstated. Restriction (d) forbids 'creating a database', "
                  "and this tool writes the data into local SQLite. There is also a separate "
                  "API Terms of Service (developer.finra.org) requiring registration."),
    "our_stance": ("This project does not interpret those terms for you. The FINRA source is "
                   "**off by default**; set FZ_ENABLE_FINRA=1 and judge for yourself whether "
                   "your use complies. This section's primary source is SEC fails-to-deliver "
                   "(tier S, commercial use unrestricted), so it works fine with FINRA off."),
}


def finra_enabled() -> bool:
    """Whether the user has explicitly switched the FINRA source on."""
    return (os.environ.get("FZ_ENABLE_FINRA") or "").strip().lower() in (
        "1", "true", "yes", "on")


class FinraDisabled(RuntimeError):
    """FINRA source not enabled — **a configuration state, not \"no data\"**.

    It gets its own type so the UI can say "you have not switched this source on"
    rather than render an empty table that reads as "there is no short volume".
    """


# ─────────────────────── SEC fails-to-deliver (tier S · primary) ───────────────────────

def ftd_files(back: int = 6, today: Optional[date] = None) -> list[str]:
    """Identifiers for the last N half-month files (newest first), e.g. `202606b`.

    SEC publishes two files a month: `a` = first half, `b` = second half.
    The first-half file appears at month end and the second-half around the 15th of the
    """
    d = today or date.today()
    out: list[str] = []
    y, m = d.year, d.month
    for _ in range((back + 1) // 2 + 1):
        for half in ("b", "a"):
            out.append(f"{y}{m:02d}{half}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out[:back]


def ftd_rows(tag: str) -> Iterator[dict]:
    """Stream the FTD records for one half-month file.

    ⚠️ Streamed: about 60k rows per file, which is not large, but the habit matches 13F —
    this project has already paid 5.3GB for materialising a table whole.
    """
    url = f"{FTD_BASE}/cnsfails{tag}.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=180)
    except requests.RequestException as e:
        raise RuntimeError(f"SEC FTD network failure: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"SEC has not published this FTD file yet: {tag}")
    if r.status_code != 200:
        raise RuntimeError(f"SEC FTD HTTP {r.status_code}: {tag}")

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"FTD {tag} did not return a ZIP (possibly an error page)") from e

    names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
    if not names:
        raise RuntimeError(f"FTD {tag}: no txt inside the ZIP (the layout may have changed)")

    with zf.open(names[0]) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        header = next(text).rstrip("\n").split("|")
        for line in text:
            vals = line.rstrip("\n").split("|")
            if len(vals) < len(header):
                continue                    # trailing explanatory lines are common; skip rather than guess
            yield dict(zip(header, vals))


# ─────────────────── FINRA off-exchange short volume (tier B · off by default) ───────────────────

def finra_short_volume(day: date, market: str = "CNMS") -> Iterator[dict]:
    """FINRA off-exchange short volume for one day.

    ⚠️ **Unavailable by default**: requires `FZ_ENABLE_FINRA=1` (see the terms in the module docstring).

    `market`: CNMS = consolidated (NMS securities, the usual choice); FNSQ / FNYX / FNRA are per-facility.
    """
    if not finra_enabled():
        raise FinraDisabled(
            "The FINRA source is not enabled. Its terms permit non-commercial personal or "
            "professional use only, and forbid building a database or bulk copying, while this "
            "tool writes into local SQLite. Judge compliance yourself, then set FZ_ENABLE_FINRA=1.")

    url = f"{FINRA_CDN}/{market}shvol{day:%Y%m%d}.txt"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"FINRA network failure: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"FINRA has no data for that day (non-trading day, or not yet published): {day}")
    if r.status_code != 200:
        raise RuntimeError(f"FINRA HTTP {r.status_code}: {day}")

    lines = r.text.splitlines()
    if not lines:
        raise DataNotAvailable(f"FINRA {day}: file is empty")
    header = lines[0].split("|")
    for line in lines[1:]:
        vals = line.split("|")
        if len(vals) < len(header):
            continue                        # trailing summary line
        yield dict(zip(header, vals))


def recent_trading_days(n: int, end: Optional[date] = None) -> list[date]:
    """The last N business days (newest first). Whether data exists is only known on fetch."""
    d = end or date.today()
    out: list[date] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out
