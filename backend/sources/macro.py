"""宏观数据源 —— 美债收益率曲线（Treasury）+ 持仓报告（CFTC）。

━━━ 合规：两个都是 S 级 ━━━
美国财政部与 CFTC 的公开数据，政府作品、不限商用、可再分发。
本分栏是全项目**最干净**的一条线 —— 没有 OPRA、没有 §13107、没有 FINRA 条款。

━━━ ⚠️ 两个必须讲清的口径 ━━━
1. **收益率曲线倒挂**：短端高于长端。市场常用 10Y−2Y 与 10Y−3M 两个口径，
   **它们的倒挂时点可以差好几个月**，说"倒挂了"必须讲清用的是哪一个。
2. **COT 持仓报告有三天时滞**：报告的是**周二**的持仓，**周五**才发布。
   看到的永远是三天前的状态。
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

#: 收益率曲线的期限字段（Treasury XML 里的 d:BC_* 名 → 年数）
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
    """某年的每日收益率曲线（Treasury 官方 XML）。"""
    url = f"{TREASURY_XML}?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"Treasury 网络故障: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"Treasury 无 {year} 年数据")
    if r.status_code != 200:
        raise RuntimeError(f"Treasury HTTP {r.status_code}: {year}")

    blocks = re.findall(r"<m:properties>(.*?)</m:properties>", r.text, re.S)
    if not blocks:
        raise RuntimeError(f"Treasury {year} 未解析出任何记录（XML 结构可能已变更）")
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


#: CFTC 金融期货持仓报告（Traders in Financial Futures）的 Socrata 数据集
CFTC_TFF = "gpe5-46if"


def _cftc_get(params: dict) -> list[dict]:
    """打一次 CFTC Socrata。

    ⚠️ 用 requests 的 params 让它自己编码，**别手工拼 URL**：
    市场名里含 `&`（"S&P 500"），手工拼进 query string 会被当成参数分隔符 → 400。
    """
    _limiter.wait()
    try:
        r = requests.get(f"{CFTC_SODA}/{CFTC_TFF}.json", params=params,
                         headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"CFTC 网络故障: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        # ⚠️ **这不是「没数据」。** 实测（2026-07-26）：
        #   有效数据集 + 零匹配 → `200 []`
        #   数据集 ID 不存在   → `404 {"code":"dataset.missing"}`
        # 所以 404 只可能是数据集被改名/下线，或我们的 ID 写错了 ——
        # 配置故障必须冒泡，不能回退成"市场上没有持仓数据"。
        raise RuntimeError(
            "CFTC 数据集不存在（404）—— 数据集 ID 可能已变更，"
            f"当前用的是 {CFTC_TFF}。这是配置问题，不是「没有数据」。")
    if r.status_code == 400:
        # SoQL 语法/参数问题 —— 把上游的说明带出来，不要只报一个状态码
        raise RuntimeError(f"CFTC 拒绝该查询 (400): {(r.text or '')[:160]}")
    if r.status_code != 200:
        raise RuntimeError(f"CFTC HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError("CFTC 返回非 JSON（可能是错误页）") from e


def cot_markets() -> list[dict]:
    """TFF 覆盖的全部市场（含各自最后一期日期与记录数）。

    ⚠️ **必须带 `last_date` 一起返回**：这个数据集里躺着大量**早已停更**的
    合约（实测 186 个市场里只有 90 个还在更新，有的停在 2022 年）。
    只给一串名字，用户选中一个停更的就会看到"没数据"，
    而真相是"这个合约不报了"——**那是两件事**。
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
    """CFTC 持仓报告（TFF：金融期货的交易商分类）。

    ⚠️ **有三天时滞**：报告的是**周二**收盘的持仓，**周五**下午才发布。

    `exact=True` 要求市场名**完全相等**，模糊匹配用于搜索。
    ⚠️ 画时间序列时**必须用 exact**：`like '%S&P 500%'` 会同时命中
    "S&P 500 Consolidated"、"E-MINI S&P 500"、"S&P 500 QUARTERLY DIVIDEND IND"
    三条**不同合约**的记录，按日期排下来就是三条线揉成一条锯齿——
    看上去像持仓在剧烈翻转，其实只是在不同合约之间跳。
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
