"""Congressional trade parsing and aggregation.

━━━ Why the parsing cannot be taken for granted ━━━
The House publishes PDFs, the Senate HTML, and the two do not even carry the same fields.
Twelve real PDFs were measured, confirming five shapes that have to be handled (every one hit for real; do not delete these branches):

1. **10.5% are paper scans** (33 of 313 PTRs, with a 7-digit DocID) —
   43 pages of pure image, 0 characters. They **must be labelled "not parsed"**;
   silently returning an empty list tells the user "this member did not trade".
2. **Amounts wrap across lines**: `$15,001 -` and only then, on the next line, `$50,000`.
3. **Two dates run together**: `06/12/202607/08/2026` (trade date + notification date, no separator).
4. **The asset name is detached from the transaction line**: the transaction line is often just `S 07/20/2026...`,
   with the asset name several lines above it — so blocks are cut at the "transaction anchor", and the text before the anchor is the asset.
5. **PDF extraction mixes in `\\x00`** (from certain glyphs), which has to be cleaned out.

⚠️ **The biggest trap: `[XX]` is an asset-type code, not a ticker.**
The GS in `Treasury Bill ... [GS]` is Government Securities, **not Goldman Sachs**.
The ticker sits in round brackets: `Abbott Laboratories Common Stock (ABT) [ST]`.
Take the square brackets for tickers and you produce a pile of very convincing rubbish.
"""
from __future__ import annotations

import io
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sources.congress import STOCK_ACT_HARD_CAP_DAYS, Filing

#: House asset-type codes (full table: https://fd.house.gov/reference/asset-type-codes.aspx)
#: Only the common ones are listed; unknown codes are kept as-is, never forced into a known bucket.
ASSET_TYPES = {
    "ST": "stock", "EF": "ETF", "OP": "option", "CS": "corporate bond",
    "GS": "government/agency bond", "MF": "mutual fund", "CT": "cryptocurrency",
    "OT": "other", "OL": "other securities", "BA": "bank account", "AB": "asset-backed security",
    "ET": "ETN", "FU": "futures", "HN": "hedge fund/PE", "HE": "hedge fund/PE (EIF)",
    "PS": "private equity", "RP": "real estate", "MA": "mutual fund account",
}

#: Transaction type codes
TX_TYPES = {
    "P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange",
}

#: The transaction anchor: type + trade date + notification date + amount.
#: ⚠️ Amounts come in two shapes and both have to be recognised:
#:   a range (the vast majority) `$1,001 - $15,000`  ——  the STOCK Act only requires banded disclosure
#:   an exact figure (a few members volunteer one) `$2,722.50`  ——  Wasserman Schultz, 2026-07-14, measured
#: Match only the range and a filing like that is judged, whole, as "no transaction lines found" — data dropped in silence.
_MONEY = r"\$[\d,]+(?:\.\d{2})?"
_TX_ANCHOR = re.compile(
    r"\b(?P<type>S \(partial\)|[PSE])\s+"
    r"(?P<tx_date>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<notify_date>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<amount>" + _MONEY + r"(?:\s*-\s*" + _MONEY + r")?)"
)

#: The "(ticker) [type]" inside an asset block. Tickers allow 1-5 letters plus an optional dot (BRK.B).
_TICKER_TYPE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z0-9]{2})\]")
#: Some entries carry an asset type and no ticker at all
_TYPE_ONLY = re.compile(r"\[([A-Z0-9]{2})\]")
#: Owner: SP=spouse, DC=dependent child, JT=joint
_OWNER = re.compile(r"\b(SP|DC|JT)\b")

#: Noise lines in the PDF (headers, footers, certifications) that are not asset names
_NOISE = re.compile(
    r"(F\s*S\s*:|S\s*O\s*:|D\s*:|Digitally Signed|"
    r"For the complete list of asset type|CERTIFY|Filing ID|"
    r"^\s*ID\s+Owner\s+Asset|Transaction\s+Date|Notification|Cap\.|Gains\s*>)",
    re.I)


