"""Vibe-Flow 后端 API。

⚠️ 合规：本服务**只应跑在用户自己的机器上**（localhost）。
Vibe-Flow 分发的是代码，不是数据 —— 用户自部署运行 = personal use。
⛔ 绝不可把本服务部署成对公网提供期权数据的站点（= OPRA redistributor，$1,500/月）。
默认只监听 127.0.0.1，就是这个原因。

启动：
    cd backend && python -m uvicorn app:app --host 127.0.0.1 --port 8920
"""
from __future__ import annotations

# ⚠️ 紧跟在 `__future__` 之后 —— 它必须是文件里的第一条语句，
#    而版本闸要在其余 import 之前跑，好在版本不够时给一句人话，
#    而不是让用户撞进某个模块深处的 SyntaxError 去猜哪里不对。
import pyversion  # noqa: F401

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from sources import cboe
from modules import greeks, history
from modules import congress_store, congress_sync
from modules import congress as congress_parse
from modules import insider_store, insider_sync
from modules import insider as insider_parse
from modules import institution_store, institution_sync
from modules import institution as institution_parse
from modules import shorts_store
from modules import shorts as shorts_parse
from sources import shorts as shorts_src
from sources import macro as macro_src
from modules import market as market_parse
from modules import market_store
from modules import flow as flow_parse
from modules import flow_store

app = FastAPI(
    title="Vibe-Flow API",
    description="开源版 Unusual Whales · 自部署 · 数据留在你自己机器上",
    version="0.1.0",
)

# 前端 dev server；生产是同源，不需要放开
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5895", "http://127.0.0.1:5895"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "vibe-flow", "version": app.version}


@app.get("/api/gex/{ticker}")
def get_gex(
    ticker: str,
    expiry: Optional[str] = Query(None, description="指定到期日 YYYY-MM-DD，或 '0DTE'"),
    dte_max: Optional[int] = Query(None, ge=0, le=365, description="只看 N 天内到期"),
    strike_pct: float = Query(0.05, gt=0, le=0.5, description="行权价范围 ±比例"),
) -> dict:
    """单只标的的 GEX 画像。

    不传 expiry / dte_max = 全链（远期合约会稀释信号，通常传 dte_max 更有意义）。
    """
    try:
        chain = cboe.cached_option_chain(ticker)
        profile = greeks.compute(chain, expiry=expiry, dte_max=dte_max,
                                 strike_pct=strike_pct)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:                              # 参数问题（含跨市场代码）
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:                            # 网络/限流 —— 必须冒泡，不吞
        raise HTTPException(status_code=502, detail=str(e)) from e

    out = greeks.to_dict(profile)
    out["timestamp"] = chain.timestamp
    out["expiries"] = chain.expiries()[:20]
    return out


@app.get("/api/gex/{ticker}/curve")
def get_gex_curve(
    ticker: str,
    expiry: Optional[str] = Query(None,
                                  description="指定到期日 YYYY-MM-DD 或 '0DTE' —— "
                                              "须与 /api/gex 主端点一致"),
    dte_max: Optional[int] = Query(None, ge=0, le=365,
                                   description="不传=全链，与 /api/gex 主端点保持一致"),
    span_pct: float = Query(0.06, gt=0, le=0.3, description="扫描股价范围 ±比例"),
    strike_pct: float = Query(0.05, gt=0, le=0.5,
                              description="行权价范围 ±比例 —— 必须与 /api/gex 主端点一致，"
                                          "否则曲线零点与返回的 gamma_flip 来自不同合约集"),
    points: int = Query(40, ge=10, le=200),
) -> dict:
    """GEX 随股价变化的曲线 —— 用来直观看到 gamma flip 在哪。

    每个点都用 Black-Scholes 重算 gamma（不能复用当前 spot 下的 gamma）。
    """
    try:
        chain = cboe.cached_option_chain(ticker)
        cs = chain.filter(expiry=expiry, dte_max=dte_max)
        # ⚠️ 用调用方给的 strike_pct，不能写死 ±15%：
        # 主端点按 strike_pct 算 flip，这里若用不同范围，曲线会在别处穿零。
        lo_k, hi_k = chain.spot * (1 - strike_pct), chain.spot * (1 + strike_pct)
        cs = [c for c in cs if lo_k <= c.strike <= hi_k]
        if not cs:
            raise ValueError(f"{ticker} 无符合条件的合约")
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    lo, hi = chain.spot * (1 - span_pct), chain.spot * (1 + span_pct)
    curve = []
    for i in range(points + 1):
        px = lo + (hi - lo) * i / points
        curve.append({"price": round(px, 2),
                      "gex_bn": round(greeks.total_gex_at(cs, px) / 1e9, 4)})
    return {
        "ticker": chain.ticker,
        "spot": round(chain.spot, 2),
        "curve": curve,
        "note": "每个点均用 Black-Scholes 重算 gamma；曲线穿越 0 处即 gamma flip",
    }

