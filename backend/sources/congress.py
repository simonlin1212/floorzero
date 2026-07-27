"""Congressional trade disclosures (STOCK Act) — the official House and Senate sources.

━━━ ⚠️ Compliance: the data is public, but **commercial use is prohibited by statute** ━━━
**5 U.S.C. §13107(c)(1)** (Ethics in Government Act; the text itself read 2026-07-26):

    "It shall be unlawful for any person to obtain or use a report—
     (B) for any commercial purpose, other than by news and communications
         media for dissemination to the general public"

§13107(c)(2): the Attorney General may bring a civil action, with a **maximum fine of $10,000**.
§13107(a) covers "the Clerk of the House of Representatives, and the Secretary of the
Senate" explicitly — **both chambers**, not the Senate alone.

→ So the correct reading of this source is:
  - public access, personal research, academic work, and news media disseminating to the public ✅
  - **any commercial purpose ❌** (selling a service, selling a subscription, forming part of a paid product — none of it)

⚠️ This is **unlike** SEC EDGAR: EDGAR's own terms limit only the rate (10 requests/second) and
require a declared User-Agent, stating plainly that "Anyone can access and download this
information for free", with no restriction on commercial use. Do not conflate the two tiers —
this file once carried "commercial ✅ redistribution ✅", and that was wrong.

→ What it means for FloorZero: this project is **free, open-source and self-hosted**, and a user
  running it for their own research falls inside what is allowed; but **this lane can never form
  part of a paid product**, and it is no banner to wave as "safe for commercial use".

━━━ The two chambers differ sharply in practice (measured 2026-07-26) ━━━

| | House | Senate |
|---|---|---|
| Index | an annual ZIP containing XML | a POST search endpoint returning JSON |
| Access | plain `requests` ✅ | ⚠️ **Akamai blocks on TLS fingerprint** |
| Detail | PDF (text extractable) | HTML table (**with its own Ticker column**) |
| Ticker | buried in brackets inside the asset name | a separate field, clean |

⚠️ **The Senate requires `curl_cffi` for TLS impersonation**: `requests` was measured returning
403 no matter how the headers were dressed up (it could not even fetch robots.txt), while a real
Chrome gets through — what is blocked is the TLS fingerprint, not the UA, and it is **not geographic** (a US-hosted server is blocked just the same).
`curl_cffi` is an **optional dependency**: without it, report honestly that "the Senate is
unavailable" and how to install it — never dress "a dependency is missing" up as "the Senate has no data".

━━━ ⚠️ Two real traps in the parsing ━━━
1. **In House PDFs, `[XX]` is an asset-type code, not a ticker.**
   The GS in `Treasury Bill ... [GS]` is Government Securities, **not Goldman Sachs**.
   The real ticker sits in round brackets: `Abbott Laboratories Common Stock (ABT) [ST]`.
   Catching tickers with `\\[([A-Z]{2})\\]` produces a great deal of very convincing rubbish.
2. **The Senate has paper scans** (linked as `/view/paper/` rather than `/view/ptr/`),
   which are images and unparseable without OCR. They must be labelled "paper filing, not parsed"
   rather than dropped in silence — otherwise the user believes they are seeing everything.
"""
from __future__ import annotations

import io
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import requests

from sources.contact import user_agent

# ⚠️ The UA is **configured by whoever deploys this** (FZ_CONTACT); nobody's email is ever hardcoded.
# See sources/contact.py.

HOUSE_BASE = "https://disclosures-clerk.house.gov/public_disc"
SENATE_BASE = "https://efdsearch.senate.gov"

#: House FilingType codes → meaning. P is the periodic transaction report we want.
HOUSE_FILING_TYPES = {
    "P": "Periodic Transaction Report (PTR)",
    "A": "Annual report",
    "C": "Candidate report",
    "D": "Candidate report (amended)",
    "W": "Departure report",
    "X": "Extension request",
    "H": "Hearing",
    "T": "Termination report",
}