@dataclass(frozen=True)
class Trade:
    """One disclosed transaction.

    ⚠️ **The amount is a range, not an exact figure** — the STOCK Act only requires banded
    disclosure ($1,001-$15,000 / $15,001-$50,000 …). Anything computed on top of it, position
    values or returns, rests on those bands and must be labelled an estimated range, never reported as a definite number.
    """

    chamber: str
    member: str
    state_district: str
    ticker: Optional[str]          # None = the asset has no public ticker (bonds, funds, private holdings)
    asset_name: str
    asset_type: Optional[str]      # the raw code, e.g. "ST"
    asset_type_label: str
    tx_type: str                   # the raw code
    tx_type_label: str
    tx_date: Optional[date]
    notification_date: Optional[date]
    filing_date: Optional[date]
    amount_low: Optional[int]
    amount_high: Optional[int]
    amount_raw: str
    owner: str                     # self / SP / DC / JT
    doc_id: str
    source_url: str

    @property
    def delay_days(self) -> Optional[int]:
        """Days from trade date to filing date (**a fact**, not a finding of violation)."""
        if not self.tx_date or not self.filing_date:
            return None
        return (self.filing_date - self.tx_date).days

    @property
    def date_anomaly(self) -> Optional[str]:
        """A date anomaly in the source itself — **the filer's data is not edited**, only reported as it stands.

        Two 2026 House filings were measured with a filing date before the trade date (a negative delay);
        the original reads `P 12/26/2026 01/21/2026`, with the trade date set in the future,
        and it is near certainly a clerical error (2025 was meant). But "near certainly" is not grounds —
        picking a year and writing it in is inventing data. The right move is to flag the anomaly, link the
        original, and drop it from the delay statistics (or -320 days skews both the median and the distribution).
        """
        d = self.delay_days
        if d is not None and d < 0:
            return "filing date precedes trade date (as filed; probably a clerical error)"
        return None

    @property
    def over_45d(self) -> bool:
        """Whether it exceeds the STOCK Act's hard 45-day limit.

        ⚠️ **This is only the fact of exceeding 45 days, and is not the same as a violation.**
        The statute (House PTR form) sets the deadline at "30 days after becoming aware, but no later
        than 45 days after the transaction", and it **rolls over weekends and holidays**. Amendments and
        late broker notifications exist too. This project presents data without concluding — the UI has to carry that sentence alongside.
        """
        d = self.delay_days
        return d is not None and d > STOCK_ACT_HARD_CAP_DAYS


#: ⭐ The two reasons that are **terminal** — retrying will never help, because without OCR an
#: image will never become text. Everything else (network, a missing file, a parser that needs
#: work) is temporary and must stay retryable.
#:
#: ⚠️ These are constants because the sync layer has to tell terminal from temporary, and it once
#: did so by searching this sentence for a word. That held only as long as nobody touched the
#: wording — translating it turned every scan back into "retry forever", silently, with the sync
#: still reporting success. Classify against the constant, never against the prose.
PAPER_SCAN_REASON = ("A paper scan (the whole filing is an image). Without OCR the detail "
                     "cannot be parsed — open the original to read it")
SENATE_PAPER_REASON = "A paper scan (an image); without OCR the detail cannot be parsed"
TERMINAL_REASONS = frozenset({PAPER_SCAN_REASON, SENATE_PAPER_REASON})


def is_terminal(reason: Optional[str]) -> bool:
    """Whether this parse failure can never succeed, however often it is retried."""
    return reason in TERMINAL_REASONS


@dataclass(frozen=True)
class ParseResult:
    """The parse result. **When it cannot be parsed, say why**; never just return an empty list."""

    trades: tuple[Trade, ...]
    unparsed_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.unparsed_reason is None


def _clean(text: str) -> str:
    """Clean PDF text: strip \\x00, normalise whitespace (this is what joins amounts and dates split across lines)."""
    return re.sub(r"[ \t\u00a0]+", " ", text.replace("\x00", " ")).strip()


def _parse_amount(raw: str) -> tuple[Optional[int], Optional[int]]:
    """Amount → (low, high).

    `$1,001 - $15,000` → (1001, 15000)   a range
    `$2,722.50`        → (2722, 2722)    exact: low and high are equal, so the midpoint is the real figure
    Carries a "-" but only one number parsed (the amount was cut off across lines) → (value, None); the upper bound is not invented.
    """
    nums = re.findall(r"\$([\d,]+(?:\.\d{2})?)", raw)
    try:
        vals = [int(float(n.replace(",", ""))) for n in nums]
    except ValueError:
        return None, None
    if len(vals) >= 2:
        return vals[0], vals[1]
    if len(vals) == 1:
        # No "-" = an exact figure (low and high equal); a "-" = a range whose upper bound was not captured
        return (vals[0], vals[0]) if "-" not in raw else (vals[0], None)
    return None, None


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


