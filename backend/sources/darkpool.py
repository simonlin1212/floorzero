"""FINRA 场外成交透明度（ATS 暗池 + 非 ATS 场外）。

━━━━━━━━━━━━━━ ⚠️ 合规：B 级，**默认关闭** ━━━━━━━━━━━━━━

与 `sources/shorts.py` 的 FINRA 部分同一套条款与同一个开关（`VF_ENABLE_FINRA`）。
条款原文、模糊之处、以及"我们不替你解释条款"的立场，见 `shorts.FINRA_TERMS`。

⚠️ **项目铁律：任何分栏都不得把 FINRA 作为唯一数据源。**
这一栏诚实地说：**ATS 成交量本身没有第二个公开来源** —— FINRA 是唯一发布方。
所以做法是：
- FINRA 提供**分子**（ATS / 非 ATS 场外的成交量），关闭时这部分**什么都不显示**，
  ⛔ 不拿别的数凑一个看起来像的答案；
- **分母**（同期总成交量）来自 CBOE 的本地行情沉淀（`scanner_store`），
  「场外占比」这个指标**真的需要两边**才算得出来。
- 因此关掉 FINRA 时本栏只剩条款说明与开启方法，这是**刻意的**。

━━━━━━━━━━━━━━ ⚠️ 三个会让人算错的坑（2026-07-26 实测坐实）━━━━━━━━━━━━━━

**① 「暗池」≠「场外」。** 同一个接口里混着两类完全不同的成交：

| 类型 | 是什么 | NVDA 2026-06-29 周 |
|---|---|---|
| **ATS** | 真正的暗池（不显示报价的另类交易系统）| 79,525,315 股 |
| **非 ATS 场外** | 批发商**内部化**（散户订单流卖给做市商成交）| **188,388,580 股** |

后者比前者**大 2.4 倍**，而且它**不是暗池**。市面上大量"暗池成交量"的说法
把两者混为一谈，数字于是虚高一倍以上。本模块**始终分开返回**。

**② 返回里混着聚合行和明细行，全加起来正好重复计一倍。**
`summaryTypeCode` 有四个值：`ATS_W_SMBL`（该标的 ATS 总量）、
`ATS_W_SMBL_FIRM`（按各家 ATS 拆分）、`OTC_W_SMBL`、`OTC_W_SMBL_FIRM`。
实测各家明细之和与聚合行**完全相等**（差 0）—— 所以它们是同一笔量的两种切法，
不加区分地 SUM 出来的 535,827,790 股恰好是真值的两倍。

**③ 非 ATS 场外的成交**在标的层**不披露是哪家**（`MPID` 全为空）。
实测 NVDA 那周 32 行非 ATS 记录，MPID 无一例外为空 —— 所以
"哪家批发商吃了多少"这个问题，**这份数据回答不了**。ATS 那边则有名有姓。

**④ 数据滞后约四周。** 实测 2026-07-26 能取到的最新周是 2026-06-29（滞后 27 天）。
FINRA 对 Tier 1 标的延迟两周发布、Tier 2 延迟四周，再加上发布节奏。
这不是"实时暗池监控"，是**事后统计**。
"""
from __future__ import annotations

import csv
import io
from typing import Iterator, Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter
from sources.shorts import FINRA_TERMS, FinraDisabled, finra_enabled  # noqa: F401

FINRA_API = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"

#: 四种记录类型。**ATS 与非 ATS 是两回事，聚合与明细是同一笔量的两种切法。**
TYPE_ATS_TOTAL = "ATS_W_SMBL"
TYPE_ATS_FIRM = "ATS_W_SMBL_FIRM"
TYPE_OTC_TOTAL = "OTC_W_SMBL"
TYPE_OTC_FIRM = "OTC_W_SMBL_FIRM"

#: 单次请求上限。FINRA 未公布硬上限，5000 实测可用。
MAX_LIMIT = 5000


def _post(body: dict) -> list[dict]:
    """打一次 FINRA 数据 API。

    ⚠️ 它**返回 CSV**（`Content-Type: text/plain`），尽管请求体是 JSON。
    按 JSON 解析会抛 `JSONDecodeError`，看着像"接口坏了"，其实只是格式没对上。
    ⚠️ 也**不要传 `sortFields`** —— 未指定全部分区键时会返回 400
    （"Sorting is allowed only if all partitions keys are specified"）。
    """
    if not finra_enabled():
        raise FinraDisabled(
            "FINRA 数据源默认关闭。设置环境变量 VF_ENABLE_FINRA=1 才启用。\n"
            "关闭是刻意的：FINRA Terms of Use 限「仅供非商业的个人或专业用途」，"
            "且明文禁止「用本站数据建立数据库」，而本项目正是下载→落 SQLite。\n"
            "条款原文与模糊之处见界面上的说明 —— 我们不替你解释条款，判断权在你。")
    _limiter.wait()
    try:
        r = requests.post(FINRA_API, json=body, timeout=90,
                          headers={"User-Agent": user_agent(),
                                   "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise RuntimeError(f"FINRA 网络故障: {type(e).__name__}: {e}") from e
    if r.status_code == 400:
        raise RuntimeError(f"FINRA 拒绝该查询 (400): {(r.text or '')[:200]}")
    if r.status_code == 404:
        raise RuntimeError(
            "FINRA 端点不存在（404）—— 数据集路径可能已变更。"
            "这是配置问题，不是「没有数据」。")
    if r.status_code != 200:
        raise RuntimeError(f"FINRA HTTP {r.status_code}")
    text = r.text or ""
    if not text.strip():
        raise DataNotAvailable("FINRA 返回空内容")
    return list(csv.DictReader(io.StringIO(text)))


def weekly(symbol: Optional[str] = None, limit: int = MAX_LIMIT) -> list[dict]:
    """某只标的（或全市场）的场外周度成交。

    ⚠️ 返回的**原始行里混着四种 `summaryTypeCode`**，
    直接 SUM 会把同一笔量算两遍 —— 分类交给 `modules/darkpool.py`。
    """
    body: dict = {"limit": max(1, min(limit, MAX_LIMIT))}
    if symbol:
        body["domainFilters"] = [
            {"fieldName": "issueSymbolIdentifier", "values": [symbol.strip().upper()]}]
    rows = _post(body)
    if len(rows) >= body["limit"]:
        # ⚠️ 取满上限 = **很可能被截断**，而且我们**不能排序**
        #    （传 sortFields 会 400），所以截掉的是哪几周无从得知。
        #    这时把周序列当"完整历史"用就是错的，必须让调用方知道。
        rows.append({"summaryTypeCode": "__TRUNCATED__",
                     "weekStartDate": "", "issueSymbolIdentifier": "",
                     "totalWeeklyShareQuantity": ""})
    if not rows:
        raise DataNotAvailable(
            f"FINRA 无 {symbol or '全市场'} 的场外成交记录"
            if symbol else "FINRA 无场外成交记录")
    return rows


def iter_weekly(symbol: Optional[str] = None) -> Iterator[dict]:
    yield from weekly(symbol)
