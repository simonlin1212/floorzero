"""AI 工具层 —— **唯一定义处**。

MCP server 从这里自动继承工具定义；以后接系统 AI / 多 agent 也复用同一份。
新增工具只改这个文件，别在 mcp_server.py 里另写一套（VibeResearch 踩过的坑：
工具定义散在多处 → 三条出口能力不一致）。

⚠️ 合规：工具输出**只给数据与计算结果**，不给买卖建议、不打「低估/高估」标签。
"""
from __future__ import annotations

from typing import Any, Callable

from sources import cboe
from modules import greeks
from modules import congress as congress_parse
from modules import congress_store
from modules import insider as insider_parse
from modules import insider_store
from modules import institution_store
from modules import shorts_store
from modules import market as market_parse
from modules import market_store
from modules import flow as flow_parse
from modules import flow_store
from modules import scanner as scanner_parse
from modules import scanner_store
from sources import darkpool as darkpool_src
from modules import darkpool as darkpool_parse
from modules import stock as stock_parse
from sources import macro as macro_src
from modules import shorts as shorts_parse

# ── 工具 schema（MCP / function-calling 通用）──
TOOLS: list[dict] = [
    {
        "name": "get_gex",
        "description": (
            "获取某只美股的 GEX（伽马敞口）画像：总 GEX、gamma flip 价位、"
            "call/put wall、按行权价与到期日的分布。"
            "GEX 为正表示做市商对冲会抑制波动，为负表示会放大波动。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "美股代码，如 SPY / NVDA"},
                "dte_max": {"type": "integer",
                            "description": "只统计 N 天内到期的合约；0 表示只看当日到期(0DTE)。不传=全链"},
                "strike_pct": {"type": "number",
                               "description": "行权价范围 ±比例，默认 0.05（±5%）"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_short_fails",
        "description": (
            "查询 SEC 交割失败（fails-to-deliver）数据。"
            "⚠️ **三条必须一起读的口径**：(1) 它是**某结算日的累计余额**不是当日新增，"
            "SEC 明说相邻两日「may have little or no relationship」、"
            "「the age of fails cannot be determined」；"
            "(2) SEC 明说交割失败**既可能来自多头也可能来自空头**，"
            "**不是裸卖空的证据**；(3) 按标的汇总用的是各结算日余额的**均值**不是加总"
            "（同一笔未交割会在连续多日重复出现）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码"},
                "settlement_date": {"type": "string", "description": "结算日 YYYY-MM-DD"},
                "since": {"type": "string", "description": "结算日下限 YYYY-MM-DD"},
                "min_quantity": {"type": "number", "description": "余额下限（股）"},
                "top": {"type": "integer", "description": "榜单取前几名，默认 10"},
            },
        },
    },
    {
        "name": "get_institution_holdings",
        "description": (
            "查询机构 13F 持仓（SEC 季度申报）。"
            "⚠️ **13F 只报季末时点、13(f) 证券的多头持仓** —— 不含空头（SEC 另立 Form SHO）、"
            "现金、债券、仅境外上市股票、私募持仓。看跌期权按标的列示，是**看空**，"
            "默认已排除在持仓统计外。申报至少滞后 45 天。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cusip": {"type": "string", "description": "CUSIP（不是股票代码 —— 13F 只给 CUSIP）"},
                "manager": {"type": "string", "description": "机构名（模糊匹配），如 Berkshire"},
                "period": {"type": "string", "description": "报告期 YYYY-MM-DD（季末）"},
                "kind": {"type": "string", "enum": ["share", "call", "put", "all"],
                         "description": "持仓类型，默认 share"},
                "top": {"type": "integer", "description": "各榜单取前几名，默认 10"},
            },
        },
    },
    {
        "name": "get_institution_changes",
        "description": (
            "机构持仓的季度环比：新建仓 / 加仓 / 减仓 / 清仓。"
            "⭐ 13F 的主要价值在变动，单季持仓只是静态快照。"
            "⚠️ 「清仓」只代表该标的不再出现在 13(f) 多头持仓里，不等于机构看空。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "本期报告期 YYYY-MM-DD"},
                "prev_period": {"type": "string", "description": "上期报告期 YYYY-MM-DD"},
                "manager": {"type": "string", "description": "只看某家机构"},
                "top": {"type": "integer", "description": "各榜单取前几名，默认 10"},
            },
        },
    },
    {
        "name": "get_insider_trades",
        "description": (
            "查询美股上市公司内部人（高管/董事/10%股东）依 SEC Form 4 申报的交易。"
            "⚠️ **默认只返回公开市场主动买卖（代码 P/S）** —— Form 4 里约七成是"
            "授予/期权行权/代扣税等薪酬类交易，把它们当成'内部人买入'会把买盘夸大数倍。"
            "读的是本地已同步的缓存。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码，如 NVDA"},
                "owner": {"type": "string", "description": "内部人姓名（模糊匹配）"},
                "direction": {"type": "string", "enum": ["buy", "sell"]},
                "role": {"type": "string", "enum": ["officer", "director", "ten_pct"],
                         "description": "身份：高管/董事/10%股东"},
                "group": {"type": "string",
                          "enum": ["open_market", "compensation", "other", "all"],
                          "description": "交易大类，默认 open_market"},
                "plan": {"type": "string", "enum": ["yes", "no"],
                         "description": "是否 10b5-1 预设计划交易"},
                "since": {"type": "string", "description": "交易日下限 YYYY-MM-DD"},
                "min_value": {"type": "number", "description": "成交金额下限（美元）"},
                "limit": {"type": "integer", "description": "最多返回几笔，默认 50"},
            },
        },
    },
    {
        "name": "get_insider_summary",
        "description": (
            "内部人交易聚合：净买卖金额、集群买入榜（多少位不同内部人买同一只）、"
            "活跃内部人。只统计公开市场交易。⚠️ 只呈现事实，不给买卖建议。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "since": {"type": "string", "description": "交易日下限 YYYY-MM-DD"},
                "role": {"type": "string", "enum": ["officer", "director", "ten_pct"]},
                "min_value": {"type": "number"},
                "top": {"type": "integer", "description": "各榜单取前几名，默认 10"},
            },
        },
    },
    {
        "name": "get_congress_trades",
        "description": (
            "查询美国国会议员依 STOCK Act 公开申报的股票交易（众议院 + 参议院）。"
            "可按标的、议员、院别、方向、起始交易日筛选。"
            "⚠️ 金额是**区间**不是精确值；披露天然滞后数十天；"
            "读的是本地已同步的缓存，未同步则为空。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码，如 NVDA"},
                "member": {"type": "string", "description": "议员姓名（模糊匹配）"},
                "chamber": {"type": "string", "enum": ["house", "senate"],
                            "description": "院别，不传=两院"},
                "tx_type": {"type": "string", "enum": ["buy", "sell"],
                            "description": "买入/卖出，不传=全部"},
                "since": {"type": "string", "description": "交易日下限 YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "最多返回几笔，默认 50"},
            },
        },
    },
    {
        "name": "get_congress_summary",
        "description": (
            "国会议员交易的聚合视图：最活跃标的、交易最多的议员、买卖比、披露延迟统计。"
            "⚠️ 金额为区间中值加总，只能横向比较，不是真实成交额。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "交易日下限 YYYY-MM-DD"},
                "chamber": {"type": "string", "enum": ["house", "senate"]},
                "top": {"type": "integer", "description": "各榜单取前几名，默认 10"},
            },
        },
    },
    {
        "name": "get_gex_curve",
        "description": (
            "获取 GEX 随假设股价变化的曲线，用于定位 gamma flip。"
            "每个价位都用 Black-Scholes 重算 gamma（不是复用当前 gamma）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "dte_max": {"type": "integer",
                            "description": "只统计 N 天内到期；不传=全链（与 get_gex 默认一致）"},
                "span_pct": {"type": "number", "description": "扫描股价范围 ±比例，默认 0.06"},
                "strike_pct": {"type": "number",
                               "description": "行权价范围 ±比例，默认 0.05（须与 get_gex 一致）"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_option_chain_summary",
        "description": (
            "获取某只美股期权链的概览：现价、合约总数、可用到期日、"
            "以及按到期天数分桶的成交量分布（看市场有多短线）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_option_flow",
        "description": (
            "获取某只美股当日的期权异动与持仓结构：vol/OI 异动榜、"
            "认沽/认购比（成交量/持仓量/权利金 三口径）、绝对 delta 敞口、到期分布。"
            "⚠️ 数据是**链快照**不是逐笔成交带 —— **无法**判断主动买卖方向、"
            "无法做 sweep 检测与大单分级，本工具因此**不给任何看涨/看跌标签**。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "dte_max": {"type": "integer", "description": "只看 N 天内到期；不传=全链"},
                "top": {"type": "integer", "description": "异动榜条数，默认 15"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_oi_change",
        "description": (
            "获取本地已沉淀的期权持仓量（OI）变化 —— 两个快照日之间谁在建仓/平仓。"
            "⚠️ 这份历史**补不回来**，只有本机攒过才有；没攒够会明确返回 enough=false"
            "（那是「还没攒够」，不是「持仓没变化」）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD，不传=次新快照"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD，不传=最新快照"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "scan_market",
        "description": (
            "在**本地已扫过**的行情快照里筛标的：IV Rank / IV 百分位 / 成交量放大倍数 / "
            "价格 / 涨跌幅。"
            "⚠️ **IV Rank 按定义需要历史**，本机攒不够时该字段为空 —— "
            "那是「还没攒够」，不是「排名低」，两者绝不能混同。"
            "⚠️ 读的是本地快照，不现拉；没扫过会明确说明。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_iv_rank": {"type": "number", "description": "IV Rank 下限 0-100"},
                "min_volume": {"type": "number", "description": "成交量下限（股）"},
                "min_volume_x": {"type": "number",
                                 "description": "成交量至少是本地历史中位数的几倍"},
                "min_price": {"type": "number"},
                "sort": {"type": "string",
                         "description": "iv_rank/iv_percentile/iv30/volume/volume_x/change_pct"},
                "top": {"type": "integer", "description": "返回几只，默认 20"},
            },
        },
    },
    {
        "name": "get_darkpool",
        "description": (
            "查询某只美股的场外成交（FINRA 周度）。"
            "⚠️ **ATS（真暗池）与非 ATS 场外（批发商内部化）是两类不同的成交，"
            "本工具分开返回，绝不相加叫「暗池成交量」** —— 实测非 ATS 常比 ATS 大一倍以上。"
            "⚠️ 数据**滞后约四周**，非 ATS 场外**不披露机构名**。"
            "⚠️ 该数据源默认关闭（FINRA 条款），未开启时会明确说明。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "week": {"type": "string", "description": "周起始日 YYYY-MM-DD，不传=最新"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_stock",
        "description": (
            "一只美股在九条数据线上的全部画像：行情/期权链、GEX、期权流、IV 排名、"
            "内部人 Form 4、交割失败、国会申报、机构 13F、场外/暗池。"
            "⚠️ **这九块的新鲜度相差两个数量级**（期权链是上一个交易时段，"
            "13F 是三个月前的季末），每块都带自己的时点与滞后天数 —— "
            "⛔ **不要把它们当成同一时刻的事**，本工具也**不做任何跨源综合评分**。"
            "⚠️ 某块缺失时会给出**它自己的原因**（还没同步/没攒够/源被关着/"
            "取数失败/定位不到），这些**都不等于**「这只票没有那类活动」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_yield_curve",
        "description": (
            "获取美债收益率曲线：最新一天的完整期限结构 + 两条利差"
            "（10Y-2Y 与 10Y-3M）的历史。"
            "⚠️ 「倒挂」有两条常用口径且时点可差数月，本工具两条都给、不挑一条。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "years": {"type": "integer",
                          "description": "往前看几年，默认 3（1 年常看不到穿越零轴）"},
            },
        },
    },
    {
        "name": "get_cot",
        "description": (
            "获取 CFTC 金融期货持仓报告（TFF）：杠杆基金 / 资产管理 / 交易商"
            "三类的多空与净持仓。⚠️ 有三天时滞（报周二持仓、周五发布）。"
            "不传 market 则返回可选合约清单。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "market": {"type": "string",
                           "description": "合约名关键词，如 'E-MINI S&P 500' / 'TREASURY'"},
                "periods": {"type": "integer", "description": "取最近几期，默认 12"},
            },
        },
    },
]


# ── 实现 ──
def _tool_get_gex(ticker: str, dte_max: int | None = None,
                  strike_pct: float = 0.05) -> dict:
    chain = cboe.cached_option_chain(ticker)
    profile = greeks.compute(chain, dte_max=dte_max, strike_pct=strike_pct)
    out = greeks.to_dict(profile)
    # 给 AI 一句人话总结，省得它自己揣摩符号含义
    flip = out["gamma_flip"]
    # ⚠️ 三种 regime 都要如实描述。把 neutral 归进「正」会凭空告诉 AI
    # 「做市商对冲会抑制波动」，那是无中生有的解读。
    regime_txt = {
        "positive": "正 gamma，做市商对冲会抑制波动",
        "negative": "负 gamma，做市商对冲会放大波动",
        "neutral": "零敞口，没有可测量的 gamma（可能是所选合约缺 gamma 数据，或多空恰好抵消）",
    }[out["regime"]]
    out["summary"] = (
        f"{out['ticker']} 现价 ${out['spot']}，总 GEX {out['total_gex_bn']:+.2f}B（{regime_txt}）。"
        + (f"Gamma flip 在 ${flip}，现价在其"
           f"{'下方' if out['spot'] < flip else '上方'}。" if flip else "区间内未出现 gamma flip。")
    )
    return out


def _tool_get_gex_curve(ticker: str, dte_max: int | None = None,
                        span_pct: float = 0.06, strike_pct: float = 0.05) -> dict:
    """⚠️ 默认口径必须与 get_gex 一致（dte_max=None 全链、strike_pct=0.05）。
    否则 AI 同时调两个工具时，曲线零点会和 get_gex 返回的 gamma_flip 对不上。"""
    chain = cboe.cached_option_chain(ticker)
    lo_k, hi_k = chain.spot * (1 - strike_pct), chain.spot * (1 + strike_pct)
    cs = [c for c in chain.filter(dte_max=dte_max) if lo_k <= c.strike <= hi_k]
    if not cs:
        raise ValueError(f"{ticker} 无符合条件的合约")
    lo, hi = chain.spot * (1 - span_pct), chain.spot * (1 + span_pct)
    curve = [{"price": round(lo + (hi - lo) * i / 40, 2),
              "gex_bn": round(greeks.total_gex_at(cs, lo + (hi - lo) * i / 40) / 1e9, 4)}
             for i in range(41)]
    return {"ticker": chain.ticker, "spot": round(chain.spot, 2), "curve": curve}


def _tool_get_option_chain_summary(ticker: str) -> dict:
    from collections import defaultdict
    chain = cboe.cached_option_chain(ticker)
    buckets: dict[str, float] = defaultdict(float)
    for c in chain.contracts:
        if not c.volume:
            continue
        d = c.dte
        key = "0-1天" if d <= 1 else "2-7天" if d <= 7 else "8-30天" if d <= 30 else "30天以上"
        buckets[key] += c.volume
    total = sum(buckets.values()) or 1
    return {
        "ticker": chain.ticker,
        "spot": round(chain.spot, 2),
        "contracts": len(chain.contracts),
        "expiries": chain.expiries()[:12],
        "volume_by_dte": {k: {"volume": v, "pct": round(v / total * 100, 1)}
                          for k, v in buckets.items()},
        "note": "0-1天占比高说明该标的的期权交易极度短线化（0DTE 生态）",
    }


def _row_to_trade(r: dict):
    """DB 行 → Trade。与 REST 层同一套派生逻辑，避免两条出口字段不一致。"""
    from datetime import date as _d

    def d(v):
        return _d.fromisoformat(v) if v else None

    return congress_parse.Trade(
        chamber=r["chamber"], member=r["member"],
        state_district=r["state_district"] or "", ticker=r["ticker"],
        asset_name=r["asset_name"] or "", asset_type=r["asset_type"],
        asset_type_label=r["asset_type_label"] or "", tx_type=r["tx_type"] or "",
        tx_type_label=r["tx_type_label"] or "", tx_date=d(r["tx_date"]),
        notification_date=d(r["notification_date"]), filing_date=d(r["filing_date"]),
        amount_low=r["amount_low"], amount_high=r["amount_high"],
        amount_raw=r["amount_raw"] or "", owner=r["owner"] or "self",
        doc_id=r["doc_id"], source_url=r["source_url"] or "")


def _tool_get_congress_trades(ticker: str | None = None, member: str | None = None,
                              chamber: str | None = None, tx_type: str | None = None,
                              since: str | None = None, limit: int = 50) -> dict:
    rows = congress_store.query_trades(chamber=chamber, ticker=ticker, member=member,
                                       since=since, tx_type=tx_type,
                                       limit=max(1, min(limit, 500)))
    trades = [congress_parse.to_dict(_row_to_trade(r)) for r in rows]
    st = congress_store.stats()
    if not trades and st["trades"] == 0:
        # 空结果有两种成因，必须说清是哪一种
        return {"trades": [], "count": 0,
                "summary": "本地还没有同步任何国会申报数据 —— "
                           "这是**尚未同步**，不是没有交易。先调用 POST /api/congress/sync。"}
    # ⚠️ 交易类型不止买卖：还有 E（交换）。用 len-buys 当卖出数会把交换算成卖出。
    # 判定规则与 summarize() 保持一致：P=买入，S 开头=卖出（含"部分卖出"），其余单列。
    buys = sum(1 for t in trades if t["tx_type"] == "P")
    sells = sum(1 for t in trades if str(t["tx_type"]).startswith("S"))
    others = len(trades) - buys - sells
    return {
        "trades": trades, "count": len(trades),
        "buys": buys, "sells": sells, "other_types": others,
        "coverage": {"cached_trades": st["trades"],
                     "unparsed_filings": st["unparsed_filings"],
                     "last_sync": st["last_sync"]},
        "summary": (f"返回 {len(trades)} 笔（买入 {buys} / 卖出 {sells}"
                    + (f" / 其他类型 {others}（如交换）" if others else "") + "）。"
                    f"本地共缓存 {st['trades']} 笔，另有 {st['unparsed_filings']} 份申报"
                    f"为纸质扫描件未能解析。金额均为区间不是精确值；"
                    f"披露滞后数十天，不代表当前持仓。"),
    }


#: Congress 汇总的取数上限。⚠️ 命中超过它就只是"最新 N 笔"的统计。
_CONGRESS_SUMMARY_LIMIT = 5000


def _tool_get_congress_summary(since: str | None = None, chamber: str | None = None,
                               top: int = 10) -> dict:
    rows = congress_store.query_trades(chamber=chamber, since=since,
                                       limit=_CONGRESS_SUMMARY_LIMIT)
    out = congress_parse.summarize([_row_to_trade(r) for r in rows])
    n = max(1, min(top, 50))
    out["by_ticker"] = out["by_ticker"][:n]
    out["by_member"] = out["by_member"][:n]
    if not rows:
        out["summary"] = "该条件下本地无数据（可能是尚未同步，或该时间段确实无申报）。"
        return out
    hot = "、".join(f"{t['ticker']}({t['trades']}笔)" for t in out["by_ticker"][:5])
    # ⚠️ 命中超上限时必须说出来：REST 端点有 truncated 标记，
    # MCP 这边不说的话，AI 会把"最新 5000 笔的统计"当成整段时间的结论。
    truncated = len(rows) >= _CONGRESS_SUMMARY_LIMIT
    out["scope"] = {"chamber": chamber, "since": since, "sampled": len(rows),
                    "limit": _CONGRESS_SUMMARY_LIMIT, "truncated": truncated}
    prefix = (f"⚠️ 命中已达上限 {_CONGRESS_SUMMARY_LIMIT} 笔，"
              f"以下统计**只覆盖最新 {len(rows)} 笔**，不是该时间段全部。"
              if truncated else "")
    out["summary"] = (prefix +
        f"共 {out['total_trades']} 笔（买 {out['buys']} / 卖 {out['sells']}）。"
        f"最活跃标的：{hot}。披露延迟中位 {out['delay']['median_days']} 天，"
        f"其中 {out['delay']['over_45d_count']} 笔超过 45 天 —— "
        f"这是事实统计不是违规认定（期限有周末顺延等情形）。"
        f"金额为区间中值加总，仅供横向比较。")
    return out


def _ins_row(r: dict):
    """DB 行 → InsiderTrade。与 REST 层同一条派生路径。"""
    from datetime import date as _d

    def d(v):
        return _d.fromisoformat(v) if v else None

    return insider_parse.InsiderTrade(
        accession=r["accession"], seq=r["seq"], ticker=r["ticker"],
        company=r["company"] or "", issuer_cik=r["issuer_cik"] or "",
        owner=r["owner"] or "", owner_cik=r["owner_cik"] or "",
        is_officer=bool(r["is_officer"]), is_director=bool(r["is_director"]),
        is_ten_pct=bool(r["is_ten_pct"]), officer_title=r["officer_title"] or "",
        security=r["security"] or "", tx_code=r["tx_code"] or "",
        tx_date=d(r["tx_date"]), filing_date=d(r["filing_date"]),
        shares=r["shares"], price=r["price"],
        acquired_disposed=r["acquired_disposed"] or "",
        shares_after=r["shares_after"], is_direct=bool(r["is_direct"]),
        is_10b5_1=None if r["is_10b5_1"] is None else bool(r["is_10b5_1"]),
        form_type=r["form_type"] or "4",
        source_url=r["source_url"] or "")


def _tool_get_insider_trades(ticker: str | None = None, owner: str | None = None,
                             direction: str | None = None, role: str | None = None,
                             group: str = "open_market", plan: str | None = None,
                             since: str | None = None, min_value: float | None = None,
                             limit: int = 50) -> dict:
    rows = insider_store.query(
        ticker=ticker, owner=owner, group=None if group == "all" else group,
        direction=direction, role=role, plan=plan, since=since,
        min_value=min_value, limit=max(1, min(limit, 500)))
    st = insider_store.stats()
    if not rows and st["trades"] == 0:
        return {"trades": [], "count": 0,
                "summary": "本地还没有同步任何 Form 4 数据 —— 这是**尚未同步**，"
                           "不是没有内部人交易。先调用 POST /api/insider/sync。"}
    out = [insider_parse.to_dict(_ins_row(r)) for r in rows]
    buys = sum(1 for t in out if t["direction"] == "buy")
    sells = sum(1 for t in out if t["direction"] == "sell")
    bv = sum(t["value"] or 0 for t in out if t["direction"] == "buy")
    sv = sum(t["value"] or 0 for t in out if t["direction"] == "sell")
    scope = ("公开市场主动买卖" if group == "open_market"
             else "薪酬类（授予/行权/代扣税）" if group == "compensation"
             else "其他类型" if group == "other" else "全部类型")
    return {
        "trades": out, "count": len(out), "buys": buys, "sells": sells,
        "coverage": {"cached_trades": st["trades"],
                     "open_market_pct": st["open_market_pct"],
                     "range": [st["earliest"], st["latest"]],
                     "last_sync": st["last_sync"]},
        "summary": (f"返回 {len(out)} 笔（{scope}）：买入 {buys} 笔 ${bv:,.0f}、"
                    f"卖出 {sells} 笔 ${sv:,.0f}。本地共 {st['trades']:,} 行，"
                    f"其中公开市场仅占 {st['open_market_pct']}% —— "
                    f"其余是授予/行权/代扣税等薪酬类，不代表买卖决策。"),
    }


def _tool_get_insider_summary(ticker: str | None = None, since: str | None = None,
                              role: str | None = None, min_value: float | None = None,
                              top: int = 10) -> dict:
    # ⚠️ 与 REST 走同一条 SQL 全量聚合，不是"取最新 N 行再算" ——
    # 否则 MCP 报出的总额会是「最新 N 行」的，却被 AI 当成整个区间的结论。
    agg = insider_store.aggregate(top=max(1, min(top, 50)), ticker=ticker,
                                  since=since, role=role, group="open_market",
                                  min_value=min_value)
    lib = insider_store.stats()
    c = agg["counts"]
    out = {
        "open_market": {"count": c["n"] or 0, "buys": c["buys"] or 0,
                        "sells": c["sells"] or 0,
                        "buy_value": round(c["bv"] or 0),
                        "sell_value": round(c["sv"] or 0)},
        "by_ticker": agg["by_ticker"], "cluster_buys": agg["cluster_buys"],
        "by_owner": agg["by_owner"], "plan_sells": c["plan_sells"] or 0,
        "notes": insider_parse.summary_notes(lib), "stats": lib,
    }
    if not c["n"]:
        out["summary"] = "该条件下本地无公开市场交易（可能尚未同步，或该区间确无）。"
        return out
    om = out["open_market"]
    cluster = "、".join(f"{c['ticker']}({c['insider_count']}人)"
                        for c in out["cluster_buys"][:5]) or "无"
    out["summary"] = (
        f"公开市场买入 {om['buys']} 笔 ${om['buy_value']:,}、"
        f"卖出 {om['sells']} 笔 ${om['sell_value']:,}。"
        f"多人买入的标的：{cluster}。其中 {out['plan_sells']} 笔卖出属 10b5-1 预设计划"
        f"（几个月前排定，非临时决定）。以上只是已申报事实的统计，不构成投资建议。")
    return out


def _tool_get_institution_holdings(cusip: str | None = None,
                                   manager: str | None = None,
                                   period: str | None = None,
                                   kind: str = "share", top: int = 10) -> dict:
    st = institution_store.stats()
    if not st["holdings"]:
        return {"holdings": [], "count": 0,
                "summary": "本地还没有导入任何 13F 数据 —— 这是**尚未导入**，"
                           "不是机构没有持仓。先调用 POST /api/institution/sync。"}
    p = period or (st["periods"][0] if st["periods"] else None)
    agg = institution_store.aggregate(top=max(1, min(top, 50)), cusip=cusip,
                                      manager=manager, period=p, kind=kind)
    c = agg["counts"]
    puts = (agg["by_kind"].get("put") or {}).get("value") or 0
    hot = "、".join(f"{x['issuer'][:22]}({_money(x['value'])}, {x['holders']}家)"
                    for x in agg["by_issuer"][:5]) or "无"
    return {
        **agg, "period": p, "stats": st,
        "summary": (f"{p} 报告期：{c['n']:,} 条持仓、{c['mgrs']:,} 家机构、"
                    f"合计 {_money(c['val'] or 0)}。持仓最大：{hot}。"
                    f"⚠️ 13F 只含**多头**且只含 13(f) 证券，不含空头/现金/债券/"
                    f"境外上市股票；看跌期权（本期 {_money(puts)}）按标的列示、"
                    f"已单独归类不计入持仓；数据至少滞后 45 天。"),
    }


def _tool_get_institution_changes(period: str | None = None,
                                  prev_period: str | None = None,
                                  manager: str | None = None,
                                  top: int = 10) -> dict:
    have = institution_store.known_periods()
    if len(have) < 2:
        return {"summary": f"需要至少两个报告期才能比对，当前只有 {have or '零'} —— "
                           f"先导入更多季度（13F 的价值在变动，不在静态快照）。"}
    p = period or have[0]
    pp = prev_period or next((x for x in have if x < p), None)
    if not pp:
        return {"summary": f"{p} 之前没有已导入的报告期，无法比对。已有：{'、'.join(have)}"}
    out = institution_store.changes(period=p, prev_period=pp,
                                    top=max(1, min(top, 50)), manager=manager)
    fmt = lambda rows: "、".join(f"{r['issuer'][:20]}({_money(r['delta_value'])})"
                                 for r in rows[:4]) or "无"
    out["summary"] = (
        f"{pp} → {p}：新建仓 {out['counts']['new']} / 加仓 {out['counts']['increased']} / "
        f"减仓 {out['counts']['decreased']} / 清仓 {out['counts']['exited']}。"
        f"加仓最多：{fmt(out['increased'])}；减仓最多：{fmt(out['decreased'])}。"
        f"⚠️ 「清仓」只代表不再出现在 13(f) 多头持仓里，不等于看空。{out['floor_note']}")
    return out


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    a = abs(n)
    sign = "-" if (n or 0) < 0 else ""
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.1f}{unit}"
    return f"{sign}${a:.0f}"