#: The last cell of the header. A multi-page PDF repeats the whole header on every page, so cutting once at the start of the body is not enough.
_HEADER_TAIL = re.compile(r"\$\s*200\s*\?")


def _asset_from_chunk(chunk: str) -> tuple[Optional[str], str, Optional[str], str]:
    """Pull (ticker, asset name, type code, owner) from the text block **preceding** a transaction anchor."""
    # ⚠️ A multi-page filing reprints the header on each page, and it lands inside the next asset block
    # (measured as things like "Type Date $200? SP Applied Materials...").
    # Take what follows the **last** "$200?" in the block and it strips clean however many times it repeats.
    tails = list(_HEADER_TAIL.finditer(chunk))
    if tails:
        chunk = chunk[tails[-1].end():]
    owner_m = _OWNER.search(chunk[:40])
    owner = owner_m.group(1) if owner_m else "self"

    lines = [ln.strip() for ln in chunk.split("\n")]
    lines = [ln for ln in lines if ln and not _NOISE.search(ln)]
    block = " ".join(lines).strip()

    ticker = asset_type = None
    m = _TICKER_TYPE.search(block)
    if m:
        ticker, asset_type = m.group(1), m.group(2)
        name = block[:m.start()].strip()
    else:
        # No (ticker): there may be only a [type] — bonds, funds and other assets with no public ticker
        t = _TYPE_ONLY.search(block)
        if t:
            asset_type = t.group(1)
            name = block[:t.start()].strip()
        else:
            name = block
    # Drop the leading transaction id and owner marker
    name = re.sub(r"^\d{6,}\s*", "", name)
    name = re.sub(r"^(SP|DC|JT)\s+", "", name).strip(" -–—·")
    return ticker, name, asset_type, owner


def parse_house_ptr(pdf_bytes: bytes, filing: Filing) -> ParseResult:
    """Parse one House PTR PDF."""
    try:
        from pypdf import PdfReader
    except ImportError as e:                         # a missing dependency ≠ no data
        raise RuntimeError("pypdf is missing, so House PDFs cannot be parsed: pip install pypdf") from e

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        return ParseResult((), f"Could not read the PDF: {type(e).__name__}: {e}")

    if len(text.strip()) < 80:
        # ⚠️ Measured: 33 of 313 PTRs are like this (43 pages of image, 0 characters).
        # An empty list with no explanation leaves the user thinking this member did not trade.
        return ParseResult((), PAPER_SCAN_REASON)

    clean = _clean(text)
    # ⚠️ Start looking after the transaction table's header, or **the first transaction in every PDF**
    # takes the page header ("P T R Clerk of the House of Representatives…", the member's name, the
    # column titles) for its asset name — it looks like data, and the first asset name is wrong every time.
    # The header's last cell is "Cap. Gains > $200?", so the cut has to land **after** "$200?":
    # cutting at "Cap. Gains" leaves "> $200?" inside the first asset name, and takes the
    # leading owner marker (JT/SP) down with it, unstrippable.
    head = re.search(r"\$\s*200\s*\?", clean)
    body = clean[head.end():] if head else clean
    matches = list(_TX_ANCHOR.finditer(body))
    if not matches:
        return ParseResult((), "No transaction lines recognised in the PDF (the format may have changed)")

    trades: list[Trade] = []
    prev_end = 0
    for m in matches:
        ticker, name, atype, owner = _asset_from_chunk(body[prev_end:m.start()])
        prev_end = m.end()
        lo, hi = _parse_amount(m.group("amount"))
        tx = m.group("type")
        trades.append(Trade(
            chamber="house", member=filing.name,
            state_district=filing.state_district,
            ticker=ticker, asset_name=name or "(unidentified)",
            asset_type=atype, asset_type_label=ASSET_TYPES.get(atype or "", atype or "unknown"),
            tx_type=tx, tx_type_label=TX_TYPES.get(tx, tx),
            tx_date=_parse_date(m.group("tx_date")),
            notification_date=_parse_date(m.group("notify_date")),
            filing_date=filing.filing_date,
            amount_low=lo, amount_high=hi,
            amount_raw=re.sub(r"\s+", " ", m.group("amount")).strip(),
            owner=owner, doc_id=filing.doc_id, source_url=filing.detail_url,
        ))
    return ParseResult(tuple(trades))


