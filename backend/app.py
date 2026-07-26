"""FloorZero 后端 API。

⚠️ 合规：本服务**只应跑在用户自己的机器上**（localhost）。
FloorZero 分发的是代码，不是数据 —— 用户自部署运行 = personal use。
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
from modules import scanner as scanner_parse
from modules import scanner_store, scanner_sync
from sources import darkpool as darkpool_src
from modules import darkpool as darkpool_parse
from modules import stock as stock_parse

app = FastAPI(
    title="FloorZero API",
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
    return {"ok": True, "service": "floorzero", "version": app.version}


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
        "env_var": "FZ_ENABLE_FINRA",
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


# ═══════════════════════ 扫描器（C 级：CBOE 延时，只在本地跑）═══════════════════════
# ⚠️ 没有全市场端点，只能逐只问 → 一轮全市场约 26 分钟，天然是**后台作业**。
#    IV Rank 按定义需要历史，攒不够就返回空值，**绝不拿短样本硬算**。

@app.get("/api/scanner")
def get_scanner(
    session: Optional[str] = Query(None, description="看哪个交易时段，不传=最新"),
    sort: str = Query("iv_rank"),
    limit: int = Query(100, ge=1, le=1000),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    min_volume: Optional[float] = Query(None),
    min_iv: Optional[float] = Query(None),
    max_iv: Optional[float] = Query(None),
    min_iv_rank: Optional[float] = Query(None, ge=0, le=100),
    min_volume_x: Optional[float] = Query(None),
    security_type: Optional[str] = Query(None),
) -> dict:
    """扫描结果（读的是本地已扫过的快照，不现拉）。"""
    sess = session or scanner_store.latest_session()
    st = scanner_store.stats()
    if not sess:
        return {"rows": [], "count": 0, "session": None, "stats": st,
                "batches": scanner_store.batches(5),
                "notes": scanner_parse.NOTES,
                "note": ("本地还没有任何扫描结果 —— 这是**还没扫过**，"
                         "不是市场上没有符合条件的标的。先跑一轮扫描。")}
    quotes = scanner_store.quotes_at(sess)
    # ⚠️ `as_of=sess` 不能省 —— 看历史某天的结果时，把之后的行情算进
    #    IV Rank 样本就是**前视偏差**（用还没发生的数据判断当天 IV 高低）。
    hist = scanner_store.history([q["symbol"] for q in quotes], as_of=sess)
    rows = [scanner_parse.build_row(
                q, hist.get(q["symbol"], {}).get("iv", []),
                hist.get(q["symbol"], {}).get("volume", []))
            for q in quotes]
    kept, excluded = scanner_parse.apply_filters(
        rows, min_price=min_price, max_price=max_price, min_volume=min_volume,
        min_iv=min_iv, max_iv=max_iv, min_iv_rank=min_iv_rank,
        min_volume_x=min_volume_x, security_type=security_type)
    kept = scanner_parse.sort_rows(kept, sort)
    return {
        "rows": [scanner_parse.to_dict(r) for r in kept[:limit]],
        "count": len(kept), "scanned": len(rows), "session": sess,
        "truncated": len(kept) > limit,
        # ⚠️ 「因为算不出而被排除」必须和「不满足条件」分开报
        "excluded": excluded,
        "stats": st, "batches": scanner_store.batches(5),
        "sorts": sorted(scanner_parse.SORTS),
        "notes": scanner_parse.NOTES,
        "thresholds": {"iv_lookback": scanner_parse.IV_LOOKBACK,
                       "iv_min_sample": scanner_parse.IV_MIN_SAMPLE},
    }


@app.get("/api/scanner/scan")
def get_scan_state() -> dict:
    """当前扫描进度。"""
    return {**scanner_sync.STATE.snapshot(), "stats": scanner_store.stats()}


@app.post("/api/scanner/scan")
def start_scan(
    symbols: Optional[str] = Query(
        None, description="逗号分隔的代码；不传=全市场（约 26 分钟）"),
) -> dict:
    """启动一轮扫描。

    ⚠️ 全市场约 **26 分钟**（6,049 只 × 自律限流 4 次/秒）。
    只想看自己盯的几十只就传 `symbols`，那是几十秒的事。
    """
    syms = None
    # ⚠️ `symbols` 传了空串要当成**参数错误**，不能落到"不传=全市场"那条路 ——
    #    用户清空输入框点"扫这几只"，会意外启动 26 分钟的全市场作业。
    if symbols is not None and not symbols.strip():
        raise HTTPException(
            status_code=400,
            detail="symbols 为空。想扫全市场请**不要传**这个参数（约 26 分钟）。")
    if symbols:
        # 去重但保持顺序：重复代码只是白白多打几次上游、还把进度分母灌大
        seen: set = set()
        syms = []
        for raw in symbols.split(","):
            t = raw.strip().upper()
            if t and t not in seen:
                seen.add(t)
                syms.append(t)
        if not syms:
            raise HTTPException(status_code=400, detail="symbols 解析后为空")
    return scanner_sync.start(syms)


@app.post("/api/scanner/scan/cancel")
def cancel_scan() -> dict:
    return scanner_sync.cancel()


@app.get("/api/scanner/universe")
def get_universe() -> dict:
    """CBOE 官方的有期权标的全集。"""
    try:
        roots = cboe.option_roots()
    except cboe.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"count": len(roots), "symbols": roots[:200],
            "note": scanner_parse.NOTES["universe"]}


# ═══════════════════════ 暗池 / 场外（B 级 FINRA，**默认关闭**）═══════════════════════
# ⚠️ 铁律：任何分栏都不得把 FINRA 作为唯一数据源。
#    这里 FINRA 出**分子**（ATS / 非 ATS 场外成交量），
#    CBOE 的本地行情沉淀出**分母**（同期总成交量）—— 占比真的需要两边。
#    关掉 FINRA 时本栏只剩条款说明，这是**刻意的**，不拿别的数凑一个像的答案。

def _week_days(week: str) -> list[str]:
    """周起始日 → 该周的五个自然工作日（FINRA 的周从周一算）。"""
    from datetime import date, timedelta
    try:
        y, m, d = (int(x) for x in week.split("-"))
    except ValueError:
        return []
    start = date(y, m, d)
    return [(start + timedelta(days=i)).isoformat() for i in range(5)]


def _trading_days(days: list[str]) -> list[str]:
    """这几天里**哪些是真的开市日**。

    ⚠️ 不能拿"周一到周五"当交易日历：美股一年十来个假日，
    把休市日当成"本地缺数据"，含假日的那些周就**永远**算不出占比。
    我们没有假日表，但有个更硬的判据 —— **本地行情沉淀里有没有那一天**：
    只要**任何一只**标的在那天有快照，市场就是开着的。
    这是用数据反推日历，不需要额外依赖、也不会随年份过期。

    代价：本地那周一只票都没扫过时，检测出 0 个交易日 → 占比算不出。
    那是正确结果（确实没有分母），不是误判。
    """
    if not days:
        return []
    from modules import db
    q = ",".join("?" * len(days))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT session FROM quote_snapshot "
            f"WHERE session IN ({q})", tuple(days)).fetchall()
    have = {r["session"] for r in rows}
    return [d for d in days if d in have]


def _calendar_note(days: list[str], observed: list[str]) -> Optional[str]:
    """本地观测不到整周时的说明。

    ⚠️ **两级不确定性，别把第二级当成没有：**
    ① 本地有那天的快照 → 那天肯定开市；
    ② 本地**没有**那天的快照 → **分不清**是"那天休市"还是"我们没扫"。
    没有交易日历就没法区分，所以只要五个工作日没凑齐，就不给占比 ——
    含假日的那些周也一样不给。这是宁可不答，不答错。
    """
    if len(observed) >= len(days):
        return None
    miss = [d for d in days if d not in observed]
    return (f"本地那周只观测到 {len(observed)}/{len(days)} 个工作日"
            f"（缺 {'、'.join(miss)}）。**没有交易日历**，"
            f"分不清这几天是休市还是我们没扫过 —— 所以不给占比。"
            f"（含美股假日的那些周会一直这样，这是刻意的取舍。）")


@app.get("/api/darkpool/{ticker}")
def get_darkpool(
    ticker: str,
    week: Optional[str] = Query(None, description="周起始日 YYYY-MM-DD，不传=最新"),
) -> dict:
    """单只标的的场外成交（ATS 与非 ATS **分开**）。"""
    tk = ticker.strip().upper()
    try:
        raw = darkpool_src.weekly(tk)
    except darkpool_src.FinraDisabled as e:
        # ⚠️ 这是**配置状态**，不是「没有数据」—— 用 409 而不是 404
        raise HTTPException(status_code=409, detail=str(e)) from e
    except darkpool_src.DataNotAvailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    parsed = darkpool_parse.parse(raw)
    weeks = darkpool_parse.weeks_of(parsed)
    if not weeks:
        if parsed["unknown_types"]:
            # ⚠️ 认得的类型一行都没有、却出现了未知类型 = **解析已不兼容**，
            #    不是"这只票没有场外成交"。502 让它冒泡，别报成 404。
            raise HTTPException(
                status_code=502,
                detail=(f"FINRA 返回了本程序不认识的记录类型 "
                        f"{parsed['unknown_types']} —— 解析规则可能已过时。"
                        f"这是**解析不兼容**，不是「{tk} 没有场外成交」。"))
        raise HTTPException(status_code=404, detail=f"{tk} 无场外成交记录")
    wk = week or weeks[-1]
    if wk not in weeks:
        raise HTTPException(
            status_code=404,
            detail=f"没有 {wk} 这一周（可选：{'、'.join(weeks[-8:])}）")

    # 分母：那一周本地已沉淀的 CBOE 日成交量之和。
    # ⚠️ 逐日精确取，**不插值也不外推** —— 少哪天就少哪天，列出来给用户看。
    #    分母偏小会让占比偏高，这种偏差必须让人知道方向。
    out = _consolidated(tk, wk, parsed)
    out["ticker"] = tk
    out["weeks"] = weeks[-52:]
    out["series"] = darkpool_parse.series(parsed)[-52:]
    out["unknown_types"] = parsed["unknown_types"]
    out["null_shares"] = parsed["null_shares"]
    out["truncated"] = parsed["truncated"]
    out["finra"] = {"enabled": True, "terms": darkpool_src.FINRA_TERMS}
    return out


def _sessions_for(symbol: str, days: list[str]) -> list[tuple]:
    """本地沉淀里这几天各自的成交量（没有的就没有，**不插值不外推**）。

    表主键是 (symbol, session)，所以同一天不可能有两行、分母不会被重复累加。
    """
    if not days:
        return []
    from modules import db
    q = ",".join("?" * len(days))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT session, volume FROM quote_snapshot "
            f"WHERE symbol = ? AND session IN ({q})", (symbol, *days)).fetchall()
    return [(r["session"], r["volume"]) for r in rows]


def _consolidated(tk: str, wk: str, parsed: dict) -> dict:
    """算出该周汇总（含分母）。**REST 与 MCP 共用这一个函数**。

    ⚠️ 抽出来是因为上一版 MCP 根本没读本地分母 —— 同一只票同一周，
    网页端能给出占比而工具端永远是 null，正是"同一份数据两个视图口径不一致"。
    """
    weekdays = _week_days(wk)
    observed = _trading_days(weekdays)          # 本地确认开市的日子
    cal_note = _calendar_note(weekdays, observed)
    covered, total = [], 0.0
    for d, v in _sessions_for(tk, observed):
        if v is not None:
            covered.append(d)
            total += v
    missing = [d for d in observed if d not in covered]
    # ⚠️ 整周五天都观测到、且这只票五天都有量，才给占比。
    #    少一天就系统性偏高，而偏多少看不出来。
    full = cal_note is None and not missing
    out = darkpool_parse.week_summary(
        parsed, wk,
        consolidated_shares=total if full else None,
        covered_days=covered,
        missing_days=missing if not cal_note else weekdays)
    out["weekdays"] = weekdays
    out["locally_observed_days"] = observed
    out["calendar_note"] = cal_note
    if cal_note:
        out["share_note"] = cal_note
    return out


@app.get("/api/darkpool-status")
def darkpool_status() -> dict:
    """这一栏当前是开是关，以及为什么。"""
    return {
        "enabled": darkpool_src.finra_enabled(),
        "env_var": "FZ_ENABLE_FINRA",
        "terms": darkpool_src.FINRA_TERMS,
        "notes": darkpool_parse.NOTES,
        "why_gated": (
            "本栏的核心数据（ATS 与非 ATS 场外成交量）**只有 FINRA 一家发布**，"
            "没有第二个公开来源。而 FINRA 的条款限「非商业的个人或专业用途」、"
            "并明文禁止「用本站数据建立数据库」—— 本项目正是下载→落 SQLite。"
            "所以它默认关闭，条款原文原样摆在这里，**判断权在你**。"
            "开启后 FINRA 只出分子；分母（同期总成交量）来自 CBOE 的本地行情沉淀。"),
    }


# ═══════════════════════ 个股页（九条线汇合）═══════════════════════
# ⚠️ 难点不是聚合，是**时间轴对不齐**：期权链是昨天收盘，13F 是三个月前的季末。
#    每一块必须带自己的时点与滞后，⛔ 不做任何跨源综合评分。

@app.get("/api/stock/{ticker}")
def get_stock(ticker: str) -> dict:
    """一只票在九条线上的全部画像。

    ⚠️ **每条线各自兜异常，而且兜的是"任何异常"**：本页的立意就是
    一条线挂掉不带倒整页。只捕获 `RuntimeError` 是不够的 ——
    上游结构变了会抛 `KeyError` / `IndexError` / `AttributeError`，
    那些同样只该让**那一块**变成"取数失败"，不该让整页 500。
    """
    tk = ticker.strip().upper()
    L = stock_parse.lane
    lanes: list = []

    # ⚠️ 代码不合法要**在入口就判**。不然 CBOE 那三条报 bad_symbol、
    #    其余各条却各自去查、各报各的 no_data —— 同一个非法代码
    #    在同一页上得到互相矛盾的解释。
    try:
        tk = cboe.assert_us_ticker(tk)
    except ValueError as e:
        return {**stock_parse.assemble(
            [L(k, reason="bad_symbol", detail=str(e)) for k in stock_parse.LANES]),
            "ticker": tk}

    def run(key: str, fn, *, as_of_on_error: Optional[str] = None) -> None:
        """跑一条线；**任何**异常都只影响这一条。

        `as_of_on_error`：失败时若**已经知道**这块数据的时点（比如期权链
        已经拿到了、只是后续计算炸了），就把它带上 —— 丢掉已知信息
        会让这块沉到时间轴末尾，看起来像"从来没有过数据"。
        """
        try:
            lanes.append(fn())
        except cboe.DataNotAvailable as e:
            lanes.append(L(key, as_of=as_of_on_error, reason="no_data", detail=str(e)))
        except ValueError as e:
            # ⚠️ 代码合法性**入口已经验过**，所以走到这里的 ValueError
            #    是下游脏数据，不是"代码不合法" —— 标成 bad_symbol 会让用户
            #    去改一个本来没问题的代码。
            lanes.append(L(key, as_of=as_of_on_error, reason="fetch_failed",
                           detail=f"ValueError: {e}"))
        except Exception as e:                       # noqa: BLE001 —— 刻意兜全部
            lanes.append(L(key, as_of=as_of_on_error, reason="fetch_failed",
                           detail=f"{type(e).__name__}: {e}"))

    # ── 1. 行情 / GEX / 期权流（同一份快照，只拉一次）──
    chain = None
    try:
        chain = cboe.cached_option_chain(tk)
    except cboe.DataNotAvailable as e:
        for k in ("quote", "gex", "flow"):
            lanes.append(L(k, reason="no_data", detail=str(e)))
    except Exception as e:                           # noqa: BLE001
        for k in ("quote", "gex", "flow"):
            lanes.append(L(k, reason="fetch_failed", detail=f"{type(e).__name__}: {e}"))

    if chain is not None:
        # ⚠️ 连读 `chain.session` 都要兜：快照结构漂移时这里抛 AttributeError，
        #    它在 run() **外面**，会直接把整页打成 500。
        try:
            sess = chain.session
        except Exception:                            # noqa: BLE001
            sess = None
        run("quote", lambda: L("quote", as_of=sess, data={
            "spot": chain.spot, "timestamp": chain.timestamp,
            "contracts": len(chain.contracts),
            "expiries": chain.expiries()[:12]}), as_of_on_error=sess)
        run("gex", lambda: L("gex", as_of=sess,
                             data=greeks.to_dict(greeks.compute(chain, dte_max=30))),
            as_of_on_error=sess)

        def _flow():
            scope = flow_parse.parse(chain, dte_max=30, traded_only=False)
            traded = [r for r in scope if r.volume > 0]
            f = flow_parse.summarize(chain, traded, all_rows=scope, top=8)
            return L("flow", as_of=sess, data={
                "counts": f["counts"], "ratios": f["ratios"],
                "unusual_rows": f["unusual_rows"], "limits": f["limits"]})
        run("flow", _flow, as_of_on_error=sess)

    # ── 2. IV 排名（本地沉淀）──
    def _scanner():
        sess2 = scanner_store.latest_session()
        if not sess2:
            return L("scanner", reason="not_enough",
                     detail="本地还没扫过任何标的 —— 去扫描器跑一轮。")
        q = [x for x in scanner_store.quotes_at(sess2) if x.get("symbol") == tk]
        if not q:
            # ⚠️ 已知本地最新时段，失败时也把它带上 —— 这块不是"从来没有数据"，
            #    而是"我们扫过别的票、没扫这只"。
            return L("scanner", as_of=sess2, reason="not_synced",
                     detail=f"{tk} 不在本地已扫过的标的里（最新时段 {sess2}）。")
        h = scanner_store.history([tk], as_of=sess2).get(tk, {})
        row = scanner_parse.build_row(q[0], h.get("iv", []), h.get("volume", []))
        return L("scanner", as_of=q[0].get("session") or sess2,
                 data=scanner_parse.to_dict(row))
    # 已知本地最新时段时，失败也把它带上 —— 这块不是"从来没有过数据"
    try:
        _sess2 = scanner_store.latest_session()
    except Exception:                                # noqa: BLE001
        _sess2 = None
    run("scanner", _scanner, as_of_on_error=_sess2)

    # ── 3. 内部人 Form 4（本地缓存）──
    def _insider():
        st = insider_store.stats()
        if not st.get("trades"):
            return L("insider", reason="not_synced", detail="本地还没同步 Form 4。")
        agg = insider_store.aggregate(ticker=tk, top=8)
        if not (agg.get("counts") or {}).get("n"):
            return L("insider", reason="no_data",
                     detail=f"已同步的 Form 4 里没有 {tk} 的公开市场交易。")
        # 用**这只票最近一笔**交易日做时点，不是全库最新日 ——
        # 后者会把一只三个月没动静的票显示成"昨天的数据"。
        recent = insider_store.query(ticker=tk, limit=1) or []
        latest = (recent[0].get("tx_date") if recent else None) or None
        return L("insider", as_of=latest, data=agg)
    run("insider", _insider)

    # ── 4. 交割失败 FTD（本地缓存）──
    def _shorts():
        st = shorts_store.stats()
        if not st.get("rows"):
            return L("shorts", reason="not_synced", detail="本地还没导入 FTD 数据。")
        agg = shorts_store.aggregate(top=8, symbol=tk)
        c = agg.get("counts") or {}
        if not c.get("n"):
            return L("shorts", reason="no_data", detail=f"已导入的 FTD 里没有 {tk}。")
        return L("shorts", as_of=c.get("hi"), data=agg)
    run("shorts", _shorts)

    # ── 5. 国会议员申报（本地缓存）──
    def _congress():
        st = congress_store.stats()
        if not st.get("trades"):
            return L("congress", reason="not_synced", detail="本地还没同步国会申报。")
        tr = congress_store.query_trades(ticker=tk, limit=12)
        if not tr:
            return L("congress", reason="no_data", detail=f"已同步的申报里没有 {tk}。")
        return L("congress", as_of=tr[0].get("tx_date"),
                 data={"trades": tr, "count": len(tr)})
    run("congress", _congress)

    # ── 6. 机构 13F（本地缓存；按 CUSIP，不是代码）──
    def _institution():
        st = institution_store.stats()
        if not st.get("holdings"):
            return L("institution", reason="not_synced", detail="本地还没导入 13F。")
        # ⚠️ **本页刻意不做代码 → CUSIP 的映射。**
        #    13F 只给 CUSIP，SEC 不提供映射表（那是商业数据）；
        #    按发行人名称匹配实测命中率仅 42.8%，而拿股票代码（"NVDA"）
        #    去匹配发行人名（"NVIDIA CORP"）几乎必然落空 ——
        #    那样这一栏会对绝大多数票显示"没有机构持有"，
        #    把「我们做不到映射」伪装成「没有机构持有」。
        return L("institution", reason="no_mapping", detail=(
            f"本地已导入 {st.get('holdings', 0):,} 条 13F 持仓"
            f"（{st.get('cusips', 0):,} 个 CUSIP、{st.get('managers', 0):,} 家机构）。"
            f"但 13F **只给 CUSIP**，SEC 不提供代码→CUSIP 映射，"
            f"所以无法从 {tk} 这个代码可靠地定位到持仓 —— "
            f"这是**我们做不到这个映射**，不是「没有机构持有它」。"
            f"去「机构持仓」分栏按**发行人名称**检索。"))
    run("institution", _institution)

    # ── 7. 场外 / 暗池（默认关闭）──
    def _darkpool():
        raw = darkpool_src.weekly(tk)
        parsed = darkpool_parse.parse(raw)
        weeks = darkpool_parse.weeks_of(parsed)
        if not weeks:
            if parsed["unknown_types"]:
                return L("darkpool", reason="fetch_failed", detail=(
                    f"FINRA 返回了不认识的记录类型 {parsed['unknown_types']} —— "
                    f"解析规则可能已过时，这是**解析不兼容**不是没有数据。"))
            return L("darkpool", reason="no_data",
                     detail=f"FINRA 没有 {tk} 的场外记录。")
        out = _consolidated(tk, weeks[-1], parsed)
        return L("darkpool", as_of=weeks[-1], data={
            "week": out["week"], "ats": out["ats"], "otc": out["otc"],
            "ats_over_otc": out["ats_over_otc"],
            "share": out["share"], "share_note": out["share_note"]})
    try:
        lanes.append(_darkpool())
    except darkpool_src.FinraDisabled as e:
        lanes.append(L("darkpool", reason="disabled", detail=str(e)))
    except darkpool_src.DataNotAvailable as e:
        lanes.append(L("darkpool", reason="no_data", detail=str(e)))
    except Exception as e:                           # noqa: BLE001
        lanes.append(L("darkpool", reason="fetch_failed",
                       detail=f"{type(e).__name__}: {e}"))

    out = stock_parse.assemble(lanes)
    out["ticker"] = tk
    return out