@app.get("/api/exposures/{ticker}")
def get_exposures(
    ticker: str,
    expiry: Optional[str] = Query(None),
    dte_max: Optional[int] = Query(None, ge=0, le=365),
    strike_pct: float = Query(0.05, gt=0, le=0.5),
) -> dict:
    """Vanna / Charm 曝险（CBOE 不给二阶希腊字母，由 Black-Scholes 自算）。"""
    try:
        chain = cboe.cached_option_chain(ticker)
        prof = greeks.compute_exposures(chain, expiry=expiry, dte_max=dte_max,
                                        strike_pct=strike_pct)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return greeks.exposures_to_dict(prof)


@app.get("/api/gex/{ticker}/surface")
def get_gex_surface(
    ticker: str,
    dte_max: Optional[int] = Query(45, ge=0, le=365),
    strike_pct: float = Query(0.06, gt=0, le=0.5),
) -> dict:
    """expiry × strike 二维 GEX 曲面（ECharts heatmap 直接可用）。"""
    try:
        chain = cboe.cached_option_chain(ticker)
        return greeks.gex_surface(chain, dte_max=dte_max, strike_pct=strike_pct)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/history/snapshot/{ticker}")
def take_snapshot(
    ticker: str,
    expiry: Optional[str] = Query(None,
                                  description="指定到期日 YYYY-MM-DD 或 '0DTE' —— "
                                              "须与 /api/gex 主端点一致"),
    dte_max: Optional[int] = Query(None, ge=0, le=365),
    strike_pct: float = Query(0.05, gt=0, le=0.5),
) -> dict:
    """采集一次快照存进本地历史库（装上即开始积累）。

    ⚠️ 过滤参数必须与 `/api/gex` 主端点**完全一致**：快照的 scope 就是入库主键
    的一部分，参数对不上会把「页面上看到的口径」与「历史库里存的口径」写岔，
    日后画出来的时间序列跟当时看的根本不是一回事。
    """
    try:
        chain = cboe.cached_option_chain(ticker)
        prof = greeks.to_dict(greeks.compute(
            chain, expiry=expiry, dte_max=dte_max, strike_pct=strike_pct))
        exp = greeks.exposures_to_dict(greeks.compute_exposures(
            chain, expiry=expiry, dte_max=dte_max, strike_pct=strike_pct))
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    # 用**数据自身的时间**当时点，不用墙上时钟：CBOE 延时行情隔一阵才更新一次，
    # 按 now() 入库会把「同一份数据」记成多个观测点（详见 history.record_gex 文档）。
    created = history.record_gex(prof, exp, captured_at=chain.timestamp)
    return {"ok": True, "created": created, "ticker": prof["ticker"],
            "scope": prof["meta"]["scope"],
            "scope_key": prof["meta"]["scope_key"], "captured_at": chain.timestamp,
            "data_stale": chain.timestamp is None}


@app.get("/api/history/{ticker}")
def get_history(ticker: str,
                scope: Optional[str] = Query(
                    None, description="归档键（= /api/gex 返回的 meta.scope_key）。"
                                      "该标的存在多个口径时必传"),
                limit: int = Query(200, ge=1, le=2000)) -> dict:
    """本地历史序列。

    ⚠️ **不同口径的 GEX 不是一个量级**（≤7DTE vs 全链差一个数量级），
    拌进同一个数组就是一条毫无意义的锯齿。所以这里不接受"省略 scope 就全给"：
    - 库里只有一个口径 → 无歧义，直接给；
    - 有多个口径却没指定 → **400 并列出可选值**，而不是返回一个会被误读的混合序列。
    """
    scopes = history.scopes_for(ticker)
    if scope is None and len(scopes) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"{ticker.upper()} 有多个口径的历史，必须指定 scope（不同口径不可比）："
                   + " | ".join(scopes))
    if scope is None and len(scopes) == 1:
        scope = scopes[0]
    return {"ticker": ticker.upper(),
            "scope": scope,
            "scopes": scopes,
            "series": history.gex_series(ticker, scope, limit)}


@app.get("/api/history")
def history_stats() -> dict:
    """库存概览：让用户看到自己攒了多少。"""
    return history.stats()


# ═══════════════════════ 国会议员交易（S 级数据源）═══════════════════════
# ⚠️ 合规：两院披露是美国政府公开记录（公众可自由获取），
#    但 **5 U.S.C. §13107(c)(1)(B) 明文禁止任何商业用途**
#    （新闻媒体面向公众传播除外），罚款上限 $10,000，两院均适用。
#    → 免费开源 + 用户自部署做个人研究 ✅；收费产品不得包含这条线 ❌。
#    与 SEC EDGAR **不同级别**（后者不限商用），详见 sources/congress.py。
#    不像 CBOE 期权数据受 OPRA 约束 —— 对外演示优先用它。

