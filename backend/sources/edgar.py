"""SEC EDGAR — the source for insider Form 4 data.

━━━ Compliance tier S: US government public record, commercial ✅ redistribution ✅ ━━━
The same tier as congressional disclosures, and the data in this project that can be shown externally without worry.

━━━ ⭐ Two paths, and neither is optional (measured 2026-07-26) ━━━

| | Quarterly structured dataset | Daily filing XML |
|---|---|---|
| Contents | TSV, pre-parsed by the SEC | each Form 4 in full |
| Coverage | complete history, one ZIP per quarter | everything filed that day |
| Cost | 1 request, 13MB ≈ 100k transactions | **1 request per filing** (645 on a single day) |
| Freshness | ⚠️ **anywhere from 7 to 49 days behind** | live |

Measured lags: 2025Q4 published 2026-01-07 (+7 days), 2026Q1 on 2026-04-07 (+7 days),
2025Q3 on 2025-11-18 (**+49 days**) — there is no stable pattern.
As of 2026-07-26 the newest available was still 2026Q1, **a 116-day gap**.

→ So the architecture has to be "**ZIP for history (cheap) + XML for the recent window (costly, but small)**".
ZIP alone never shows the last few months; XML alone costs tens of thousands of requests to backfill a year.

━━━ Rate limit ━━━
The SEC requires ≤10 requests/second and an identifiable User-Agent carrying contact details.
This client holds itself to 8/second.
"""
from __future__ import annotations

import io
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import requests

from sources.contact import user_agent

#: ⚠️ The UA is **configured by whoever deploys this** (FZ_CONTACT); nobody's email is ever hardcoded —
#: otherwise, once open-sourced, every user's traffic goes out under the author's name and every
#: throttle or ban lands on him. See sources/contact.py.

ARCHIVES = "https://www.sec.gov/Archives"
DATASET_BASE = ("https://www.sec.gov/files/structureddata/data/"
                "insider-transactions-data-sets")


class NoXmlInFiling(RuntimeError):
    """The filing **document itself has no XML** (the pre-2003 plain-text format) — terminal, retrying will not help.

    ⚠️ Strictly distinct from "the object could not be fetched": that one may just have been indexed
    with the body not yet propagated, and will appear shortly. Use DataNotAvailable for both and a
    single propagation delay registers that filing permanently as dead and never fetches it again.
    """


class DataNotAvailable(RuntimeError):
    """That day or quarter genuinely holds no data (a non-trading day, a quarter not yet published) — the caller can safely skip it.

    ⚠️ Strictly distinct from "denied / rate-limited / network failure", which must propagate.
    SEC Archives is S3-backed and may answer a missing object with **403 AccessDenied (XML) rather than 404**
    (standard behaviour without ListBucket permission) — global-stock-data v2.0.1 was bitten by this,
    so `_is_object_missing()` below tells them apart by the response body and never by the status code alone.
    """


class _RateLimiter:
    """A thread-safe minimum-interval throttle."""

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


_limiter = _RateLimiter(8)          # the SEC allows 10/s; leave headroom


#: Canary URL: used on a 403 to answer "is it me that has been blocked?".
#: It has to be a lightweight resource that **reliably stays put** — using some particular filing
#: is wrong (filings age out, paths get guessed wrong, and the moment it 404s every legitimate 403
#: is misread as "blocked"). This directory index is 3.7KB and has always been there.
_CANARY = f"{ARCHIVES}/edgar/daily-index/index.json"


