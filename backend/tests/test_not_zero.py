"""铁律一：**算不出来的东西，不能渲染成 0。**

这是全项目被 codex 抓得最多的一类缺陷（十个分栏累计九次）。形状各不相同 ——
累加器从 0 起步、`or 0.0`、`if v` 把 `None` 和 `False` 合并、空 `catch` ——
但后果一样：一个"我们不知道"被写成了一个具体的数，而读者没法从数字本身看出来。

这些用例就是把每一次踩过的坑钉在原地。
"""
from __future__ import annotations

import pytest

from modules import darkpool as dp
from modules import flow
from modules import market as mkt
from modules import scanner as sc


# ─────────────────────── 期权流 ───────────────────────

def _contract(**kw):
    """造一个 CBOE 合约（字段名与 sources.cboe.Contract 一致）。"""
    from sources.cboe import Contract
    base = dict(symbol="X260130C00100000", expiry="2260-01-30", type="call",
                strike=100.0, bid=1.0, ask=1.2, volume=100.0,
                open_interest=50.0, iv=0.3, delta=0.5, gamma=0.01,
                vega=0.1, theta=-0.05, rho=0.01, last=1.1)
    base.update(kw)
    return Contract(**base)


def _chain(contracts, spot=100.0):
    from sources.cboe import Chain
    return Chain(ticker="X", spot=spot, timestamp="2260-01-01 00:00:00",
                 session="2260-01-01", contracts=tuple(contracts))


def test_vol_oi_零持仓时不是无穷也不是零():
    """OI=0 → 比值**算不出**。塞 999 会伪装成"比值极高"，塞 0 会把它埋掉。"""
    rows = flow.parse(_chain([_contract(open_interest=0.0, volume=100.0)]))
    assert rows[0].vol_oi is None
    assert rows[0].zero_prior_oi is True


def test_delta_敞口某一边全缺时给空值而不是零():
    """认沽有 delta、认购全缺 → 认购侧必须是 None。

    只看"总共有几个 delta"是不够的：那样认购侧照样显示 0，
    把"这边算不出"说成了"这边没有敞口"。
    """
    ch = _chain([
        _contract(type="call", delta=None),
        _contract(type="put", strike=90.0, delta=-0.4,
                  symbol="X260130P00090000"),
    ])
    exp = flow.exposure(flow.parse(ch), spot=100.0)
    assert exp["call_delta_shares"] is None, "认购侧应为 None"
    assert exp["put_delta_shares"] is not None
    # 一边缺了，总额只是半张图 —— 也必须是 None
    assert exp["total_delta_shares"] is None
    assert exp["counted_delta_call"] == 0 and exp["missing_delta_call"] == 1


def test_权利金某一边全缺报价时给空值而不是零():
    ch = _chain([
        _contract(type="call", bid=None, ask=None),
        _contract(type="put", strike=90.0, bid=2.0, ask=2.2,
                  symbol="X260130P00090000"),
    ])
    rows = flow.parse(ch)
    r = flow.ratios(rows, rows)
    assert r["by_notional"]["call"] is None, "认购侧算不出，不能是 0"
    assert r["by_notional"]["put"] is not None
    assert r["by_notional"]["pc"] is None, "一边算不出，比值也算不出"


def test_认沽认购比分母为零时给空值():
    """"没有认购成交"和"认沽/认购 = 0"是完全相反的两件事。"""
    ch = _chain([_contract(type="put", strike=90.0, symbol="X260130P00090000")])
    rows = flow.parse(ch)
    assert flow.ratios(rows, rows)["by_volume"]["pc"] is None


def test_中间价缺一边报价时不回退到最后成交价():
    """`last` 可能是几天前的价，用它乘今天的成交量会错得看不出来。"""
    rows = flow.parse(_chain([_contract(bid=None, ask=None, last=99.0)]))
    assert rows[0].mid is None
    assert rows[0].notional is None, "不能拿 last 顶替中间价"


def test_持仓量口径必须覆盖今日无成交的合约():
    """今天没成交 ≠ 持仓是 0。

    第一版把三个口径都算在"有成交子集"上，于是"沉淀下来的仓位结构"
    实际是"今天碰过的那些合约的仓位" —— 冷门标的上差一个数量级。
    """
    ch = _chain([
        _contract(volume=100.0, open_interest=10.0),
        _contract(strike=110.0, volume=0.0, open_interest=999.0,
                  symbol="X260130C00110000"),
    ])
    scope = flow.parse(ch, traded_only=False)
    traded = [r for r in scope if r.volume > 0]
    r = flow.ratios(traded, scope)
    assert r["by_oi"]["call"] == 1009.0, "持仓量要算上今天没成交的那 999"
    assert r["by_volume"]["call"] == 100.0, "成交量只算有成交的"


# ─────────────────────── 扫描器 ───────────────────────

