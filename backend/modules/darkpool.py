"""Off-exchange volume: keeping ATS (dark pools) and non-ATS off-exchange (internalisation) **apart**.

The source's four traps are documented at the top of `sources/darkpool.py`; this module only processes.
All of its work reduces to one sentence: **never let these two kinds of volume be added together**.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sources.darkpool import (TYPE_ATS_FIRM, TYPE_ATS_TOTAL, TYPE_OTC_FIRM,
                              TYPE_OTC_TOTAL)

NOTES = {
    "ats_vs_otc": (
        "**\"Dark pool\" is not the same as \"off-exchange\".** This data mixes two entirely different things: "
        "**ATS** is a genuine dark pool (an alternative trading system that displays no quotes); "
        "**non-ATS off-exchange** is wholesaler **internalisation** — retail order flow sold to market makers "
        "and filled inside their own books, which is not a dark pool. Measured on NVDA for the week of "
        "2026-06-29, non-ATS off-exchange was **2.4× larger than ATS**. Add the two together and call it \"dark pool volume\" and the figure is more than double. This page always shows them separately."),
    "no_double_count": (
        "The endpoint returns **aggregate rows** and **per-firm detail rows** at once: two cuts of the same "
        "volume (measured, the two totals match exactly, with zero discrepancy). Sum them without distinguishing and the "
        "result is precisely twice the truth. This page totals the detail rows only, and **reconciles them week by week** against the aggregate rows, flagging any mismatch outright."),
    "otc_anonymous": (
        "⚠️ **Non-ATS off-exchange does not disclose which firm at the symbol level** (every MPID is blank) — "
        "so \"which wholesaler took how much\" is a question this data cannot answer. On the ATS side the firms are named."),
    "lag": (
        "⚠️ **About four weeks behind.** Measured on 2026-07-26, the newest week available was 2026-06-29. "
        "FINRA publishes Tier 1 symbols two weeks late and Tier 2 four weeks late. "
        "This is not live dark-pool monitoring; it is **after-the-fact statistics**."),
    "share_needs_local": (
        "The denominator of \"off-exchange share\" is **total volume over the same period**, which FINRA does not "
        "provide — this page fills it from **locally accrued** Cboe quotes (the ones the scanner collects). "
        "When a day of that week never accrued locally, the share column returns **null** and names the missing days; "
        "⛔ it does not assemble a plausible-looking percentage out of some other measure."),
}


def _f(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class VenueRow:
    """One venue's (or one aggregate record's) volume for a given week."""

    symbol: str
    week: str
    kind: str                 # "ats" | "otc"
    mpid: Optional[str]
    name: Optional[str]
    tier: Optional[str]
    shares: Optional[float]
    trades: Optional[float]
    notional: Optional[float]

    @property
    def avg_trade_size(self) -> Optional[float]:
        """Average shares per trade.

        ⚠️ Returns None when the trade count is 0 — "nothing traded" and "0 shares per trade" are not the same thing.
        The metric itself is revealing: measured on NVDA in one week,
        Morgan Stanley moved 17,153,149 shares in just 1,468 trades (about 11,684 shares each, institutional blocks),
        while Citadel Securities moved 36,873,543 shares across 477,261 trades (about 77 shares each, retail flow).
        """
        if self.shares is None or not self.trades:
            return None
        return self.shares / self.trades


def parse(rows: Iterable[dict]) -> dict:
    """Sort the raw CSV rows into four piles.

    ⚠️ **This is why the module exists.** Four `summaryTypeCode` values arrive in one response;
    ATS and non-ATS are two different kinds of trading, and aggregate and detail are two cuts of the same volume —
    confuse either pair anywhere and the figure comes out double.
    """
    ats_firm: list[VenueRow] = []
    otc_firm: list[VenueRow] = []
    ats_total: dict[str, float] = {}
    otc_total: dict[str, float] = {}
    unknown_types: dict[str, int] = {}
    # ⚠️ The count of rows with a missing value. When a CSV field empties out or changes format, `or 0.0`
    #    quietly turns "could not read it" into 0, which then flows into the totals, the reconciliation and the share. Count them and report them.
    null_shares: dict[str, int] = {}

    truncated = False
    for r in rows:
        t = (r.get("summaryTypeCode") or "").strip()
        if t == "__TRUNCATED__":
            # A sentinel the source layer inserts: this fetch hit the row limit and was probably truncated.
            # ⚠️ Recognised on its own, never folded into unknown_types — that would be misreported as "the parser is incompatible".
            truncated = True
            continue
        week = (r.get("weekStartDate") or "")[:10]
        sym = (r.get("issueSymbolIdentifier") or "").strip().upper()
        shares = _f(r.get("totalWeeklyShareQuantity"))
        if t in (TYPE_ATS_TOTAL, TYPE_OTC_TOTAL):
            bucket = ats_total if t == TYPE_ATS_TOTAL else otc_total
            if shares is None:
                # An aggregate row with a missing value — record it, and **do not treat it as 0** (0 lets the reconciliation pretend to pass)
                null_shares[t] = null_shares.get(t, 0) + 1
                continue
            bucket[week] = (bucket.get(week) or 0.0) + shares
            continue
        if t not in (TYPE_ATS_FIRM, TYPE_OTC_FIRM):
            # ⚠️ **Record** the types we do not recognise rather than dropping them silently — when FINRA
            #    adds a new type, we would otherwise miss a whole class of trading without a sound.
            unknown_types[t or "(blank)"] = unknown_types.get(t or "(blank)", 0) + 1
            continue
        row = VenueRow(
            symbol=sym, week=week,
            kind="ats" if t == TYPE_ATS_FIRM else "otc",
            mpid=(r.get("MPID") or "").strip() or None,
            name=(r.get("marketParticipantName") or "").strip() or None,
            tier=(r.get("tierDescription") or "").strip() or None,
            shares=shares,
            trades=_f(r.get("totalWeeklyTradeCount")),
            notional=_f(r.get("totalNotionalSum")),
        )
        (ats_firm if row.kind == "ats" else otc_firm).append(row)

    return {"ats_firm": ats_firm, "otc_firm": otc_firm,
            "ats_total": ats_total, "otc_total": otc_total,
            "unknown_types": unknown_types, "null_shares": null_shares,
            # Hitting the row limit = the weekly series may be incomplete, and since it cannot be sorted, which weeks got cut is unknown
            "truncated": truncated,
            # Not one row of a known type, yet unknown types present → **the parser is no longer compatible**,
            # and it must not be reported as "this symbol has no off-exchange volume"
            "parsed_any": bool(ats_firm or otc_firm or ats_total or otc_total)}


def to_dict(v: VenueRow) -> dict:
    return {"symbol": v.symbol, "week": v.week, "kind": v.kind,
            "mpid": v.mpid, "name": v.name, "tier": v.tier,
            "shares": v.shares, "trades": v.trades, "notional": v.notional,
            "avg_trade_size": v.avg_trade_size}


def weeks_of(parsed: dict) -> list[str]:
    ws = set(parsed["ats_total"]) | set(parsed["otc_total"])
    ws |= {r.week for r in parsed["ats_firm"]} | {r.week for r in parsed["otc_firm"]}
    return sorted(w for w in ws if w)


def week_summary(parsed: dict, week: str,
                 consolidated_shares: Optional[float] = None,
                 covered_days: Optional[list[str]] = None,
                 missing_days: Optional[list[str]] = None) -> dict:
    """One week's summary.

    `consolidated_shares` is **total volume over the same period** (from locally accrued Cboe quotes).

    ⚠️ **A single missing trading day means no share is computed.** The previous version computed one
    whenever `covered` was non-empty, leaving a partial denominator against a whole week's numerator —
    a share that is **systematically too high**, and invisibly so (with only one day accrued locally, it comes out roughly five times the truth).
    """
    af = [r for r in parsed["ats_firm"] if r.week == week]
    of = [r for r in parsed["otc_firm"] if r.week == week]
    # ⚠️ Rows with a missing value are **excluded and counted**, never summed in as 0 —
    #    0 makes the total too small, the reconciliation pretend to pass, and the share too low: wrong the whole way down.
    af_ok = [r for r in af if r.shares is not None]
    of_ok = [r for r in of if r.shares is not None]
    ats_null = len(af) - len(af_ok)
    otc_null = len(of) - len(of_ok)
    ats_sum = sum(r.shares or 0.0 for r in af_ok)
    otc_sum = sum(r.shares or 0.0 for r in of_ok)
    ats_ref = parsed["ats_total"].get(week)
    otc_ref = parsed["otc_total"].get(week)

    def _recon(detail: float, ref: Optional[float]) -> dict:
        """Reconcile the detail total against the aggregate row. Say so when they disagree; do not pick one to display."""
        if ref is None:
            return {"reference": None, "matches": None,
                    "note": "That week has no aggregate row, so it cannot be reconciled."}
        diff = detail - ref
        # ⚠️ Share counts are **integers**, so the tolerance is one share.
        #    This was written `max(1.0, ref*1e-6)`, which takes **the larger** —
        #    at 268m shares that is a tolerance of 268 shares, enough to let a genuinely missing or duplicated row through.
        ok = abs(diff) < 1.0
        return {"reference": ref, "diff": diff, "matches": ok,
                "note": ("The detail total matches the aggregate row." if ok else
                         f"⚠️ The detail total **does not match** the aggregate row (a difference of {diff:,.0f} shares) — "
                         f"some firms may not be disclosed in the detail. This page shows the detail total.")}

    off_exchange = ats_sum + otc_sum
    share = None
    share_note = NOTES["share_needs_local"]
    if ats_null or otc_null:
        # ⚠️ A row in the numerator that could not be read → the numerator is too small → the share too low.
        #    As with a partial denominator: better to give nothing than something skewed.
        share_note = (
            f"⚠️ {ats_null + otc_null} records that week have a null volume (excluded, not counted as 0), "
            f"so the numerator is too small and the share would come out **too low** — hence null here.")
    elif missing_days:
        share_note = (
            f"⚠️ {len(missing_days)} trading days of that week never accrued locally "
            f"({', '.join(missing_days)}), leaving a partial denominator against a whole week's numerator — "
            f"the share would be **systematically too high**, so this returns null rather than a skewed figure.")
    elif consolidated_shares and consolidated_shares > 0:
        share = {
            "consolidated": consolidated_shares,
            "ats_pct": ats_sum / consolidated_shares * 100.0,
            "otc_pct": otc_sum / consolidated_shares * 100.0,
            "off_exchange_pct": off_exchange / consolidated_shares * 100.0,
            "covered_days": covered_days or [],
        }
        share_note = (
            "The denominator is the sum of Cboe daily volume accrued locally for that week "
            f"(covering {len(covered_days or [])} trading days). "
            "⚠️ That is **what accrued here**, not the official consolidated volume — "
            "if some days of that week were never scanned, the denominator is small and the share too high.")
    return {
        "week": week,
        # Missing values and unknown types are reported alongside — both make the total too small and distort the reconciliation
        "null_share_records": ats_null + otc_null,
        # ⚠️ `records` is a **row count**; `firms` is the **number of distinct MPIDs**.
        #    The previous version used the row count as the firm count: on the non-ATS side every MPID is
        #    blank, so 32 rows read as "32 firms" when not one can actually be named. The two must be given separately.
        "ats": {"shares": ats_sum, "records": len(af),
                "firms": len({r.mpid for r in af if r.mpid}),
                "anonymous_records": sum(1 for r in af if not r.mpid),
                "null_share_records": ats_null,
                "trades": sum(r.trades or 0.0 for r in af if r.trades is not None),
                "null_trade_records": sum(1 for r in af if r.trades is None),
                "reconcile": _recon(ats_sum, ats_ref)},
        "otc": {"shares": otc_sum, "records": len(of),
                "firms": len({r.mpid for r in of if r.mpid}),
                "anonymous_records": sum(1 for r in of if not r.mpid),
                "null_share_records": otc_null,
                "trades": sum(r.trades or 0.0 for r in of if r.trades is not None),
                "null_trade_records": sum(1 for r in of if r.trades is None),
                "reconcile": _recon(otc_sum, otc_ref),
                "named_firms": len({r.mpid for r in of if r.mpid})},
        "off_exchange_shares": off_exchange,
        # ⚠️ There is deliberately **no** field named "dark_pool_shares":
        #    it would invite callers to treat ats+otc as "dark pool volume", which is the commonest mistake of all.
        "ats_over_otc": (ats_sum / otc_sum) if otc_sum else None,
        "share": share,
        "share_note": share_note,
        "missing_days": missing_days or [],
        "venues": {
            "ats": [to_dict(r) for r in sorted(af, key=lambda x: -(x.shares or 0))],
            "otc": [to_dict(r) for r in sorted(of, key=lambda x: -(x.shares or 0))],
        },
        "notes": NOTES,
    }


def series(parsed: dict) -> list[dict]:
    """Week-by-week ATS / non-ATS volume series (from the detail totals, not the aggregate rows)."""
    out = []
    for w in weeks_of(parsed):
        af = [r for r in parsed["ats_firm"] if r.week == w and r.shares is not None]
        of = [r for r in parsed["otc_firm"] if r.week == w and r.shares is not None]
        # ⚠️ Nulls are **excluded and counted**, never folded in with `or 0.0` —
        #    that would quietly shorten that week's bar with nothing on the chart to show for it.
        nulls = (sum(1 for r in parsed["ats_firm"] if r.week == w and r.shares is None)
                 + sum(1 for r in parsed["otc_firm"] if r.week == w and r.shares is None))
        a = sum(r.shares or 0.0 for r in af)
        o = sum(r.shares or 0.0 for r in of)
        out.append({"week": w, "ats_shares": a, "otc_shares": o,
                    "null_share_records": nulls,
                    "ats_share_of_off_exchange":
                        (a / (a + o) * 100.0) if (a + o) else None})
    return out
