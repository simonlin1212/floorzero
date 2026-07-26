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


_IMPL: dict[str, Callable[..., dict]] = {
    "get_gex": _tool_get_gex,
    "get_gex_curve": _tool_get_gex_curve,
    "get_option_chain_summary": _tool_get_option_chain_summary,
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
