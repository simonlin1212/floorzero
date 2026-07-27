"""13F institutional holdings: parsing, classification and quarter-on-quarter change.

━━━ ⭐ "Institutional holdings" misleads as a phrase, so the limits come first ━━━

A 13F reports **long positions in 13(f) securities as of quarter-end**. It **excludes**:
short positions (the SEC created Form SHO in 2023 exactly because 13F does not cover them),
cash, bonds, commodities, stocks listed only outside the US, private holdings, and holdings granted confidential treatment.

So "this manager holds $X bn" is only **the part of their long side that happens to fall on the 13(f) list**:
neither their total assets nor their net exposure.

━━━ ⚠️ Options: the easiest place to read this data backwards ━━━

Form 13F Special Instruction 10 requires options to be listed under the **underlying security**, marked `PUT` / `CALL`.
So **a put — a bearish position — appears in the table shaped exactly like holding the stock**.
Measured on the 2026Q1 window: ordinary holdings $74.9tn / calls $2.95tn / **puts $3.66tn**.

→ Summing VALUE and calling it "institutions are buying" **counts $3.66tn of bearish exposure as bullish**.
   This module counts by `position_kind` (share / call / put) separately,
   and the default view holds ordinary shares only.

━━━ Three further traps (all measured) ━━━
1. **13F-NT is a notice filing and carries no holdings** (2,045 of them in that window, 18%).
2. **The unit of VALUE**: since 2023 it is **dollars** (measured implied share price median $53.30, which is plausible);
   earlier filings are in **thousands of dollars** — backfilling history without converting is off by 1000×.
3. **There are CUSIPs and no tickers.** Matching issuer names against `company_tickers.json`
   was measured hitting only **42.8%** (most misses being ETFs and funds) → a ticker is auxiliary only,
   and **aggregation and deduplication always run on CUSIP**.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from sources import edgar13f as src

#: The first reporting period where VALUE switched to dollars. Anything earlier is in thousands.
#: (The SEC amended Form 13F in 2022, effective from 2023.)
DOLLARS_FROM = date(2023, 1, 1)

#: Position kinds — **they must be kept apart**, see the module docstring
POSITION_KINDS = {
    "share": "ordinary holding",
    "call": "call option (on the underlying)",
    "put": "put option (on the underlying, bearish)",
}


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _parse_date(v: Optional[str]) -> Optional[date]:
    """Parse `31-MAR-2026` (the dataset) or `2026-03-31`."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def position_kind(putcall: Optional[str]) -> str:
    """The PUTCALL field → position kind. Empty = an ordinary holding."""
    s = (putcall or "").strip().lower()
    if s.startswith("put"):
        return "put"
    if s.startswith("call"):
        return "call"
    return "share"


@dataclass(frozen=True)
class Holding:
    """One 13F holding record."""

    accession: str
    holding_key: str              # stable primary key (see assign_keys)
    manager: str                  # name of the filing institution
    manager_cik: str
    period: Optional[date]        # reporting period (quarter-end)
    filing_date: Optional[date]
    is_amendment: bool
    cusip: str                    # ⭐ this is the key, not the ticker
    issuer: str
    title_of_class: str
    kind: str                     # share / call / put
    value: Optional[float]        # dollars (already converted for the filing period)
    shares: Optional[float]
    shares_type: str              # SH (share count) / PRN (principal amount)
    discretion: str               # SOLE / DEFINED / OTHER
    voting_sole: Optional[float]
    voting_shared: Optional[float]
    voting_none: Optional[float]
    source_url: str

    @property
    def kind_label(self) -> str:
        return POSITION_KINDS.get(self.kind, self.kind)


def to_dict(h: Holding) -> dict:
    return {
        "accession": h.accession, "holding_key": h.holding_key,
        "manager": h.manager, "manager_cik": h.manager_cik,
        "period": h.period.isoformat() if h.period else None,
        "filing_date": h.filing_date.isoformat() if h.filing_date else None,
        "is_amendment": h.is_amendment,
        "cusip": h.cusip, "issuer": h.issuer, "title_of_class": h.title_of_class,
        "kind": h.kind, "kind_label": h.kind_label,
        "value": h.value, "shares": h.shares, "shares_type": h.shares_type,
        "discretion": h.discretion,
        "voting_sole": h.voting_sole, "voting_shared": h.voting_shared,
        "voting_none": h.voting_none, "source_url": h.source_url,
    }


def accession_url(cik: str, accession: str) -> str:
    cik = (cik or "").lstrip("0")
    if not cik or not accession:
        return ""
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


