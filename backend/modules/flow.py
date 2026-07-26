"""期权流 —— 从**链快照**能算出什么，以及**算不出什么**。

━━━━━━━━━━━━━━ ⚠️ 先把能力边界说清楚 ━━━━━━━━━━━━━━

Unusual Whales 的 flow 建立在**逐笔成交带（options tape）**上：每一笔成交
带着 size、成交价、交易所、时间戳。有了它才能判断"这笔打在 ask 上还是 bid 上"，
也才有 UW 招牌的 **bullish / bearish flow** 标签。

**我们没有 tape。** CBOE 的免费延时接口给的是**链的快照**：
每个合约的**当日累计成交量**、**持仓量**、bid/ask、希腊字母。
拿到 tape 需要 OPRA feed —— 那正是本项目为了不做 redistributor 而刻意不碰的东西。

于是能与不能是很清楚的两堆：

| 能算（快照就够） | 算不出（必须有逐笔） |
|---|---|
| vol/OI 比（今日成交 vs 存量持仓） | **Sweep**：同一订单毫秒内跨多个交易所拆单 |
| P/C 比（成交量 / 持仓量 / 名义金额 三口径） | **大单分级**：5,000 手是一笔还是 5,000 笔，快照里一模一样 |
| 名义金额排序、到期与行权分布 | **主动买 / 主动卖**：成交打在 ask 还是 bid |
| 绝对 delta / gamma 敞口 | **开仓 / 平仓**：同一笔成交是新建还是了结 |
| **OI 日变化**（靠本地逐日沉淀，见 `history`） | —— |

⛔ **因此本模块不产出任何方向性标签。** 没有 aggressor side 却写「看涨流入」，
是在把猜测当事实卖 —— 既违反项目「只输出数据不输出结论」的铁律，
也正是 UW 那类产品最容易误导人的地方。

━━━━━━━━━━━━━━ ⭐ 快照反而有 tape 没有的一样东西 ━━━━━━━━━━━━━━

**持仓量（OI）**。tape 只告诉你成交了什么，OI 告诉你**沉淀下来多少**。
逐日记录 OI，其差值就是净新增头寸 —— 这是"谁在建仓"最硬的证据，
且**不需要**猜方向。代价是它每天只更新一次，且**要靠自己攒**（见 `flow_store`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sources.cboe import Chain, Contract

#: vol/OI 超过这个倍数才算"异动"。1.0 = 今日成交量已超过全部存量持仓。
UNUSUAL_RATIO = 1.0

#: 低于这个成交量的合约不参与异动榜。
#: ⚠️ 没有这道闸，OI=1、成交 3 手的僵尸合约会以 vol/OI=3.0 霸榜 ——
#:    比值大是因为分母小，不是因为有人在动它。
MIN_VOLUME = 50.0

#: 合约乘数（美股期权固定 100 股/张）
MULTIPLIER = 100.0

LIMITS = {
    "no_tape": (
        "本页数据来自**期权链快照**（每个合约的当日累计成交量与持仓量），"
        "不是逐笔成交带。因此 **sweep 检测、大单分级、主动买/卖方向、开仓/平仓判定"
        "都做不了** —— 那些需要 OPRA 逐笔数据，而本项目刻意不碰 OPRA"
        "（碰了就要按 redistributor 交费，整个自部署模式也就不成立了）。"),
    "no_direction": (
        "⛔ **本页不给任何「看涨/看跌流入」标签。** 判断一笔期权成交是买方发起还是"
        "卖方发起，必须知道它成交在 ask 还是 bid —— 快照里没有这个信息。"
        "没有它却标方向，就是把猜测当事实。"),
    "vol_oi": (
        "**vol/OI > 1** 的字面含义只有一条：今日成交量超过了该合约的存量持仓。"
        "它**既可能**是新头寸在建、**也可能**全部是存量头寸在换手 —— "
        "两者在快照里长得一模一样，这份数据分不出来。"
        "要看头寸有没有真的增加，得比较**两天的持仓量**（本页下方那张卡）。"),
    "oi_lag": (
        "⚠️ **持仓量（OI）是隔夜结算数**，反映的是**昨日收盘**的持仓，"
        "不含今天新开的仓。所以 vol/OI 的分母天然滞后一天 —— "
        "这是 OCC 的结算节奏，不是本项目的取数问题。"),
    "delayed": (
        "CBOE 免费接口是**延时**数据（通常 15 分钟）。做研究够用，抢单不够。"),
}


def _mid(c: Contract) -> Optional[float]:
    """中间价。买一卖一缺一边就返回 None —— **不拿 last 顶替**。

    ⚠️ `last` 是"最后一笔成交价"，可能是几天前的。用它当今天的价去乘成交量，
    算出来的名义金额会离谱地错，而且错得看不出来。

    ⚠️ **零买价（bid=0、ask>0）照常算中间价，这是刻意的**：深度虚值合约
    常年 bid=0 / ask=0.01，把它们全剔掉会让"按权利金"这个口径严重偏向实值侧，
    偏差比留着更大。但零买价意味着**根本没人接盘**，中间价高估了它的价值 ——
    所以这类合约的数量会被单独统计并在界面上报出来（`one_sided_quotes`）。
    """
    if c.bid is None or c.ask is None:
        return None
    if c.bid <= 0 and c.ask <= 0:
        return None
    return (c.bid + c.ask) / 2.0


def _one_sided(r: "FlowRow") -> bool:
    """只有卖价没有买价 —— 中间价偏乐观。"""
    return r.mid is not None and (r.bid_zero is True)


@dataclass(frozen=True)
class FlowRow:
    """单个合约的当日快照指标。"""

    symbol: str
    expiry: str
    type: str
    strike: float
    dte: int
    volume: float
    open_interest: float
    mid: Optional[float]
    #: 买价为 0（或缺失）而卖价有效 —— 中间价偏乐观，见 `_mid` 说明
    bid_zero: bool
    last: Optional[float]
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]

    @property
    def vol_oi(self) -> Optional[float]:
        """成交量 / 持仓量。OI 为 0 时**返回 None 而不是无穷大**。

        ⚠️ OI=0 是**前收结算持仓为 0**这个事实，比值算不出来。
        塞个 999 进去排序会把它伪装成"比值极高"，塞 0 又会把它埋掉。
        两种都是撒谎，所以返回 None，另用 `zero_prior_oi` 标出来。
        """
        if self.open_interest <= 0:
            return None
        return self.volume / self.open_interest

    @property
    def zero_prior_oi(self) -> bool:
        """**前收结算持仓为 0**，且今天有成交。

        ⚠️ 这是一条**事实陈述**，不是"今天全是新开仓"的推断。
        原来叫 `is_new_strike`（"全新行权价"）是过度断言了 ——
        快照分不出这三种情况：
        ① 这个行权价刚挂出来（确实是全新的）
        ② 行权价早就在，只是此前没人持有（不新，只是冷）
        ③ 今天开了又平的日内往返（明天 OI 还是 0，一张也没沉淀下来）
        能说的只有"昨收时没有未平仓头寸，今天有人交易它"。
        要知道有没有真沉淀下来，得看**明天**的持仓量（下方那张卡）。
        """
        return self.open_interest <= 0 and self.volume > 0

    @property
    def notional(self) -> Optional[float]:
        """权利金规模的**估算值** = 当日累计成交量 × **抓取时**中间价 × 100。

        ⚠️⚠️ **这不是实际成交金额，而且差距可以很大。**
        链快照没有逐笔成交价、也没有 VWAP，只有"抓取那一刻"的买卖盘口。
        若 1,000 张在上午以 $1 成交、抓取时中间价已涨到 $5，
        这里会算出 $50 万，而真实付出的权利金约 $10 万 —— **五倍**。

        没有 tape 就没法算准，所以本项目的做法是：照算，但**在所有出口都
        叫它「估算」**，绝不叫「实际成交额」。字段名 `notional` 保留，
        对外文案一律是"按当前中间价估算"。

        中间价缺失时返回 None，**不回退到 `last`**（见 `_mid` 的说明）。
        """
        return None if self.mid is None else self.volume * self.mid * MULTIPLIER

    @property
    def unusual(self) -> bool:
        return (self.volume >= MIN_VOLUME
                and (self.zero_prior_oi
                     or (self.vol_oi is not None and self.vol_oi >= UNUSUAL_RATIO)))


def parse(chain: Chain, dte_max: Optional[int] = None,
          expiry: Optional[str] = None,
          traded_only: bool = True) -> list[FlowRow]:
    """链 → 逐合约快照行。

    ⚠️ `traded_only` 必须由调用方明确选：
    - **展示**用 True（今天没成交的合约在"今日异动"里没有意义）
    - **归档 / 持仓量口径**用 **False** —— 一个合约今天没成交，
      不代表它的持仓量是 0。只存有成交的合约，第二天它没成交就从库里消失，
      OI 差值会把它记成"净平仓全部头寸"，而它其实一张都没动。
      （这个坑在第一版里真实存在，被 codex 抓出来。）
    """
    rows = []
    for c in chain.filter(expiry=expiry, dte_max=dte_max,
                          traded_only=traded_only):
        rows.append(FlowRow(
            symbol=chain.ticker, expiry=c.expiry, type=c.type, strike=c.strike,
            dte=c.dte, volume=c.volume, open_interest=c.open_interest,
            mid=_mid(c), bid_zero=not (c.bid and c.bid > 0),
            last=c.last, iv=c.iv, delta=c.delta, gamma=c.gamma))
    return rows


def to_dict(r: FlowRow) -> dict:
    return {
        "symbol": r.symbol, "expiry": r.expiry, "type": r.type,
        "strike": r.strike, "dte": r.dte,
        "volume": r.volume, "open_interest": r.open_interest,
        "vol_oi": r.vol_oi, "zero_prior_oi": r.zero_prior_oi,
        "mid": r.mid, "bid_zero": r.bid_zero, "last": r.last,
        "notional": r.notional,
        "iv": r.iv, "delta": r.delta, "gamma": r.gamma,
        "unusual": r.unusual,
    }


# ─────────────────────────── 汇总 ───────────────────────────

def ratios(traded: Iterable[FlowRow], all_rows: Iterable[FlowRow]) -> dict:
    """认沽/认购比 —— **三个口径都给，不挑一个当「那个」P/C**。

    三者量的是不同的东西，结论不同是正常的：
    - **按成交量**：今天有多少张合约换手（最常被引用，也最容易被低价虚值合约灌水）
    - **按持仓量**：沉淀下来的仓位结构（慢，但反映真实仓位）
    - **按权利金估算**：钱的分布（一张 $50 的深度实值 ≠ 一张 $0.03 的彩票）

    ⚠️ **两个参数不是冗余的。** 成交量与权利金只能来自**今日有成交**的合约，
    而持仓量必须来自**筛选范围内的全部合约** —— 一个合约今天没成交，
    它的持仓量照样在那里。第一版把三个口径都算在"有成交子集"上，
    结果"沉淀下来的仓位结构"实际是"今天碰过的那些合约的仓位"，
    在冷门标的上会差出一个数量级。
    """
    cv = pv = cn = pn = 0.0
    n_no_mid = 0
    nc = np_ = 0                    # 各边**算得出**权利金的合约数
    one_sided = 0                   # 零买价（没人接盘）却计入了估算的合约数
    for r in traded:
        is_put = r.type == "put"
        cv, pv = (cv, pv + r.volume) if is_put else (cv + r.volume, pv)
        nt = r.notional
        if nt is None:
            n_no_mid += 1
        elif is_put:
            pn += nt
            np_ += 1
            if r.bid_zero:
                one_sided += 1
        else:
            cn += nt
            nc += 1
            if r.bid_zero:
                one_sided += 1

    co = po = 0.0
    n_all = 0
    for r in all_rows:
        n_all += 1
        if r.type == "put":
            po += r.open_interest
        else:
            co += r.open_interest

    def _r(p: float, c: float) -> Optional[float]:
        # ⚠️ 分母为 0 时返回 None，不返回 0 也不返回无穷 ——
        #    "没有认购成交"和"认沽/认购=0"是完全相反的两件事。
        return None if c <= 0 else p / c

    return {
        "by_volume": {"call": cv, "put": pv, "pc": _r(pv, cv),
                      "basis": "今日有成交的合约"},
        "by_oi": {"call": co, "put": po, "pc": _r(po, co),
                  "basis": f"筛选范围内全部 {n_all} 个合约（含今日无成交的）"},
        # ⚠️ 某一边**一个都算不出**时给 None，不给 0。
        #    认沽全缺报价而认购能算时，输出 put=0 / pc=0.00，
        #    读起来是"认沽侧没有钱"，实际是"认沽侧算不出来"。
        "by_notional": {"call": cn if nc else None,
                        "put": pn if np_ else None,
                        "pc": _r(pn, cn) if (nc and np_) else None,
                        "counted_call": nc, "counted_put": np_,
                        "basis": "今日有成交的合约，按抓取时中间价**估算**"},
        # 没有任何报价的合约算不出权利金，**要说有多少条被排除**
        "notional_excluded": n_no_mid,
        # 有卖价没买价的合约照算了，但中间价偏乐观，**要说有多少条**
        "one_sided_quotes": one_sided,
    }


def exposure(rows: Iterable[FlowRow], spot: float) -> dict:
    """今日成交所对应的 delta / gamma 敞口 —— **只给绝对量，不给净额**。

    ⚠️ 为什么不给「净 delta」：净额要求知道每一笔是买入还是卖出。
    快照没有 aggressor side，把所有成交都当成买方发起去加总，
    算出来的"净 delta 敞口"是个**看着很专业的假数字**。
    这里给的是 Σ|delta|×量 —— 「今天成交的合约总共挂着多少方向性敞口」，
    这个陈述不需要知道谁是买方，因此是真的。
    """
    call_d = put_d = gam = 0.0
    n_call = n_put = n_gamma = 0
    miss_call = miss_put = miss_gamma = 0
    for r in rows:
        is_put = r.type == "put"
        if r.delta is None:
            if is_put:
                miss_put += 1
            else:
                miss_call += 1
        else:
            share = abs(r.delta) * r.volume * MULTIPLIER
            if is_put:
                put_d += share
                n_put += 1
            else:
                call_d += share
                n_call += 1
        if r.gamma is None:
            miss_gamma += 1
        else:
            n_gamma += 1
            gam += abs(r.gamma) * r.volume * MULTIPLIER * spot * spot / 100.0

    # ⚠️ **算不出时返回 None，不是 0 —— 而且要按边分别判。**
    #    只看"总共有几个 delta"是不够的：认沽全有、认购全缺时，
    #    认购那一侧照样会显示 0，把"这边算不出"说成"这边没有敞口"。
    #    总额也一样：一边缺了，加总出来的只是半张图，所以两边齐全才给。
    hc, hp, hg = n_call > 0, n_put > 0, n_gamma > 0
    return {
        "call_delta_shares": call_d if hc else None,
        "put_delta_shares": put_d if hp else None,
        "total_delta_shares": (call_d + put_d) if (hc and hp) else None,
        "call_delta_notional": call_d * spot if hc else None,
        "put_delta_notional": put_d * spot if hp else None,
        "gamma_notional_per_1pct": gam if hg else None,
        "missing_delta_call": miss_call,
        "missing_delta_put": miss_put,
        "missing_gamma": miss_gamma,
        "counted_delta_call": n_call,
        "counted_delta_put": n_put,
        "counted_gamma": n_gamma,
        "note": ("这是**绝对**敞口（Σ|delta|×成交量×100），不是净敞口。"
                 "净额需要知道每笔是买是卖，快照里没有这个信息。"),
    }


def by_expiry(rows: Iterable[FlowRow]) -> list[dict]:
    """按到期日汇总 —— 看今天的成交集中在多短的期限上。"""
    buckets: dict[str, dict] = {}
    for r in rows:
        b = buckets.setdefault(r.expiry, {
            "expiry": r.expiry, "dte": r.dte, "call_volume": 0.0,
            "put_volume": 0.0, "volume": 0.0, "open_interest": 0.0,
            "notional": 0.0, "notional_counted": 0, "contracts": 0})
        b["volume"] += r.volume
        b["open_interest"] += r.open_interest
        b["contracts"] += 1
        if r.type == "put":
            b["put_volume"] += r.volume
        else:
            b["call_volume"] += r.volume
        nt = r.notional
        if nt is not None:
            b["notional"] += nt
            b["notional_counted"] += 1
    out = sorted(buckets.values(), key=lambda b: b["expiry"])
    # 整个到期桶一个报价都没有 → 权利金是**算不出**，不是 0
    for b in out:
        if b["notional_counted"] == 0:
            b["notional"] = None
    return out


def by_strike(rows: Iterable[FlowRow], spot: float,
              width_pct: float = 0.15) -> dict:
    """按行权价汇总（只取现价附近，远端行权价噪声大且没人看）。

    ⚠️ **裁剪范围必须跟着结果一起返回。** 这张图与"按到期日"那张
    同处一张卡片，但后者统计**全部**行权价 —— 两张图的成交量合计对不上。
    不把窗口说出来，用户只会以为其中一张算错了。
    """
    lo, hi = spot * (1 - width_pct), spot * (1 + width_pct)
    dropped = 0
    dropped_volume = 0.0
    buckets: dict[float, dict] = {}
    for r in rows:
        if not (lo <= r.strike <= hi):
            dropped += 1
            dropped_volume += r.volume
            continue
        b = buckets.setdefault(r.strike, {
            "strike": r.strike, "call_volume": 0.0, "put_volume": 0.0,
            "call_oi": 0.0, "put_oi": 0.0})
        if r.type == "put":
            b["put_volume"] += r.volume
            b["put_oi"] += r.open_interest
        else:
            b["call_volume"] += r.volume
            b["call_oi"] += r.open_interest
    return {
        "rows": sorted(buckets.values(), key=lambda b: b["strike"]),
        "window_pct": width_pct,
        "low": lo, "high": hi,
        "dropped_contracts": dropped,
        "dropped_volume": dropped_volume,
    }


def summarize(chain: Chain, rows: list[FlowRow],
              all_rows: Optional[list[FlowRow]] = None, top: int = 40) -> dict:
    """一页所需的全部聚合。

    `rows` = 今日有成交的合约（成交量 / 权利金 / 敞口 口径）。
    `all_rows` = 同一筛选范围内的**全部**合约（持仓量口径）。不传则退回 `rows`，
    但那样持仓量口径就只覆盖有成交的子集 —— 调用方应当显式传入。
    """
    scope_rows = all_rows if all_rows is not None else rows
    # ⚠️ 一律按名义金额排，**不把"前收持仓为 0"当排序键**。
    # 实测（SPY 2026-07-25）把它们顶到最前，榜首是三张各值几千美元的
    # 深度虚值彩票（820C / 815C / 590P，名义 $0.00M），
    # 而当天真正的大动作 —— 739P 成交 86,279 张、名义 $24M —— 被压到第 5。
    # "前收持仓为 0"是个**属性**（用徽章标出来），不是重要性的度量：
    # 有钱的会自己浮上来，没钱的本来就该沉下去。
    unusual = sorted((r for r in rows if r.unusual),
                     key=lambda r: -(r.notional or 0.0))
    biggest = sorted(rows, key=lambda r: -(r.notional or 0.0))
    return {
        "ticker": chain.ticker,
        "spot": chain.spot,
        "timestamp": chain.timestamp,
        # ⚠️ session = 数据所属交易时段；timestamp = CBOE 发布时刻。两者常差一天。
        "session": chain.session,
        "counts": {
            "traded_contracts": len(rows),
            "scope_contracts": len(scope_rows),
            "unusual": sum(1 for r in rows if r.unusual),
            "zero_prior_oi": sum(1 for r in rows if r.zero_prior_oi),
            "total_volume": sum(r.volume for r in rows),
            # ⚠️ 持仓量合计走全链，与 by_oi 口径一致
            "total_oi": sum(r.open_interest for r in scope_rows),
        },
        "ratios": ratios(rows, scope_rows),
        "exposure": exposure(rows, chain.spot),
        "unusual_rows": [to_dict(r) for r in unusual[:top]],
        "biggest_rows": [to_dict(r) for r in biggest[:top]],
        "by_expiry": by_expiry(rows),
        "by_strike": by_strike(rows, chain.spot),
        "thresholds": {"unusual_ratio": UNUSUAL_RATIO, "min_volume": MIN_VOLUME},
        "limits": LIMITS,
    }
