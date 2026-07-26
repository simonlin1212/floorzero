"""内部人交易的本地存储。

━━━ 为什么必须落库 ━━━
两条来源都不适合每次请求现拉：
- 季度数据集单季 10 万笔（下载解析 ~2.4 秒，但每次请求都做太浪费）
- 每日 XML 逐份抓，单日 645 份 = 645 次请求

落库后：季度导入一次管一个季度，每日增量只补新增申报。

⚠️ **两条来源会重叠**（季度数据集覆盖到季末，逐日抓取可能也抓了同一天）。
靠 `(accession, seq)` 唯一索引去重 —— 同一笔交易无论从哪条路进来只留一份。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from modules import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_trade (
    accession   TEXT NOT NULL,
    -- ⭐ 内容指纹，不是行序：两条导入路径的遍历顺序不保证一致，
    --    用行序当主键会让不同交易撞同一个键、静默丢数据（见 insider.trade_key）
    trade_key   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ticker      TEXT,
    company     TEXT,
    issuer_cik  TEXT,
    owner       TEXT,
    owner_cik   TEXT,
    is_officer  INTEGER,
    is_director INTEGER,
    is_ten_pct  INTEGER,
    officer_title TEXT,
    security    TEXT,
    tx_code     TEXT,
    tx_group    TEXT,               -- open_market / compensation / other
    direction   TEXT,               -- buy / sell / NULL（只有公开市场交易才有方向）
    tx_date     TEXT,
    filing_date TEXT,
    shares      REAL,
    price       REAL,
    value       REAL,
    acquired_disposed TEXT,
    shares_after REAL,
    is_direct   INTEGER,
    is_10b5_1   INTEGER,            -- NULL = 申报未勾选/旧格式
    form_type   TEXT NOT NULL DEFAULT '4',   -- 4 / 4/A
    delay_days  INTEGER,
    source_url  TEXT,
    source      TEXT NOT NULL,      -- dataset / daily
    PRIMARY KEY (accession, trade_key)
);
CREATE INDEX IF NOT EXISTS idx_ins_ticker ON insider_trade (ticker, tx_date);
CREATE INDEX IF NOT EXISTS idx_ins_date   ON insider_trade (tx_date);
CREATE INDEX IF NOT EXISTS idx_ins_group  ON insider_trade (tx_group, tx_date);
CREATE INDEX IF NOT EXISTS idx_ins_owner  ON insider_trade (owner);

-- 永远解析不了的单份申报（如 2003 年前的纯文本 Form 4，没有 XML）。
-- ⚠️ 不登记的话，一份读不了的申报会让**那一整天**永远无法标记完成，
-- 于是每次同步都要把那天 600-700 份申报全部重下一遍 —— 代价无上限。
-- 这与 Congress 分栏的「终态 vs 暂时」是同一类问题（那边已修，这边同形）。
CREATE TABLE IF NOT EXISTS insider_dead_filing (
    accession   TEXT PRIMARY KEY,
    day         TEXT,
    reason      TEXT,
    recorded_at TEXT NOT NULL
);

-- 已导入的来源批次（季度 / 某一天），用于增量跳过
CREATE TABLE IF NOT EXISTS insider_batch (
    kind        TEXT NOT NULL,      -- quarter / day
    key         TEXT NOT NULL,      -- '2026q1' / '2026-07-24'
    rows        INTEGER NOT NULL,
    filings     INTEGER,
    errors      INTEGER NOT NULL DEFAULT 0,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
"""

_COLS = ("accession", "trade_key", "seq", "ticker", "company", "issuer_cik", "owner", "owner_cik",
         "is_officer", "is_director", "is_ten_pct", "officer_title", "security",
         "tx_code", "tx_group", "direction", "tx_date", "filing_date", "shares",
         "price", "value", "acquired_disposed", "shares_after", "is_direct",
         "is_10b5_1", "form_type", "delay_days", "source_url", "source")


def _init() -> None:
    db.ensure_schema("insider", _SCHEMA)