@app.get("/api/congress/trades")
def congress_trades(
    chamber: Optional[str] = Query(None, pattern="^(house|senate)$"),
    ticker: Optional[str] = Query(None),
    member: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="交易日下限 YYYY-MM-DD"),
    tx_type: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    limit: int = Query(300, ge=1, le=2000),
) -> dict:
    """已缓存的议员交易明细（按交易日倒序）。

    ⚠️ 读的是**本地缓存**，不是实时抓 —— 众议院单年 313 份 PDF，
    现抓要 100+ 秒。先调 `/api/congress/sync` 灌数据。
    """
    rows = congress_store.query_trades(chamber=chamber, ticker=ticker, member=member,
                                       since=since, tx_type=tx_type, limit=limit)
    # ⚠️ 必须与 /summary 走**同一条派生路径**（_row_to_trade → to_dict）。
    # 直接返回 DB 原始行会缺 date_anomaly 等派生字段 ——
    # 结果就是汇总卡说"2 笔日期存疑"、明细表却照常显示 -320 天。
    # 「同一份数据的两个视图必须同一套加工」，这条在本项目已经踩第三次了。
    out = [congress_parse.to_dict(_row_to_trade(r)) for r in rows]
    st = congress_store.stats()
    return {
        "trades": out, "count": len(out), "stats": st,
        "disclaimer": {
            "amount": "金额是**区间**不是精确值 —— STOCK Act 只要求按档披露"
                      "（如 $1,001-$15,000）。任何金额汇总都是区间中值的估算。",
            "delay": "「延迟天数」= 归档日 − 交易日，是事实统计，**不是违规认定**："
                     "法定期限为「知悉后 30 天内、且不晚于交易后 45 天」，周末/假日顺延。",
            "coverage": st["note"],
        },
    }


@app.get("/api/congress/summary")
def congress_summary(
    since: Optional[str] = Query(None, description="交易日下限 YYYY-MM-DD"),
    chamber: Optional[str] = Query(None, pattern="^(house|senate)$"),
    ticker: Optional[str] = Query(None),
    member: Optional[str] = Query(None),
    tx_type: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    limit: int = Query(2000, ge=1, le=20000),
) -> dict:
    """按标的 / 议员聚合。

    ⚠️ 筛选参数必须与 `/api/congress/trades` **完全一致**：
    少接一个 ticker/tx_type，就会出现"明细表只剩 NVDA、上方统计卡和图表却还是全市场"——
    同一屏里两个视图互相打架。这类「同数据两视图口径不一」在本项目已犯四次。
    """
    rows = congress_store.query_trades(chamber=chamber, since=since, ticker=ticker,
                                       member=member, tx_type=tx_type, limit=limit)
    trades = [_row_to_trade(r) for r in rows]
    out = congress_parse.summarize(trades)
    out["stats"] = congress_store.stats()
    out["scope"] = {"chamber": chamber or "两院", "since": since, "ticker": ticker,
                    "member": member, "tx_type": tx_type, "sampled": len(rows),
                    "limit": limit,
                    # 命中上限时要说出来，不能让用户以为看的是全量
                    "truncated": len(rows) >= limit}
    return out


def _row_to_trade(r: dict) -> congress_parse.Trade:
    """DB 行 → Trade（聚合函数复用同一套逻辑，避免两处口径漂移）。"""
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


@app.get("/api/congress/unparsed")
def congress_unparsed(limit: int = Query(100, ge=1, le=500)) -> dict:
    """读不了的申报清单（大多是纸质扫描件）。

    ⭐ 单独开一个端点，是为了**让"读不了"这件事可见**：
    10% 的众议院 PTR 是整份扫描图片，藏起来会让用户以为看到的就是全部。
    """
    rows = congress_store.unparsed_filings(limit)
    return {"filings": rows, "count": len(rows),
            "note": "这些申报确实存在但明细无法自动解析，点 source_url 可看原件。"}


@app.post("/api/congress/sync")
def congress_sync_start(
    year: Optional[int] = Query(None, ge=2012, le=2100, description="起始年份，默认当年"),
    years_back: int = Query(0, ge=0, le=10,
                            description="往前多回补几年（0=只当年）。众议院归档按年分卷，"
                                        "每多一年多几百份 PDF"),
    limit: Optional[int] = Query(None, ge=1, le=5000,
                                 description="本次最多同步几份（两院共享此额度）"),
    chamber: Optional[str] = Query(None, pattern="^(house|senate)$"),
) -> dict:
    """启动增量同步（后台线程，进度查 GET /api/congress/sync）。"""
    ch = (chamber,) if chamber else ("house", "senate")
    return congress_sync.start(year=year, years_back=years_back, limit=limit, chambers=ch)


@app.get("/api/congress/sync")
def congress_sync_status() -> dict:
    """同步进度。"""
    return {**congress_sync.STATE.snapshot(), "stats": congress_store.stats()}