def test_iv_rank_三种空值原因必须分得开():
    """`None` 有三种成因，一律说成"还差 N 天"会在后两种上撒谎 ——
    尤其"当前 IV 缺失"时会显示"还差 **0** 天"，自相矛盾。"""
    cases = [
        ({"symbol": "A", "iv30": 30.0}, [20.0] * 70, "flat_history"),
        ({"symbol": "B", "iv30": None}, [20.0, 25.0] * 40, "no_current_iv"),
        ({"symbol": "C", "iv30": 30.0}, [20.0] * 5, "insufficient_history"),
    ]
    for q, hist, want in cases:
        row = sc.build_row(q, hist, [100.0] * 10)
        assert row.iv_rank is None
        assert row.iv_reason == want, f"{q['symbol']} 应为 {want}"


def test_中位数偶数样本取中间两个的平均():
    """`srt[n//2]` 取的是**上**中位数：[10,20,30,40] 会得到 30 而非 25，
    差 20%，量比筛选的结果会跟着变。"""
    assert sc._median([10, 20, 30, 40]) == 25.0
    assert sc._median([10, 20, 30]) == 20.0
    assert sc._median([]) is None


def test_量比中位数为零时算不出而不是零或无穷():
    row = sc.build_row({"symbol": "Z", "iv30": 30.0, "volume": 100.0},
                       [], [0.0] * 10)
    assert row.volume_x_median is None
    assert row.volume_x_reason == "zero_median", "这和'历史不够'是两码事"


def test_算不出的行排序时沉底而不是当成最低值():
    """`or 0` 会让"没有"和"真的 0"排在一起，读者分不出哪个是哪个。"""
    def mk(sym, rank):
        return sc.ScanRow(symbol=sym, session="2260-01-01", price=1.0,
                          change_pct=0.0, volume=1.0, iv30=1.0, iv30_change=0.0,
                          security_type="stock", iv_samples=99, iv_rank=rank,
                          iv_percentile=rank, iv_reason=None,
                          volume_x_median=None, volume_samples=0,
                          volume_x_reason=None)
    out = sc.sort_rows([mk("NONE", None), mk("ZERO", 0.0), mk("HIGH", 90.0)],
                       "iv_rank")
    assert [r.symbol for r in out] == ["HIGH", "ZERO", "NONE"]


def test_筛选要把算不出与不满足条件分开报():
    """`min_iv_rank=80` 滤掉的行里，"还没攒够历史"不是"排名低于 80"。"""
    rows = [sc.build_row({"symbol": "A", "iv30": 30.0}, [20.0] * 5, [])]
    kept, exc = sc.apply_filters(rows, min_iv_rank=80)
    assert kept == []
    assert exc["excluded_no_iv_rank"] == 1
    assert exc["iv_reasons"] == {"insufficient_history": 1}


# ─────────────────────── 宏观 ───────────────────────

def test_倒挂是三态不是两态():
    """某个期限缺值 → 那条利差**算不出**，不能混进"均为正"。"""
    p = mkt.parse_curve({"date": "2260-01-01", "BC_3MONTH": 3.9,
                         "BC_2YEAR": 4.3, "BC_10YEAR": 4.7, "BC_30YEAR": None})
    inv = p.is_inverted
    assert inv["10Y-2Y"] is False
    assert inv["30Y-10Y"] is None, "30Y 缺值 → 算不出，不是「未倒挂」"
    assert p.spread("30Y-10Y") is None


# ─────────────────────── 暗池 ───────────────────────

def _dp_row(**kw):
    base = dict(summaryTypeCode="ATS_W_SMBL_FIRM", weekStartDate="2260-01-05",
                issueSymbolIdentifier="X", MPID="AAA",
                marketParticipantName="A", tierDescription="T1",
                totalWeeklyShareQuantity="1000", totalWeeklyTradeCount="10",
                totalNotionalSum="5000")
    base.update(kw)
    return base


def test_成交量为空的记录被排除而不是当成零():
    parsed = dp.parse([_dp_row(MPID="AAA", totalWeeklyShareQuantity=""),
                       _dp_row(MPID="BBB")])
    w = dp.week_summary(parsed, "2260-01-05")
    assert w["ats"]["shares"] == 1000.0, "只算有值的那条"
    assert w["ats"]["null_share_records"] == 1
    assert w["share"] is None, "分子偏小 → 占比也不给"


def test_行数不等于机构数():
    """非 ATS 场外的 MPID 全为空 —— 32 条记录点不出一家。"""
    parsed = dp.parse([_dp_row(summaryTypeCode="OTC_W_SMBL_FIRM", MPID=""),
                       _dp_row(summaryTypeCode="OTC_W_SMBL_FIRM", MPID="")])
    w = dp.week_summary(parsed, "2260-01-05")
    assert w["otc"]["records"] == 2
    assert w["otc"]["firms"] == 0, "一家都点不出来，不能说成 2 家"
    assert w["otc"]["anonymous_records"] == 2
