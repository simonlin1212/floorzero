"""做空数据的本地存储与同步。

单个半月档约 6 万行 FTD，很轻 —— 但仍走流式 + 暂存切换，
与 13F 保持同一套习惯（这个项目已经因为「整表驻留」和「先删后写」各踩过一次）。
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any, Optional

from modules import db
from modules import shorts as parse
from sources import shorts as src

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ftd (
    settlement_date TEXT NOT NULL,
    cusip        TEXT NOT NULL,
    symbol       TEXT,
    description  TEXT,
    quantity     REAL,          -- ⚠️ 累计余额，不是当日新增（见 shorts.OFFICIAL_NOTES）
    price        REAL,          -- 前一日收盘价，SEC 不保证与他处一致
    value        REAL,
    tag          TEXT NOT NULL, -- 半月档标识，如 202606b
    PRIMARY KEY (settlement_date, cusip)
);
CREATE INDEX IF NOT EXISTS idx_ftd_symbol ON ftd (symbol, settlement_date);
CREATE INDEX IF NOT EXISTS idx_ftd_date   ON ftd (settlement_date);

CREATE TABLE IF NOT EXISTS ftd_staging (
    settlement_date TEXT NOT NULL,
    cusip        TEXT NOT NULL,
    symbol       TEXT,
    description  TEXT,
    quantity     REAL,
    price        REAL,
    value        REAL,
    tag          TEXT NOT NULL,
    PRIMARY KEY (settlement_date, cusip)
);

CREATE TABLE IF NOT EXISTS ftd_batch (
    tag        TEXT PRIMARY KEY,
    rows       INTEGER NOT NULL,
    symbols    INTEGER,
    date_from  TEXT,
    date_to    TEXT,
    synced_at  TEXT NOT NULL
);
"""

_COLS = ("settlement_date", "cusip", "symbol", "description",
         "quantity", "price", "value", "tag")


def _init() -> None:
    db.ensure_schema("shorts", _SCHEMA)


def _tuple(r: dict, tag: str) -> tuple:
    return (r["settlement_date"], r["cusip"], r["symbol"], r["description"],
            r["quantity"], r["price"], r["value"], tag)


def clear_staging(tag: Optional[str] = None) -> None:
    _init()
    with db.connect() as conn:
        if tag:
            conn.execute("DELETE FROM ftd_staging WHERE tag = ?", (tag,))
        else:
            conn.execute("DELETE FROM ftd_staging")


def save_staging(rows: list[dict], tag: str) -> int:
    _init()
    if not rows:
        return 0
    sql = (f"INSERT OR REPLACE INTO ftd_staging ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        conn.executemany(sql, [_tuple(r, tag) for r in rows])
    return len(rows)


def commit_staging(tag: str) -> int:
    """原子切换：删旧档 → 暂存转正。

    ⚠️ 与 13F 同一套做法：**不能先删后写** ——
    导入中途失败会留下空的或半份数据，而批次元数据还写着旧条数。
    """
    _init()
    cols = ",".join(_COLS)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM ftd_staging WHERE tag = ?",
                         (tag,)).fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ftd WHERE tag = ?", (tag,))
        conn.execute(f"INSERT OR REPLACE INTO ftd ({cols}) "
                     f"SELECT {cols} FROM ftd_staging WHERE tag = ?", (tag,))
        conn.execute("DELETE FROM ftd_staging WHERE tag = ?", (tag,))
        conn.execute("COMMIT")
        return n


def mark_batch(tag: str, rows: int, symbols: int,
               date_from: Optional[str], date_to: Optional[str]) -> None:
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ftd_batch "
            "(tag, rows, symbols, date_from, date_to, synced_at) VALUES (?,?,?,?,?,?)",
            (tag, rows, symbols, date_from, date_to,
             datetime.now().isoformat(timespec="seconds")))


def known_tags() -> list[str]:
    _init()
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT tag FROM ftd_batch ORDER BY tag DESC")]


def _where(symbol: Optional[str] = None, since: Optional[str] = None,
           settlement_date: Optional[str] = None,
           min_quantity: Optional[float] = None,
           alias: str = "") -> tuple[str, list]:
    """筛选条件 —— 明细与聚合共用，避免两边口径漂移。"""
    p = f"{alias}." if alias else ""
    sql, args = "WHERE 1=1", []
    if symbol:
        sql += f" AND {p}symbol = ?"; args.append(symbol.upper())
    if settlement_date:
        sql += f" AND {p}settlement_date = ?"; args.append(settlement_date)
    if since:
        sql += f" AND {p}settlement_date >= ?"; args.append(since)
    if min_quantity:
        sql += f" AND {p}quantity >= ?"; args.append(min_quantity)
    return sql, args


def query(limit: int = 200, **filters) -> list[dict]:
    _init()
    w, args = _where(**filters)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM ftd {w} ORDER BY settlement_date DESC, "
            f"value IS NULL, value DESC LIMIT ?", args + [limit])]


