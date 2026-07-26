"""场外成交：把 ATS（暗池）与非 ATS 场外（内部化）**分开**。

数据源的四个坑写在 `sources/darkpool.py` 的文件头，这里只做加工。
本模块的全部工作可以概括成一句：**不让这两类量被加在一起**。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sources.darkpool import (TYPE_ATS_FIRM, TYPE_ATS_TOTAL, TYPE_OTC_FIRM,
                              TYPE_OTC_TOTAL)

NOTES = {
    "ats_vs_otc": (
        "**「暗池」不等于「场外」。** 这份数据里混着两类完全不同的成交："
        "**ATS** 是真正的暗池（不显示报价的另类交易系统）；"
        "**非 ATS 场外**是批发商**内部化** —— 散户订单流卖给做市商在其内部成交，"
        "那不是暗池。实测 NVDA 2026-06-29 那周，非 ATS 场外比 ATS **大 2.4 倍**。"
        "把两者相加叫「暗池成交量」，数字会虚高一倍以上。本页始终分开显示。"),
    "no_double_count": (
        "接口同时返回**聚合行**与**按机构明细行**，两者是同一笔量的两种切法"
        "（实测两边合计完全相等、差 0）。不加区分地求和，结果恰好是真值的两倍。"
        "本页只用明细行汇总，并与聚合行**逐周对账**，对不上会直接标出来。"),
    "otc_anonymous": (
        "⚠️ **非 ATS 场外在标的层不披露是哪家机构**（MPID 全为空）—— "
        "「哪家批发商吃了多少」这个问题，这份数据回答不了。ATS 那边则有名有姓。"),
    "lag": (
        "⚠️ **滞后约四周。** 实测 2026-07-26 能取到的最新周是 2026-06-29。"
        "FINRA 对 Tier 1 标的延迟两周发布、Tier 2 延迟四周。"
        "这不是实时暗池监控，是**事后统计**。"),
    "share_needs_local": (
        "「场外占比」的分母是**同期总成交量**，FINRA 不提供 —— "
        "本页用 CBOE 行情的**本地沉淀**（扫描器攒的那份）来补。"
        "那一周的日成交量本地没攒到时，占比一栏返回**空值**并说明差哪几天，"
        "⛔ 不拿别的口径凑一个看起来像的百分比。"),
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
    """一家场所（ATS）或一条聚合记录在某一周的成交。"""

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
        """平均每笔股数。

        ⚠️ 笔数为 0 时返回 None —— "没有成交"和"平均每笔 0 股"不是一回事。
        这个指标本身很能说明问题：实测同一周 NVDA，
        摩根士丹利 17,153,149 股只用了 1,468 笔（约 11,684 股/笔，机构大单），
        而 Citadel Securities 36,873,543 股用了 477,261 笔（约 77 股/笔，散户流）。
        """
        if self.shares is None or not self.trades:
            return None
        return self.shares / self.trades


def parse(rows: Iterable[dict]) -> dict:
    """把原始 CSV 行分成四堆。

    ⚠️ **这是本模块存在的理由。** 四种 `summaryTypeCode` 混在一个返回里，
    ATS 与非 ATS 是两类不同的成交、聚合与明细是同一笔量的两种切法 ——
    任何一处混淆都会把数字弄错一倍。
    """
    ats_firm: list[VenueRow] = []
    otc_firm: list[VenueRow] = []
    ats_total: dict[str, float] = {}
    otc_total: dict[str, float] = {}
    unknown_types: dict[str, int] = {}
    # ⚠️ 数值缺失的行数。CSV 字段空掉或改格式时，`or 0.0` 会把"取不到"
    #    悄悄变成 0，然后一路混进汇总、对账、占比。必须计数并上报。
    null_shares: dict[str, int] = {}

    truncated = False
    for r in rows:
        t = (r.get("summaryTypeCode") or "").strip()
        if t == "__TRUNCATED__":
            # 数据源层塞的哨兵：这次取满了上限，很可能被截断。
            # ⚠️ 单独识别，不能混进 unknown_types —— 那会被误报成"解析不兼容"。
            truncated = True
            continue
        week = (r.get("weekStartDate") or "")[:10]
        sym = (r.get("issueSymbolIdentifier") or "").strip().upper()
        shares = _f(r.get("totalWeeklyShareQuantity"))
        if t in (TYPE_ATS_TOTAL, TYPE_OTC_TOTAL):
            bucket = ats_total if t == TYPE_ATS_TOTAL else otc_total
            if shares is None:
                # 聚合行的数值缺失 —— 记下来，**不当成 0**（0 会让对账假装通过）
                null_shares[t] = null_shares.get(t, 0) + 1
                continue
            bucket[week] = (bucket.get(week) or 0.0) + shares
            continue
        if t not in (TYPE_ATS_FIRM, TYPE_OTC_FIRM):
            # ⚠️ 认不出的类型**记下来**，不能默默丢 —— FINRA 加一个新类型时
            #    我们会悄无声息地漏掉一整类成交。
            unknown_types[t or "(空)"] = unknown_types.get(t or "(空)", 0) + 1
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
            # 取满上限 = 周序列可能不全，且因为不能排序、截掉哪几周未知
            "truncated": truncated,
            # 已知类型一行都没有、却出现了未知类型 → **解析已不兼容**，
            # 不能报成"这只票没有场外成交"
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
    """某一周的汇总。

    `consolidated_shares` 是**同期总成交量**（来自 CBOE 本地沉淀）。

    ⚠️ **缺任何一个交易日就不算占比。** 上一版只要 `covered` 非空就算，
    分母残缺而分子是整周的 —— 占比**系统性偏高**，且偏得看不出来
    （本地只攒到 1 天时，占比会是真实值的五倍左右）。
    """
    af = [r for r in parsed["ats_firm"] if r.week == week]
    of = [r for r in parsed["otc_firm"] if r.week == week]
    # ⚠️ 数值缺失的行**排除并计数**，不用 0 参与求和 ——
    #    0 会让合计偏小、对账假装通过、占比偏低，一路错到底。
    af_ok = [r for r in af if r.shares is not None]
    of_ok = [r for r in of if r.shares is not None]
    ats_null = len(af) - len(af_ok)
    otc_null = len(of) - len(of_ok)
    ats_sum = sum(r.shares or 0.0 for r in af_ok)
    otc_sum = sum(r.shares or 0.0 for r in of_ok)
    ats_ref = parsed["ats_total"].get(week)
    otc_ref = parsed["otc_total"].get(week)

    def _recon(detail: float, ref: Optional[float]) -> dict:
        """明细合计与聚合行对账。对不上要直说，不要挑一个显示。"""
        if ref is None:
            return {"reference": None, "matches": None,
                    "note": "该周没有聚合行，无法对账。"}
        diff = detail - ref
        # ⚠️ 成交股数是**整数**，容差就是 1 股。
        #    原来写 `max(1.0, ref*1e-6)` 取的是**较大者** ——
        #    2.68 亿股时容差高达 268 股，足以放过真实的漏行/重行。
        ok = abs(diff) < 1.0
        return {"reference": ref, "diff": diff, "matches": ok,
                "note": ("明细合计与聚合行一致。" if ok else
                         f"⚠️ 明细合计与聚合行**对不上**（差 {diff:,.0f} 股）—— "
                         f"可能有机构未在明细里披露，本页显示的是明细合计。")}

    off_exchange = ats_sum + otc_sum
    share = None
    share_note = NOTES["share_needs_local"]
    if ats_null or otc_null:
        # ⚠️ 分子里有行取不到数 → 分子偏小 → 占比偏低。
        #    和"分母残缺"一样，宁可不给，也不给一个偏的数。
        share_note = (
            f"⚠️ 该周有 {ats_null + otc_null} 条记录的成交量为空（已排除、未当成 0），"
            f"分子因此偏小 —— 占比会**偏低**，所以这里返回空值。")
    elif missing_days:
        share_note = (
            f"⚠️ 该周有 {len(missing_days)} 个交易日的行情本地没攒到"
            f"（{'、'.join(missing_days)}），分母残缺而分子是整周的 —— "
            f"算出来的占比会**系统性偏高**，所以这里返回空值，不给一个偏的数。")
    elif consolidated_shares and consolidated_shares > 0:
        share = {
            "consolidated": consolidated_shares,
            "ats_pct": ats_sum / consolidated_shares * 100.0,
            "otc_pct": otc_sum / consolidated_shares * 100.0,
            "off_exchange_pct": off_exchange / consolidated_shares * 100.0,
            "covered_days": covered_days or [],
        }
        share_note = (
            "分母为该周本地已沉淀的 CBOE 日成交量之和"
            f"（覆盖 {len(covered_days or [])} 个交易日）。"
            "⚠️ 这是**本地攒到的**总量，不是官方合并成交量 —— "
            "若那周有几天没扫到，分母偏小、占比会偏高。")
    return {
        "week": week,
        # 数值缺失与未知类型一并上报 —— 它们会让合计偏小、对账失真
        "null_share_records": ats_null + otc_null,
        # ⚠️ `records` 是**行数**，`firms` 是**按 MPID 去重的机构数**。
        #    上一版拿行数当机构数：非 ATS 那边 MPID 全为空，32 行会被说成
        #    "32 家"，而实际上一家都点不出来。两个数必须分开给。
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
        # ⚠️ 刻意**不给** "dark_pool_shares" 这种字段名：
        #    它会诱使调用方把 ats+otc 当成"暗池成交量"，而那正是最常见的错误。
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
    """逐周的 ATS / 非 ATS 成交量序列（用明细合计，不用聚合行）。"""
    out = []
    for w in weeks_of(parsed):
        af = [r for r in parsed["ats_firm"] if r.week == w and r.shares is not None]
        of = [r for r in parsed["otc_firm"] if r.week == w and r.shares is not None]
        # ⚠️ 空值**排除并计数**，不用 `or 0.0` 混进求和 ——
        #    那会让那一周的柱子悄悄矮一截，而图上完全看不出来。
        nulls = (sum(1 for r in parsed["ats_firm"] if r.week == w and r.shares is None)
                 + sum(1 for r in parsed["otc_firm"] if r.week == w and r.shares is None))
        a = sum(r.shares or 0.0 for r in af)
        o = sum(r.shares or 0.0 for r in of)
        out.append({"week": w, "ats_shares": a, "otc_shares": o,
                    "null_share_records": nulls,
                    "ats_share_of_off_exchange":
                        (a / (a + o) * 100.0) if (a + o) else None})
    return out
