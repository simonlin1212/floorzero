"""Insider trades (Form 4): parsing, classification and aggregation.

━━━ ⭐ The entire value of this section lies in telling the transaction types apart ━━━

**Most rows in a Form 4 are not "an insider likes the stock, so they bought it".**
Across 103,733 non-derivative transactions market-wide in 2026Q1, the measured code mix:

    F tax-withheld 27,019 │ A grant 24,690 │ S sale 22,822 │ M exercise 16,300
    P open-market buy **5,935 (just 5.6%)** │ all of D/J/G/C/L/X/U/I/W together ~7k

Count instead by the SEC's acquired/disposed flag (`TRANS_ACQUIRED_DISP_CD`) and
48,849 rows come out as "acquired" — **eight times the real open-market buying**.
Reading grants, exercises and gifts as "insiders buying" is the classic misreading of this data.

**A measured sample (3M CO, 2026-07-23, one filing, two rows):**

    M exercise 7,880 shares @ $154.69  → flagged "acquired"
    S sale     7,880 shares @ $170.44  → flagged "disposed"

Exercised and sold the same day. A blunt count reports "insiders bought $1.2m";
**he bought not one share on the open market and sold all he received** — pay being cashed out, not conviction.

→ Hence this module's first principle: **classify by transaction code, not by the acquired/disposed flag**.
   `is_open_market` (P/S) is the part that carries any signal.

━━━ The other dimension: Rule 10b5-1 pre-arranged plans ━━━
A sale made under a plan adopted in advance was scheduled months ago, which carries
nothing like the weight of "decided to sell today". The SEC has required the box be ticked since 2023.
⚠️ The field's encoding is a mess: within a single quarter `0/1`, `false/true` and blanks all coexist, so it has to be normalised.

━━━ Disclaimer ━━━
This module classifies and counts. **It attaches no bullish/bearish label and no score.**
Whether insider trades predict anything is academically contested, and disclosure lags; presenting the facts is enough.
"""
from __future__ import annotations

import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

#: The SEC's official Form 4 transaction codes (read 2026-07-26 from https://www.sec.gov/files/form4.pdf §8)
TX_CODES: dict[str, str] = {
    # General
    "P": "open-market purchase", "S": "open-market sale", "V": "voluntary early filing",
    # Rule 16b-3 (compensation-related; most carry no active buy or sell intent)
    "A": "grant/award", "D": "disposition to the issuer", "F": "tax withheld / exercise price paid",
    "I": "16b-3(f) discretionary transaction", "M": "option exercise/conversion",
    # Derivatives
    "C": "derivative conversion", "E": "short derivative expired", "H": "long derivative expired (value received)",
    "O": "out-of-the-money option exercise", "X": "in- or at-the-money option exercise",
    # Exempt and small
    "G": "gift", "L": "16a-6 small acquisition", "W": "inheritance/bequest", "Z": "voting trust deposit or withdrawal",
    # Other
    "J": "other (explanation required)", "K": "equity swap", "U": "change-of-control tender",
}

#: ⭐ **Only these two codes are active open-market trading** — the only part that carries signal
OPEN_MARKET = frozenset({"P", "S"})

#: Compensation and mechanical transactions: grants, exercises, tax withholding, disposition to the issuer.
#: Kept separate because they are **by far the most numerous**; mixed into the totals they drown the real signal.
COMPENSATION = frozenset({"A", "M", "F", "D", "I"})


def code_label(code: str) -> str:
    """Transaction code → label. Unknown codes come back as they are, never forced into a known bucket."""
    if not code:
        return "unknown"
    # A compound code such as "S/K" (swap) takes the primary code
    return TX_CODES.get(code.split("/")[0].strip().upper(), code)


def code_group(code: str) -> str:
    """Transaction code → group: open_market / compensation / other."""
    c = (code or "").split("/")[0].strip().upper()
    if c in OPEN_MARKET:
        return "open_market"
    if c in COMPENSATION:
        return "compensation"
    return "other"