def save_trades(rows: list[dict], source: str) -> int:
    """批量写入（幂等）。返回实际新增行数。

    用 `INSERT OR IGNORE`：两条来源重叠时保留先到的那份 ——
    同一笔交易的内容本来就一样（都来自同一份申报），不存在"哪份更新"的问题。
    去重键是 `(accession, trade_key)`，**trade_key 是内容指纹不是行序** ——
    行序在两条路径下不保证一致，用它会让不同交易撞键而静默丢数据。
    """
    _init()
    if not rows:
        return 0
    payload = [
        (r["accession"], r["trade_key"], r["seq"], r["ticker"], r["company"], r["issuer_cik"],
         r["owner"], r["owner_cik"], int(bool(r["is_officer"])),
         int(bool(r["is_director"])), int(bool(r["is_ten_pct"])),
         r["officer_title"], r["security"], r["tx_code"], r["group"], r["direction"],
         r["tx_date"], r["filing_date"], r["shares"], r["price"], r["value"],
         r["acquired_disposed"], r["shares_after"], int(bool(r["is_direct"])),
         None if r["is_10b5_1"] is None else int(r["is_10b5_1"]),
         r.get("form_type") or "4", r["delay_days"], r["source_url"], source)
        for r in rows
    ]
    sql = (f"INSERT OR IGNORE INTO insider_trade ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM insider_trade").fetchone()[0]
        conn.executemany(sql, payload)
        after = conn.execute("SELECT COUNT(*) FROM insider_trade").fetchone()[0]
    return after - before


def mark_batch(kind: str, key: str, rows: int,
               filings: Optional[int] = None, errors: int = 0) -> None:
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO insider_batch (kind, key, rows, filings, errors, synced_at) "
            "VALUES (?,?,?,?,?,?)",
            (kind, key, rows, filings, errors,
             datetime.now().isoformat(timespec="seconds")))


def mark_dead(accession: str, day: str, reason: str) -> None:
    """登记一份**永远解析不了**的申报（无 XML 的老格式）。"""
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO insider_dead_filing "
            "(accession, day, reason, recorded_at) VALUES (?,?,?,?)",
            (accession, day, reason, datetime.now().isoformat(timespec="seconds")))


def dead_accessions() -> set[str]:
    """已知读不了的申报 —— 重试时跳过它们，别再白下一遍。"""
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT accession FROM insider_dead_filing")}


def known_batches(kind: str) -> set[str]:
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT key FROM insider_batch WHERE kind = ?", (kind,))}


def query(ticker: Optional[str] = None, owner: Optional[str] = None,
          group: Optional[str] = None, direction: Optional[str] = None,
          since: Optional[str] = None, min_value: Optional[float] = None,
          role: Optional[str] = None, plan: Optional[str] = None,
          include_amendments: bool = False, limit: int = 300) -> list[dict]:
    """查询交易明细（按交易日倒序）。

    `group` 默认不限；UI 上默认只看 `open_market` ——
    因为薪酬类占了七成，混在一起会把真正的买卖淹没。
    """
    _init()
    # ⚠️ NULL = **未标注**（2023 年前的申报没这个字段），不是"确认非计划内" ——
    # 混在一起会歪曲「计划内 vs 临时决定」的对比。三态见 _where()。
    w, args = _where(ticker=ticker, owner=owner, group=group, direction=direction,
                     since=since, min_value=min_value, role=role, plan=plan,
                     include_amendments=include_amendments)
    sql = f"SELECT * FROM insider_trade {w}"
    sql += " ORDER BY tx_date IS NULL, tx_date DESC, value DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _where(ticker=None, owner=None, group=None, direction=None, since=None,
           min_value=None, role=None, plan=None,
           include_amendments: bool = False) -> tuple[str, list]:
    """筛选条件 —— 明细与聚合**共用这一处**，避免两边口径漂移。"""
    # ⚠️ 默认排除修订件（4/A）：修订通常是把原申报的交易**重述一遍**，
    # 与原件一起统计就是重复计数。我们**没有做原件↔修订的配对替换**
    # （需要 DATE_OF_ORIG_SUB 逐份匹配），所以取保守做法：
    # 聚合只用原始 Form 4，修订件仍可用 include_amendments=True 查出来。
    sql, args = "WHERE 1=1", []
    if not include_amendments:
        sql += " AND form_type = '4'"
    if ticker:
        sql += " AND ticker = ?"; args.append(ticker.upper())
    if owner:
        sql += " AND owner LIKE ?"; args.append(f"%{owner}%")
    if group:
        sql += " AND tx_group = ?"; args.append(group)
    if direction:
        sql += " AND direction = ?"; args.append(direction)
    if since:
        sql += " AND tx_date >= ?"; args.append(since)
    if min_value:
        sql += " AND value >= ?"; args.append(min_value)
    if role == "officer":
        sql += " AND is_officer = 1"
    elif role == "director":
        sql += " AND is_director = 1"
    elif role == "ten_pct":
        sql += " AND is_ten_pct = 1"
    if plan == "yes":
        sql += " AND is_10b5_1 = 1"
    elif plan == "no":
        sql += " AND is_10b5_1 = 0"
    elif plan == "unknown":
        sql += " AND is_10b5_1 IS NULL"
    return sql, args