def _is_object_missing(resp: requests.Response) -> bool:
    """Tell "the object is not there" from "you are denied" — and measurement confirms **the response alone cannot**.

    Measured against SEC Archives (S3-backed) on 2026-07-26:

    | Case | Response |
    |---|---|
    | A missing **file object** | `404` + `<Code>NoSuchKey</Code>` |
    | A missing **daily index** (weekend, or a future date) | `403` + `<Code>AccessDenied</Code>` |
    | **The client is blocked** | `403` + `<Code>AccessDenied</Code>` (**an identical body**) |

    The last two are indistinguishable at the response level — without ListBucket permission, S3
    answers AccessDenied for a key that does not exist. So on a 403 this **draws no conclusion from
    the body** and instead probes a resource that must exist, as a canary:
    - canary responds → we are not blocked → this 403 means "that resource really is not there"
    - canary fails too → **we are blocked** → it has to propagate

    Judging "not there" from `403 + AccessDenied` alone, as this once did, records every day as
    "no filings that day" while blocked and marks it complete — after which it is **never retried**, and nothing ever errors.
    """
    if resp.status_code == 404:
        return True                      # NoSuchKey, unambiguous
    if resp.status_code != 403:
        return False
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "xml" not in ctype or "AccessDenied" not in (resp.text or "")[:500]:
        return False                     # a 403 that is not S3-shaped → treat as denied, always
    return _canary_ok()


_canary_state: dict[str, float] = {}
_canary_lock = threading.Lock()


def _canary_ok(ttl: float = 60.0) -> bool:
    """Probe "can we still reach the SEC?" (cached for 60s; a 403 is rare to begin with)."""
    with _canary_lock:
        ts = _canary_state.get("t", 0.0)
        if time.monotonic() - ts < ttl:
            return bool(_canary_state.get("ok"))
    ok = False
    try:
        _limiter.wait()
        r = requests.get(_CANARY, headers={"User-Agent": user_agent()},
                         timeout=30, stream=True)
        ok = r.status_code == 200
        r.close()
    except requests.RequestException:
        ok = False
    with _canary_lock:
        _canary_state["t"] = time.monotonic()
        _canary_state["ok"] = ok
    return ok


def _get(url: str, timeout: int = 60, binary: bool = False):
    """One fetch path: identify "not there" positively, and let everything else propagate."""
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"EDGAR network failure: {type(e).__name__}: {e}") from e
    if _is_object_missing(r):
        raise DataNotAvailable(f"EDGAR has no such resource: {url[-80:]}")
    if r.status_code == 429:
        raise RuntimeError(f"EDGAR rate limit (429): slow the request rate down. {url[-60:]}")
    if r.status_code == 403:
        raise RuntimeError(
            f"EDGAR denied access (403) and the canary request failed too — **this is denial, not absence of data**. "
            f"Usually a non-compliant User-Agent, or requests fast enough to earn a temporary ban. {url[-60:]}")
    if r.status_code != 200:
        raise RuntimeError(f"EDGAR HTTP {r.status_code}: {url[-80:]}")
    return r.content if binary else r.text


# ─────────────────────── Daily filing index ───────────────────────

@dataclass(frozen=True)
class FilingRef:
    """One filing's index entry (no detail — that needs a second fetch of the body)."""

    form: str
    company: str
    cik: str
    filed: str                    # YYYY-MM-DD
    accession: str                # 0000066740-26-000255
    txt_url: str                  # the full submission text file (XML inside)


def _recent_weekdays(n: int, end: Optional[date] = None) -> Iterator[date]:
    d = end or date.today()
    got = 0
    while got < n:
        if d.weekday() < 5:                       # no index at weekends
            yield d
            got += 1
        d -= timedelta(days=1)


