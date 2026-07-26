"""CBOE 官方延时期权数据源。

⚠️ 合规（C 级）：Cboe 的 Use of Content 政策要求使用前取得书面批准与 license。
本模块**仅供用户在自己机器上做个人研究**；FloorZero 只分发代码、不托管数据，
所以运行它的用户是 personal use，我们不是 OPRA redistributor。
⛔ 绝不能把本模块的输出做成对外展示的在线服务（=$1,500/月 redistributor fee）。

代码基于 global-stock-data V2.0（已过 Codex 三轮审计）的已验证实现。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

# OCC 合约代码：标的 + YYMMDD + C/P + 8 位行权价（千分之一美元）
_OSI = re.compile(
    r"^(?P<root>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
    r"(?P<cp>[CP])(?P<strike>\d{8})$"
)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                    # Windows 可能缺 tzdata
    _ET = None


class DataNotAvailable(RuntimeError):
    """该标的/该日确实没有数据 —— 调用方可安全跳过。

    与配置错误、限流、网络故障区分开：后者必须冒泡，
    否则「取不到」会被伪装成「没有」。
    """


class _RateLimiter:
    """线程安全的最小间隔节流器（用锁，避免并发下被击穿）。"""

    def __init__(self, max_per_sec: float) -> None:
        self._interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_limiter = _RateLimiter(4)          # CBOE 自律保护值


@dataclass(frozen=True)
class Contract:
    """单个期权合约（不可变，便于安全传递与缓存）。"""
    symbol: str
    expiry: str            # YYYY-MM-DD
    type: str              # "call" | "put"
    strike: float
    bid: Optional[float]
    ask: Optional[float]
    volume: float
    open_interest: float
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    rho: Optional[float]
    last: Optional[float]

    @property
    def dte(self) -> int:
        """距到期天数（按美东日期算）。"""
        return (datetime.strptime(self.expiry, "%Y-%m-%d").date()
                - datetime.strptime(et_today(), "%Y-%m-%d").date()).days


@dataclass(frozen=True)
class Chain:
    ticker: str
    spot: float
    timestamp: Optional[str]
    contracts: tuple[Contract, ...]
    #: 数据所属的**交易时段**（YYYY-MM-DD，来自 CBOE 的 `last_trade_time`）。
    #: ⚠️ 与 `timestamp` 不是一回事：`timestamp` 是 CBOE **发布**这份文件的时刻
    #: （实测周五 16:00 ET 收盘的数据，发布时间戳写的是 `2026-07-25 03:44:48`）。
    #: 做本地历史沉淀**必须按 session 归档**——按墙上时间归档的话，
    #: 周六和周日各打开一次，会把同一份周五收盘数据存成"两天的观测"，
    #: 差值算出来全是 0，看着像"持仓没变"，其实是根本没有新数据。
    session: Optional[str] = None

    def expiries(self) -> list[str]:
        return sorted({c.expiry for c in self.contracts})

    def filter(self, expiry: Optional[str] = None, dte_max: Optional[int] = None,
               traded_only: bool = False) -> list[Contract]:
        """expiry='0DTE' 取当日到期；dte_max 取 N 天内；traded_only 只要今日有成交的。"""
        cs = list(self.contracts)
        if expiry == "0DTE":
            cs = [c for c in cs if c.expiry == et_today()]
        elif expiry:
            cs = [c for c in cs if c.expiry == expiry]
        if dte_max is not None:
            cs = [c for c in cs if 0 <= c.dte <= dte_max]
        if traded_only:
            cs = [c for c in cs if c.volume > 0]
        return cs


def et_today() -> str:
    """美东今日 YYYY-MM-DD。

    ⚠️ 必须区分 EDT(UTC-4) 与 EST(UTC-5)：硬编码 UTC-4 会让冬令时
    UTC 04:00–05:00 这一小时算成次日，导致 0DTE 选错到期日。
    """
    now = datetime.now(timezone.utc)
    if _ET is not None:
        return now.astimezone(_ET).strftime("%Y-%m-%d")
    # 无 tzdata 的回退：按美国 DST 规则自算（切换发生在当地 2:00 = 07:00/06:00 UTC）
    y = now.year
    mar8 = datetime(y, 3, 8, tzinfo=timezone.utc)
    dst_start = (mar8 + timedelta(days=(6 - mar8.weekday()) % 7)).replace(hour=7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = (nov1 + timedelta(days=(6 - nov1.weekday()) % 7)).replace(hour=6)
    offset = 4 if dst_start <= now < dst_end else 5
    return (now - timedelta(hours=offset)).strftime("%Y-%m-%d")


def parse_osi(symbol: str) -> Optional[dict]:
    """解析 OCC 合约代码 → {expiry, type, strike}；无法解析返回 None。"""
    m = _OSI.match(symbol)
    if not m:
        return None
    g = m.groupdict()
    return {
        "expiry": f"20{g['y']}-{g['m']}-{g['d']}",
        "type": "call" if g["cp"] == "C" else "put",
        "strike": int(g["strike"]) / 1000.0,
    }


def assert_us_ticker(ticker: str) -> str:
    """CBOE 只覆盖美股；传入港股/A 股代码时给出明确提示而不是空结果。"""
    t = str(ticker).upper().strip()
    if t.endswith(".HK") or (t.isdigit() and len(t) in (4, 5, 6)):
        raise ValueError(f"'{ticker}' 不是美股代码；CBOE 期权仅覆盖美股。")
    if not t or not t.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"无效 ticker: '{ticker}'")
    return t


def _get(url: str, timeout: int = 30) -> dict:
    """拉一个 CBOE JSON 端点。

    ⚠️ 异常分类必须**正向识别**，不能用排除法：
    · 404          = 标的确实没有数据 → DataNotAvailable（调用方可跳过）
    · 403 / 429 / 5xx = 被拒绝、限流、上游故障 → RuntimeError（必须冒泡）
    把 403 归成「没数据」会让"被封 IP"静默伪装成"这个标的没期权"，
    使用者永远查不出真实原因。
    """
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()                    # 放进 try：上游可能 200 却返回 HTML
    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 404:
            raise DataNotAvailable(f"CBOE 无此标的数据 (404): {url[:70]}") from e
        hint = {403: "被拒绝（限流或封禁）", 429: "请求过快"}.get(code, "")
        raise RuntimeError(f"CBOE HTTP {code} {hint}: {url[:70]}") from e
    except ValueError as e:                # r.json() 解析失败（HTML 错误页等）
        raise RuntimeError(f"CBOE 返回非 JSON（可能是错误页）: {url[:70]}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"CBOE 请求失败: {type(e).__name__}: {e}") from e


def option_chain(ticker: str) -> Chain:
    """拉取单只美股的期权全链（延时）。"""
    tk = assert_us_ticker(ticker)
    raw = _get(f"{CBOE_BASE}/options/{tk}.json")
    data = raw.get("data") or {}
    out: list[Contract] = []
    for o in data.get("options") or []:
        meta = parse_osi(o.get("option", ""))
        if not meta:
            continue
        out.append(Contract(
            symbol=o["option"],
            expiry=meta["expiry"], type=meta["type"], strike=meta["strike"],
            bid=o.get("bid"), ask=o.get("ask"),
            volume=o.get("volume") or 0.0,
            open_interest=o.get("open_interest") or 0.0,
            iv=o.get("iv"), delta=o.get("delta"), gamma=o.get("gamma"),
            vega=o.get("vega"), theta=o.get("theta"), rho=o.get("rho"),
            last=o.get("last_trade_price"),
        ))
    if not out:
        raise DataNotAvailable(f"{tk} 未返回任何期权合约（可能无期权或不在 CBOE 覆盖内）")
    spot = data.get("current_price")
    if not spot:
        raise DataNotAvailable(f"{tk} 未返回现价")
    # 交易时段：`last_trade_time` 形如 "2026-07-24T16:00:00"（美东收盘时刻）
    ltt = data.get("last_trade_time") or ""
    session = ltt[:10] if len(ltt) >= 10 and ltt[4] == "-" else None
    return Chain(ticker=tk, spot=float(spot),
                 timestamp=raw.get("timestamp"), session=session,
                 contracts=tuple(out))


# ── 短时快照缓存（REST 与 MCP **共用**）──
# 放在数据源层而不是 app.py：否则 MCP 路径绕过缓存，
# 同一次 AI 会话里 get_gex 与 get_gex_curve 会拿到两个不同快照，结论对不上。
_CACHE: dict[str, tuple[float, "Chain"]] = {}
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_GUARD = threading.Lock()
CACHE_TTL = 60.0


def _cache_lock(key: str) -> threading.Lock:
    with _CACHE_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def cached_option_chain(ticker: str) -> Chain:
    """带缓存的期权链读取；同一标的的并发请求会合并成一次上游调用。"""
    key = assert_us_ticker(ticker)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    with _cache_lock(key):
        hit = _CACHE.get(key)              # 双重检查：等锁期间可能已被填好
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        chain = option_chain(key)
        _CACHE[key] = (time.monotonic(), chain)
        return chain


def quote(ticker: str) -> dict:
    """个股延时快照（含现价，可与期权链配合定 ATM）。"""
    return _get(f"{CBOE_BASE}/quotes/{assert_us_ticker(ticker)}.json")["data"]

# ── 扫描器用的两个轻量端点 ──

def option_roots() -> list[str]:
    """全部**有期权**的标的代码（CBOE 官方清单，实测 6,300 个 / 217KB）。

    这是扫描器的全集。⚠️ 它是"挂了期权的标的"，不是"今天有成交的标的" ——
    里头有大量常年零成交的冷门票。
    """
    raw = _get(f"{CBOE_BASE}/symbol_book/option-roots.json")
    data = raw.get("data") or []
    out, seen = [], set()
    for row in data:
        sym = (row.get("symbol") or "").strip().upper()
        # 同一标的可能有多个 root（拆股后的调整合约等），按 symbol 去重
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    if not out:
        raise RuntimeError("CBOE 期权标的清单为空（接口结构可能已变更）")
    return out


@dataclass(frozen=True)
class Quote:
    """轻量行情快照 —— **只有标的层，没有期权链**。

    扫描器靠它做第一遍粗筛：0.4KB / 只，而全链是 1.5MB / 只（**3,750 倍**）。
    全市场拉全链需要约 3.7 小时 / 9.5GB，拉轻量行情约 26 分钟（限流 4/s）。
    """

    symbol: str
    price: Optional[float]
    change_pct: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    prev_close: Optional[float]
    volume: Optional[float]
    #: 30 天隐含波动率（%）。⚠️ **IV Rank 需要历史**，单点 iv30 排不出高低。
    iv30: Optional[float]
    iv30_change: Optional[float]
    #: 数据所属交易时段（YYYY-MM-DD）
    session: Optional[str]
    security_type: Optional[str]


def quote(ticker: str) -> Quote:
    """单只标的的轻量行情（不含期权链）。"""
    tk = assert_us_ticker(ticker)
    raw = _get(f"{CBOE_BASE}/quotes/{tk}.json")
    d = raw.get("data") or {}
    if not d.get("symbol"):
        raise DataNotAvailable(f"{tk} 无行情数据")
    ltt = d.get("last_trade_time") or ""
    return Quote(
        symbol=d["symbol"],
        price=d.get("current_price"),
        change_pct=d.get("price_change_percent"),
        open=d.get("open"), high=d.get("high"), low=d.get("low"),
        prev_close=d.get("prev_day_close"),
        volume=d.get("volume"),
        iv30=d.get("iv30"),
        iv30_change=d.get("iv30_change"),
        session=ltt[:10] if len(ltt) >= 10 and ltt[4] == "-" else None,
        security_type=d.get("security_type"),
    )

