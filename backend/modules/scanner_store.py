"""扫描器的本地沉淀：逐日的轻量行情快照。

━━━ 为什么必须攒 ━━━
扫描器最核心的指标是 **IV Rank**，而它按定义就是"当前 IV 在过去一年区间里的位置" ——
没有历史就没有这个指标。CBOE 只给当下的 `iv30`，**过去的拿不到**。

所以这张表和 `flow_store` 的 OI 是同一个性质：**装上才开始有，补不回来**。
差别是它更划算 —— 一行只有 6 个数字，6,000 只标的攒一年也就百万行量级。

━━━ ⚠️ 按交易时段归档，不是墙上日期 ━━━
同 `flow_store`：CBOE 的 `last_trade_time` 才是数据所属的交易时段。
周末连开两天页面，按墙上日期会存成"两天"，IV 样本被灌进重复值 ——
样本数虚高，而 IV Rank 直接建立在样本数上。
"""
from __future__ import annotations

from typing import Iterable, Optional

from modules import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_snapshot (
    symbol        TEXT NOT NULL,
    session       TEXT NOT NULL,        -- YYYY-MM-DD，数据所属交易时段
    price         REAL,
    change_pct    REAL,
    volume        REAL,
    iv30          REAL,
    security_type TEXT,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, session)
);
CREATE INDEX IF NOT EXISTS ix_q_session ON quote_snapshot(session);
CREATE INDEX IF NOT EXISTS ix_q_symbol  ON quote_snapshot(symbol, session);

