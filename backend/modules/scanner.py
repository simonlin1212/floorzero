"""全市场扫描器。

━━━━━━━━━━━━━━ ⚠️ 先说清楚这一栏为什么慢 ━━━━━━━━━━━━━━

UW 的扫描器能秒出全市场结果，因为他们**吃 OPRA 逐笔 feed**，
全市场的状态本来就在他们自己的库里。我们没有那条管子，只能一只一只去问 CBOE。

实测（2026-07-26）两条路的代价差 **3,750 倍**：

| 路 | 单只 | 全市场 6,049 只 |
|---|---|---|
| 轻量行情 `quotes/{SYM}.json` | 0.4KB / ~120ms | ~26 分钟（自律限流 4/s）|
| 完整期权链 `options/{SYM}.json` | 1.5MB / ~2.1s | **~3.5 小时 / 9GB** |

所以扫描是**两遍**：轻量行情扫全市场 → 短名单再拉全链。
而且它天然是个**后台作业**，不是点一下就出结果的接口。这一点必须让用户
一眼看到，而不是让他对着转圈的按钮猜要等多久。

━━━━━━━━━━━━━━ ⚠️ IV Rank 第一天算不出来 ━━━━━━━━━━━━━━

**IV Rank / IV 百分位是扫描器最核心的指标，而它按定义就需要历史**
（当前 IV 在过去 252 个交易日区间里的位置）。CBOE 只给当下的 `iv30`，
一个孤零零的 28.9% 说明不了任何问题 —— 对这只票是高是低，只有和它**自己的**
历史比才知道。

这份历史**补不回来**，只能从装上那天起逐日攒（同 `flow_store` 的 OI）。
所以：

- 攒够之前，`iv_rank` 返回 **None**，并附上"还差多少天"。
  ⛔ **绝不拿 20 天的样本硬算一个数字冒充 IV Rank** —— 那个数看着一样专业，
  但它衡量的根本不是同一件事，而用户没法从数字本身看出这一点。
- 样本天数一律随结果返回，让读者自己判断这个排名有多少分量。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

#: IV Rank 的标准回看窗口（交易日）。252 ≈ 一年。
IV_LOOKBACK = 252

#: 少于这个天数就**不出** IV Rank。
#: 60 个交易日约三个月 —— 低于此，"过去一年区间"这个说法已经名不副实。
IV_MIN_SAMPLE = 60

NOTES = {
    "why_slow": (
        "全市场扫描是**后台作业**，不是点一下就出结果。CBOE 没有全市场端点，"
        "只能逐只去问：轻量行情 6,049 只约 **26 分钟**（自律限流 4 次/秒），"
        "完整期权链则要 3.5 小时 / 9GB —— 所以扫描分两遍，"
        "轻量扫全市场、短名单再拉全链。"),
    "iv_rank": (
        "**IV Rank 按定义需要历史**：当前 IV 在过去 252 个交易日区间里的位置。"
        "CBOE 只给当下的 iv30，单点数值说明不了高低。这份历史**补不回来**，"
        f"只能逐日攒；不足 {IV_MIN_SAMPLE} 个交易日时本页返回**空值**，"
        "**不会**拿短样本硬算一个数字冒充 IV Rank。"),
    "universe": (
        "全集是 CBOE 官方的**有期权标的**清单（6,049 只去重后），"
        "不是「今天有成交的标的」—— 里头有大量常年零成交的冷门票。"),
    "snapshot": (
        "一轮扫描要跑几十分钟，期间各标的的抓取时刻不同。因为 CBOE 是**延时**"
        "数据（收盘后基本静止），同一轮内的可比性没问题，但每行都带自己的"
        "交易时段，对不上的时候能看出来。"),
}


def _pct_rank(value: float, samples: list[float]) -> Optional[float]:
    """`value` 在样本里的百分位（0-100）。

    ⚠️ 用的是"有多少样本 ≤ 它"，**不是** min-max 线性归一化。
    两者常被混叫 IV Rank：
    - **IV Rank**（业内主流）= (当前 − 最低) / (最高 − 最低) × 100，只看两个端点
    - **IV 百分位** = 有多少天低于当前，看的是整个分布
    一年里有一天暴涨到 200%，IV Rank 会被那一天永久压扁，而百分位不会。
    本模块**两个都算、都返回**，不挑一个叫"那个 IV Rank"。
    """
    if not samples:
        return None
    below = sum(1 for s in samples if s <= value)
    return below / len(samples) * 100.0


def _minmax_rank(value: float, samples: list[float]) -> Optional[float]:
    """业内主流口径的 IV Rank：当前值在 [最低, 最高] 区间里的位置。"""
    if not samples:
        return None
    lo, hi = min(samples), max(samples)
    if hi <= lo:
        # 整段样本一模一样 —— 位置**无从谈起**，不是 0 也不是 50
        return None
    return (value - lo) / (hi - lo) * 100.0


@dataclass(frozen=True)
class ScanRow:
    """扫描器里的一行（轻量行情 + 本地历史算出的排名）。"""

    symbol: str
    session: Optional[str]
    price: Optional[float]
    change_pct: Optional[float]
    volume: Optional[float]
    iv30: Optional[float]
    iv30_change: Optional[float]
    security_type: Optional[str]
    #: 本地攒到的 iv30 样本数（交易日）
    iv_samples: int
    iv_rank: Optional[float]          # min-max 口径
    iv_percentile: Optional[float]    # 分布口径
    #: ⚠️ 排名为空的**原因**：insufficient_history / no_current_iv / flat_history。
    #: 三者在界面上必须说得不一样 —— 只有第一种才是"再等几天就有了"。
    iv_reason: Optional[str]
    #: 成交量相对本地历史中位数的倍数（算不出时为 None）
    volume_x_median: Optional[float]
    volume_samples: int
    #: 为空的原因：insufficient_history / no_current_volume / zero_median
    volume_x_reason: Optional[str]

    @property
    def iv_ready(self) -> bool:
        return self.iv_samples >= IV_MIN_SAMPLE

    @property
    def iv_days_needed(self) -> int:
        return max(0, IV_MIN_SAMPLE - self.iv_samples)


def to_dict(r: ScanRow) -> dict:
    return {
        "symbol": r.symbol, "session": r.session, "price": r.price,
        "change_pct": r.change_pct, "volume": r.volume,
        "iv30": r.iv30, "iv30_change": r.iv30_change,
        "security_type": r.security_type,
        "iv_samples": r.iv_samples,
        "iv_rank": r.iv_rank, "iv_percentile": r.iv_percentile,
        "iv_ready": r.iv_ready, "iv_days_needed": r.iv_days_needed,
        "iv_reason": r.iv_reason,
        "volume_x_median": r.volume_x_median,
        "volume_samples": r.volume_samples,
        "volume_x_reason": r.volume_x_reason,
    }


def build_row(quote: dict, iv_history: list[float],
              volume_history: list[float]) -> ScanRow:
    """把一条轻量行情 + 它的本地历史，合成一行扫描结果。

    ⚠️ 历史不够时 `iv_rank` / `iv_percentile` 返回 **None**，
    而不是用手上这几天硬算一个 —— 详见模块头部说明。
    """
    iv = quote.get("iv30")
    n_iv = len(iv_history)
    window = iv_history[-IV_LOOKBACK:]

    vol = quote.get("volume")
    n_vol = len(volume_history)
    vx = None
    vx_reason: Optional[str] = None
    if vol is None:
        vx_reason = "no_current_volume"
    elif n_vol < 5:
        vx_reason = "insufficient_history"
    else:
        med = _median(volume_history[-IV_LOOKBACK:])
        if med is None or med <= 0:
            # 中位数为 0（长期零成交）时**算不出倍数**，不能返回 0 或无穷。
            # ⚠️ 但这和"历史不够"是**两码事**，所以理由要分开标。
            vx_reason = "zero_median"
        else:
            vx = vol / med

    # ⚠️ `iv_rank=None` 有**三种**原因，必须分开标：
    #    ① 历史不够 ② 当前 iv30 缺失 ③ 整段历史一模一样（区间为 0，位置无从谈起）
    #    一律说成"还差 N 天"会在 ②③ 上撒谎 —— 尤其 ② 时会显示"还差 0 天"。
    iv_reason: Optional[str] = None
    rank = pct = None
    if iv is None:
        iv_reason = "no_current_iv"
    elif n_iv < IV_MIN_SAMPLE:
        iv_reason = "insufficient_history"
    else:
        rank = _minmax_rank(iv, window)
        pct = _pct_rank(iv, window)
        if rank is None:
            iv_reason = "flat_history"

    return ScanRow(
        symbol=quote["symbol"], session=quote.get("session"),
        price=quote.get("price"), change_pct=quote.get("change_pct"),
        volume=vol, iv30=iv, iv30_change=quote.get("iv30_change"),
        security_type=quote.get("security_type"),
        iv_samples=n_iv, iv_rank=rank, iv_percentile=pct,
        iv_reason=iv_reason,
        volume_x_median=vx, volume_samples=n_vol, volume_x_reason=vx_reason,
    )


def _median(xs: list[float]) -> Optional[float]:
    """中位数。

    ⚠️ 偶数个样本要取**中间两个的平均**，不是 `srt[n//2]`（那取的是上中位数）。
    `[10,20,30,40]` 的中位数是 25 不是 30 —— 差 20%，量比筛选的结果会跟着变。
    """
    if not xs:
        return None
    srt = sorted(xs)
    n = len(srt)
    mid = n // 2
    return srt[mid] if n % 2 else (srt[mid - 1] + srt[mid]) / 2.0


# ─────────────────────────── 筛选 ───────────────────────────

def apply_filters(rows: Iterable[ScanRow], *,
                  min_price: Optional[float] = None,
                  max_price: Optional[float] = None,
                  min_volume: Optional[float] = None,
                  min_iv: Optional[float] = None,
                  max_iv: Optional[float] = None,
                  min_iv_rank: Optional[float] = None,
                  min_volume_x: Optional[float] = None,
                  security_type: Optional[str] = None) -> tuple[list[ScanRow], dict]:
    """按条件筛。

    ⚠️ **同时返回"因为算不出而被排除"的条数。**
    `min_iv_rank=80` 时，IV Rank 为 None 的行不满足条件、会被滤掉 ——
    但那是"这只票还没攒够历史"，不是"它的 IV Rank 低于 80"。
    只给结果不给这个数字，用户会以为全市场就这么点票符合条件。
    """
    out = []
    skipped_no_iv_rank = 0
    skipped_no_volume_x = 0
    iv_why: dict = {}
    vol_why: dict = {}
    for r in rows:
        if min_price is not None and (r.price is None or r.price < min_price):
            continue
        if max_price is not None and (r.price is None or r.price > max_price):
            continue
        if min_volume is not None and (r.volume is None or r.volume < min_volume):
            continue
        if min_iv is not None and (r.iv30 is None or r.iv30 < min_iv):
            continue
        if max_iv is not None and (r.iv30 is None or r.iv30 > max_iv):
            continue
        if min_iv_rank is not None:
            if r.iv_rank is None:
                skipped_no_iv_rank += 1
                iv_why[r.iv_reason or "unknown"] = iv_why.get(r.iv_reason or "unknown", 0) + 1
                continue
            if r.iv_rank < min_iv_rank:
                continue
        if min_volume_x is not None:
            if r.volume_x_median is None:
                skipped_no_volume_x += 1
                vol_why[r.volume_x_reason or "unknown"] = vol_why.get(
                    r.volume_x_reason or "unknown", 0) + 1
                continue
            if r.volume_x_median < min_volume_x:
                continue
        if security_type and (r.security_type or "") != security_type:
            continue
        out.append(r)
    return out, {
        # 被滤掉是因为**算不出**，不是因为不满足数值条件 —— 两者必须分开报
        "excluded_no_iv_rank": skipped_no_iv_rank,
        "excluded_no_volume_x": skipped_no_volume_x,
        # 按理由细分 —— "还差几天"和"当前 IV 缺失"要给用户不同的下一步
        "iv_reasons": iv_why,
        "volume_reasons": vol_why,
    }


SORTS = {
    "iv_rank": lambda r: (r.iv_rank is None, -(r.iv_rank or 0)),
    "iv_percentile": lambda r: (r.iv_percentile is None, -(r.iv_percentile or 0)),
    "iv30": lambda r: (r.iv30 is None, -(r.iv30 or 0)),
    "volume": lambda r: (r.volume is None, -(r.volume or 0)),
    "volume_x": lambda r: (r.volume_x_median is None, -(r.volume_x_median or 0)),
    "change_pct": lambda r: (r.change_pct is None, -(r.change_pct or 0)),
    "symbol": lambda r: (False, r.symbol),
}


def sort_rows(rows: list[ScanRow], key: str = "iv_rank") -> list[ScanRow]:
    """排序。

    ⚠️ 每个 key 的元组第一位都是"值是否为 None"，
    所以**算不出的一律沉底**，不会因为 `or 0` 被当成"最低值"混在真值里 ——
    真的 0 和"没有"排在一起，读者分不出哪个是哪个。
    """
    fn = SORTS.get(key) or SORTS["iv_rank"]
    return sorted(rows, key=fn)


#: 空值理由的人话（REST / MCP / UI 三处共用同一份，避免各写各的而说法不一）
REASON_LABEL = {
    "insufficient_history": "本机还没攒够历史",
    "no_current_iv": "本次快照没有 IV30（上游没给）",
    "no_current_volume": "本次快照没有成交量（上游没给）",
    "flat_history": "历史区间为 0（最高=最低），位置无从谈起",
    "zero_median": "历史成交量中位数为 0（长期零成交），倍数算不出",
    "unknown": "原因未知",
}