#: The STOCK Act's statutory deadline (verbatim from the House PTR form, read 2026-07-26):
#: "30 days from when you became aware of the transaction,
#:  but no later than 45 days after the transaction."
#: ⚠️ It rolls over weekends and holidays, so "over 45 days" is an **observation of fact**, not a finding of violation — see modules/congress.py
STOCK_ACT_HARD_CAP_DAYS = 45


class DataNotAvailable(RuntimeError):
    """That year, or that file, genuinely is not there — the caller can safely skip it.

    Kept apart from "denied / dependency missing / network failure", all of which must propagate.
    This boundary has already been got wrong twice in this project (the 403 in global-stock-data
    v2.0.1, and finding #6 of FloorZero's nine review rounds). Do not make it a third time.
    """


class SenateUnavailable(RuntimeError):
    """The Senate source is currently unreachable — which is **not** "senators did not trade".

    It gets its own type precisely so the frontend can present **environment problems** —
    "curl_cffi will not install", "Akamai is blocking us" — as environment problems, rather
    than as an empty table that reads as senators having traded nothing in the period.
    """


class _RateLimiter:
    """A thread-safe minimum-interval throttle (lock-based, so concurrency cannot punch through it)."""

    def __init__(self, max_per_sec: float) -> None:
        self._interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


# A government site, deliberately slowed: this is not a high-frequency endpoint and haste buys nothing
_limiter = _RateLimiter(3)


@dataclass(frozen=True)
class Filing:
    """The **metadata** of one disclosure (no transaction detail — that needs a second fetch)."""

    chamber: str                  # "house" | "senate"
    name: str                     # name as displayed
    last: str
    first: str
    state_district: str           # House "IN02"; Senate gives a state, or nothing
    filing_type: str              # the raw code (House) or the report title (Senate)
    filing_date: Optional[date]   # date filed
    year: str
    doc_id: str                   # House DocID / Senate UUID
    detail_url: str
    is_paper: bool = False        # ⚠️ a paper scan: the detail cannot be parsed

    @property
    def is_ptr(self) -> bool:
        """Whether this is a periodic transaction report (the only type we care about)."""
        if self.chamber == "house":
            return self.filing_type == "P"
        return "periodic transaction" in self.filing_type.lower()