# ═══════════════════════ 内部人交易 Form 4（S 级数据源）═══════════════════════
# ⭐ SEC EDGAR：官方条款只限速率（10 请求/秒）+ 要求声明 UA，
#    明写 "Anyone can access and download this information for free" ——
#    **不限商用**。这与国会披露（禁商用）不是一个级别，别混用。
# ⚠️ 本分栏的核心是**分类**：Form 4 里公开市场主动买卖只占约 1/4，
#    其余是授予/行权/代扣税等薪酬类。默认只看公开市场，否则信号会被淹没。

def _ins_row_to_trade(r: dict) -> insider_parse.InsiderTrade:
    """DB 行 → InsiderTrade。**明细与汇总共用这一条派生路径**，
    避免两个视图字段/口径不一致（本项目已在这上面栽过五次）。"""
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


@app.get("/api/insider/trades")
def insider_trades(
    ticker: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    group: Optional[str] = Query("open_market",
                                 pattern="^(open_market|compensation|other|all)$",
                                 description="默认只看公开市场买卖（P/S）；"
                                             "all=不过滤（薪酬类占七成，会淹没信号）"),
    direction: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    since: Optional[str] = Query(None, description="交易日下限 YYYY-MM-DD"),
    min_value: Optional[float] = Query(None, ge=0, description="成交金额下限（美元）"),
    role: Optional[str] = Query(None, pattern="^(officer|director|ten_pct)$"),
    plan: Optional[str] = Query(None, pattern="^(yes|no|unknown)$",
                                description="10b5-1 预设计划：yes=是 / no=明确否 / "
                                            "unknown=申报未标注（2023 年前无此字段）"),
    include_amendments: bool = Query(False,
                                     description="是否包含修订件 4/A。默认否 —— "
                                                 "修订通常重述原申报的交易，"
                                                 "与原件同时统计会重复计数"),
    limit: int = Query(300, ge=1, le=2000),
) -> dict:
    """内部人交易明细（本地缓存，按交易日倒序）。"""
    rows = insider_store.query(
        ticker=ticker, owner=owner, group=None if group == "all" else group,
        direction=direction, since=since, min_value=min_value, role=role,
        plan=plan, include_amendments=include_amendments, limit=limit)
    out = [insider_parse.to_dict(_ins_row_to_trade(r)) for r in rows]
    return {
        "trades": out, "count": len(out), "stats": insider_store.stats(),
        "disclaimer": {
            "forms": "本页只含 Form 4（及可选的 4/A 修订件）。同一个 SEC 数据集里还有 "
                     "Form 3（初始持股声明，不是交易）与 Form 5（年度补报，"
                     "延迟中位 274 天、三成超一年）—— 都已排除，"
                     "否则会把「内部人申报延迟」整体拉高。",
            "classification": "Form 4 里**公开市场主动买卖只占约 1/4**，"
                              "其余是授予/行权/代扣税等薪酬类。"
                              "按 SEC 的「取得/处置」标志笼统统计，会把内部人买入夸大数倍。",
            "plan": "10b5-1 为预先制定的交易计划，卖出往往是几个月前排好的。",
            "disclaimer": "只呈现已公开申报的事实，不构成任何投资建议。",
        },
    }