def _tool_get_short_fails(symbol: str | None = None,
                          settlement_date: str | None = None,
                          since: str | None = None,
                          min_quantity: float | None = None,
                          top: int = 10) -> dict:
    st = shorts_store.stats()
    if not st["rows"]:
        return {"fails": [], "count": 0,
                "summary": "本地还没有导入 FTD 数据 —— 这是**尚未导入**，"
                           "不是市场上没有交割失败。先调用 POST /api/shorts/sync。"}
    agg = shorts_store.aggregate(top=max(1, min(top, 50)), symbol=symbol,
                                 settlement_date=settlement_date, since=since,
                                 min_quantity=min_quantity)
    c = agg["counts"]
    hot = "、".join(
        f"{x['symbol']}(均 {(x['avg_quantity'] or 0):,.0f} 股)"
        for x in agg["by_symbol"][:5]) or "无"
    return {
        **agg, "stats": st, "notes": shorts_parse.OFFICIAL_NOTES,
        "summary": (
            f"{c['lo']} ~ {c['hi']} 共 {c['n']:,} 条记录、{c['syms']:,} 只标的、"
            f"{c['days']} 个结算日。余额最大：{hot}。"
            f"⚠️ 这是**某结算日的累计余额**不是当日新增（SEC：相邻两日"
            f"「may have little or no relationship」、无法判断 fails 的存续时长）；"
            f"⚠️ SEC 明说交割失败**既可能来自多头也可能来自空头、不是裸卖空的证据**；"
            f"⚠️ 榜单用的是各结算日余额的**均值**不是加总。"
            f"以上只是已公开数据的统计，不构成投资建议。"),
    }