def _value_dollars(raw: Optional[str], period: Optional[date]) -> Optional[float]:
    """VALUE → dollars.

    ⚠️ Filings before 2023 are in **thousands of dollars** (the SEC amended Form 13F in 2022).
    Without the conversion, backfilling 2022 and earlier comes out a full 1000× off —
    and because the numbers still "look about right", it is not easy to spot.
    """
    v = _num(raw)
    if v is None:
        return None
    if period and period < DOLLARS_FROM:
        return v * 1000.0
    return v


def assign_keys(rows: Iterable[tuple[str, str, str, str]]) -> list[str]:
    """Assign holdings a stable key: `cusip|kind|discretion#index` (counted within a filing).

    ⚠️ `(accession, cusip)` will not do: within one filing, the same CUSIP splits across rows by
    **position kind** (shares / calls / puts) and by **investment discretion**
    (SOLE / DEFINED / different other managers) — all of them legitimately distinct rows.
    Nor will row order (the dataset and the per-filing parse are not guaranteed to agree on it).
    """
    seen: dict[tuple[str, str], int] = {}
    out: list[str] = []
    for acc, cusip, kind, disc in rows:
        base = f"{cusip}|{kind}|{disc}"
        n = seen.get((acc, base), 0)
        seen[(acc, base)] = n + 1
        out.append(f"{base}#{n}")
    return out


def iter_holdings(zf, src, period: str, min_value: float = 0.0):
    """Yield one reporting period's holdings **as a stream** — constant memory.

    ⚠️ Streaming is mandatory: reading INFOTABLE (3.8m rows) whole into dicts and then parsing was
    measured peaking at **5.3 GB**, which gets an 8GB machine OOM-killed.
    Only the small tables (SUBMISSION/COVERPAGE, ~12k rows each) are read into memory to index on,
    while INFOTABLE is filtered and yielded as it is read.

    `min_value` is applied here, so rows destined to be dropped are never built into objects.
    Yields `(Holding, whether the threshold dropped it, that row's value)`, so the caller can report what was discarded.
    """
    want = _parse_date(period)

    subs: dict[str, dict] = {}
    for r in src.read_table(zf, "SUBMISSION"):
        # Use the shared constant from sources; do not write a second copy here (two definitions drift sooner or later)
        if (r.get("SUBMISSIONTYPE") or "").strip().upper() not in src.HOLDINGS_TYPES:
            continue                       # exclude 13F-NT (a notice filing, carrying no holdings)
        if want and _parse_date(r.get("PERIODOFREPORT")) != want:
            continue
        subs[r["ACCESSION_NUMBER"]] = r
    if not subs:
        return

    covers = {r["ACCESSION_NUMBER"]: r
              for r in src.read_table(zf, "COVERPAGE")
              if r["ACCESSION_NUMBER"] in subs}

    seen: dict[tuple[str, str], int] = {}
    for r in src.iter_table(zf, "INFOTABLE"):
        acc = r["ACCESSION_NUMBER"]
        s = subs.get(acc)
        if s is None:
            continue
        p = _parse_date(s.get("PERIODOFREPORT"))
        val = _value_dollars(r.get("VALUE"), p)
        if min_value and (val or 0) < min_value:
            yield None, True, (val or 0.0)      # dropped by the threshold, but still counted
            continue
        cusip = (r.get("CUSIP") or "").strip().upper()
        kind = position_kind(r.get("PUTCALL"))
        disc = (r.get("INVESTMENTDISCRETION") or "").strip().upper()
        base = f"{cusip}|{kind}|{disc}"
        n = seen.get((acc, base), 0)
        seen[(acc, base)] = n + 1
        cp = covers.get(acc, {})
        cik = (s.get("CIK") or "").lstrip("0")
        yield Holding(
            accession=acc, holding_key=f"{base}#{n}",
            manager=(cp.get("FILINGMANAGER_NAME") or "").strip() or "(unknown)",
            manager_cik=cik, period=p, filing_date=_parse_date(s.get("FILING_DATE")),
            is_amendment=(s.get("SUBMISSIONTYPE") or "").strip().upper().endswith("/A"),
            cusip=cusip, issuer=(r.get("NAMEOFISSUER") or "").strip(),
            title_of_class=(r.get("TITLEOFCLASS") or "").strip(),
            kind=kind, value=val, shares=_num(r.get("SSHPRNAMT")),
            shares_type=(r.get("SSHPRNAMTTYPE") or "").strip().upper(),
            discretion=disc,
            voting_sole=_num(r.get("VOTING_AUTH_SOLE")),
            voting_shared=_num(r.get("VOTING_AUTH_SHARED")),
            voting_none=_num(r.get("VOTING_AUTH_NONE")),
            source_url=accession_url(cik, acc),
        ), False, (val or 0.0)