-- 每轮扫描的批次记录：跑了多久、扫了多少、失败多少。
-- ⚠️ 失败数必须留痕：一轮扫描里若有 800 只取不到，
--    结果表看上去只是"少了些票"，不留痕就永远发现不了。
CREATE TABLE IF NOT EXISTS scan_batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    universe    INTEGER NOT NULL,
    scanned     INTEGER NOT NULL,
    stored      INTEGER NOT NULL,
    failed      INTEGER NOT NULL,
    session     TEXT,
    note        TEXT
);
"""


def _ensure() -> None:
    db.ensure_schema("quote_snapshot", SCHEMA)


def record_quotes(rows: Iterable[dict]) -> dict:
    """写入一批轻量行情（按 symbol+session 幂等）。

    ⚠️ 没有 `session` 的行**丢弃并计数**，不拿今天的日期顶上：
    归档键错了，IV 样本就串了，而串掉之后从数据里看不出来。
    **丢了几条必须返回** —— 上一版算了 `dropped` 却没返回，
    于是"上游没给交易时段"这件事在界面上完全看不见，
    表现成"那些标的不在扫描结果里"，正是把「取不到」伪装成「没有」。

    ⚠️ **不用裸 `INSERT OR REPLACE`。** 同一交易时段重扫时，若上游这次
    临时没给 `iv30`，裸覆盖会把已经攒到的有效值清成 NULL ——
    样本数不增反减，而这份历史**补不回来**。所以对可空的三个字段用
    `COALESCE(新值, 旧值)`：有新值就更新，没有就保留旧的。
    """
    _ensure()
    now = _now()
    payload = []
    dropped = 0
    for r in rows:
        sess = r.get("session")
        if not sess:
            dropped += 1
            continue
        payload.append((r["symbol"], sess, r.get("price"), r.get("change_pct"),
                        r.get("volume"), r.get("iv30"),
                        r.get("security_type"), now))
    if payload:
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO quote_snapshot"
                "(symbol, session, price, change_pct, volume, iv30,"
                " security_type, captured_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol, session) DO UPDATE SET "
                "  price         = COALESCE(excluded.price,         price),"
                "  change_pct    = COALESCE(excluded.change_pct,    change_pct),"
                "  volume        = COALESCE(excluded.volume,        volume),"
                "  iv30          = COALESCE(excluded.iv30,          iv30),"
                "  security_type = COALESCE(excluded.security_type, security_type),"
                "  captured_at   = excluded.captured_at", payload)
    return {"stored": len(payload), "dropped_no_session": dropped}


def history(symbols: Optional[list[str]] = None,
            lookback: int = 252,
            as_of: Optional[str] = None) -> dict[str, dict]:
    """取每只标的的 iv30 / volume 历史（按时段升序）。

    返回 `{symbol: {"iv": [...], "volume": [...]}}`。

    ⚠️ **`as_of` 必须传。** 看历史某一天的扫描结果时，若把之后的数据
    也算进 IV Rank 的样本里，就是**前视偏差** —— 用还没发生的行情
    去判断当天 IV 是高是低。极端情况下当前值会落在样本区间外，
    算出来的排名甚至超出 0-100。
    ⚠️ 一次查完再在内存里分组，**不是逐只查** —— 6,000 只逐只查
    等于 6,000 次往返，扫描页会卡到没法用。
    """
    _ensure()
    out: dict[str, dict] = {}
    where = []
    args_list: list = []
    if symbols:
        where.append(f"symbol IN ({','.join('?' * len(symbols))})")
        args_list += list(symbols)
    if as_of:
        where.append("session <= ?")
        args_list.append(as_of)
    sql = "SELECT symbol, session, iv30, volume FROM quote_snapshot"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY symbol, session"
    args = tuple(args_list)
    with db.connect() as conn:
        for r in conn.execute(sql, args):
            d = out.setdefault(r["symbol"], {"iv": [], "volume": []})
            # ⚠️ 空值**跳过**而不是补 0：补 0 会把 IV 区间的下沿拉到 0，
            #    IV Rank 于是永远显示很高。
            if r["iv30"] is not None:
                d["iv"].append(float(r["iv30"]))
            if r["volume"] is not None:
                d["volume"].append(float(r["volume"]))
    for d in out.values():
        d["iv"] = d["iv"][-lookback:]
        d["volume"] = d["volume"][-lookback:]
    return out


def latest_session() -> Optional[str]:
    _ensure()
    with db.connect() as conn:
        r = conn.execute("SELECT MAX(session) s FROM quote_snapshot").fetchone()
    return r["s"] if r and r["s"] else None


def quotes_at(session: str) -> list[dict]:
    """截至某交易时段，**每只标的各自最新的一条**行情。

    ⚠️ **不是 `WHERE session = ?`。** 一轮扫描里各标的的 `last_trade_time`
    未必相同（停牌、上游延迟更新、扫描跨了交易时段都会造成这种情况）。
    只取等于最新时段的那批，会把另一批**成功入库的**标的整个隐掉 ——
    界面上表现成"它们不在扫描结果里"，而它们只是时段旧一天。

    这里按"≤ 目标时段的最新一条"取，并把各行自己的 session 一起返回，
    对不上的时候用户能一眼看见。
    """
    _ensure()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT q.symbol, q.session, q.price, q.change_pct, q.volume, "
            "       q.iv30, q.security_type "
            "FROM quote_snapshot q "
            "JOIN (SELECT symbol, MAX(session) AS mx FROM quote_snapshot "
            "      WHERE session <= ? GROUP BY symbol) m "
            "  ON q.symbol = m.symbol AND q.session = m.mx", (session,))]


def start_batch(universe: int) -> int:
    _ensure()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO scan_batch(started_at, universe, scanned, stored, failed)"
            " VALUES (?,?,0,0,0)", (_now(), universe))
        return int(cur.lastrowid or 0)


def finish_batch(batch_id: int, scanned: int, stored: int, failed: int,
                 session: Optional[str], note: str = "") -> None:
    _ensure()
    with db.connect() as conn:
        conn.execute(
            "UPDATE scan_batch SET finished_at=?, scanned=?, stored=?, failed=?,"
            " session=?, note=? WHERE id=?",
            (_now(), scanned, stored, failed, session, note, batch_id))


def batches(limit: int = 10) -> list[dict]:
    _ensure()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scan_batch ORDER BY id DESC LIMIT ?", (limit,))]


def stats() -> dict:
    _ensure()
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            "COUNT(DISTINCT session) sessions, MIN(session) lo, MAX(session) hi "
            "FROM quote_snapshot").fetchone()
        # 攒够 IV Rank 的标的有几只 —— 这是"这一栏现在有多可用"的直接答案
        ready = conn.execute(
            "SELECT COUNT(*) n FROM (SELECT symbol FROM quote_snapshot "
            "WHERE iv30 IS NOT NULL GROUP BY symbol HAVING COUNT(*) >= ?)",
            (60,)).fetchone()
    return {"rows": r["n"], "symbols": r["syms"], "sessions": r["sessions"],
            "earliest": r["lo"], "latest": r["hi"],
            "iv_ready_symbols": ready["n"]}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