def _tool_get_option_flow(ticker: str, dte_max: int | None = None,
                          top: int = 15) -> dict:
    chain = cboe.cached_option_chain(ticker)
    scope = flow_parse.parse(chain, dte_max=dte_max, traded_only=False)
    traded = [r for r in scope if r.volume > 0]
    out = flow_parse.summarize(chain, traded, all_rows=scope,
                               top=max(1, min(top, 60)))
    c, rt = out["counts"], out["ratios"]
    hot = "；".join(
        f"{x['expiry']} {'认沽' if x['type'] == 'put' else '认购'} {x['strike']:g}"
        f"（成交 {x['volume']:,.0f} 张"
        + ("、前收持仓为 0（**这不代表今天全是新开仓** —— 也可能是"
           "冷门行权价，或开了又平的日内往返）" if x["zero_prior_oi"]
           else f"、vol/OI {x['vol_oi']:.1f}")
        # ⚠️ 别写 `x['notional'] or 0` —— 结构化字段是 null（算不出），
        #    摘要却说"$0"，同一个响应里两个说法互相打架。
        + (f"、权利金估算 {_money(x['notional'])}）" if x["notional"] is not None
           else "、权利金**算不出**（缺双边报价））")
        for x in out["unusual_rows"][:3]) or "无"

    def _pc(k: str) -> str:
        """⚠️ `pc=None` 有**两种**原因，不能一律说成"分母为 0"。"""
        d = rt[k]
        if d["pc"] is not None:
            return f"{d['pc']:.2f}"
        if k == "by_notional":
            # ⚠️ counted=0 有**两种**原因：那一侧根本没成交，或成交了但全缺报价。
            #    一律说成"缺双边报价"会把"没有认购成交"误诊成数据问题。
            bad = []
            for side, cnt, vol in (("认购", d.get("counted_call"), rt["by_volume"]["call"]),
                                   ("认沽", d.get("counted_put"), rt["by_volume"]["put"])):
                if cnt:
                    continue
                bad.append(f"{side}侧" + ("今日无成交" if not vol else "成交了但全部缺报价"))
            if bad:
                return "算不出（" + "、".join(bad) + "）"
        return "算不出（分母为 0，即该侧完全没有成交/持仓）"

    return {
        **out,
        "summary": (
            f"{chain.ticker} ${chain.spot:.2f}（交易时段 {chain.session or '未知'}）："
            f"口径 {'全链' if dte_max is None else f'{dte_max} 天内到期'}"
            f"（⚠️ 网页端默认只看 7 天，口径不同则合约数与各比值都会不同）："
            f"范围内 {c['scope_contracts']:,} 个合约、其中 {c['traded_contracts']:,} 个今日有成交，"
            f"合计成交 {c['total_volume']:,.0f} 张、持仓 {c['total_oi']:,.0f} 张。"
            f"异动 {c['unusual']} 个（含 {c['zero_prior_oi']} 个前收持仓为 0 的）。"
            f"认沽/认购比：按成交量 {_pc('by_volume')}、"
            f"按持仓量 {_pc('by_oi')}、按权利金估算 {_pc('by_notional')} "
            f"—— **三个口径量的是不同的东西，结论不同是正常的**。"
            f"最大异动：{hot}。"
            f"⛔ **本数据是链快照，不是逐笔成交带**：无法判断这些成交是买方还是卖方发起，"
            f"因此**不能**据此说「看涨」或「看跌」，也做不了 sweep 检测与大单分级。"
            f"权利金为「累计成交量 × 抓取时中间价」的**估算**，与实际成交额可能差数倍。"
            f"以上为公开延时数据的呈现，不构成投资建议。"),
    }