@app.get("/api/insider/summary")
def insider_summary(
    ticker: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    group: Optional[str] = Query("open_market",
                                 pattern="^(open_market|compensation|other|all)$"),
    direction: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    since: Optional[str] = Query(None),
    min_value: Optional[float] = Query(None, ge=0),
    role: Optional[str] = Query(None, pattern="^(officer|director|ten_pct)$"),
    plan: Optional[str] = Query(None, pattern="^(yes|no|unknown)$"),
    include_amendments: bool = Query(False),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """按标的 / 内部人聚合，含集群买入榜。

    ⚠️ 聚合在 **SQL 里对全部命中行**做，不是"取最新 N 行再算" ——
    后者在命中超过 N 行时会把「最新 N 行的统计」标成「整个时间段的统计」，
    加个"已截断"提示也救不了数字本身。

    ⚠️ 筛选参数与 `/api/insider/trades` **必须完全一致**（共用 store._where）。
    """
    import statistics as _st

    filters = dict(ticker=ticker, owner=owner,
                   group=None if group == "all" else group,
                   direction=direction, since=since, min_value=min_value,
                   role=role, plan=plan, include_amendments=include_amendments)
    agg = insider_store.aggregate(top=top, **filters)
    lib = insider_store.stats()
    c = agg["counts"]
    delays = agg["delays"]
    n = c["n"] or 0
    om = c["om"] or 0
    return {
        "total_rows": n,
        "open_market": {
            "count": om, "buys": c["buys"] or 0, "sells": c["sells"] or 0,
            "buy_value": round(c["bv"] or 0), "sell_value": round(c["sv"] or 0),
        },
        "compensation_count": c["comp"] or 0,
        "other_count": c["other"] or 0,
        "open_market_pct": round(om / n * 100, 1) if n else 0.0,
        "by_ticker": agg["by_ticker"],
        "cluster_buys": agg["cluster_buys"],
        "by_owner": agg["by_owner"],
        "plan_sells": c["plan_sells"] or 0,
        # 剔出金额统计 ≠ 隐藏其存在：数量照报，UI 会提示"N 笔已标注为价格存疑"
        "implausible_price": agg["implausible"],
        "date_anomaly_count": agg["anomalies"],
        "delay": {
            "median_days": round(_st.median(delays), 1) if delays else None,
            "over_2d": sum(1 for d in delays if d > 2),
            "note": "Section 16(a) 要求交易后两个工作日内申报。此处按自然日计算、"
                    "未扣周末与节假日，是事实统计而非违规认定。",
        },
        "notes": insider_parse.summary_notes(lib),
        "stats": lib,
        "scope": {**filters, "group": group, "aggregated_rows": n,
                  "truncated": False},
    }


@app.post("/api/insider/sync")
def insider_sync_start(
    quarters_back: int = Query(2, ge=0, le=12,
                               description="补最近几个**已发布**季度（便宜，约 3 秒/季度、"
                                           "单季 10 万笔）"),
    days: int = Query(5, ge=0, le=120,
                      description="本次逐日补几个**尚未同步**的工作日（贵，约 90 秒/天）。"
                                  "从今天往回走、跳过已完成的，所以反复调用可逐段填满"
                                  "季度数据集与今天之间的缺口（当前约 117 天）"),
) -> dict:
    """启动同步（后台线程，进度查 GET /api/insider/sync）。"""
    return insider_sync.start(quarters_back=quarters_back, days=days)


@app.get("/api/insider/sync")
def insider_sync_status() -> dict:
    return {**insider_sync.STATE.snapshot(), "stats": insider_store.stats()}


@app.get("/api/insider/codes")
def insider_codes() -> dict:
    """SEC Form 4 交易代码表 —— 摊开给用户看，别让分类逻辑成为黑箱。"""
    return {
        "codes": [{"code": c, "label": l, "group": insider_parse.code_group(c)}
                  for c, l in insider_parse.TX_CODES.items()],
        "open_market": sorted(insider_parse.OPEN_MARKET),
        "compensation": sorted(insider_parse.COMPENSATION),
        "source": "https://www.sec.gov/files/form4.pdf §8 Transaction Codes",
    }


# ═══════════════════════ 机构持仓 13F（S 级数据源）═══════════════════════
# ⚠️ 「机构持仓」这个说法本身会误导：13F 只报**季末时点、13(f) 证券的多头持仓**，
#    不含空头（SEC 2023 年另立 Form SHO 就是因为 13F 不覆盖）、现金、债券、
#    仅境外上市股票、私募持仓。看跌期权按标的列示，必须单独归类 ——
#    混进"持仓"就是把看空算成看多。

@app.get("/api/institution/holdings")
def institution_holdings(
    period: Optional[str] = Query(None, description="报告期 YYYY-MM-DD（季末）"),
    cusip: Optional[str] = Query(None, description="按 CUSIP 筛（不是 ticker）"),
    manager: Optional[str] = Query(None, description="机构名（模糊匹配）"),
    kind: str = Query("share", pattern="^(share|call|put|all)$",
                      description="持仓类型。默认 share —— put 是看空，"
                                  "混进持仓统计会把看空算成看多"),
    min_value: Optional[float] = Query(None, ge=0),
    include_amendments: bool = Query(False,
                                     description="是否含修订件。默认否 —— "
                                                 "13F 修订要求全文重述，与原件同时"
                                                 "统计会重复计数"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """持仓明细（本地缓存，按金额倒序）。"""
    rows = institution_store.query(
        period=period, cusip=cusip, manager=manager, kind=kind,
        min_value=min_value, include_amendments=include_amendments, limit=limit)
    # ⚠️ 必须补 kind_label：UI 的「类型」列读它。缺了的话整列全空，
    # 而**区分普通持股 / 看涨 / 看跌正是本分栏存在的意义**（看跌是看空）。
    for r in rows:
        r["kind_label"] = institution_parse.POSITION_KINDS.get(r["kind"], r["kind"])
    return {
        "holdings": rows, "count": len(rows),
        "stats": institution_store.stats(),
        "disclaimer": {
            "coverage": "13F 只含**季末时点、13(f) 证券的多头持仓**。"
                        "不含空头头寸、现金、债券、大宗商品、仅境外上市的股票、"
                        "私募持仓，以及获保密豁免暂缓披露的持仓。"
                        "所以「某机构持仓 X 亿」既不是它的全部资产，也不代表净敞口。",
            "options": "看跌/看涨期权按**标的证券**列示（Form 13F 特别说明第 10 条），"
                       "本页按 share / call / put 分开统计，默认只看 share。",
            "lag": "13F 的法定申报期限是季末后 45 天，看到的是**至少一个半月前**的时点持仓，"
                   "期间机构可能已大幅调仓。",
            "cusip": "13F 只给 CUSIP 不给股票代码；SEC 不提供 CUSIP→代码映射，"
                     "所以本页以发行人名称 + CUSIP 为准。",
        },
    }


@app.get("/api/institution/summary")
def institution_summary(
    period: Optional[str] = Query(None),
    cusip: Optional[str] = Query(None),
    manager: Optional[str] = Query(None),
    kind: str = Query("share", pattern="^(share|call|put|all)$"),
    min_value: Optional[float] = Query(None, ge=0),
    include_amendments: bool = Query(False),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """按标的 / 机构聚合（SQL 全量聚合，不是取前 N 行再算）。"""
    agg = institution_store.aggregate(
        top=top, period=period, cusip=cusip, manager=manager, kind=kind,
        min_value=min_value, include_amendments=include_amendments)
    st = institution_store.stats()
    return {**agg, "stats": st,
            "scope": {"period": period, "cusip": cusip, "manager": manager,
                      "kind": kind, "min_value": min_value,
                      "include_amendments": include_amendments}}


@app.get("/api/institution/changes")
def institution_changes(
    period: str = Query(..., description="本期报告期 YYYY-MM-DD"),
    prev_period: str = Query(..., description="上期报告期 YYYY-MM-DD"),
    kind: str = Query("share", pattern="^(share|call|put)$"),
    manager: Optional[str] = Query(None),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """季度环比：新建仓 / 加仓 / 减仓 / 清仓。

    ⭐ 这才是 13F 的主要价值 —— 单季持仓是静态快照，变动才有信息量。
    ⚠️ 结果里带 `floor_note`：金额门槛会污染「新建仓/清仓」的判定，必须一起看。
    """
    have = institution_store.known_periods()
    missing = [p for p in (period, prev_period) if p not in have]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"以下报告期尚未导入：{'、'.join(missing)}。已有：{'、'.join(have) or '无'}")
    return institution_store.changes(period=period, prev_period=prev_period,
                                     kind=kind, manager=manager, top=top)


@app.post("/api/institution/sync")
def institution_sync_start(
    window: Optional[str] = Query(None,
                                  description="数据集窗口，如 01mar2026-31may2026。"
                                              "不传=最新"),
    period: Optional[str] = Query(None,
                                  description="报告期 YYYY-MM-DD。不传=该窗口内"
                                              "申报最多的那一期"),
    min_value: float = Query(institution_sync.DEFAULT_MIN_VALUE, ge=0,
                             description="金额门槛（美元）。默认 100 万 —— "
                                         "实测保留 37.5% 行数、覆盖 99.37% 金额。"
                                         "设 0 收全量（单季 332 万条、约 580MB）"),
) -> dict:
    """导入一个报告期（后台线程，进度查 GET /api/institution/sync）。"""
    return institution_sync.start(window=window, period=period, min_value=min_value)


@app.get("/api/institution/sync")
def institution_sync_status() -> dict:
    return {**institution_sync.STATE.snapshot(),
            "stats": institution_store.stats()}


# ═══════════════════════ 做空数据（S 级 FTD 主源 + B 级 FINRA 可选）═══════════════════════
# ⚠️ 这个分栏的数据是全项目最容易被读反的：
#    FTD 是**某时点的累计余额**（不是当日新增），且 SEC 明说它**不是裸卖空的证据**。
#    FINRA 场外空头成交量 ≠ 空头持仓，且只含场外那部分。
#    三条官方原文都在 shorts.OFFICIAL_NOTES，UI 必须原样显示。

@app.get("/api/shorts/ftd")
def shorts_ftd(
    symbol: Optional[str] = Query(None, description="股票代码"),
    settlement_date: Optional[str] = Query(None, description="结算日 YYYY-MM-DD"),
    since: Optional[str] = Query(None, description="结算日下限 YYYY-MM-DD"),
    min_quantity: Optional[float] = Query(None, ge=0, description="余额下限（股）"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """交割失败明细（SEC，S 级）。"""
    rows = shorts_store.query(symbol=symbol, settlement_date=settlement_date,
                              since=since, min_quantity=min_quantity, limit=limit)
    return {"fails": rows, "count": len(rows), "stats": shorts_store.stats(),
            "notes": shorts_parse.OFFICIAL_NOTES}


@app.get("/api/shorts/summary")
def shorts_summary(
    symbol: Optional[str] = Query(None),
    settlement_date: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    min_quantity: Optional[float] = Query(None, ge=0),
    top: int = Query(20, ge=1, le=100),
) -> dict:
    """按标的 / 结算日聚合（SQL 全量）。

    ⚠️ 按标的用的是**各结算日余额的均值**，不是加总 ——
    FTD 是时点余额，同一笔未交割会在连续多日重复出现，加总没有意义。
    """
    agg = shorts_store.aggregate(top=top, symbol=symbol,
                                 settlement_date=settlement_date, since=since,
                                 min_quantity=min_quantity)
    return {**agg, "stats": shorts_store.stats(),
            "notes": shorts_parse.OFFICIAL_NOTES,
            "scope": {"symbol": symbol, "settlement_date": settlement_date,
                      "since": since, "min_quantity": min_quantity,
                      "aggregation": "avg_per_settlement_date"}}


@app.post("/api/shorts/sync")
def shorts_sync_start(
    back: int = Query(2, ge=1, le=12,
                      description="导入最近几个半月档（每月两档：上半月/下半月）"),
) -> dict:
    """导入 SEC FTD（后台线程，进度查 GET /api/shorts/sync）。"""
    return shorts_store.start(back=back)


@app.get("/api/shorts/sync")
def shorts_sync_status() -> dict:
    return {**shorts_store.STATE.snapshot(), "stats": shorts_store.stats()}


@app.get("/api/shorts/finra-status")
def shorts_finra_status() -> dict:
    """FINRA 源的开启状态与**条款原文**。

    ⭐ 单独开一个端点，是因为这条线的合规判断必须由**用户自己**做：
    我们把 FINRA Terms of Use 的原文和其中的模糊之处摆出来，不替他解释。
    """
    return {
        "enabled": shorts_src.finra_enabled(),
        "env_var": "VF_ENABLE_FINRA",
        "terms": shorts_src.FINRA_TERMS,
    }


# ═══════════════════════ 宏观（S 级：Treasury + CFTC）═══════════════════════
# 全项目最干净的一条线：政府作品、不限商用、可再分发。
# ⚠️ 两个口径：倒挂要说清是 10Y-2Y 还是 10Y-3M；COT 有三天时滞。

@app.get("/api/market/curve")
def market_curve(
    year: Optional[int] = Query(None, ge=1990, le=2100, description="不传=今年"),
    years: int = Query(1, ge=1, le=15, description="往前取几年（含 year 本年）"),
    refresh: bool = Query(False, description="强制重拉（Treasury 会修订历史值）"),
) -> dict:
    """美债收益率曲线 + 两条利差的时间序列。

    ⚠️ `years` 默认 1，但**看倒挂至少要看几年** —— 一年的窗口里
    利差常常全程同号，看不出穿越零轴的那一下。

    数据落本地库：往年不再重拉（值不会变），今年按最新一天判新鲜度。
    """
    from datetime import date as _d
    y = year or _d.today().year
    wanted = list(range(y - years + 1, y + 1))

    status = market_store.year_status(wanted)
    todo = wanted if refresh else [yy for yy in wanted if status[yy]["stale"]]
    fetched, missing, failed = {}, [], {}
    for yy in todo:
        try:
            n = market_store.save_year(yy, macro_src.yield_curve(yy))
            fetched[yy] = n
            if not n:
                missing.append(yy)
        except macro_src.DataNotAvailable:
            # 某一年没有 ≠ 整个请求失败：早年缺数据是常态
            missing.append(yy)
        except RuntimeError as e:
            # ⚠️ 拉失败 ≠ 没有数据。库里若已有旧数据仍然给出去，
            #    但**必须把失败原样报出来**，不能让用户以为看到的是最新的。
            failed[yy] = str(e)

    pts = [p for p in (market_parse.parse_curve(r)
                       for r in market_store.load_years(wanted)) if p]
    if not pts:
        if failed:
            raise HTTPException(
                status_code=502,
                detail="；".join(f"{k}: {v}" for k, v in failed.items()))
        raise HTTPException(status_code=404,
                            detail=f"{wanted[0]}-{y} 无收益率数据")
    return {"year": y, "years": wanted, "missing_years": missing,
            "fetched": fetched, "failed": failed,
            "cache": market_store.stats(),
            **market_parse.curve_series(pts)}


@app.get("/api/market/cot/markets")
def market_cot_markets(
    active_only: bool = Query(True, description="只要仍在更新的合约"),
) -> dict:
    """TFF 覆盖的市场清单（带各自最后一期日期）。

    ⚠️ 默认只给**仍在更新**的：数据集里躺着大量停更多年的旧合约，
    选中它们看到的空白是"这合约不报了"，不是"取数失败"。
    """
    try:
        rows = macro_src.cot_markets()
    except macro_src.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    latest = max((r["last_date"] or "" for r in rows), default="")
    # "仍在更新" = 最后一期就是全库最新一期（同一期里各合约日期一致）
    live = [r for r in rows if r["last_date"] == latest] if latest else []
    return {"markets": live if active_only else rows,
            "total": len(rows), "active": len(live),
            "latest_report": latest or None,
            "active_only": active_only, "notes": market_parse.NOTES}


@app.get("/api/market/cot")
def market_cot(
    market: Optional[str] = Query(None, description="市场名关键词，如 S&P 500 / TREASURY"),
    exact: bool = Query(False, description="市场名完全匹配（画时间序列必须开）"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """CFTC 金融期货持仓（TFF）。

    ⚠️ 有**三天时滞**：报告周二收盘的持仓、周五才发布。
    """
    try:
        raw = macro_src.cot_rows(limit=limit, market_contains=market, exact=exact)
    except macro_src.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    rows = [market_parse.cot_to_dict(c)
            for c in (market_parse.parse_cot(r) for r in raw) if c]
    return {"rows": rows, "count": len(rows), "notes": market_parse.NOTES,
            "markets": sorted({r["market"] for r in rows}),
            "scope": {"market": market, "exact": exact, "limit": limit,
                      "truncated": len(raw) >= limit}}


# ═══════════════════════ 期权流（C 级：CBOE 延时，只在本地跑）═══════════════════════
# ⚠️ 数据是**链快照**不是逐笔成交带 —— sweep / 大单分级 / 主动买卖方向都做不了。
#    详见 modules/flow.py 的能力边界说明。本分栏不产出任何方向性标签。

def _session_of(chain) -> str:
    """归档键 = 数据自己的交易时段，**不是墙上日期**。

    ⚠️ 按墙上日期归档，周末打开两次就会把同一份周五收盘数据
    存成"两天的观测"，OI 差值全是 0 —— 看着像"持仓没变"，
    其实是根本没有新数据。CBOE 没给 session 时才回退到美东今日。
    """
    return chain.session or cboe.et_today()


@app.get("/api/flow/{ticker}")
def get_flow(
    ticker: str,
    expiry: Optional[str] = Query(None, description="指定到期日 YYYY-MM-DD 或 '0DTE'"),
    dte_max: Optional[int] = Query(None, ge=0, le=365),
    top: int = Query(40, ge=1, le=200),
    record: bool = Query(False, description="把这份快照写进本地 OI 历史"),
) -> dict:
    """单只标的的当日期权流画像（基于链快照）。"""
    try:
        chain = cboe.cached_option_chain(ticker)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    # ⚠️ 两份：展示用"有成交的"，持仓量口径用"范围内全部合约"。
    #    合成一份的话，"沉淀下来的仓位结构"实际会变成
    #    "今天碰过的那些合约的仓位"，冷门标的上能差一个数量级。
    scope_rows = flow_parse.parse(chain, dte_max=dte_max, expiry=expiry,
                                  traded_only=False)
    rows = [r for r in scope_rows if r.volume > 0]
    out = flow_parse.summarize(chain, rows, all_rows=scope_rows, top=top)
    out["expiries"] = chain.expiries()[:20]
    out["session"] = chain.session
    out["scope"] = {"expiry": expiry, "dte_max": dte_max, "top": top}
    if record:
        # ⚠️ 沉淀的是**全链且含今日无成交的合约**（`traded_only=False`）。
        #    ① 换 dte_max 会让存量对不上，差值把"筛选口径变了"记成"持仓变了"；
        #    ② 更要命的是漏掉今日无成交的合约 —— 它明天不出现在库里，
        #       差值就把它记成"净平仓全部头寸"，而它一张都没动。
        allrows = flow_parse.parse(chain, traded_only=False)
        out["recorded"] = flow_store.record(
            chain.ticker, _session_of(chain), chain.spot,
            [flow_parse.to_dict(r) for r in allrows])
    out["history"] = flow_store.dates(chain.ticker)[:30]
    return out


@app.post("/api/flow/{ticker}/record")
def record_flow(ticker: str) -> dict:
    """把当前链快照写进本地 OI 历史（这份历史补不回来，只能逐日攒）。"""
    try:
        chain = cboe.cached_option_chain(ticker)
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    rows = flow_parse.parse(chain, traded_only=False)
    sess = _session_of(chain)
    n = flow_store.record(chain.ticker, sess, chain.spot,
                          [flow_parse.to_dict(r) for r in rows])
    return {"ticker": chain.ticker, "snapshot_date": sess,
            "recorded": n, "history": flow_store.dates(chain.ticker)[:30],
            "stats": flow_store.stats()}


@app.get("/api/flow/{ticker}/oi-change")
def get_oi_change(
    ticker: str,
    date_to: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    top: int = Query(40, ge=1, le=200),
) -> dict:
    """两个快照日之间的持仓量变化。

    ⚠️ 只攒到一天时返回 `enough=false` —— 那是**还没攒够**，不是"持仓没变"。
    """
    return flow_store.oi_change(ticker.strip().upper(), date_to=date_to,
                                date_from=date_from, top=top)