def _norm_bool(v: Optional[str]) -> Optional[bool]:
    """Normalise the three boolean encodings that coexist in SEC data.

    ⚠️ In 2026Q1 the `AFF10B5ONE` field carries `0/1`, `false/true` and blanks at the same time.
    Recognising only one of them leaves most 10b5-1 plan trades unidentified.
    """
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "y", "yes"):
        return True
    if s in ("0", "false", "n", "no"):
        return False
    return None


#: Ways filers write "there is no symbol"
_NO_TICKER = {"NONE", "N/A", "NA", "N//A", "--", "-", "", "TBD", "NOT APPLICABLE",
              "NO SYMBOL", "NONE.", "0"}
#: Exchange prefixes (`NYSE: KRC` / `ASX:LNW` / `NASDAQ: XYZ`)
_EXCHANGE_PREFIX = re.compile(
    r"^(?:NYSE|NASDAQ|NYSEAMERICAN|NYSE AMERICAN|AMEX|OTC|OTCQB|OTCQX|ASX|TSX|LSE)\s*[:：]\s*",  # cn-ok: filers do type a full-width colon
    re.I)


def clean_ticker(raw: Optional[str]) -> Optional[str]:
    """Normalise `ISSUERTRADINGSYMBOL` — a field **the filer types freely**, and it is dirty.

    Real values across 160k rows: `NONE`(875) / `N/A`(209) / `MOGA/MOGB`(107) /
    `GEF, GEF-B`(99) / `Z AND ZG`(87) / `NYSE: KRC`(58) / `(SIRI)`(38) / `N O G`(44).
    Left alone, `NONE` walks to the top of "most active tickers" with 875 trades — a company that does not exist.

    The conventions (**all of them lossy judgements, which is why they are written here rather than hidden inside a regex**):
    - Spellings that explicitly mean "no symbol" → None
    - Strip exchange prefixes and brackets, squeeze out internal spaces (`N O G` → `NOG`)
    - **Multiple symbols (dual-class) take the first** (`MOGA/MOGB` → `MOGA`):
      for share classes of one company, folding into the primary symbol is closer to the truth than inventing two tickers
    - Still not symbol-shaped after cleaning (>6 characters, or illegal ones) → None, never forced
    """
    if raw is None:
        return None
    t = str(raw).strip().upper()
    if t in _NO_TICKER:
        return None
    t = _EXCHANGE_PREFIX.sub("", t).strip()
    t = t.strip("()[]{} ")
    # Multiple symbols: separated by /, comma or AND → take the first
    t = re.split(r"\s*(?:/|,|\bAND\b|\|)\s*", t)[0].strip()
    t = t.replace(" ", "")                      # `N O G` → `NOG`
    if t in _NO_TICKER or not t:
        return None
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", t):
        return None                             # not symbol-shaped, so not accepted — no invented data
    return t


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _parse_date(v: Optional[str]) -> Optional[date]:
    """Parse `YYYY-MM-DD` (XML) or `01-APR-2026` (dataset TSV)."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class InsiderTrade:
    """One insider transaction."""

    accession: str
    seq: int
    ticker: Optional[str]
    company: str
    issuer_cik: str
    owner: str
    owner_cik: str
    is_officer: bool
    is_director: bool
    is_ten_pct: bool
    officer_title: str
    security: str
    tx_code: str
    tx_date: Optional[date]
    filing_date: Optional[date]
    shares: Optional[float]
    price: Optional[float]
    acquired_disposed: str            # A / D (the SEC's own flag, for reference only)
    shares_after: Optional[float]
    is_direct: bool                   # D=held directly / I=indirectly
    is_10b5_1: Optional[bool]
    form_type: str                    # 4 / 4/A (only these two are taken here, see parse_dataset)
    source_url: str

    @property
    def group(self) -> str:
        return code_group(self.tx_code)

    @property
    def is_open_market(self) -> bool:
        """⭐ Active open-market trading — the only part that carries signal."""
        return self.group == "open_market"

    @property
    def direction(self) -> Optional[str]:
        """Buy or sell direction, **meaningful only for open-market transactions**.

        ⚠️ Deliberately returns nothing for compensation transactions: a grant is flagged
        "acquired" and tax withholding "disposed", so treating them as trades wrecks the
        totals entirely (measured: "acquired" rows run 8× the real purchases).
        """
        if not self.is_open_market:
            return None
        return "buy" if self.tx_code.split("/")[0].upper() == "P" else "sell"

    @property
    def date_anomaly(self) -> Optional[str]:
        """Trade date after the filing date — **physically impossible** (you cannot file before you trade).

        14 rows out of 165k, every one a year typo:
        PRCH reports "trade 2028-03-19, filed 2026-03-20";
        NFRX reports "trade 2002-02-24, filed 2026-02-25" — month and day line up, only the year is wrong.

        ⚠️ **There are far more typos of this kind than those 14**: within the 366-day-delay batch (1,270 rows),
        ASTS reports "trade 2025-03-17 / filed 2026-03-18", one day apart and one year apart,
        plainly a typo as well — but "filed a year late" is not legally impossible,
        and **it cannot be confirmed row by row, so it is not flagged**. The delay figures therefore still carry a few typos; do not over-read them.

        Same stance as with prices: **do not edit the filer's data**, only mark the part that is certainly impossible.
        """
        if self.tx_date and self.filing_date and self.tx_date > self.filing_date:
            return "trade date after filing date (as filed; probably a year typo)"
        return None

    @property
    def price_implausible(self) -> bool:
        """A per-share price that is plainly impossible — **the filer put the total value into the price-per-share field**.

        Measured (2026-07-26, 165k rows):
        - REEMF reports 100,149,060 shares × "$24,035,774.40/share" → **$2.4 quadrillion** in one row.
          The raw XML really does carry it in `transactionPricePerShare`;
          and $24,035,774.40 ÷ 100,149,060 = **exactly $0.2400/share** — the total, beyond doubt.
        - Also PSX at $2,110,482/share and LLY at $1,032,319/share (real prices $130 / $800).

        The threshold is **$1m/share**: BRK.A at ~$700k/share is the highest price in US market history,
        so anything above that is physically impossible and the test cannot catch a real trade.

        ⚠️ **This filter is incomplete**: IHT's $14,561/share (real price about $2) is the same
        mistake but falls under the threshold — with no external quote to compare against, it cannot be spotted.
        So the claim is never "the values are clean", only "the provably wrong entries are out".

        **No auto-correction** (dividing the total by the share count): that would be guessing at the filer's intent.
        Instead such rows are dropped from the totals, kept in the detail and marked.
        """
        if self.price is not None and self.price > 1_000_000:
            return True
        # Second anchor: a single trade larger than any US individual's holding.
        # Musk's TSLA stake, about $150bn, is the largest individual position known,
        # so a single trade > $200bn has to be a mis-entry (measured: MYNZ reports $402,000/share
        # × 643,850 shares = $258.8bn, while Mainz Biomed actually trades under $1).
        # ⚠️ This cannot be set any lower: TSLA has a perfectly normal $141.6bn row at $334.09
        # (a trust transfer of Musk's whole position), and killing that one deletes real data.
        if self.shares is not None and self.price is not None:
            if self.shares * self.price > 200_000_000_000:
                return True
        return False

    @property
    def value(self) -> Optional[float]:
        """Trade value = shares × price.

        Returns None when there is no price, or the price is plainly mis-entered — **better nothing than a fake number**:
        one mis-entered row is enough to push market-wide buying to $4.8 quadrillion.
        """
        if self.shares is None or self.price is None or self.price_implausible:
            return None
        return self.shares * self.price

    @property
    def delay_days(self) -> Optional[int]:
        """Days from trade date to filing date.

        Section 16(a) requires filing **within two business days of the trade** (since 2003).
        This reports the plain day count and makes no finding of violation (holidays, amendments and so on).
        """
        if not self.tx_date or not self.filing_date:
            return None
        return (self.filing_date - self.tx_date).days


def to_dicts(trades: list[InsiderTrade]) -> list[dict]:
    """Convert in bulk and assign stable primary keys — **every write goes through this**, never to_dict row by row."""
    keys = assign_trade_keys(trades)
    out = []
    for t, k in zip(trades, keys):
        d = to_dict(t)
        d["trade_key"] = k
        out.append(d)
    return out


def to_dict(t: InsiderTrade) -> dict:
    return {
        "accession": t.accession, "seq": t.seq,
        # trade_key is assigned in bulk by assign_trade_keys() (which needs the counting
        # context within a filing); a placeholder here, filled in by to_dicts()
        "trade_key": None,
        "ticker": t.ticker, "company": t.company, "issuer_cik": t.issuer_cik,
        "owner": t.owner, "owner_cik": t.owner_cik,
        "is_officer": t.is_officer, "is_director": t.is_director,
        "is_ten_pct": t.is_ten_pct, "officer_title": t.officer_title,
        "security": t.security,
        "tx_code": t.tx_code, "tx_code_label": code_label(t.tx_code),
        "group": t.group, "is_open_market": t.is_open_market,
        "direction": t.direction,
        "tx_date": t.tx_date.isoformat() if t.tx_date else None,
        "filing_date": t.filing_date.isoformat() if t.filing_date else None,
        "shares": t.shares, "price": t.price, "value": t.value,
        "price_implausible": t.price_implausible,
        "date_anomaly": t.date_anomaly,
        "acquired_disposed": t.acquired_disposed,
        "shares_after": t.shares_after, "is_direct": t.is_direct,
        "is_10b5_1": t.is_10b5_1, "form_type": t.form_type,
        "is_amendment": t.form_type.endswith("/A"),
        "delay_days": t.delay_days,
        "source_url": t.source_url,
    }


# ─────────────────────── XML parsing (recent filings) ───────────────────────

def trade_key(accession: str, security: str, tx_date, tx_code: str,
              shares, price, acquired_disposed: str, owner: str) -> str:
    """A **content fingerprint** for one transaction, used as a stable cross-source primary key.

    ⚠️ "Which row it was in the filing" cannot serve as the key: the quarterly dataset orders
    by TSV row and the XML by element, and the two are not guaranteed to agree. The moment
    they disagree, `(accession, seq)` maps **two different transactions** onto one key —
    and `INSERT OR IGNORE` then silently drops the right one, without a trace.

    With a content fingerprint the same transaction gets the same key whichever path it came
    in by, and a difference in order cannot shift rows onto one another.

    ⚠️ But **a fingerprint on its own loses data**: one filing legitimately contains several rows
    of identical content (2026Q1 has 976 such groups, the largest of 8 — same security, same day,
     same price, merely split across an IRA and a Roth IRA). Deduplicating on the fingerprint alone drops 1,120 rows.
    So the real key is `fingerprint + occurrence index within that fingerprint` (see `assign_trade_keys`):
    every duplicate is kept, while a cross-source difference in order still cannot shift rows
    (as long as both sides parse out the same multiset).
    """
    import hashlib

    d = tx_date.isoformat() if hasattr(tx_date, "isoformat") else str(tx_date or "")
    raw = "|".join([
        accession or "", (security or "").strip().upper(), d,
        (tx_code or "").strip().upper(),
        f"{float(shares):.4f}" if shares is not None else "",
        f"{float(price):.6f}" if price is not None else "",
        (acquired_disposed or "").strip().upper(), (owner or "").strip().upper(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def assign_trade_keys(trades: list["InsiderTrade"]) -> list[str]:
    """Assign stable primary keys to a batch: `fingerprint#index-within-fingerprint`.

    Counted per filing, so identical content inside one filing gets `#0`/`#1`/…,
    which neither overwrites itself nor depends on iteration order (the two sides need only agree as multisets).
    """
    seen: dict[tuple[str, str], int] = {}
    out: list[str] = []
    for t in trades:
        fp = trade_key(t.accession, t.security, t.tx_date, t.tx_code,
                       t.shares, t.price, t.acquired_disposed, t.owner)
        n = seen.get((t.accession, fp), 0)
        seen[(t.accession, fp)] = n + 1
        out.append(f"{fp}#{n}")
    return out


def accession_url(cik: str, accession: str) -> str:
    """Link to the filing itself (EDGAR's readable index page). Shared by both import paths, so the style cannot drift."""
    cik = (cik or "").lstrip("0")
    if not cik or not accession:
        return ""
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


def _txt(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    e = node.find(path)
    return (e.text or "").strip() if e is not None and e.text else None


def parse_form4_xml(xml: str, ref) -> list[InsiderTrade]:
    """Parse the ownershipDocument XML of one Form 4.

    ⚠️ A filing may have **several reporting owners** (2026Q1: up to 10, with 927 filings above 1).
    Their names are joined for display and their relationships unioned —
    taking only the first would lose the fact that it was a joint filing.
    """
    root = ET.fromstring(xml)

    issuer = root.find("issuer")
    ticker = clean_ticker(_txt(issuer, "issuerTradingSymbol"))
    company = _txt(issuer, "issuerName") or ref.company
    issuer_cik = (_txt(issuer, "issuerCik") or ref.cik).lstrip("0") or ref.cik

    owners, ciks = [], []
    is_officer = is_director = is_ten_pct = False
    titles = []
    for ro in root.findall("reportingOwner"):
        owners.append(_txt(ro, "reportingOwnerId/rptOwnerName") or "")
        ciks.append(_txt(ro, "reportingOwnerId/rptOwnerCik") or "")
        rel = ro.find("reportingOwnerRelationship")
        if rel is not None:
            is_officer |= _norm_bool(_txt(rel, "isOfficer")) is True
            is_director |= _norm_bool(_txt(rel, "isDirector")) is True
            is_ten_pct |= _norm_bool(_txt(rel, "isTenPercentOwner")) is True
            t = _txt(rel, "officerTitle")
            if t:
                titles.append(t)

    # 10b5-1 plan flag (the SEC has required the box since 2023)
    plan = _norm_bool(_txt(root, "aff10b5One"))

    filing_date = _parse_date(ref.filed)
    # **This and the quarterly-dataset path both use `-index.htm`**: one transaction imported
    # from two sources should not yield two styles of link. (The flat `.txt` is just as
    # reachable, but shows the raw SGML; `-index.htm` is the readable filing index, and reads better.)
    url = accession_url(ref.cik, ref.accession)

    out: list[InsiderTrade] = []
    for i, tr in enumerate(root.iter("nonDerivativeTransaction")):
        out.append(InsiderTrade(
            accession=ref.accession, seq=i,
            ticker=ticker, company=company, issuer_cik=issuer_cik,
            owner=" / ".join(x for x in owners if x) or "(unknown)",
            owner_cik=",".join(x for x in ciks if x),
            is_officer=is_officer, is_director=is_director, is_ten_pct=is_ten_pct,
            officer_title=" / ".join(dict.fromkeys(titles)),
            security=_txt(tr, "securityTitle/value") or "",
            tx_code=_txt(tr, "transactionCoding/transactionCode") or "",
            tx_date=_parse_date(_txt(tr, "transactionDate/value")),
            filing_date=filing_date,
            shares=_num(_txt(tr, "transactionAmounts/transactionShares/value")),
            price=_num(_txt(tr, "transactionAmounts/transactionPricePerShare/value")),
            acquired_disposed=_txt(
                tr, "transactionAmounts/transactionAcquiredDisposedCode/value") or "",
            shares_after=_num(_txt(
                tr, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            is_direct=(_txt(tr, "ownershipNature/directOrIndirectOwnership/value")
                       or "D").upper().startswith("D"),
            is_10b5_1=plan, form_type=ref.form, source_url=url,
        ))
    return out


# ─────────────────────── TSV parsing (quarterly dataset) ───────────────────────

def parse_dataset(tables: dict[str, list[dict]]) -> list[InsiderTrade]:
    """Stitch the quarterly dataset's three tables into a list of transactions.

    ⚠️ REPORTINGOWNER to SUBMISSION is **one-to-many** (up to 10 owners per filing),
    so owners are aggregated by accession first and only then joined to transactions — a direct join doubles the transactions.
    """
    # ⚠️ **This ZIP bundles Forms 3, 4 and 5, so it must be filtered by form type first**.
    # 2026Q1: Form 4 = 100,341 rows (median delay **2 days**, exactly what the law requires);
    # while **Form 5 = 2,168 rows, median delay 274 days, 32.6% over a year** —
    # it is the **annual catch-up filing**, late by design, not filled in wrong by anyone.
    # Letting it through inflates "insider filing delay" across the board, and counts Form 3
    # (an initial statement of holdings, not a transaction at all) as trading.
    # (This also corrects an earlier misreading in this project: that batch of 366+ day delays
    #  is mostly Form 5, not "year typos".)
    keep = {"4", "4/A"}
    subs = {r["ACCESSION_NUMBER"]: r for r in tables.get("SUBMISSION", [])
            if (r.get("DOCUMENT_TYPE") or "").strip() in keep}

    owners: dict[str, dict] = {}
    for r in tables.get("REPORTINGOWNER", []):
        acc = r["ACCESSION_NUMBER"]
        o = owners.setdefault(acc, {"names": [], "ciks": [], "titles": [],
                                    "officer": False, "director": False, "ten": False})
        if r.get("RPTOWNERNAME"):
            o["names"].append(r["RPTOWNERNAME"])
        if r.get("RPTOWNERCIK"):
            o["ciks"].append(r["RPTOWNERCIK"])
        if r.get("RPTOWNER_TITLE"):
            o["titles"].append(r["RPTOWNER_TITLE"])
        rel = (r.get("RPTOWNER_RELATIONSHIP") or "").lower()
        o["officer"] |= "officer" in rel
        o["director"] |= "director" in rel
        o["ten"] |= "10" in rel or "ten" in rel

    out: list[InsiderTrade] = []
    seq_by_acc: dict[str, int] = {}
    for r in tables.get("NONDERIV_TRANS", []):
        acc = r["ACCESSION_NUMBER"]
        s = subs.get(acc)
        if s is None:
            continue                       # transaction matches no filing header — skip it rather than invent one
        o = owners.get(acc, {})
        seq = seq_by_acc.get(acc, 0)
        seq_by_acc[acc] = seq + 1
        cik = (s.get("ISSUERCIK") or "").lstrip("0")
        out.append(InsiderTrade(
            accession=acc, seq=seq,
            ticker=clean_ticker(s.get("ISSUERTRADINGSYMBOL")),
            company=s.get("ISSUERNAME") or "",
            issuer_cik=cik,
            owner=" / ".join(o.get("names", [])) or "(unknown)",
            owner_cik=",".join(o.get("ciks", [])),
            is_officer=bool(o.get("officer")), is_director=bool(o.get("director")),
            is_ten_pct=bool(o.get("ten")),
            officer_title=" / ".join(dict.fromkeys(o.get("titles", []))),
            security=r.get("SECURITY_TITLE") or "",
            tx_code=(r.get("TRANS_CODE") or "").strip().upper(),
            tx_date=_parse_date(r.get("TRANS_DATE")),
            filing_date=_parse_date(s.get("FILING_DATE")),
            shares=_num(r.get("TRANS_SHARES")),
            price=_num(r.get("TRANS_PRICEPERSHARE")),
            acquired_disposed=(r.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper(),
            shares_after=_num(r.get("SHRS_OWND_FOLWNG_TRANS")),
            is_direct=not (r.get("DIRECT_INDIRECT_OWNERSHIP") or "D").upper().startswith("I"),
            is_10b5_1=_norm_bool(s.get("AFF10B5ONE")),
            form_type=(s.get("DOCUMENT_TYPE") or "4").strip(),
            source_url=accession_url(cik, acc),
        ))
    return out


# ─────────────────────── Aggregation ───────────────────────

def summary_notes(library: Optional[dict]) -> dict:
    """Wording of the definitions (shared by REST and MCP, so the two cannot drift apart)."""
    return {
        "forms": "This page holds Form 4 only (amendments 4/A excluded by default, to avoid "
                 "double-counting the original). The same SEC dataset also carries Form 3, an "
                 "initial statement of holdings rather than a transaction, and Form 5, the annual "
                 "catch-up filing at a median 274 days late with 32.6% over a year — both excluded, "
                 "or they would inflate insider filing delay across the board. Separated out, Form 4's median delay is 2 days, matching the statute.",
        "classification": _classification_note(library, 0, 0, 0),
        "plan": "A 10b5-1 plan is arranged in advance, so a sale under one was often scheduled "
                "months earlier and means something different from a sale decided on the day. "
                "Note that not marked, which is every filing before 2023, is not the same as marked as not under a plan.",
        "price": "Some filings put the total value into the price-per-share field; one reads "
                 "$24m per share. Rows above $1m per share, or above $200bn for a single trade, "
                 "are dropped from the value totals, but **subtler mis-entries cannot be caught** — read the values as approximate.",
        "disclaimer": "This page presents facts already publicly filed. It is not investment advice.",
    }


def _classification_note(library: Optional[dict], om: int,
                         comp: int, other: int) -> str:
    """The classification note. Uses library-wide figures when there are any, and falls back to the current sample saying so."""
    if library and library.get("trades"):
        g = library.get("by_group") or {}
        return (f"Of {library['trades']:,} rows library-wide, **active open-market trading is only "
                f"{g.get('open_market', 0):,} rows — {library.get('open_market_pct')}% of them**; "
                f"the rest is compensation — grants, exercises, tax withholding — at {g.get('compensation', 0):,} rows, "
                f"plus {g.get('other', 0):,} other. Buy and sell totals count the open-market part only — "
                f"counting bluntly by the SEC's acquired/disposed flag overstates buying several times over.")
    return (f"In the current sample of {om + comp + other:,} rows: {om:,} open-market, "
            f"{comp:,} compensation, {other:,} other (library-wide figures were not available).")


def summarize(trades: list[InsiderTrade], top: int = 20,
              library: Optional[dict] = None) -> dict:
    """Aggregate by ticker and by insider.

    ⭐ **Every buy and sell total counts open-market transactions (P/S) only.**
    Compensation — grants, exercises, tax withholding — is counted separately and never mixed in,
    or "insider buying" comes out roughly 8× too large.
    """
    om = [t for t in trades if t.is_open_market]
    comp = [t for t in trades if t.group == "compensation"]
    other = [t for t in trades if t.group == "other"]

    by_ticker: dict[str, dict] = {}
    by_owner: dict[str, dict] = {}
    for t in om:
        buy = t.direction == "buy"
        val = t.value or 0.0
        if t.ticker:
            e = by_ticker.setdefault(t.ticker, {
                "ticker": t.ticker, "company": t.company, "buys": 0, "sells": 0,
                "buy_value": 0.0, "sell_value": 0.0, "insiders": set()})
            e["buys" if buy else "sells"] += 1
            e["buy_value" if buy else "sell_value"] += val
            # ⚠️ Only **buyers** go in: this set drives `insider_count`, and the table
            # is headed "how many distinct insiders **bought**".
            # Letting sellers in inflates the cluster-buy signal out of nothing — and that is the number this page is most read for.
            if buy:
                e["insiders"].add(t.owner)
        k = f"{t.owner}|{t.ticker or t.company}"
        m = by_owner.setdefault(k, {
            "owner": t.owner, "ticker": t.ticker, "company": t.company,
            "title": t.officer_title, "is_officer": t.is_officer,
            "is_director": t.is_director, "is_ten_pct": t.is_ten_pct,
            "buys": 0, "sells": 0, "buy_value": 0.0, "sell_value": 0.0})
        m["buys" if buy else "sells"] += 1
        m["buy_value" if buy else "sell_value"] += val

    ticks = []
    for e in by_ticker.values():
        e["insider_count"] = len(e["insiders"])          # = number of distinct **buyers**
        e["insiders"] = sorted(e["insiders"])[:10]
        e["net_value"] = round(e["buy_value"] - e["sell_value"])
        e["buy_value"] = round(e["buy_value"])
        e["sell_value"] = round(e["sell_value"])
        ticks.append(e)
    # ⭐ Sorted by how many distinct insiders bought — cluster buying is the shape this data is most
    #    watched for: one large trade by one person may be personal finance, while several people buying in the same window is harder to put down to coincidence.
    cluster = sorted([e for e in ticks if e["buys"] > 0],
                     key=lambda x: (x["insider_count"], x["buy_value"]), reverse=True)

    owners = sorted(by_owner.values(),
                    key=lambda x: max(x["buy_value"], x["sell_value"]), reverse=True)
    for m in owners:
        m["buy_value"] = round(m["buy_value"])
        m["sell_value"] = round(m["sell_value"])

    delays = [t.delay_days for t in trades
              if t.delay_days is not None and t.delay_days >= 0]
    plan_sells = sum(1 for t in om if t.direction == "sell" and t.is_10b5_1 is True)

    return {
        "total_rows": len(trades),
        "open_market": {
            "count": len(om),
            "buys": sum(1 for t in om if t.direction == "buy"),
            "sells": sum(1 for t in om if t.direction == "sell"),
            "buy_value": round(sum(t.value or 0 for t in om if t.direction == "buy")),
            "sell_value": round(sum(t.value or 0 for t in om if t.direction == "sell")),
        },
        "compensation_count": len(comp),
        "other_count": len(other),
        "open_market_pct": round(len(om) / len(trades) * 100, 1) if trades else 0.0,
        "by_ticker": sorted(ticks, key=lambda x: abs(x["net_value"]), reverse=True)[:top],
        "cluster_buys": cluster[:top],
        "by_owner": owners[:top],
        "plan_sells": plan_sells,
        "implausible_price": sum(1 for t in trades if t.price_implausible),
        "date_anomaly_count": sum(1 for t in trades if t.date_anomaly),
        "delay": {
            "median_days": round(statistics.median(delays), 1) if delays else None,
            "over_2d": sum(1 for d in delays if d > 2),
            "note": "Section 16(a) requires filing within two business days of the trade. "
                    "Counted here in calendar days, without deducting weekends and holidays, "
                    "so over 2 days is a plain count of the facts and not a finding of violation.",
        },
        "notes": {
            # ⚠️ The classification share has to be the **library-wide** one, not the current sample's:
            # the page filters to open_market by default, so the sample is naturally 100% open-market
            # and the sentence turns into a tautology that explains nothing. What the user needs to see
            # is how small the open-market share is **across all of Form 4** — and that is the library-wide figure.
            "classification": _classification_note(library, len(om), len(comp), len(other)),
            "plan": "A 10b5-1 plan is arranged in advance, so a sale under one was often scheduled "
                    "months earlier and means something different from a sale decided on the day.",
            "price": "Some filings put the total value into the price-per-share field; one reads "
                     "$24m per share. Rows above $1m per share are dropped from the value totals "
                     "(BRK.A at ~$700k/share is the highest price US markets have seen, so anything above it is impossible), "
                     "but **subtler mis-entries cannot be caught** — read the value totals as approximate.",
            "disclaimer": "This page presents facts already publicly filed. It is not investment advice.",
        },
    }