def _norm_idx_date(raw: str) -> Optional[str]:
    """The index's filing date → `YYYY-MM-DD`. Returns None when unrecognised."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    return None


def form4_filings(day: date) -> list[FilingRef]:
    """Every Form 4 filing index entry for one day.

    ⚠️ Index entries only; **the detail lives in each filing's body** and costs one fetch apiece.
    """
    q = (day.month - 1) // 3 + 1
    url = f"{ARCHIVES}/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"
    raw = _get(url)

    lines = raw.splitlines()
    start = next((i + 1 for i, L in enumerate(lines) if L.startswith("---")), 11)
    out: list[FilingRef] = []
    for line in lines[start:]:
        # ⚠️ **Split from the right, never by column position.**
        # The header itself spans two lines ("Form Type Company Name CIK" / "Date Filed File Name"),
        # parsing at fixed byte offsets was measured splitting outright wrong (reading
        # "4    edgar/data/..." as the path), and the SEC has changed column widths before. Path,
        # date and CIK hold no spaces while company names may — so splitting three fields off the right is the sturdiest read, leaving the first token as form type and the middle as the company name.
        parts = line.split()
        if len(parts) < 4:
            continue
        form = parts[0]
        # 4/A are amendments: **fetch them** (otherwise the daily and quarterly paths cover different
        # ground), but store them tagged with form_type and exclude them by default when aggregating, so the original is not counted twice.
        if form not in ("4", "4/A"):
            continue
        path, filed_raw, cik = parts[-1], parts[-2], parts[-3]
        if not path.endswith(".txt") or not cik.isdigit():
            continue                               # line is not shaped as expected — skip rather than guess
        # The date has been YYYYMMDD throughout 2020-2026; ISO is accepted too, so that
        # a format change at the SEC does not silently erase a whole day of data
        filed = _norm_idx_date(filed_raw)
        if filed is None:
            continue
        company = " ".join(parts[1:-3])
        # edgar/data/66740/0000066740-26-000255.txt → accession
        acc = path.rsplit("/", 1)[-1].removesuffix(".txt")
        out.append(FilingRef(form=form, company=company, cik=cik, filed=filed,
                             accession=acc, txt_url=f"{ARCHIVES}/{path}"))
    if not out:
        # ⚠️ Distinguish "there genuinely were no filings that day" from "the parser cannot read
        # this index". Return DataNotAvailable for both and one SEC format change presents as
        # "no filings on any day" — a run that reports success over a blank dataset, the hardest kind of failure to find.
        data_lines = [L for L in lines[start:] if L.strip()]
        if data_lines:
            raise RuntimeError(
                f"The index for {day} has {len(data_lines)} data lines and not one Form 4 parsed out of them — "
                f"the index format may have changed. First line sample: {data_lines[0][:90]!r}")
        raise DataNotAvailable(f"No Form 4 filings on {day} (possibly a non-trading day)")
    return out


def recent_form4_days(days: int, end: Optional[date] = None) -> list[date]:
    """The last N business days (whether an index exists is only known once fetched)."""
    return list(_recent_weekdays(days, end))


def pending_form4_days(count: int, settled: set[str], max_back: int = 400,
                       end: Optional[date] = None,
                       floor: Optional[date] = None) -> list[date]:
    """Walk backwards, skipping days already done, and take the first `count` **outstanding** business days.

    ⚠️ `recent_form4_days(N)` will not do: it only ever looks at the last N business days.
    The gap between the quarterly dataset and today was measured at 117 days (≈83 business days),
    so once the most recent batch is synced, running again merely filters the same batch afresh —
    everything earlier is **never filled in**, the run reports no error, and nothing shows it is still missing.
    Written this way, repeated syncs fill the gap a stretch at a time.

    `floor`: **do not walk past this day** (inclusive). Pass the cutoff of the quarters already
    imported — carry on backwards after the gap closes and it starts re-downloading, filing by filing,
    data the quarterly ZIPs already hold (600-700 filings a day, about 90 seconds a day), walking on into years of duplicate history.
    """
    out: list[date] = []
    d = end or date.today()
    walked = 0
    while len(out) < count and walked < max_back:
        if floor is not None and d <= floor:
            break                                  # now inside the quarterly dataset's coverage, stop
        if d.weekday() < 5 and d.isoformat() not in settled:
            out.append(d)
        d -= timedelta(days=1)
        walked += 1
    return out


def filing_xml(txt_url: str) -> str:
    """Pull the ownershipDocument XML out of the full submission text file.

    A Form 4 `.txt` is an SGML multi-document container with the XML wrapped in `<XML>…</XML>`.
    Fetching the `.txt` takes **one request** — half of fetching the directory and then the xml.
    """
    raw = _get(txt_url)
    m = re.search(r"<XML>(.*?)</XML>", raw, re.S)
    if not m:
        # The document genuinely has no XML (the pre-2003 plain-text format) — this is **terminal**
        raise NoXmlInFiling(f"Filing contains no XML (early plain-text format): {txt_url[-60:]}")
    return m.group(1).strip()


# ─────────────────────── Quarterly structured dataset ───────────────────────

#: The three tables from the dataset that we use
DATASET_TABLES = ("SUBMISSION", "REPORTINGOWNER", "NONDERIV_TRANS")


def dataset_quarters(back: int = 8, today: Optional[date] = None,
                     start: Optional[str] = None) -> list[str]:
    """The last N quarter identifiers (newest first), e.g. ['2026q2', '2026q1', …].

    `start` sets the starting quarter (say '2026q1'); omitted, it counts back from the current calendar quarter.

    ⚠️ The dataset is **always behind**, so counting back from the current calendar quarter wastes
    the first slot or two on quarters not yet published — `back=2` was measured importing nothing at all.
    For "the last N **available** quarters" use `available_quarters()`.
    """
    if start:
        y, q = int(start.split("q")[0]), int(start.split("q")[1])
    else:
        d = today or date.today()
        y, q = d.year, (d.month - 1) // 3 + 1
    out = []
    for _ in range(back):
        out.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


def quarter_dataset(quarter: str) -> dict[str, list[dict]]:
    """Download and parse one quarter's insider transaction dataset.

    Returns {table name: [row dicts]}. ⚠️ About 100k transactions per quarter: it fits in memory, but it is not small.

    ⚠️ Raises `DataNotAvailable` when the quarter is not published yet (**which is not an error**) —
    the latest quarter or two frequently is not, anywhere from 7 to 49 days behind, with no stable pattern.
    """
    url = f"{DATASET_BASE}/{quarter}_form345.zip"
    blob = _get(url, timeout=180, binary=True)
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"{quarter} did not return a ZIP (possibly an error page)") from e

    out: dict[str, list[dict]] = {}
    names = {n.upper(): n for n in zf.namelist()}
    for table in DATASET_TABLES:
        real = names.get(f"{table}.TSV")
        if real is None:
            raise RuntimeError(f"The {quarter} dataset is missing {table}.tsv (its structure may have changed)")
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


def latest_available_quarter(back: int = 8) -> tuple[Optional[str], list[str]]:
    """Probe for the newest published quarter; returns (newest quarter, quarters probed but unpublished).

    Used to tell the user honestly that "the dataset only reaches quarter X, and anything after it comes from the daily fetch".
    """
    missing: list[str] = []
    for q in dataset_quarters(back):
        url = f"{DATASET_BASE}/{q}_form345.zip"
        _limiter.wait()
        try:
            r = requests.head(url, headers={"User-Agent": user_agent()}, timeout=30)
        except requests.RequestException as e:
            raise RuntimeError(f"Probing the quarterly dataset failed: {type(e).__name__}: {e}") from e
        if r.status_code == 200:
            return q, missing
        # ⚠️ **Only a 404 counts as "not published yet".**
        # This endpoint was measured to be nginx/Drupal rather than S3: an unpublished quarter
        # returns a clean 404 with an HTML error page, and a 403 can only mean the client or IP was denied.
        # Counting 403 as "not published" makes every quarter read as "no such file" while blocked —
        # the sync reports success with not one row of history imported, exactly the silent failure that is hardest to trace.
        if r.status_code == 404:
            missing.append(q)
            continue
        if r.status_code == 403:
            raise RuntimeError(
                f"Probing {q} was denied (403) — this is denial, not 'not published yet'. "
                f"Usually a non-compliant User-Agent, or an IP the SEC has restricted. Check and retry.")
        raise RuntimeError(f"Probing {q} returned HTTP {r.status_code}")
    return None, missing


def available_quarters(back: int = 4) -> tuple[list[str], list[str]]:
    """The last N **published** quarters (newest first), plus the unpublished ones found while probing.

    This is the entry point a sync should use: calling `dataset_quarters()` directly spends the
    budget on quarters not yet published, and ends in "it ran, it did not error, it imported nothing".
    """
    latest, missing = latest_available_quarter()
    if latest is None:
        return [], missing
    return dataset_quarters(back, start=latest), missing


def quarter_end(quarter: str) -> date:
    """'2026q1' → date(2026, 3, 31). Used to work out how far the dataset reaches."""
    y, q = quarter.split("q")
    m = int(q) * 3
    last = {3: 31, 6: 30, 9: 30, 12: 31}[m]
    return date(int(y), m, last)