#: A table row on a Senate detail page
_SEN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_SEN_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def _strip_html(s: str) -> str:
    import html
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_senate_ptr(page_html: str, filing: Filing) -> ParseResult:
    """Parse a Senate electronic PTR (an HTML table that **carries its own Ticker column**, so cleaner than the House)."""
    rows = _SEN_ROW.findall(page_html)
    trades: list[Trade] = []
    for row in rows:
        cells = [_strip_html(c) for c in _SEN_CELL.findall(row)]
        # Header: # / Transaction Date / Owner / Ticker / Asset Name / Asset Type / Type / Amount / Comment
        if len(cells) < 8 or not re.fullmatch(r"\d+", cells[0] or ""):
            continue
        raw_ticker = (cells[3] or "").strip()
        # The Senate writes "--" for no public ticker; do not read it as a ticker called "--"
        ticker = raw_ticker if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", raw_ticker) else None
        lo, hi = _parse_amount(cells[7])
        tx_label = cells[6] or ""
        # The Senate spells them out (Purchase / Sale (Full) / Sale (Partial) / Exchange) → normalise to the House codes
        tx_code = ("P" if tx_label.lower().startswith("purchase")
                   else "S (partial)" if "partial" in tx_label.lower()
                   else "S" if tx_label.lower().startswith("sale")
                   else "E" if tx_label.lower().startswith("exchange") else tx_label)
        trades.append(Trade(
            chamber="senate", member=filing.name,
            state_district=filing.state_district,
            ticker=ticker, asset_name=cells[4] or "(unidentified)",
            asset_type=None, asset_type_label=cells[5] or "unknown",
            tx_type=tx_code, tx_type_label=TX_TYPES.get(tx_code, tx_label),
            tx_date=_parse_date(cells[1]),
            notification_date=None,        # the Senate table gives no notification date
            filing_date=filing.filing_date,
            amount_low=lo, amount_high=hi, amount_raw=cells[7],
            owner=cells[2] or "self", doc_id=filing.doc_id,
            source_url=filing.detail_url,
        ))
    if not trades:
        return ParseResult((), "No transaction lines recognised on the page (it may be a filing with no trades, or the page structure has changed)")
    return ParseResult(tuple(trades))


# ────────────────────────────── Aggregation ──────────────────────────────

def to_dict(t: Trade) -> dict:
    return {
        "chamber": t.chamber, "member": t.member, "state_district": t.state_district,
        "ticker": t.ticker, "asset_name": t.asset_name,
        "asset_type": t.asset_type, "asset_type_label": t.asset_type_label,
        "tx_type": t.tx_type, "tx_type_label": t.tx_type_label,
        "tx_date": t.tx_date.isoformat() if t.tx_date else None,
        "notification_date": t.notification_date.isoformat() if t.notification_date else None,
        "filing_date": t.filing_date.isoformat() if t.filing_date else None,
        "amount_low": t.amount_low, "amount_high": t.amount_high,
        "amount_raw": t.amount_raw, "owner": t.owner,
        "delay_days": t.delay_days, "over_45d": t.over_45d,
        "date_anomaly": t.date_anomaly,
        "doc_id": t.doc_id, "source_url": t.source_url,
    }


def _mid(t: Trade) -> float:
    """The midpoint of an amount range — for **ordering and relative comparison only**, never a real trade size."""
    if t.amount_low is None:
        return 0.0
    if t.amount_high is None:
        return float(t.amount_low)
    return (t.amount_low + t.amount_high) / 2.0


#: Delay buckets (upper bound inclusive). >45 days stands alone, because 45 is the statutory hard limit.
_DELAY_BUCKETS = ((7, "≤7d"), (15, "8-15"), (30, "16-30"), (45, "31-45"),
                  (10 ** 9, ">45d"))


def _delay_buckets(delays: list[int]) -> list[dict]:
    out, lo = [], -(10 ** 9)
    for hi, label in _DELAY_BUCKETS:
        out.append({"label": label, "count": sum(1 for d in delays if lo <= d <= hi)})
        lo = hi + 1
    return out


