"""Vibe-Flow 后端 API。

⚠️ 合规：本服务**只应跑在用户自己的机器上**（localhost）。
Vibe-Flow 分发的是代码，不是数据 —— 用户自部署运行 = personal use。
⛔ 绝不可把本服务部署成对公网提供期权数据的站点（= OPRA redistributor，$1,500/月）。
默认只监听 127.0.0.1，就是这个原因。

启动：
    cd backend && python -m uvicorn app:app --host 127.0.0.1 --port 8920
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from sources import cboe
from modules import greeks

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
