"""持仓量（OI）的逐日沉淀。

━━━ 为什么值得单独攒这一份 ━━━
逐笔成交带（tape）告诉你**成交了什么**，持仓量告诉你**沉淀下来多少**。
今天 OI 减昨天 OI = **净新增头寸** —— 这是"有人在建仓"最硬的证据，
而且**不需要猜方向**（不像 UW 的 bullish/bearish 标签要靠 aggressor side）。

⚠️ **这份数据补不回来。** CBOE 只给当下的链，昨天的拿不到。
所以它和 EDGAR/FINRA 那几条线的性质完全不同：那些能回填，这条**只能靠自己攒**，
从装上 Vibe-Flow 的那天开始。这也正是 UW 拿来单独卖钱的东西。

━━━ ⚠️ 两个必须记住的口径 ━━━

1. **OI 是隔夜结算数**，反映**昨日收盘**的持仓，不含今天新开的仓。
   所以"今天的快照"里那个 OI，实际是**前一交易日**的状态。
   本表按 `snapshot_date`（拉取当天）存，读的时候要知道这层滞后。

2. **两条相邻记录之间未必是一个交易日。** 用户可能周一装上、周五才又打开。
   所以差值必须带上"跨了几天"，**不能默认它是日变化** ——
   把跨周的变动说成"今日新增"，是凭空造出一个不存在的数字。
"""
from __future__ import annotations

from typing import Iterable, Optional