def summarize(trades: list[Trade]) -> dict:
    """Aggregate by ticker, by member and by direction.

    ⚠️ Every amount is a **sum of range midpoints** (the STOCK Act discloses bands only), usable
    for comparison between rows and never as a real amount of money. The output carries an `amount_is_estimate` flag.
    """
    by_ticker: dict[str, dict] = {}
    by_member: dict[str, dict] = {}
    buys = sells = 0

    for t in trades:
        is_buy = t.tx_type == "P"
        is_sell = t.tx_type.startswith("S")
        buys += is_buy
        sells += is_sell
        mid = _mid(t)

        if t.ticker:
            e = by_ticker.setdefault(t.ticker, {
                "ticker": t.ticker, "asset_name": t.asset_name,
                "trades": 0, "buys": 0, "sells": 0,
                "est_amount": 0.0, "members": set()})
            e["trades"] += 1
            e["buys"] += is_buy
            e["sells"] += is_sell
            e["est_amount"] += mid
            e["members"].add(t.member)

        m = by_member.setdefault(t.member, {
            "member": t.member, "chamber": t.chamber,
            "state_district": t.state_district,
            "trades": 0, "buys": 0, "sells": 0,
            "est_amount": 0.0, "tickers": set()})
        m["trades"] += 1
        m["buys"] += is_buy
        m["sells"] += is_sell
        m["est_amount"] += mid
        if t.ticker:
            m["tickers"].add(t.ticker)

    # ⚠️ by_ticker sorts by **number of trades** (that is what "most active tickers" means, and what the chart draws).
    # It once sorted by est_amount → "2 trades above 11 trades", with the ordering matching neither the title nor the chart.
    # The amount is only an estimate from range midpoints, and was never suited to being the primary sort key.
    tick = sorted(by_ticker.values(),
                  key=lambda x: (x["trades"], x["est_amount"]), reverse=True)
    for e in tick:
        e["members"] = sorted(e["members"])
        e["member_count"] = len(e["members"])
        e["est_amount"] = round(e["est_amount"])
    # by_member stays sorted by estimated amount (which is what the UI says it is)
    memb = sorted(by_member.values(), key=lambda x: x["est_amount"], reverse=True)
    for e in memb:
        e["ticker_count"] = len(e["tickers"])
        e["tickers"] = sorted(e["tickers"])[:12]
        e["est_amount"] = round(e["est_amount"])

    # ⚠️ Drop date anomalies before computing the delay statistics: one -320 day row skews the median and the distribution alike.
    # Dropping is not hiding — the count is reported separately as anomaly_count, and the rows still appear, flagged, in the detail.
    delays = [t.delay_days for t in trades
              if t.delay_days is not None and t.date_anomaly is None]
    return {
        "total_trades": len(trades),
        "buys": buys, "sells": sells,
        "by_ticker": tick,
        "by_member": memb,
        # ⭐ The histogram is computed here, from **the same sample** as the median and overdue count above.
        # The frontend once bucketed the detail table's 300 rows itself while the card used the summary's 2,000 —
        # two controls on one screen reporting different distributions. Computing from one source rules that out structurally.
        "delay": {
            "buckets": _delay_buckets(delays),
            # statistics.median, because `sorted(x)[len//2]` takes the upper median on an
            # **even-sized sample** ([1,100] reports 100 rather than 50.5), which distorts
            # visibly once the sample is small — as it often is after filtering to one ticker.
            "median_days": round(statistics.median(delays), 1) if delays else None,
            "max_days": max(delays) if delays else None,
            "over_45d_count": sum(1 for t in trades
                                  if t.over_45d and t.date_anomaly is None),
            "anomaly_count": sum(1 for t in trades if t.date_anomaly),
            "anomaly_note": "Some filings carry a filing date earlier than the trade date (an error in the original). "
                            "They are excluded from the delay statistics above, but still listed and flagged in the detail.",
            "note": "Over 45 days is a statement of fact, not a finding of violation: the statutory deadline is "
                    "30 days after becoming aware and no later than 45 days after the trade, it rolls over weekends and holidays, and amendments and late broker notifications exist too.",
        },
        "amount_is_estimate": True,
        "amount_note": "The STOCK Act only requires disclosure in ranges (such as $1,001-$15,000), "
                       "so these amounts are sums of range midpoints: good for comparison between rows, not real trade sizes.",
    }