def aggregate(top: int = 20, **filters) -> dict:
    """SQL 全量聚合。

    ⚠️ 按标的汇总用的是**各结算日余额的平均**，不是加总 ——
    FTD 是某时点的累计余额，把多个交易日的余额相加没有意义
    （同一笔未交割会在连续多日重复出现）。
    """
    _init()
    w, a = _where(**filters)
    with db.connect() as conn:
        tot = conn.execute(
            f"SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            f" COUNT(DISTINCT settlement_date) days, "
            f" MIN(settlement_date) lo, MAX(settlement_date) hi "
            f"FROM ftd {w}", a).fetchone()
        by_symbol = [dict(r) for r in conn.execute(
            f"SELECT symbol, MAX(description) description, MAX(cusip) cusip, "
            f" COUNT(*) days, "
            f" AVG(quantity) avg_quantity, MAX(quantity) max_quantity, "
            f" AVG(value) avg_value, MAX(value) max_value "
            f"FROM ftd {w} AND symbol IS NOT NULL AND symbol != '' "
            # ⚠️ 按**股数**排序，因为图上画的柱长和文案说的「余额最大」都是股数。
            # 早前按 avg_value（金额）排，结果 GOOG 1,784 万股排在 XOM 2,668 万股前面
            # —— 排序依据与所见不符。金额作为标注显示，不做主排序键。
            f"GROUP BY symbol ORDER BY avg_quantity IS NULL, avg_quantity DESC LIMIT ?",
            a + [top])]
        by_date = [dict(r) for r in conn.execute(
            f"SELECT settlement_date, COUNT(*) symbols, SUM(quantity) total_quantity, "
            f" SUM(value) total_value FROM ftd {w} "
            f"GROUP BY settlement_date ORDER BY settlement_date", a)]
    return {"counts": dict(tot), "by_symbol": by_symbol, "by_date": by_date}


def stats() -> dict:
    _init()
    with db.connect() as conn:
        t = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            "COUNT(DISTINCT settlement_date) days, "
            "MIN(settlement_date) lo, MAX(settlement_date) hi FROM ftd").fetchone()
        b = [dict(r) for r in conn.execute(
            "SELECT * FROM ftd_batch ORDER BY tag DESC")]
    return {
        "rows": t["n"] or 0, "symbols": t["syms"] or 0, "days": t["days"] or 0,
        "earliest": t["lo"], "latest": t["hi"],
        "tags": [x["tag"] for x in b], "batches": b,
        "last_sync": b[0]["synced_at"] if b else None,
        "db_path": db.DB_PATH,
        "finra_enabled": src.finra_enabled(),
        "note": "FTD 是**某结算日的累计余额**（不是当日新增），"
                "且 SEC 明说它不是裸卖空的证据 —— 详见页面上的口径说明。",
    }


# ─────────────────────────── 同步 ───────────────────────────

class SyncState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.rows = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []

    def _snapshot_locked(self) -> dict:
        """⚠️ 调用方须已持锁（Lock 不可重入，持锁再调 snapshot() 会死锁）。"""
        return {"running": self.running, "stage": self.stage, "rows": self.rows,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "errors": self.errors[-10:], "error_count": len(self.errors)}

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)


def _sync_tag(tag: str) -> int:
    clear_staging(tag)
    batch, total, symbols = [], 0, set()
    lo = hi = None
    for raw in src.ftd_rows(tag):
        f = parse.parse_ftd(raw)
        if f is None:
            continue
        d = parse.to_dict(f)
        batch.append(d)
        if f.symbol:
            symbols.add(f.symbol)
        ds = d["settlement_date"]
        lo = ds if lo is None or ds < lo else lo
        hi = ds if hi is None or ds > hi else hi
        if len(batch) >= 20_000:
            total += len(batch)
            save_staging(batch, tag)
            batch = []
    if batch:
        total += len(batch)
        save_staging(batch, tag)
    if not total:
        clear_staging(tag)
        return 0
    commit_staging(tag)                    # 到这里才动旧数据
    mark_batch(tag, rows=total, symbols=len(symbols), date_from=lo, date_to=hi)
    return total


def _run(candidates: list[str], want: int) -> None:
    """按候选档从新到旧尝试，**直到真的导入了 `want` 个档**。

    ⚠️ 不能"取最近 N 档就完事"：最新一两档 SEC 常常还没发布
    （上半月的档月底才发、下半月的次月 15 号左右才发）。
    照旧写法，默认 back=2 在月初会**一条都导不进来**，
    而且每次重试都是同一批 404 —— 用户点了按钮什么也拿不到，还看不出为什么。
    """
    got = 0
    try:
        for tag in candidates:
            if got >= want:
                break
            STATE.stage = f"导入 FTD {tag}"
            try:
                n = _sync_tag(tag)
                with STATE.lock:
                    STATE.rows += n
                if n:
                    got += 1
                else:
                    _err(f"{tag}: 文件里没有可解析的记录")
            except src.DataNotAvailable as e:
                # SEC 还没发这档 —— 不是错误，继续往更早的档试
                _err(f"{tag}: {e}（继续向更早的档回退）")
            except Exception as e:
                _err(f"{tag}: {type(e).__name__}: {e}")
        STATE.stage = ("完成" if got else
                       "完成（未导入任何档 —— 候选范围内 SEC 都还没发布或已导入过）")
    except Exception as e:
        STATE.stage = f"中断：{type(e).__name__}: {e}"
        _err(f"同步中断：{type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(back: int = 2) -> dict:
    """导入最近 N 个半月档（跳过已导入的）。"""
    with STATE.lock:
        if STATE.running:
            return {"started": False, "reason": "已有同步在进行中",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.rows = 0
        STATE.errors = []
        STATE.stage = "启动中"
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    want = max(1, min(back, 12))
    known = set(known_tags())
    # 候选放宽到 want + 6 档：最新几档常常还没发布，要留够回退余量
    tags = [t for t in src.ftd_files(want + 6) if t not in known]
    if not tags:
        with STATE.lock:
            STATE.running = False
            STATE.stage = "已是最新（无新档可导）"
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")
        return {"started": False, "reason": "最近的档都已导入", **STATE.snapshot()}

    threading.Thread(target=_run, args=(tags, want), daemon=True).start()
    return {"started": True, **STATE.snapshot()}