def aggregate(top: int = 20, **filters) -> dict:
    """在 **SQL 里对全部命中行**做聚合。

    ⚠️ 不能"取最新 N 行再用 Python 聚合"：筛选命中超过 N 行时，
    总额、集群买入榜、标的榜描述的就只是**最新那 N 行**，
    而标签写的是整个时间段 —— 加个"已截断"提示并不能让数字变准。

    分类（tx_group / direction）在**入库时**由 Python 算好并存成列，
    这里只是按列分组 —— 所以分类逻辑仍然只有一份，不存在 SQL/Python 两套。
    """
    _init()
    w, a = _where(**filters)
    with db.connect() as conn:
        tot = conn.execute(
            f"SELECT COUNT(*) n, "
            f" SUM(tx_group='open_market') om, "
            f" SUM(tx_group='compensation') comp, "
            f" SUM(tx_group='other') other, "
            f" SUM(direction='buy') buys, SUM(direction='sell') sells, "
            f" SUM(CASE WHEN direction='buy' THEN value ELSE 0 END) bv, "
            f" SUM(CASE WHEN direction='sell' THEN value ELSE 0 END) sv, "
            f" SUM(direction='sell' AND is_10b5_1=1) plan_sells "
            f"FROM insider_trade {w}", a).fetchone()
        by_ticker = [dict(r) for r in conn.execute(
            f"SELECT ticker, MAX(company) company, "
            f" SUM(direction='buy') buys, SUM(direction='sell') sells, "
            f" SUM(CASE WHEN direction='buy' THEN value ELSE 0 END) buy_value, "
            f" SUM(CASE WHEN direction='sell' THEN value ELSE 0 END) sell_value, "
            f" COUNT(DISTINCT CASE WHEN direction='buy' THEN owner END) insider_count, "
            f" GROUP_CONCAT(DISTINCT CASE WHEN direction='buy' THEN owner END) buyers "
            f"FROM insider_trade {w} AND ticker IS NOT NULL "
            f"GROUP BY ticker", a)]
        by_owner = [dict(r) for r in conn.execute(
            f"SELECT owner, ticker, MAX(officer_title) title, "
            f" SUM(direction='buy') buys, SUM(direction='sell') sells, "
            f" SUM(CASE WHEN direction='buy' THEN value ELSE 0 END) buy_value, "
            f" SUM(CASE WHEN direction='sell' THEN value ELSE 0 END) sell_value "
            f"FROM insider_trade {w} "
            f"GROUP BY owner, ticker "
            # ⚠️ 排序不引用聚合别名，直接重写表达式：
            # `MAX(alias, alias)` 到底被解析成标量 max 还是聚合 MAX 依 SQLite 版本而异，
            # 老版本会报 "misuse of aliased aggregate"。本机 3.53.2 能跑，
            # 但自部署用户的版本不可控 —— 用到处都对的写法，代价为零。
            f"ORDER BY MAX(SUM(CASE WHEN direction='buy' THEN value ELSE 0 END), "
            f"           SUM(CASE WHEN direction LIKE 'sell' THEN value ELSE 0 END)) DESC "
            f"LIMIT ?", a + [top])]
        delays = [r[0] for r in conn.execute(
            f"SELECT delay_days FROM insider_trade {w} AND delay_days >= 0", a)]
        anomalies = conn.execute(
            f"SELECT COUNT(*) FROM insider_trade {w} AND tx_date > filing_date",
            a).fetchone()[0]
        # 价格错填的笔数：入库时 value 已置空（不进金额统计），
        # 但**数量必须报出来** —— 剔出统计不等于假装它不存在。
        # 条件与 InsiderTrade.price_implausible 保持一致。
        implausible = conn.execute(
            f"SELECT COUNT(*) FROM insider_trade {w} AND "
            f"(price > 1000000 OR (shares IS NOT NULL AND price IS NOT NULL "
            f" AND shares * price > 200000000000))", a).fetchone()[0]
    for e in by_ticker:
        e["buy_value"] = round(e["buy_value"] or 0)
        e["sell_value"] = round(e["sell_value"] or 0)
        e["net_value"] = e["buy_value"] - e["sell_value"]
        # 买入者名单：UI 的 tooltip 要显示它。此前写死空列表 ——
        # 界面上永远只有人数、没有名字，等于把一半信息藏了。
        e["insiders"] = sorted((e.pop("buyers", None) or "").split(","))[:10]
    for m in by_owner:
        m["buy_value"] = round(m["buy_value"] or 0)
        m["sell_value"] = round(m["sell_value"] or 0)
    return {
        "counts": dict(tot), "delays": delays, "anomalies": anomalies,
        "implausible": implausible,
        "by_ticker": sorted(by_ticker, key=lambda x: abs(x["net_value"]),
                            reverse=True)[:top],
        "cluster_buys": sorted([e for e in by_ticker if e["buys"]],
                               key=lambda x: (x["insider_count"], x["buy_value"]),
                               reverse=True)[:top],
        "by_owner": by_owner,
    }