from modules import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS oi_snapshot (
    ticker        TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,          -- YYYY-MM-DD（拉取当天，美东）
    expiry        TEXT NOT NULL,
    type          TEXT NOT NULL,          -- call | put
    strike        REAL NOT NULL,
    open_interest REAL NOT NULL,
    volume        REAL NOT NULL,
    spot          REAL,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (ticker, snapshot_date, expiry, type, strike)
);
CREATE INDEX IF NOT EXISTS ix_oi_ticker_date ON oi_snapshot(ticker, snapshot_date);
"""


def _ensure() -> None:
    db.ensure_schema("oi_snapshot", SCHEMA)


def record(ticker: str, snapshot_date: str, spot: float,
           rows: Iterable[dict]) -> int:
    """记一份当日快照（同一天重复拉取会覆盖，不会灌重）。

    `rows` 用 `flow.to_dict()` 的形状。
    """
    _ensure()
    now = _now()
    # ⚠️ 不写 `r["open_interest"] or 0` —— 上游若真给了 None，
    #    那是"取不到"，把它永久落库成 0 会在明天变成一笔凭空的持仓变化。
    #    这两个字段在 `Contract` 里是非可空的 float，真出现 None 说明
    #    上游解析出了问题，**应该当场炸掉**，而不是悄悄记个 0。
    payload = []
    for r in rows:
        oi, vol = r["open_interest"], r["volume"]
        if oi is None or vol is None:
            raise ValueError(
                f"{ticker} {r['expiry']} {r['type']} {r['strike']} 的持仓量/成交量为空 —— "
                f"这是**取不到**，不能当成 0 记进历史。请检查数据源解析。")
        payload.append((ticker, snapshot_date, r["expiry"], r["type"],
                        float(r["strike"]), float(oi), float(vol), spot, now))
    if not payload:
        return 0
    # ⚠️ **整段替换，不是逐行 merge。**
    # 只用 `INSERT OR REPLACE` 的话，本次没带上的旧行会原样留着 ——
    # 于是同一个交易时段下混着两次抓取的结果（比如先按 dte_max=7 存过、
    # 再存全链），差值算的是"两个不同口径的拼接"。
    # 放在一个事务里做，中途失败不会留下空表。
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM oi_snapshot WHERE ticker=? AND snapshot_date=?",
                     (ticker, snapshot_date))
        conn.executemany(
            "INSERT OR REPLACE INTO oi_snapshot"
            "(ticker, snapshot_date, expiry, type, strike, open_interest,"
            " volume, spot, captured_at) VALUES (?,?,?,?,?,?,?,?,?)", payload)
        conn.execute("COMMIT")
    return len(payload)


def dates(ticker: str) -> list[dict]:
    """该标的已攒下的快照日（新到旧）。"""
    _ensure()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT snapshot_date, COUNT(*) contracts, MAX(spot) spot, "
            "MAX(captured_at) captured_at FROM oi_snapshot WHERE ticker = ? "
            "GROUP BY snapshot_date ORDER BY snapshot_date DESC", (ticker,))]


def oi_change(ticker: str, date_to: Optional[str] = None,
              date_from: Optional[str] = None, top: int = 40) -> dict:
    """两个快照日之间的持仓量变化。

    不传日期则取**最近两个**快照日。
    ⚠️ 只攒到一天时返回 `enough=False` —— 那是「还没攒够」，
    **不是「持仓没变化」**，这两件事在界面上必须长得不一样。
    """
    _ensure()
    ds = [d["snapshot_date"] for d in dates(ticker)]
    if len(ds) < 2:
        return {"enough": False, "have": len(ds), "dates": ds,
                "note": ("持仓量变化需要**至少两个**快照日。"
                         "这份历史补不回来（CBOE 只给当下），"
                         "从装上那天起每天打开一次就会攒起来。")}
    to_d = date_to or ds[0]
    # ⚠️ **传进来的日期必须真的在库里。** `_load()` 查不到只会返回空字典，
    #    差值就把"这天没存过"算成"这天持仓全是 0"，
    #    输出一份"全量清仓"或"全量建仓"的假报告。
    if to_d not in ds:
        return {"enough": False, "have": len(ds), "dates": ds,
                "note": (f"本地没有 {to_d} 的快照（已有：{'、'.join(ds[:8])}）。"
                         f"这是**没存过这一天**，不是那天持仓为零。")}
    if date_from:
        from_d = date_from
        if from_d not in ds:
            return {"enough": False, "have": len(ds), "dates": ds,
                    "note": (f"本地没有 {from_d} 的快照（已有：{'、'.join(ds[:8])}）。"
                             f"这是**没存过这一天**，不是那天持仓为零。")}
        if from_d >= to_d:
            return {"enough": False, "have": len(ds), "dates": ds,
                    "note": f"起始日 {from_d} 必须早于结束日 {to_d}。"}
    else:
        earlier = [d for d in ds if d < to_d]
        if not earlier:
            return {"enough": False, "have": len(ds), "dates": ds,
                    "note": f"{to_d} 之前没有更早的快照。"}
        from_d = earlier[0]

    # ⚠️ **刻意在 Python 里做全外连接，而不是写 `FULL OUTER JOIN`。**
    # SQLite 直到 3.39（2022）才支持它。本机是 3.53，但用户自部署时用的是
    # 他们自己 Python 里那个 sqlite3 —— macOS 系统 Python 至今还带着 3.3x。
    # 一句"在我机器上能跑"就能让整个自部署承诺落空，而这里的量级
    # （单标的单日几千行）在内存里合并毫无压力。
    #
    # ⚠️ 两天的数据用**一条 SQL 一次读完**，不是先后两次查。
    #    分两次（哪怕共用连接）中间仍可能夹进一次归档提交，
    #    比较的就成了两个不同版本的库 —— 得到的差值不对应任何一个真实时刻。
    #    单条语句在 SQLite 里天然是原子读，不需要显式事务。
    cur: dict = {}
    prev: dict = {}
    with db.connect() as conn:
        for r in conn.execute(
                "SELECT snapshot_date, expiry, type, strike, open_interest, volume "
                "FROM oi_snapshot WHERE ticker=? AND snapshot_date IN (?, ?)",
                (ticker, to_d, from_d)):
            bucket = cur if r["snapshot_date"] == to_d else prev
            bucket[(r["expiry"], r["type"], r["strike"])] = dict(r)
    rows = []
    expired = 0
    expired_oi = 0.0
    incomplete = 0
    incomplete_oi = 0.0
    new_listings = 0
    for key in cur.keys() | prev.keys():
        expiry, typ, strike = key
        a, b = cur.get(key), prev.get(key)
        if a is None:
            # ⚠️ **到期消失 ≠ 净平仓。** 周五到期的合约下周就不在链里了，
            #    把它算成"持仓归零"，会凭空造出一大笔"净减持"，
            #    而实际上没有任何人平仓 —— 它只是到期了。
            if expiry < to_d:
                expired += 1
                expired_oi += float(b["open_interest"]) if b else 0.0
                continue
            # ⚠️ **还没到期却不在结束快照里 = 那次快照不完整。**
            #    CBOE 会把合约一直挂到到期为止，持仓归零也照样列出来（OI=0）。
            #    所以"未到期 + 缺席"只能说明抓取那次漏了它 ——
            #    当成 OI=0 就会伪造出一笔并不存在的全额平仓。
            #    这是「取不到」，不是「没有」，必须单列并排除。
            incomplete += 1
            incomplete_oi += float(b["open_interest"]) if b else 0.0
            continue
        if b is None:
            # ⚠️ 结束快照里有、起始快照里没有 —— 这里**有真实的二义性**：
            #    ① 期间新挂出来的行权价（确实从 0 开始，算进增持是对的）
            #    ② 起始那次抓取漏了它（那它根本不是新增，算进去就是虚增）
            #    两种情况在数据里长得**完全一样**，分不出来。
            #    结束侧那边能分（未到期还缺席只可能是漏抓，因为 CBOE 挂到到期为止），
            #    起始侧分不了 —— 所以照常计入，但把数量单独报出来，
            #    并在文案里说清这层不确定，不假装它一定是新挂牌。
            new_listings += 1
        oi_to = float(a["open_interest"]) if a else 0.0
        oi_from = float(b["open_interest"]) if b else 0.0
        change = oi_to - oi_from
        rows.append({
            "expiry": expiry, "type": typ, "strike": strike,
            "oi_from": oi_from, "oi_to": oi_to,
            "volume_to": float(a["volume"]) if a else 0.0,
            "change": change,
            # 从 0 涨起来算不出百分比 —— 返回 None，别塞个假的 100%
            "change_pct": None if oi_from <= 0 else change / oi_from * 100.0,
        })
    gained = sorted((r for r in rows if r["change"] > 0),
                    key=lambda r: -r["change"])[:top]
    lost = sorted((r for r in rows if r["change"] < 0),
                  key=lambda r: r["change"])[:top]
    span = _daydiff(from_d, to_d)
    # ⚠️ 「相邻」判定要看**中间有没有漏掉快照**，不能只看自然日差。
    #    周二 → 周五也是 3 天，但中间漏了两次观测；而周五 → 周一虽然也是 3 天，
    #    却确实是相邻的两个交易时段。库里的顺序才是真相。
    idx_from, idx_to = ds.index(from_d), ds.index(to_d)
    adjacent = (idx_from - idx_to) == 1        # ds 是新到旧
    return {
        "enough": True, "dates": ds, "date_from": from_d, "date_to": to_d,
        "span_days": span,
        # 跨了几天必须说 —— 用户可能周一装上周五才打开，
        # 那几天的累计变动写成"今日新增"就是凭空造数字。
        "is_consecutive": adjacent,
        "snapshots_between": max(0, idx_from - idx_to - 1),
        "expired_excluded": expired,
        "expired_oi": expired_oi,
        # 未到期却缺席 = 那次快照不完整。数量大就说明这次比较不可信。
        "incomplete_excluded": incomplete,
        "incomplete_oi": incomplete_oi,
        "new_listings": new_listings,
        # 起始快照里没有、结束快照里有的合约数。**新挂牌与漏抓在数据上无法区分**，
        # 这个数字大到不像正常新挂牌时，说明起始那次抓取可能不完整。
        "new_listings_ambiguous": True,
        # 两次快照各自的合约数：差得离谱就是有一次抓漏了
        "contracts_from": len(prev),
        "contracts_to": len(cur),
        "totals": {
            "call_change": sum(r["change"] for r in rows if r["type"] == "call"),
            "put_change": sum(r["change"] for r in rows if r["type"] == "put"),
            "contracts": len(rows),
        },
        "gained": gained, "lost": lost,
        "note": ("持仓量是**隔夜结算数**：这里比较的是两个快照各自看到的"
                 "「前一交易日收盘持仓」。增加=净新开仓，减少=净平仓，"
                 "**但都不指示方向**（每张合约都有买卖两方，"
                 "净新增的多头和空头一样多）。"
                 + (f"已排除 {expired} 个**在此期间到期**的合约"
                    f"（合计 {expired_oi:,.0f} 张持仓）—— 它们从链里消失是因为到期，"
                    f"不是有人平仓，算进去会凭空造出一大笔净减持。"
                    if expired else "")
                 + (f"⚠️ 另有 {incomplete} 个**尚未到期却不在结束快照里**的合约"
                    f"（{incomplete_oi:,.0f} 张持仓）已排除 —— CBOE 会把合约挂到到期为止，"
                    f"未到期还缺席只能说明那次抓取不完整，这是**取不到**不是**归零**。"
                    if incomplete else "")
                 + (f"⚠️ 有 {new_listings} 个合约只出现在结束快照里，按「从 0 新增」计入。"
                    f"**期间新挂牌**与**起始那次漏抓**在数据上无法区分，"
                    f"这个数字异常大时应当怀疑起始快照不完整"
                    f"（两次合约数 {len(prev):,} → {len(cur):,}）。"
                    if new_listings else "")),
    }


def stats() -> dict:
    _ensure()
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, "
            "COUNT(DISTINCT snapshot_date) d, MIN(snapshot_date) lo, "
            "MAX(snapshot_date) hi FROM oi_snapshot").fetchone()
    return {"rows": r["n"], "tickers": r["t"], "days": r["d"],
            "earliest": r["lo"], "latest": r["hi"]}


def _daydiff(a: str, b: str) -> Optional[int]:
    from datetime import date
    try:
        pa = date(*map(int, a.split("-")))
        pb = date(*map(int, b.split("-")))
    except (ValueError, TypeError):
        return None
    return (pb - pa).days


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