def parse_dataset(tables: dict[str, list[dict]],
                  period: Optional[str] = None) -> list[Holding]:
    """⚠️ **Deprecated** (it keeps whole tables in memory) — new code should use `iter_holdings()`.

    Stitch the dataset's three tables into a list of holdings.

    `period`: take one reporting period only (`YYYY-MM-DD`). **Strongly recommended** —
    one window mixes several reporting periods (measured with late filings going back to 2008),
    and without the filter "this quarter's institutional holdings" picks up a decade of history.

    ⚠️ Only 13F-HR / 13F-HR/A are taken: **13F-NT is a notice filing and carries no holdings at all**
    (2,045 of them in that window, 18%). Leave them in and those managers
    appear in the tables holding nothing.
    """
    want = _parse_date(period) if period else None

    subs: dict[str, dict] = {}
    for r in tables.get("SUBMISSION", []):
        stype = (r.get("SUBMISSIONTYPE") or "").strip().upper()
        if stype not in src.HOLDINGS_TYPES:      # the same constant the streaming path uses
            continue                       # exclude 13F-NT / 13F-NT/A
        p = _parse_date(r.get("PERIODOFREPORT"))
        if want and p != want:
            continue
        subs[r["ACCESSION_NUMBER"]] = r

    covers = {r["ACCESSION_NUMBER"]: r for r in tables.get("COVERPAGE", [])}

    raw: list[tuple] = []
    for r in tables.get("INFOTABLE", []):
        acc = r["ACCESSION_NUMBER"]
        s = subs.get(acc)
        if s is None:
            continue
        raw.append((r, s, covers.get(acc, {})))

    keys = assign_keys(
        (r["ACCESSION_NUMBER"], (r.get("CUSIP") or "").strip().upper(),
         position_kind(r.get("PUTCALL")),
         (r.get("INVESTMENTDISCRETION") or "").strip().upper())
        for r, _, _ in raw)

    out: list[Holding] = []
    for (r, s, cp), key in zip(raw, keys):
        p = _parse_date(s.get("PERIODOFREPORT"))
        cik = (s.get("CIK") or "").lstrip("0")
        acc = r["ACCESSION_NUMBER"]
        out.append(Holding(
            accession=acc, holding_key=key,
            manager=(cp.get("FILINGMANAGER_NAME") or "").strip() or "(unknown)",
            manager_cik=cik,
            period=p, filing_date=_parse_date(s.get("FILING_DATE")),
            is_amendment=(s.get("SUBMISSIONTYPE") or "").strip().upper().endswith("/A"),
            cusip=(r.get("CUSIP") or "").strip().upper(),
            issuer=(r.get("NAMEOFISSUER") or "").strip(),
            title_of_class=(r.get("TITLEOFCLASS") or "").strip(),
            kind=position_kind(r.get("PUTCALL")),
            value=_value_dollars(r.get("VALUE"), p),
            shares=_num(r.get("SSHPRNAMT")),
            shares_type=(r.get("SSHPRNAMTTYPE") or "").strip().upper(),
            discretion=(r.get("INVESTMENTDISCRETION") or "").strip().upper(),
            voting_sole=_num(r.get("VOTING_AUTH_SOLE")),
            voting_shared=_num(r.get("VOTING_AUTH_SHARED")),
            voting_none=_num(r.get("VOTING_AUTH_NONE")),
            source_url=accession_url(cik, acc),
        ))
    return out


# ─────────────────────── Name → ticker (best effort) ───────────────────────

_SUFFIX = re.compile(
    r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|SA|NV|AG|LLC|LP|"
    r"TRUST|GROUP|HOLDINGS?|THE|CLASS|COM|NEW|SE|CL|A|B)\b")


def normalize_issuer(name: str) -> str:
    """Normalise an issuer name (for matching against the SEC's company_tickers.json)."""
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_ticker_map(company_tickers: dict) -> dict[str, str]:
    """SEC `company_tickers.json` → {normalised name: ticker}.

    ⚠️ **This is best effort only**: measured against 13F issuers it hits just **42.8%**,
    and the overwhelming majority of misses are ETFs and funds (`company_tickers.json` covers operating companies only).
    So in this section a ticker is an **auxiliary display field**;
    aggregation, deduplication and quarter-on-quarter comparison all run on CUSIP.
    """
    out: dict[str, str] = {}
    for v in (company_tickers or {}).values():
        n = normalize_issuer(v.get("title", ""))
        if n and n not in out:
            out[n] = v.get("ticker", "")
    return out