def stats() -> dict:
    """库存概览 —— **必须把「公开市场只占多少」摆在明面上**。"""
    _init()
    with db.connect() as conn:
        t = conn.execute(
            # ⚠️ 覆盖范围用**申报日**不用交易日：
            # 申报日由 EDGAR 系统赋予（可靠），交易日是申报人手填（有年份笔误）——
            # 用交易日会把"已导入 2 个季度"显示成"覆盖 2002 ~ 2028"。
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) tk, COUNT(DISTINCT owner) ow, "
            "MIN(filing_date) lo, MAX(filing_date) hi FROM insider_trade").fetchone()
        g = {r["tx_group"]: r["n"] for r in conn.execute(
            "SELECT tx_group, COUNT(*) n FROM insider_trade GROUP BY tx_group")}
        b = [dict(r) for r in conn.execute(
            "SELECT kind, key, rows, filings, errors, synced_at FROM insider_batch "
            "ORDER BY kind, key DESC")]
        last = conn.execute("SELECT MAX(synced_at) FROM insider_batch").fetchone()[0]
    total = t["n"] or 0
    om = g.get("open_market", 0)
    return {
        "trades": total, "tickers": t["tk"], "owners": t["ow"],
        "earliest": t["lo"], "latest": t["hi"],   # 按申报日
        "range_basis": "filing_date",
        "by_group": g,
        "open_market_pct": round(om / total * 100, 1) if total else 0.0,
        "quarters": [x["key"] for x in b if x["kind"] == "quarter"],
        "days": [x["key"] for x in b if x["kind"] == "day"],
        "batches": b[:40], "last_sync": last, "db_path": db.DB_PATH,
        "note": "「公开市场」= 交易代码 P/S，是唯一含主动买卖意图的部分；"
                "其余为授予/行权/代扣税等薪酬类与其他豁免交易。",
    }
