"""铁律二：本地沉淀的语义。

这两份历史（逐合约持仓量、逐标的 iv30）**补不回来** —— CBOE 只给当下。
所以它们出错的代价和别处不一样：别的线错了重拉一次就好，这里错了就是永久的。

四个具体的坑，每个都真踩过：
1. 按**墙上日期**归档 → 周末连开两天，同一份周五数据存成"两天的观测"
2. 算 IV Rank 时不按目标时段截断 → **前视偏差**（用还没发生的行情判断当天）
3. 到期合约从链里消失 → 被当成"净平仓全部头寸"
4. 同时段重录用裸 `INSERT OR REPLACE` → 上游这次少给一个字段就把已攒的值清成 NULL
"""
from __future__ import annotations

import pytest


def _q(symbol, session, iv30=20.0, volume=100.0):
    return {"symbol": symbol, "session": session, "price": 1.0,
            "change_pct": 0.0, "volume": volume, "iv30": iv30,
            "security_type": "stock"}


def _oi(expiry, typ, strike, oi, vol=0.0):
    return {"expiry": expiry, "type": typ, "strike": strike,
            "open_interest": oi, "volume": vol}


# ─────────────────── 归档键 = 交易时段，不是墙上日期 ───────────────────

def test_归档键用的是传入的交易时段而不是墙上日期(tmp_db):
    """周末连开两次页面，拿到的是同一份周五收盘数据。

    按墙上日期存 → 库里出现"两天"，OI 差值全是 0 ——
    看着像"持仓没变"，其实是**根本没有新数据**。

    ⚠️ 只断言"只有一天"是**假阳性**：两次调用发生在同一个墙上日期，
    哪怕实现完全忽略传入的 session 改用 `date.today()`，结果照样是一天。
    所以这里直接断言**归档键等于传进去的那个值**。
    """
    from modules import flow_store as fs
    rows = [_oi("2260-02-20", "call", 100.0, 500.0, 10.0)]
    fs.record("X", "2260-01-05", 100.0, rows)
    fs.record("X", "2260-01-05", 100.0, rows)      # 同一时段再来一次
    got = [d["snapshot_date"] for d in fs.dates("X")]
    assert got == ["2260-01-05"], "归档键必须是传入的时段，不是今天"


def test_同一时段重录是整段替换而不是逐行合并(tmp_db):
    """只用 `INSERT OR REPLACE`，本次没带的旧行会留着 ——
    于是同一时段下混着两次不同口径的抓取，差值算的是拼接结果。"""
    from modules import flow_store as fs
    from modules import db
    fs.record("X", "2260-01-05", 100.0,
              [_oi("2260-02-20", "call", 100.0, 500.0),
               _oi("2260-02-20", "call", 110.0, 300.0)])
    fs.record("X", "2260-01-05", 100.0,
              [_oi("2260-02-20", "call", 100.0, 500.0)])   # 这次只给一行
    # ⚠️ 直接查明细行，不看 `dates()` 的汇总字段 ——
    #    否则是"被测的写入逻辑"与"被测的汇总逻辑"互相作证。
    with db.connect() as conn:
        strikes = [r["strike"] for r in conn.execute(
            "SELECT strike FROM oi_snapshot WHERE ticker='X' "
            "AND snapshot_date='2260-01-05'")]
    assert strikes == [100.0], "110 那行不该留着"


def test_持仓量为空时当场报错而不是记个零(tmp_db):
    """上游真给了 None，那是"取不到" —— 落库成 0 会在明天变成一笔凭空的变化。"""
    from modules import flow_store as fs
    with pytest.raises(ValueError, match="取不到"):
        fs.record("X", "2260-01-05", 100.0,
                  [{"expiry": "2260-02-20", "type": "call", "strike": 100.0,
                    "open_interest": None, "volume": 1.0}])


def test_缺交易时段的行情真的没进库(tmp_db):
    """归档键错了 IV 样本就串了，而串掉之后从数据里看不出来。
    所以宁可丢，但**必须报出来丢了几条**。

    ⚠️ 只看返回的计数是**假阳性**：实现完全可以一边返回 `dropped=1`、
    一边把 B 用墙上日期或 NULL 写进去 —— 计数对了，历史已经脏了。
    所以这里**直接查库**。
    """
    from modules import db, scanner_store as ss
    rec = ss.record_quotes([_q("A", "2260-01-05"), _q("B", None)])
    assert rec == {"stored": 1, "dropped_no_session": 1}
    with db.connect() as conn:
        syms = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM quote_snapshot")]
    assert syms == ["A"], "B 不该以任何形式落库"


# ─────────────────── 前视偏差 ───────────────────

def test_按目标时段截断历史避免前视偏差(tmp_db):
    """查 01-10 的扫描结果时，样本里混进 01-28 的数据 ——
    等于用还没发生的行情判断当天 IV 是高是低。"""
    from modules import scanner_store as ss
    ss.record_quotes([_q("X", f"2260-01-{d:02d}", iv30=float(d))
                      for d in range(1, 29)])
    full = ss.history(["X"])["X"]["iv"]
    cut = ss.history(["X"], as_of="2260-01-10")["X"]["iv"]
    assert max(full) == 28.0
    assert max(cut) == 10.0, "as_of 之后的数据不该进样本"
    assert len(cut) == 10


