"""收益率曲线的本地缓存。

━━━ 为什么这条线要落库，而别的"读一下就走"的源不用 ━━━
Treasury 的年度 XML **每年一份、单次 8 秒**（实测），取 3 年就是 29 秒。
而这份数据有个别的源没有的性质：**往年的值永远不会再变** ——
2024 年的收益率曲线今天读和十年后读是同一份东西。
每开一次页面重付 29 秒，买回来的是完全一样的数字。

所以这里的缓存策略是**按年判定新鲜度**，不是统一 TTL：

- **往年**：只要那年的记录数看着完整（≥200 个交易日），就**永不再拉**。
- **今年**：还在长，按 `MAX(date)` 判断 —— 落后于"今天往前数 4 天"就重拉。
  4 天是为了容下周末 + 假日：周日打开页面时最新一条是周五的，
  这很正常，不该因此每次都重拉。

⚠️ **不缓存"这一年我拉过了"这个事实，而是缓存数据本身。**
差别在于：Treasury 中途改了历史值（修订过往数据是会发生的），
重拉一次就会覆盖进来；而如果只记"拉过了"，就永远发现不了。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from modules import db
from sources.macro import TENORS

SCHEMA = """
CREATE TABLE IF NOT EXISTS treasury_yield (
    date        TEXT PRIMARY KEY,      -- YYYY-MM-DD
    year        INTEGER NOT NULL,
    yields      TEXT NOT NULL,         -- JSON: {BC_1MONTH: 4.3, ...}
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ty_year ON treasury_yield(year);
"""

#: 一年里"看着完整"的最少交易日数。美股一年约 250 个交易日，
#: 取 200 是留足假期与偶发缺报的余量 —— 宁可多拉一次，也不要把
#: 一份拉了一半就断掉的年份当成完整的钉死在库里。
_YEAR_COMPLETE = 200

#: 今年允许落后几天。周末 + 假日最长能连着 4 天没有新数据。
_STALE_DAYS = 4


def _ensure() -> None:
    db.ensure_schema("market", SCHEMA)


def year_status(years: Iterable[int], today: Optional[date] = None) -> dict[int, dict]:
    """每年在库里的状态：有多少天、最新一天是哪天、是否还需要去拉。"""
    _ensure()
    today = today or date.today()
    ys = sorted(set(years))
    if not ys:
        return {}
    out: dict[int, dict] = {
        y: {"rows": 0, "latest": None, "stale": True} for y in ys}
    with db.connect() as conn:
        q = ",".join("?" * len(ys))
        for r in conn.execute(
                f"SELECT year, COUNT(*) n, MAX(date) mx FROM treasury_yield "
                f"WHERE year IN ({q}) GROUP BY year", ys):
            out[r["year"]] = {"rows": r["n"], "latest": r["mx"], "stale": True}
    for y, st in out.items():
        if y < today.year:
            # 往年：够完整就永不再拉
            st["stale"] = st["rows"] < _YEAR_COMPLETE
        else:
            # 今年：看最新一条落后多少
            st["stale"] = (st["latest"] or "") < str(today - timedelta(days=_STALE_DAYS))
    return out


def save_year(year: int, rows: Iterable[dict]) -> int:
    """写入某年的全部日度曲线（按日期幂等覆盖）。"""
    import json

    _ensure()
    now = _now()
    payload = []
    for r in rows:
        d = (r.get("date") or "")[:10]
        if not d:
            continue
        ys = {k: r.get(k) for k in TENORS if r.get(k) is not None}
        if not ys:
            continue
        payload.append((d, year, json.dumps(ys, separators=(",", ":")), now))
    if not payload:
        return 0
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO treasury_yield(date, year, yields, fetched_at) "
            "VALUES (?,?,?,?)", payload)
    return len(payload)


def load_years(years: Iterable[int]) -> list[dict]:
    """读出这些年的全部曲线，按日期升序。返回 `sources.macro.yield_curve` 的行格式。"""
    import json

    _ensure()
    ys = sorted(set(years))
    if not ys:
        return []
    q = ",".join("?" * len(ys))
    out = []
    with db.connect() as conn:
        for r in conn.execute(
                f"SELECT date, yields FROM treasury_yield WHERE year IN ({q}) "
                f"ORDER BY date", ys):
            row: dict = {"date": r["date"]}
            stored = json.loads(r["yields"])
            for k in TENORS:
                row[k] = stored.get(k)
            out.append(row)
    return out


def stats() -> dict:
    """库里攒了多少。"""
    _ensure()
    with db.connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, MIN(date) lo, MAX(date) hi, "
            "COUNT(DISTINCT year) ys, MAX(fetched_at) f FROM treasury_yield"
        ).fetchone()
    return {"rows": r["n"], "earliest": r["lo"], "latest": r["hi"],
            "years": r["ys"], "last_fetch": r["f"]}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
