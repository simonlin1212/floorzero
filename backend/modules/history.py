"""本地历史沉淀。

━━━ 为什么需要这个 ━━━
Unusual Whales 的真护城河不是数据独家（源头几乎都免费公开），
而是**他们跑了很多年、攒下了历史**——历史数据还单独卖钱。

用户自部署 FloorZero 的第一天是**零历史**，这一点必须对用户诚实。
本模块的作用是：**装上就开始积累**，越用越值钱。

⚠️ 补不回来的部分：期权链的历史快照。CBOE 只给当下，过去的拿不到。
    能回补的：EDGAR / FINRA 本身带历史（那两条线以后可以回填）。

━━━ 存储 ━━━
SQLite，零配置，默认落在用户目录 `~/.floorzero/history.db`
（不放仓库内：更新代码/重新 clone 不该弄丢用户攒的历史 ——
 VibeResearch 的 issue #12 就是这个坑）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from modules import db
from modules.db import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gex_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    captured_at TEXT    NOT NULL,          -- ISO8601 UTC
    scope       TEXT    NOT NULL,          -- 口径：不同 scope 的快照不可混比
    spot        REAL    NOT NULL,
    total_gex   REAL    NOT NULL,          -- 名义美元 / 1%
    gamma_flip  REAL,
    call_wall   REAL,
    put_wall    REAL,
    regime      TEXT    NOT NULL,
    total_vanna REAL,
    total_charm REAL
);
-- 同一标的+口径下按时间查，是最主要的读法
CREATE INDEX IF NOT EXISTS idx_gex_ticker_time
    ON gex_snapshot (ticker, scope, captured_at);
-- 幂等：同一标的/口径/时间点只留一条，重复采集不会灌重
CREATE UNIQUE INDEX IF NOT EXISTS idx_gex_unique
    ON gex_snapshot (ticker, scope, captured_at);
"""


def _ensure_db() -> None:
    db.ensure_schema("gex_snapshot", _SCHEMA)


def _conn():
    _ensure_db()
    return db.connect()


def record_gex(profile_dict: dict, exposures: Optional[dict] = None,
               captured_at: Optional[str] = None) -> bool:
    """记录一次 GEX 快照。返回 True=新写入，False=该数据时点已存在。

    ⚠️ **`captured_at` 要传数据自身的时间（`chain.timestamp`），不是墙上时钟。**
    CBOE 的延时行情每隔一段才更新一次；用 `now()` 当主键的话，
    连点两次按钮会存进两行**数据完全相同、时间戳不同**的记录，
    画进时间序列就是凭空多出来的「观测点」——看着像行情动了，其实一动没动。
    按数据时点入库后，(ticker, scope, captured_at) 唯一索引才真正挡得住重复：
    只有上游确实发布了新数据才会新增一行。

    退化情况：上游没给 timestamp 时才回退到 UTC 墙钟（此时幂等性不保证）。
    """
    ts = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = profile_dict["meta"]
    # ⭐ 必须用 scope_key（不含合约数）而不是展示用的 scope：
    # 合约数每天都变，混进主键会让明天的快照落进另一个 scope，序列永远攒不起来。
    scope_key = meta.get("scope_key") or meta["scope"]
    row = (
        profile_dict["ticker"], ts, scope_key,
        profile_dict["spot"], profile_dict["total_gex_bn"] * 1e9,
        profile_dict.get("gamma_flip"), profile_dict.get("call_wall"),
        profile_dict.get("put_wall"), profile_dict["regime"],
        (exposures or {}).get("total_vanna_mm"),
        (exposures or {}).get("total_charm_mm"),
    )
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO gex_snapshot "
            "(ticker, captured_at, scope, spot, total_gex, gamma_flip, call_wall, "
            " put_wall, regime, total_vanna, total_charm) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        return cur.rowcount > 0


def gex_series(ticker: str, scope: Optional[str] = None,
               limit: int = 200) -> list[dict[str, Any]]:
    """取某标的的历史序列（新→旧）。

    ⚠️ scope 必须参与筛选：`≤7DTE` 与 `全链` 的 GEX 不是一个量级，
    混在一张图里会画出毫无意义的锯齿。
    """
    sql = "SELECT * FROM gex_snapshot WHERE ticker = ?"
    args: list[Any] = [ticker.upper()]
    if scope:
        sql += " AND scope = ?"
        args.append(scope)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def scopes_for(ticker: str) -> list[str]:
    """该标的已积累了哪些口径的历史（供前端选择，避免混比）。"""
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT scope FROM gex_snapshot WHERE ticker = ? ORDER BY scope",
            (ticker.upper(),))]


def stats() -> dict:
    """库存概览 —— 让用户看到「自己攒了多少」。"""
    with _conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, "
            "MIN(captured_at) lo, MAX(captured_at) hi FROM gex_snapshot").fetchone()
    return {
        "snapshots": r["n"], "tickers": r["t"],
        "earliest": r["lo"], "latest": r["hi"],
        "db_path": DB_PATH,
        "note": "期权链历史无法回补（CBOE 只给当下），装上后才开始积累。",
    }
