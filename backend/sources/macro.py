"""Macro sources — Treasury yield curve and CFTC positioning.

━━━ Compliance: both are tier S ━━━
US Treasury and CFTC public data. Government works: commercial use and redistribution unrestricted.
This is the **cleanest** lane in the project — no OPRA, no §13107, no FINRA terms.

━━━ ⚠️ Two definitions that must be stated ━━━
1. **Curve inversion**: short end above long end. Two spreads are in common use,
   10Y−2Y and 10Y−3M, and **they can cross zero months apart** — so "the curve
   inverted" says nothing until you say which one.
2. **COT lags three days**: it reports **Tuesday's** positions, published **Friday**.
   What you see is always three days old.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from typing import Iterator, Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter

TREASURY_XML = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/pages/xml")
CFTC_SODA = "https://publicreporting.cftc.gov/resource"

#: Tenor fields on the curve (Treasury XML `d:BC_*` names → years)
TENORS: dict[str, float] = {
    "BC_1MONTH": 1 / 12, "BC_2MONTH": 2 / 12, "BC_3MONTH": 0.25,
    "BC_4MONTH": 4 / 12, "BC_6MONTH": 0.5, "BC_1YEAR": 1, "BC_2YEAR": 2,
    "BC_3YEAR": 3, "BC_5YEAR": 5, "BC_7YEAR": 7, "BC_10YEAR": 10,
    "BC_20YEAR": 20, "BC_30YEAR": 30,
}
TENOR_LABEL = {
    "BC_1MONTH": "1M", "BC_2MONTH": "2M", "BC_3MONTH": "3M", "BC_4MONTH": "4M",
    "BC_6MONTH": "6M", "BC_1YEAR": "1Y", "BC_2YEAR": "2Y", "BC_3YEAR": "3Y",
    "BC_5YEAR": "5Y", "BC_7YEAR": "7Y", "BC_10YEAR": "10Y",
    "BC_20YEAR": "20Y", "BC_30YEAR": "30Y",
}


def yield_curve(year: int) -> Iterator[dict]:
    """Daily yield curve for one year (official Treasury XML)."""
    url = f"{TREASURY_XML}?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"Treasury network failure: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"Treasury has no data for {year}")
    if r.status_code != 200:
        raise RuntimeError(f"Treasury HTTP {r.status_code}: {year}")

    blocks = re.findall(r"<m:properties>(.*?)</m:properties>", r.text, re.S)
    if not blocks:
        raise RuntimeError(f"Treasury {year}: parsed no records (the XML shape may have changed)")
    for b in blocks:
        fields = dict(re.findall(r"<d:(\w+)[^>]*>([^<]*)</d:\1>", b))
        d = fields.get("NEW_DATE", "")[:10]
        if not d:
            continue
        row: dict = {"date": d}
        for k in TENORS:
            v = fields.get(k, "").strip()
            row[k] = float(v) if v else None
        yield row


#: CFTC Traders in Financial Futures — the Socrata dataset id
CFTC_TFF = "gpe5-46if"


def _cftc_get(params: dict) -> list[dict]:
    """One call to CFTC Socrata.

    ⚠️ Let requests do the encoding via `params`; **never hand-build the URL**.
    Market names contain `&` ("S&P 500"), which a hand-built query string reads as a separator → 400.
    """
    _limiter.wait()
    try:
        r = requests.get(f"{CFTC_SODA}/{CFTC_TFF}.json", params=params,
                         headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"CFTC network failure: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        # ⚠️ **This is not "no data".** Measured 2026-07-26:
        #   valid dataset + zero matches → `200 []`
        #   dataset id does not exist   → `404 {"code":"dataset.missing"}`
        # So a 404 can only mean the dataset was renamed or retired, or our id is wrong —
        # a configuration failure, which must propagate rather than degrade into "the market has no positioning data".
        raise RuntimeError(
            "CFTC dataset not found (404) — the dataset id may have changed; "
            f"currently using {CFTC_TFF}. This is a configuration problem, not \"no data\".")
    if r.status_code == 400:
        # A SoQL syntax or parameter problem — carry upstream's explanation, not just a status code
        raise RuntimeError(f"CFTC rejected the query (400): {(r.text or '')[:160]}")
    if r.status_code != 200:
        raise RuntimeError(f"CFTC HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError("CFTC returned non-JSON (possibly an error page)") from e


def cot_markets() -> list[dict]:
    """Every market TFF covers, with each one's last report date and record count.

    ⚠️ **`last_date` must come back with it.** This dataset holds a great many contracts
    that **stopped reporting long ago** (measured: only 90 of 186 markets are still live,
    some frozen since 2022). Return bare names and a user who picks a dead one sees
    "no data", when the truth is "this contract is no longer reported" — **two different things**.
    """
    rows = _cftc_get({
        "$select": ("market_and_exchange_names, "
                    "max(report_date_as_yyyy_mm_dd) as last_date, count(*) as n"),
        "$group": "market_and_exchange_names",
        "$order": "market_and_exchange_names",
        "$limit": 2000,
    })
    out = []
    for r in rows:
        name = (r.get("market_and_exchange_names") or "").strip()
        if not name:
            continue
        out.append({"market": name,
                    "last_date": (r.get("last_date") or "")[:10] or None,
                    "reports": int(float(r.get("n") or 0))})
    return out


def cot_rows(limit: int = 500, market_contains: Optional[str] = None,
             exact: bool = False) -> list[dict]:
    """CFTC positioning (TFF: trader categories for financial futures).

    ⚠️ **Lags three days**: it reports **Tuesday's** close, published **Friday** afternoon.

    `exact=True` requires the market name to match exactly; fuzzy matching is for search.
    ⚠️ **Time series must use exact**: `like '%S&P 500%'` also matches
    "S&P 500 Consolidated"、"E-MINI S&P 500"、"S&P 500 QUARTERLY DIVIDEND IND"
    three **different contracts**, and ordering those by date braids them into one
    sawtooth that looks like violent position flips but is only hopping between contracts.
    """
    params: dict = {"$limit": limit,
                    "$order": "report_date_as_yyyy_mm_dd DESC"}
    if market_contains:
        safe = market_contains.replace("'", "''")
        if exact:
            params["$where"] = f"market_and_exchange_names = '{safe}'"
        else:
            params["$where"] = (
                f"upper(market_and_exchange_names) like upper('%{safe}%')")
    return _cftc_get(params)