def _parse_us_date(s: Optional[str]) -> Optional[date]:
    """Parse M/D/YYYY or MM/DD/YYYY; returns None when it cannot (this source data is inherently messy)."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


# ────────────────────────────── House ──────────────────────────────

def house_filings(year: int) -> list[Filing]:
    """Fetch one year of the House disclosure index (an annual ZIP containing XML).

    ⚠️ This carries **metadata only** (who, what type, filed when); the transaction detail is in each PDF.
    """
    url = f"{HOUSE_BASE}/financial-pdfs/{year}FD.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=45)
        if r.status_code == 404:
            raise DataNotAvailable(f"The House has no {year} disclosure file (the year is too early, or it is not published yet)")
        r.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {403: "denied (throttled or banned)", 429: "requesting too fast"}.get(code, "")
        raise RuntimeError(f"House HTTP {code} {hint}: {url}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"House network failure: {type(e).__name__}: {e}") from e

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise RuntimeError(f"The House {year} ZIP contains no XML (its structure may have changed)")
        raw = zf.read(xml_names[0])
    except zipfile.BadZipFile as e:
        # Reached when upstream returns an error page instead of a ZIP — do not let it pass as "no data"
        raise RuntimeError(f"The House {year} response is not a ZIP (possibly an error page)") from e

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(f"Parsing the House {year} XML failed: {e}") from e

    out: list[Filing] = []
    for m in root:
        doc_id = (m.findtext("DocID") or "").strip()
        if not doc_id:
            continue
        last = (m.findtext("Last") or "").strip()
        first = (m.findtext("First") or "").strip()
        yr = (m.findtext("Year") or str(year)).strip()
        out.append(Filing(
            chamber="house",
            name=" ".join(x for x in (first, last) if x),
            last=last, first=first,
            state_district=(m.findtext("StateDst") or "").strip(),
            filing_type=(m.findtext("FilingType") or "").strip(),
            filing_date=_parse_us_date(m.findtext("FilingDate")),
            year=yr, doc_id=doc_id,
            detail_url=f"{HOUSE_BASE}/ptr-pdfs/{yr}/{doc_id}.pdf",
        ))
    if not out:
        raise DataNotAvailable(f"The House {year} index is empty")
    return out


def house_ptr_pdf(year: str, doc_id: str) -> bytes:
    """Download the original PDF of one House PTR."""
    url = f"{HOUSE_BASE}/ptr-pdfs/{year}/{doc_id}.pdf"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=45)
        if r.status_code == 404:
            raise DataNotAvailable(f"The House has no such PTR file: {year}/{doc_id}")
        r.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {403: "denied (throttled or banned)", 429: "requesting too fast"}.get(code, "")
        raise RuntimeError(f"House PDF HTTP {code} {hint}: {url}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"House PDF network failure: {type(e).__name__}: {e}") from e

    if not r.content.startswith(b"%PDF"):
        # A proper error beats feeding an HTML error page to the PDF parser
        raise RuntimeError(f"The House {year}/{doc_id} response is not a PDF (possibly an error page)")
    return r.content


# ────────────────────────────── Senate ──────────────────────────────

_SENATE_HINT = (
    "Senate eFD is blocked by Akamai on TLS fingerprint, so ordinary HTTP clients all get 403. "
    "It needs an optional dependency: pip install curl_cffi"
)

_senate_session = None
_senate_lock = threading.Lock()


def _senate_client():
    """Establish a session that gets past Akamai (including the accept-terms step).

    ⚠️ Every step must report **which step and why** when it fails —
    this path has four links in it (install the dependency / home page / CSRF / accept terms),
    and a vague "the Senate failed" leaves the user with nowhere to start.
    """
    global _senate_session
    with _senate_lock:
        if _senate_session is not None:
            return _senate_session
        try:
            from curl_cffi import requests as cr
        except ImportError as e:
            raise SenateUnavailable(f"curl_cffi is missing. {_SENATE_HINT}") from e

        s = cr.Session(impersonate="chrome")
        try:
            home = s.get(f"{SENATE_BASE}/search/home/", timeout=30)
        except Exception as e:
            raise SenateUnavailable(f"The Senate home page request failed: {type(e).__name__}: {e}") from e
        if home.status_code == 403:
            raise SenateUnavailable(f"The Senate home page returned 403. {_SENATE_HINT}")
        if home.status_code != 200:
            raise SenateUnavailable(f"The Senate home page returned HTTP {home.status_code}")

        tok = re.search(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)", home.text)
        if not tok:
            raise SenateUnavailable("No CSRF token found on the Senate home page (its structure may have changed)")

        # eFD requires the prohibited-use declaration to be ticked before it will allow a search
        r = s.post(f"{SENATE_BASE}/search/home/",
                   data={"prohibition_agreement": "1",
                         "csrfmiddlewaretoken": tok.group(1)},
                   headers={"Referer": f"{SENATE_BASE}/search/home/"}, timeout=30)
        if r.status_code != 200:
            raise SenateUnavailable(f"Accepting the Senate terms failed with HTTP {r.status_code}")
        if not s.cookies.get("csrftoken"):
            raise SenateUnavailable("No Senate session established (no csrftoken cookie)")
        _senate_session = s
        return s


#: The eFD server **caps the page size at 100** (asking for 250 was measured returning 100) —
#: without paging, data is dropped in silence: 949 PTRs since 2020, so taking only the first page loses 89%.
SENATE_PAGE_SIZE = 100


def senate_filings(start: str, end: str = "", limit: int = 500) -> list[Filing]:
    """Search Senate PTRs (paging automatically until the limit is reached or the results run out).

    start / end take `MM/DD/YYYY`. **Filtered on filing date**, not trade date.

    ⚠️ Paging is mandatory: the server gives at most 100 rows a page, and `recordsTotal` is often far larger.
    Read only the first page and earlier filings **never reach the cache** (later syncs fetch the same
    page again and filter all of it out against known_doc_ids), and the whole thing happens without a word.
    """
    out: list[Filing] = []
    offset = 0
    total: Optional[int] = None
    while True:
        page, total = _senate_page(start, end, offset)
        out.extend(page)
        offset += SENATE_PAGE_SIZE
        if not page or len(out) >= limit or (total is not None and offset >= total):
            break
    return out[:limit]


def _senate_page(start: str, end: str, offset: int) -> tuple[list[Filing], Optional[int]]:
    """Fetch one page; returns (this page's Filings, total record count)."""
    s = _senate_client()
    csrf = s.cookies.get("csrftoken")
    _limiter.wait()
    try:
        r = s.post(
            f"{SENATE_BASE}/search/report/data/",
            data={"start": str(offset), "length": str(SENATE_PAGE_SIZE),
                  "report_types": "[11]",              # 11 = Periodic Transaction Report
                  "filer_types": "[]",
                  "submitted_start_date": f"{start} 00:00:00",
                  "submitted_end_date": f"{end} 23:59:59" if end else "",
                  "candidate_state": "", "senator_state": "", "office_id": "",
                  "first_name": "", "last_name": "", "csrfmiddlewaretoken": csrf},
            headers={"Referer": f"{SENATE_BASE}/search/",
                     "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf},
            timeout=45)
    except Exception as e:
        raise SenateUnavailable(f"The Senate search request failed: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise SenateUnavailable(f"The Senate search returned HTTP {r.status_code}")
    try:
        payload = r.json()
    except Exception as e:
        raise SenateUnavailable("The Senate search returned something other than JSON (the session may have expired)") from e

    rows_total = payload.get("recordsTotal")
    out: list[Filing] = []
    for row in payload.get("data", []):
        if len(row) < 5:
            continue
        first, last, office, title_html, filed = row[0], row[1], row[2], row[3], row[4]
        link = re.search(r'href="([^"]+)"', title_html or "")
        href = link.group(1) if link else ""
        title = re.sub(r"<[^>]+>", "", title_html or "").strip()
        # ⚠️ /view/paper/ = a paper scan (an image), whose detail cannot be parsed
        is_paper = "/paper/" in href
        doc_id = href.rstrip("/").rsplit("/", 1)[-1] if href else ""
        out.append(Filing(
            chamber="senate",
            name=re.sub(r"\s+", " ", f"{first} {last}").strip().rstrip(","),
            last=(last or "").strip(), first=(first or "").strip(),
            state_district=re.sub(r"<[^>]+>", "", office or "").strip(),
            filing_type=title,
            filing_date=_parse_us_date(re.sub(r"<[^>]+>", "", filed or "")),
            year=str(_parse_us_date(re.sub(r"<[^>]+>", "", filed or "")) or "")[:4],
            doc_id=doc_id,
            detail_url=f"{SENATE_BASE}{href}" if href else "",
            is_paper=is_paper,
        ))
    return out, rows_total if isinstance(rows_total, int) else None


def senate_ptr_html(detail_url: str) -> str:
    """Fetch a Senate PTR detail page as HTML (only electronic filings have a table)."""
    if "/paper/" in detail_url:
        raise DataNotAvailable(
            "This is a paper scan (an image). Without OCR the detail cannot be parsed — the original has to be opened by hand")
    s = _senate_client()
    _limiter.wait()
    try:
        r = s.get(detail_url, timeout=40)
    except Exception as e:
        raise SenateUnavailable(f"The Senate detail page request failed: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"The Senate has no such filing: {detail_url}")
    if r.status_code != 200:
        raise SenateUnavailable(f"The Senate detail page returned HTTP {r.status_code}")
    return r.text


def senate_available() -> tuple[bool, str]:
    """Probe whether the Senate lane currently works (so the UI can show its real state rather than draw an empty table)."""
    try:
        _senate_client()
        return True, "available"
    except SenateUnavailable as e:
        return False, str(e)