def _tool_get_oi_change(ticker: str, date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    tk = (ticker or "").strip().upper()
    out = flow_store.oi_change(tk, date_from=date_from, date_to=date_to, top=15)
    if not out.get("enough"):
        # ⚠️ `enough=false` 有**三种**原因，不能一律归成"本机历史不足"：
        #    ① 真的只攒了 0~1 天 ② 指定的日期库里没有 ③ 起止日期传反了。
        #    ②③ 是**参数问题**，说成"历史不足"会把 AI 引向错误的下一步。
        have = out.get("have", 0)
        if date_from or date_to:
            why = ("这是**指定的日期有问题**（库里没有那一天，或起止顺序反了）—— "
                   f"本地已有的快照日：{'、'.join(out.get('dates', [])[:8]) or '无'}。")
        elif have < 2:
            why = ("这是**本机历史不足**（这份数据补不回来，只能逐日攒），"
                   "不是市场上持仓没有变动。")
        else:
            why = "这不是「持仓没有变化」，具体原因见 note。"
        return {**out, "summary": f"{tk} 无法计算持仓量变化：{out['note']} {why}"}
    t = out["totals"]
    top3 = "；".join(
        f"{x['expiry']} {'认沽' if x['type'] == 'put' else '认购'} {x['strike']:g} "
        f"{x['change']:+,.0f} 张" for x in out["gained"][:3]) or "无"
    span = ("相邻两个快照" if out["is_consecutive"]
            else f"相隔 {out['span_days']} 天、中间还夹着 "
                 f"{out['snapshots_between']} 次观测，属**累计**变化")
    return {**out, "summary": (
        f"{tk} {out['date_from']} → {out['date_to']}（{span}）："
        f"认购持仓净变 {t['call_change']:+,.0f} 张、认沽 {t['put_change']:+,.0f} 张，"
        f"涉及 {t['contracts']:,} 个合约。增持最多：{top3}。"
        + (f"已排除 {out['expired_excluded']} 个期间到期的合约"
           f"（{out['expired_oi']:,.0f} 张）—— 到期消失不是平仓。"
           if out.get("expired_excluded") else "")
        # ⚠️ 网页端会为这两条打警告横幅，工具层不说就成了「两个视图对
        #    同一份数据的可信度表述不一致」—— 本项目反复踩的那类坑。
        + (f"⚠️ **本次比较可信度存疑**：有 {out['incomplete_excluded']} 个尚未到期的合约"
           f"不在结束快照里（{out['incomplete_oi']:,.0f} 张持仓），说明那次抓取不完整，已排除。"
           if out.get("incomplete_excluded") else "")
        + (f"⚠️ 有 {out['new_listings']} 个合约只出现在结束快照里、按「从 0 新增」计入 —— "
           f"**期间新挂牌**与**起始那次漏抓**在数据上无法区分"
           f"（两次合约数 {out.get('contracts_from')} → {out.get('contracts_to')}）。"
           if out.get("new_listings") else "")
        + f"⚠️ 持仓量增减**不指示方向**：每张合约都有买卖两方，"
          f"净新增的多头与空头数量相同。以上不构成投资建议。")}


def _tool_scan_market(min_iv_rank: float | None = None,
                      min_volume: float | None = None,
                      min_volume_x: float | None = None,
                      min_price: float | None = None,
                      sort: str = "iv30", top: int = 20) -> dict:
    sess = scanner_store.latest_session()
    st = scanner_store.stats()
    if not sess:
        return {"rows": [], "count": 0, "stats": st,
                "summary": ("本地还没有任何扫描结果 —— 这是**还没扫过**，"
                            "不是市场上没有符合条件的标的。"
                            "先跑一轮扫描（POST /api/scanner/scan）。")}
    quotes = scanner_store.quotes_at(sess)
    hist = scanner_store.history([q["symbol"] for q in quotes], as_of=sess)
    rows = [scanner_parse.build_row(
                q, hist.get(q["symbol"], {}).get("iv", []),
                hist.get(q["symbol"], {}).get("volume", []))
            for q in quotes]
    kept, excluded = scanner_parse.apply_filters(
        rows, min_iv_rank=min_iv_rank, min_volume=min_volume,
        min_volume_x=min_volume_x, min_price=min_price)
    kept = scanner_parse.sort_rows(kept, sort)
    total = len(kept)                      # ⚠️ 截断**之前**的总数
    kept = kept[:max(1, min(top, 100))]
    hot = "；".join(
        f"{r.symbol}（IV30 " + ("—" if r.iv30 is None else f"{r.iv30:.1f}")
        + (f"、IV Rank {r.iv_rank:.0f}" if r.iv_rank is not None
           else "、IV Rank 空（"
                + scanner_parse.REASON_LABEL.get(r.iv_reason or "unknown", "原因未知")
                + (f"，还差 {r.iv_days_needed} 个交易日"
                   if r.iv_reason == "insufficient_history" else "") + "）")
        + "）" for r in kept[:5]) or "无"
    # ⚠️ 「因为算不出而被排除」必须和「不满足条件」分开说，
    #    否则 AI 会把"本机历史不足"读成"全市场只有这么几只符合"。
    exc = ""
    def _why(d: dict) -> str:
        return "、".join(
            f"{scanner_parse.REASON_LABEL.get(k, k)} {v} 只" for k, v in d.items())
    if excluded["excluded_no_iv_rank"]:
        exc += (f"⚠️ 另有 {excluded['excluded_no_iv_rank']} 只因 **IV Rank 算不出**"
                f"而被该条件滤掉（{_why(excluded['iv_reasons'])}）—— "
                f"它们是**算不出**，不是不满足条件。")
    if excluded["excluded_no_volume_x"]:
        exc += (f"⚠️ 另有 {excluded['excluded_no_volume_x']} 只因**量比算不出**"
                f"而被该条件滤掉（{_why(excluded['volume_reasons'])}）。")
    return {
        "rows": [scanner_parse.to_dict(r) for r in kept],
        # ⚠️ `count` 与 REST 同口径 = **截断前**的符合总数。
        #    返回截断后的条数会让同一份数据在两个视图里报出不同的总量。
        "count": total, "returned": len(kept),
        "session": sess, "excluded": excluded, "stats": st,
        "summary": (
            f"交易时段 {sess}：本地共扫到 {len(rows):,} 只，筛出 {total} 只"
            f"（本次返回前 {len(kept)} 只）。"
            f"前几名：{hot}。{exc}"
            f"本地已攒 {st['sessions']} 个交易时段、"
            f"其中 {st['iv_ready_symbols']:,} 只标的攒够了 IV Rank 所需历史"
            f"（需 {scanner_parse.IV_MIN_SAMPLE} 个交易日；这份历史**补不回来**，"
            f"只能逐日攒）。以上为公开延时数据的统计，不构成投资建议。"),
    }


def _tool_get_darkpool(ticker: str, week: str | None = None) -> dict:
    tk = (ticker or "").strip().upper()
    try:
        raw = darkpool_src.weekly(tk)
    except darkpool_src.FinraDisabled as e:
        # ⚠️ 这是**配置状态**，不是「这只票没有场外成交」
        return {"enabled": False, "error": str(e),
                "summary": (f"暗池/场外数据源**当前关闭**，取不到 {tk} 的数据。"
                            f"这是**配置状态**，不是「这只票没有场外成交」。"
                            f"设置 VF_ENABLE_FINRA=1 才启用；关闭是刻意的，"
                            f"因为 FINRA 条款限非商业用途且禁止用其数据建库。")}
    parsed = darkpool_parse.parse(raw)
    weeks = darkpool_parse.weeks_of(parsed)
    if not weeks:
        # ⚠️ 与 REST 同语义：认得的类型一行没有、却有未知类型 = **解析不兼容**，
        #    不能说成"这只票没有场外成交"。
        if parsed["unknown_types"]:
            return {"error": "解析不兼容", "unknown_types": parsed["unknown_types"],
                    "summary": (f"FINRA 返回了本程序不认识的记录类型 "
                                f"{parsed['unknown_types']} —— 解析规则可能已过时。"
                                f"这是**解析不兼容**，"
                                f"**不是**「{tk} 没有场外成交」。")}
        return {"rows": [], "summary": f"{tk} 无场外成交记录。"}
    wk = week or weeks[-1]
    if wk not in weeks:
        return {"weeks": weeks[-8:],
                "summary": f"没有 {wk} 这一周。可选：{'、'.join(weeks[-8:])}。"}
    # ⚠️ **与 REST 走同一个函数**（含本地分母）—— 上一版 MCP 不读分母，
    #    同一只票同一周网页端能给占比、工具端永远 null，两个视图口径不一致。
    from app import _consolidated
    out = _consolidated(tk, wk, parsed)
    a, o = out["ats"], out["otc"]
    # ⚠️ `shares` 可能为空（上游字段缺失）—— 无条件 `:,.0f` 会抛 TypeError，
    #    整个工具结果丢失，而 REST/UI 那边能正常显示"—"。
    def _v(x: dict) -> str:
        sh = "—" if x["shares"] is None else f"{x['shares']:,.0f} 股"
        avg = ("" if x["avg_trade_size"] is None
               else f"（均 {x['avg_trade_size']:,.0f} 股/笔）")
        return f"{x['mpid'] or '（不披露）'} {(x['name'] or '')[:24]} {sh}{avg}"
    top = "；".join(_v(v) for v in out["venues"]["ats"][:3]) or "无"
    return {
        **out, "ticker": tk, "weeks": weeks[-12:],
        "summary": (
            f"{tk} {wk} 起那周："
            f"**ATS（真暗池）{a['shares']:,.0f} 股**（{a['firms']} 家、"
            f"{a['trades']:,.0f} 笔）；"
            f"**非 ATS 场外（批发商内部化）{o['shares']:,.0f} 股**"
            f"（{o['records']} 条记录，其中能点名 {o['firms']} 家）。"
            + (f"⚠️ 有 {a['null_share_records'] + o['null_share_records']} 条记录"
               f"成交量为空、已排除（未当成 0），合计因此偏小。"
               if (a["null_share_records"] + o["null_share_records"]) else "")
            + "⛔ **这两个数不能相加叫「暗池成交量」** —— 内部化不是暗池，"
            + f"相加会把数字虚高一倍以上。ATS 前几家：{top}。"
            + f"⚠️ 数据**滞后约四周**（最新一周 {weeks[-1]}），是事后统计不是实时监控。"
            + (f"场外占比：ATS {out['share']['ats_pct']:.2f}%、"
               f"非 ATS {out['share']['otc_pct']:.2f}%"
               f"（分母为本地沉淀的该周日成交量之和）。"
               if out.get("share") else
               f"⚠️ **场外占比算不出**：{out['share_note']}")
            + (f"⚠️ 本次取满了 {darkpool_src.MAX_LIMIT} 行上限、周序列可能不全，"
               f"且该接口不支持排序，截掉了哪几周无从得知。"
               if parsed.get("truncated") else "")
            + "以上为公开数据的呈现，不构成投资建议。"),
    }


def _tool_get_stock(ticker: str) -> dict:
    from app import get_stock as _get
    out = _get(ticker)
    ok = [l for l in out["lanes"] if l["ok"]]
    miss = [l for l in out["lanes"] if not l["ok"]]
    have = "；".join(
        f"{l['title']}（{l['as_of'] or '时点未知'}"
        + (f"，{l['lag_days']} 天前）" if l["lag_days"] is not None else "）")
        for l in ok) or "无"
    # ⚠️ 缺的那些必须**逐条给出自己的原因**，而且**不能笼统说成"没有"** ——
    #    `disabled` / `fetch_failed` / `no_mapping` 说的是"我们拿不到"，
    #    只有 `no_data` 才是"这只票确实没有那类记录"。上一版把两者混在一句
    #    "X 条没有 …… 这些都不等于没有活动"里，自相矛盾且两头都不对。
    # ⚠️ 用 `stock_parse.MEANS_ABSENT` 而不是硬写 "no_data" ——
    #    以后再加一个"确实没有"的原因码，这里会自动跟上。
    truly_none = [l for l in miss if l["reason"] in stock_parse.MEANS_ABSENT]
    cant_get = [l for l in miss if l["reason"] not in stock_parse.MEANS_ABSENT]
    gone = ""
    if truly_none:
        gone += ("**确实没有记录**的：" + "、".join(l["title"] for l in truly_none)
                 + "（这几条是真的没有那类活动）。")
    if cant_get:
        gone += ("**我们拿不到**的：" + "；".join(
            f"{l['title']}（{l['reason_label'] or l['reason']}）" for l in cant_get)
            + " —— ⛔ 这几条**不等于**「这只票没有那类活动」。")
    spread = out.get("lag_spread_days")
    spread_txt = (
        f"⚠️ 这些数据**横跨 {spread['oldest'] - spread['newest']} 天**"
        f"（最新 {spread['newest']} 天前、最旧 {spread['oldest']} 天前）——"
        f"**它们不是同一时刻的事**，串成一个叙事之前先看清各自时点。"
        if spread else "")
    return {
        **out,
        "summary": (
            # ⚠️ 首句也不能说"X 条没有" —— 那里头大多是"我们拿不到"。
            f"{out['ticker']}：{out['available']} 条线有数据、"
            f"{out['unavailable']} 条空着（原因见下，多数不是「没有」）。{spread_txt}"
            f"有数据的：{have}。"
            f"{gone}"
            f"⛔ 本工具**不做跨源综合评分**：把不同时点、不同口径的数据"
            f"加权成一个「多空分数」，等于把三个月前的持仓和昨天的期权成交"
            f"当成同一件事。以上为公开数据的呈现，不构成投资建议。"),
    }


def _n(v: float | None, spec: str = ",.0f") -> str:
    """空值安全的数字格式化。

    ⚠️ 解析层把这些字段声明成 `Optional[float]`，API 与前端都按 "—" 呈现空值，
    只有工具层直接 `f"{v:,.0f}"` —— 同一份数据在 REST 能看、在 MCP 崩掉，
    正是本项目反复踩的「同数据两视图口径不一致」。
    （实测抽样 1000 期 TFF 未见空字段，属**尚未触发**的不一致，不是必现崩溃。）
    """
    return "—" if v is None else format(v, spec)


def _tool_get_yield_curve(years: int = 3) -> dict:
    from datetime import date as _d
    y = _d.today().year
    wanted = list(range(y - max(1, min(years, 15)) + 1, y + 1))
    status = market_store.year_status(wanted)
    failed: dict[int, str] = {}
    missing: list[int] = []
    for yy in wanted:
        if not status[yy]["stale"]:
            continue
        try:
            if not market_store.save_year(yy, macro_src.yield_curve(yy)):
                missing.append(yy)
        except macro_src.DataNotAvailable:
            # ⚠️ 「那一年确实没有」要**带出去**：REST 视图有 missing_years，
            #    工具视图吞掉的话，AI 看到的窗口就悄悄变短了。
            missing.append(yy)
        except RuntimeError as e:
            failed[yy] = str(e)
    pts = [p for p in (market_parse.parse_curve(r)
                       for r in market_store.load_years(wanted)) if p]
    if not pts:
        return {"error": "取不到收益率数据" + (f"：{failed}" if failed else ""),
                "note": "这是**取数失败**，不是「没有收益率」。"}
    out = market_parse.curve_series(pts)
    L = out["latest"]
    # ⚠️ 三态，不是两态：倒挂 / 未倒挂 / **算不出来**（某个期限当天缺值）。
    #    压成两态就会在缺值时输出"三条口径均为正"——那是把「不知道」说成了事实。
    inv = [k for k, v in L["inverted"].items() if v is True]
    pos = [k for k, v in L["inverted"].items() if v is False]
    unk = [k for k, v in L["inverted"].items() if v is None]
    parts = []
    if inv:
        parts.append("、".join(f"{k}={_n(L['spreads'][k], '+.2f')}%" for k in inv) + " 为负")
    if pos and not inv:
        parts.append(f"{len(pos)} 条口径为正")
    elif pos:
        parts.append(f"其余 {len(pos)} 条为正")
    if unk:
        parts.append(f"{'、'.join(unk)} **当日缺期限数据、算不出**")
    inv_txt = "；".join(parts) if parts else "无可用口径"
    return {
        **out, "failed": failed, "missing_years": missing,
        "summary": (
            f"截至 {L['date']}：10Y {L['yields'].get('10Y')}%、"
            f"2Y {L['yields'].get('2Y')}%、3M {L['yields'].get('3M')}%。"
            f"利差 10Y-2Y {_n(L['spreads']['10Y-2Y'], '+.2f')}%、"
            f"10Y-3M {_n(L['spreads']['10Y-3M'], '+.2f')}%（{inv_txt}）。"
            + (f"⚠️ Treasury 没有 {'、'.join(map(str, missing))} 年的数据"
               f"（与取数失败不同）。" if missing else "")
            + (f"⚠️ {'、'.join(map(str, failed))} 年**取数失败**，"
               f"下面用的是本地已有数据、可能不是最新：{failed}。" if failed else "")
            + "⚠️ 「倒挂」的两条口径时点可差数月，说结论必须讲清用的哪条。"
              "以上为美国财政部公开数据的呈现，不构成投资建议。"),
    }


def _tool_get_cot(market: str | None = None, periods: int = 12) -> dict:
    """CFTC 持仓。

    ⚠️ **先把合约定死，再取时间序列。** 直接拿关键词去 like 查有两个坑：
    ① 一个关键词能命中多个合约（"E-MINI S&P 500" 同时命中 E-MINI 与 MICRO E-MINI），
       而按日期倒序取 N 行，同一天里哪个合约排前面是**任意的** ——
       问 E-MINI 却答 MICRO 的数字，比报错还糟。
    ② limit 是**总行数**，多合约命中时 N 行会散在几个合约上，
       根本凑不出一条时间序列。
    """
    periods = max(1, min(periods, 200))
    ms = macro_src.cot_markets()
    latest = max((m["last_date"] or "" for m in ms), default="")
    live = [m for m in ms if m["last_date"] == latest]

    if not market:
        return {"markets": [m["market"] for m in live], "count": len(live),
                "latest_report": latest, "notes": market_parse.NOTES,
                "summary": (f"TFF 当前在报 {len(live)} 个合约（共收录 {len(ms)} 个，"
                            f"其余已停更）。最新一期 {latest}。"
                            f"传 market 参数取具体合约的持仓。")}

    key = market.strip().upper()
    matches = [m for m in ms if key in m["market"].upper()]
    exact = [m for m in matches if m["market"].upper() == key]
    if exact:
        chosen = exact[0]
    elif len(matches) == 1:
        chosen = matches[0]
    elif matches:
        # 命中多个 → **不替调用方挑**，把候选连同各自最后一期给回去
        return {"candidates": [{"market": m["market"], "last_date": m["last_date"],
                                "reports": m["reports"]} for m in matches[:25]],
                "count": 0, "rows": [], "notes": market_parse.NOTES,
                "summary": (
                    f"「{market}」命中 {len(matches)} 个合约，"
                    f"**没有替你挑**（它们是不同合约，数字不能混着看）："
                    + "、".join(m["market"] for m in matches[:6])
                    + ("…" if len(matches) > 6 else "")
                    + "。用完整合约名再调一次。")}
    else:
        return {"rows": [], "count": 0, "candidates": [],
                "summary": f"没有匹配「{market}」的合约。不传 market 可取全部清单。"}

    name = chosen["market"]
    stale = chosen["last_date"] != latest
    raw = macro_src.cot_rows(limit=periods, market_contains=name, exact=True)
    rows = [market_parse.cot_to_dict(c)
            for c in (market_parse.parse_cot(r) for r in raw) if c]
    if not rows:
        return {"rows": [], "count": 0, "market": name,
                "summary": f"{name} 没有记录。"}
    r0 = rows[0]
    # ⚠️ 停更合约要说清 —— 否则用户会把 2022 年的数字当成当下的持仓
    stale_txt = (f"⚠️ 该合约**已停更**，最后一期是 {chosen['last_date']}"
                 f"（全库最新一期为 {latest}）。" if stale else "")
    return {
        "rows": rows, "count": len(rows), "market": name,
        "last_date": chosen["last_date"], "stale": stale,
        "notes": market_parse.NOTES,
        "summary": (
            f"{stale_txt}{name} 于 {r0['report_date']}（周二收盘）："
            f"杠杆基金净 {_n(r0['lev_net'])} 手"
            f"（多 {_n(r0['lev_long'])} / 空 {_n(r0['lev_short'])}）、"
            f"资产管理净 {_n(r0['asset_net'])} 手、"
            f"总持仓 {_n(r0['open_interest'])} 手。"
            f"⚠️ CFTC 有**三天时滞**：这是周二的状态、周五才发布，不是当下。"
            f"以上为 CFTC 公开数据的呈现，不构成投资建议。"),
    }


_IMPL: dict[str, Callable[..., dict]] = {
    "get_short_fails": _tool_get_short_fails,
    "get_institution_holdings": _tool_get_institution_holdings,
    "get_institution_changes": _tool_get_institution_changes,
    "get_insider_trades": _tool_get_insider_trades,
    "get_insider_summary": _tool_get_insider_summary,
    "get_congress_trades": _tool_get_congress_trades,
    "get_congress_summary": _tool_get_congress_summary,
    "get_gex": _tool_get_gex,
    "get_gex_curve": _tool_get_gex_curve,
    "get_option_chain_summary": _tool_get_option_chain_summary,
    "get_option_flow": _tool_get_option_flow,
    "get_oi_change": _tool_get_oi_change,
    "scan_market": _tool_scan_market,
    "get_darkpool": _tool_get_darkpool,
    "get_stock": _tool_get_stock,
    "get_yield_curve": _tool_get_yield_curve,
    "get_cot": _tool_get_cot,
}


def exec_tool(name: str, args: dict[str, Any]) -> dict:
    """统一执行入口。异常转成 {"error": ...} 而不是抛出 ——
    MCP/function-calling 的调用方需要拿到结构化错误，而不是断连。"""
    fn = _IMPL.get(name)
    if fn is None:
        return {"error": f"未知工具：{name}"}
    try:
        return fn(**args)
    except cboe.DataNotAvailable as e:
        return {"error": f"无数据：{e}"}
    except (ValueError, TypeError) as e:
        return {"error": f"参数错误：{e}"}
    except RuntimeError as e:
        return {"error": f"取数失败：{e}"}