def test_取数按每只各自最新一条而不是只取全库最新时段(tmp_db):
    """一轮扫描里各标的的 `last_trade_time` 未必相同（停牌、上游延迟）。
    只取等于最新时段的那批，会把另一批**成功入库的**标的整个隐掉。"""
    from modules import scanner_store as ss
    ss.record_quotes([_q("A", "2260-01-28"), _q("B", "2260-01-27")])
    got = {r["symbol"] for r in ss.quotes_at("2260-01-28")}
    assert got == {"A", "B"}, "B 只是时段旧一天，不该被隐掉"


def test_重扫时上游缺字段不会清掉已攒的值(tmp_db):
    """裸 `INSERT OR REPLACE` 会把已经攒到的 iv30 覆盖成 NULL ——
    样本数不增反减，而这份历史补不回来。"""
    from modules import scanner_store as ss
    ss.record_quotes([_q("A", "2260-01-05", iv30=42.0)])
    ss.record_quotes([{"symbol": "A", "session": "2260-01-05", "price": 9.0,
                       "change_pct": 0.0, "volume": None, "iv30": None,
                       "security_type": "stock"}])
    row = ss.quotes_at("2260-01-05")[0]
    assert row["iv30"] == 42.0, "已有的 iv30 要保住"
    assert row["price"] == 9.0, "有新值的字段照常更新"


# ─────────────────── 持仓量差值 ───────────────────

def _seed_oi(fs):
    fs.record("T", "2260-01-05", 100.0, [
        _oi("2260-01-06", "call", 100.0, 1000.0, 50.0),   # 期间到期
        _oi("2260-03-19", "put", 90.0, 500.0, 0.0),       # 无成交、持仓不变
        _oi("2260-03-19", "call", 110.0, 200.0, 10.0),
        _oi("2260-06-18", "call", 120.0, 777.0, 5.0),     # 结束快照里会缺席
    ])
    fs.record("T", "2260-01-07", 100.0, [
        _oi("2260-03-19", "put", 90.0, 500.0, 0.0),
        _oi("2260-03-19", "call", 110.0, 400.0, 140.0),
    ])


def test_到期合约消失不算净平仓(tmp_db):
    from modules import flow_store as fs
    _seed_oi(fs)
    r = fs.oi_change("T")
    assert r["expired_excluded"] == 1
    assert r["expired_oi"] == 1000.0
    assert all(x["expiry"] != "2260-01-06" for x in r["lost"]), \
        "到期只是到期，不是有人平仓"


def test_未到期却缺席是抓取不完整不是持仓归零(tmp_db):
    """CBOE 会把合约挂到到期为止、归零也照列 ——
    所以"未到期 + 缺席"只能说明那次抓取漏了它。"""
    from modules import flow_store as fs
    _seed_oi(fs)
    r = fs.oi_change("T")
    assert r["incomplete_excluded"] == 1
    assert r["incomplete_oi"] == 777.0
    assert all(x["expiry"] != "2260-06-18" for x in r["lost"]), \
        "不该伪造出 -777 的净减持"


def test_无成交但持仓未变的合约不进增减榜(tmp_db):
    from modules import flow_store as fs
    _seed_oi(fs)
    r = fs.oi_change("T")
    moved = {(x["expiry"], x["type"]) for x in r["gained"] + r["lost"]}
    assert ("2260-03-19", "put") not in moved


def test_只攒到一天时说的是还没攒够而不是没有变化(tmp_db):
    from modules import flow_store as fs
    fs.record("T", "2260-01-05", 100.0, [_oi("2260-03-19", "call", 100.0, 1.0)])
    r = fs.oi_change("T")
    assert r["enough"] is False
    assert "至少两个" in r["note"]


def test_伪造或反向的日期被拒绝(tmp_db):
    """`_load()` 查不到只会返回空字典 —— 差值就把"这天没存过"
    算成"这天持仓全是 0"，输出一份"全量清仓"的假报告。"""
    from modules import flow_store as fs
    _seed_oi(fs)
    for kw in ({"date_to": "1999-01-01"},
               {"date_from": "2099-01-01"},
               {"date_from": "2260-01-07", "date_to": "2260-01-05"}):
        assert fs.oi_change("T", **kw)["enough"] is False, kw


def test_相邻判定看快照顺序不看自然日差(tmp_db):
    """周二→周五也是 3 天但中间漏了两次观测；
    周五→周一虽然也是 3 天，却确实相邻。库里的顺序才是真相。"""
    from modules import flow_store as fs
    _seed_oi(fs)
    fs.record("T", "2260-01-09", 100.0, [_oi("2260-03-19", "call", 110.0, 500.0)])
    near = fs.oi_change("T")                                     # 01-07 → 01-09
    assert near["is_consecutive"] is True
    far = fs.oi_change("T", date_from="2260-01-05", date_to="2260-01-09")
    assert far["is_consecutive"] is False
    assert far["snapshots_between"] == 1
