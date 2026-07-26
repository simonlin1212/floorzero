"""国会交易的本地缓存与增量同步。

━━━ 为什么必须缓存 ━━━
众议院 2026 年有 313 份 PTR，每份都是一个独立 PDF。
按自律限速 3 次/秒抓完要 100+ 秒 —— **不可能每次页面请求都现抓**。
所以：抓一次 → 解析 → 落本地 → 之后只增量补新增的申报。

━━━ 与 GEX 历史的关键区别 ━━━
GEX 的历史**补不回来**（CBOE 只给当下）；
国会披露**天然带全部历史**（年度 ZIP 里就是全年），所以这条线可以完整回补。
这是 README 里要对用户讲清楚的差别。

⚠️ **解析不了的申报也要入库**（`unparsed_reason` 记原因）：
10.5% 的众议院 PTR 是纸质扫描件。不记录的话，每次同步都会重抓它们，
而且用户永远看不到「有 33 份存在但读不了」这个事实。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

from modules import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS congress_filing (
    doc_id      TEXT NOT NULL,
    chamber     TEXT NOT NULL,
    member      TEXT NOT NULL,
    state_district TEXT,
    filing_date TEXT,
    year        TEXT,
    source_url  TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    unparsed_reason TEXT,                    -- NULL = 解析成功
    terminal    INTEGER NOT NULL DEFAULT 0,  -- 1 = 永远解析不了（扫描件），别再重试
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (chamber, doc_id)
);
CREATE TABLE IF NOT EXISTS congress_trade (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chamber     TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,            -- 该申报内的序号，用于幂等
    member      TEXT NOT NULL,
    state_district TEXT,
    ticker      TEXT,
    asset_name  TEXT,
    asset_type  TEXT,
    asset_type_label TEXT,
    tx_type     TEXT,
    tx_type_label TEXT,
    tx_date     TEXT,
    notification_date TEXT,
    filing_date TEXT,
    amount_low  INTEGER,
    amount_high INTEGER,
    amount_raw  TEXT,
    owner       TEXT,
    delay_days  INTEGER,
    source_url  TEXT
);
-- 幂等：重跑同步不会把同一笔灌进去两次
CREATE UNIQUE INDEX IF NOT EXISTS idx_ctrade_unique
    ON congress_trade (chamber, doc_id, seq);
CREATE INDEX IF NOT EXISTS idx_ctrade_ticker ON congress_trade (ticker);
CREATE INDEX IF NOT EXISTS idx_ctrade_date   ON congress_trade (tx_date);
CREATE INDEX IF NOT EXISTS idx_ctrade_member ON congress_trade (member);
"""


def _init() -> None:
    db.ensure_schema("congress", _SCHEMA)


def known_doc_ids(chamber: str) -> set[str]:
    """所有已见过的申报（含解析失败的）。"""
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT doc_id FROM congress_filing WHERE chamber = ?", (chamber,))}


def settled_doc_ids(chamber: str) -> set[str]:
    """**不需要再处理**的申报：解析成功的 + 永远解析不了的（扫描件）。

    ⚠️ 这里的分寸很关键，两个方向都会出错：
    - 只跳过"解析成功"的 → 33 份扫描件每次都被重选，**配额被它们吃光**，
      更早的申报永远轮不到（limit 越小饿死越快）。
    - 把所有失败都算已完成 → 解析器改进后、或临时取不到的文件恢复后，
      **永远没机会重试**。
    所以按「终态 vs 暂时」分：扫描件是终态（无 OCR 就是读不了），
    网络故障等是暂时的，下次继续试。
    """
    _init()
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT doc_id FROM congress_filing "
            "WHERE chamber = ? AND (unparsed_reason IS NULL OR terminal = 1)",
            (chamber,))}


def save_filing(filing: Any, trades: list[dict],
                unparsed_reason: Optional[str] = None,
                terminal: bool = False) -> None:
    """写入一份申报及其交易（幂等，且**重跑即整份替换**）。

    ⚠️ 交易必须「先删后插」而不是 `INSERT OR IGNORE`：
    同一份申报被重新解析时（解析器改进了、或上游发了修订件），
    IGNORE 会保留旧行、丢弃新行，还会留下多余的尾部行 ——
    结果是 `trade_count` 与实际缓存对不上，且旧的错误解析永远洗不掉。
    删+插放在同一个事务里，中途失败不会留下半份数据。
    """
    _init()
    now = datetime.now().isoformat(timespec="seconds")
    fd = filing.filing_date.isoformat() if filing.filing_date else None
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO congress_filing "
            "(doc_id, chamber, member, state_district, filing_date, year, "
            " source_url, trade_count, unparsed_reason, terminal, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (filing.doc_id, filing.chamber, filing.name, filing.state_district,
             fd, filing.year, filing.detail_url, len(trades), unparsed_reason,
             int(terminal), now))
        # 整份替换：旧行先清掉，避免残留（唯一索引只挡重复，挡不住"多出来的旧行"）
        conn.execute("DELETE FROM congress_trade WHERE chamber = ? AND doc_id = ?",
                     (filing.chamber, filing.doc_id))
        for i, t in enumerate(trades):
            conn.execute(
                "INSERT OR IGNORE INTO congress_trade "
                "(chamber, doc_id, seq, member, state_district, ticker, asset_name, "
                " asset_type, asset_type_label, tx_type, tx_type_label, tx_date, "
                " notification_date, filing_date, amount_low, amount_high, amount_raw, "
                " owner, delay_days, source_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (filing.chamber, filing.doc_id, i, t["member"], t["state_district"],
                 t["ticker"], t["asset_name"], t["asset_type"], t["asset_type_label"],
                 t["tx_type"], t["tx_type_label"], t["tx_date"], t["notification_date"],
                 t["filing_date"], t["amount_low"], t["amount_high"], t["amount_raw"],
                 t["owner"], t["delay_days"], t["source_url"]))


def query_trades(chamber: Optional[str] = None, ticker: Optional[str] = None,
                 member: Optional[str] = None, since: Optional[str] = None,
                 tx_type: Optional[str] = None, limit: int = 500) -> list[dict]:
    """查询已缓存的交易（按交易日倒序）。"""
    _init()
    sql = "SELECT * FROM congress_trade WHERE 1=1"
    args: list[Any] = []
    if chamber:
        sql += " AND chamber = ?"; args.append(chamber)
    if ticker:
        sql += " AND ticker = ?"; args.append(ticker.upper())
    if member:
        sql += " AND member LIKE ?"; args.append(f"%{member}%")
    if since:
        sql += " AND tx_date >= ?"; args.append(since)
    if tx_type == "buy":
        sql += " AND tx_type = 'P'"
    elif tx_type == "sell":
        sql += " AND tx_type LIKE 'S%'"
    # tx_date 是 ISO 字符串，字典序即时间序；NULL 排最后
    sql += " ORDER BY tx_date IS NULL, tx_date DESC, id DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def stats() -> dict:
    """缓存概览 —— **必须把「有多少份读不了」也报出来**。

    只报"已解析 N 笔"会让用户以为那就是全部，
    而实际有 10% 的申报是纸质扫描件、根本没进这个数。
    """
    _init()
    with db.connect() as conn:
        t = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT ticker) tk, COUNT(DISTINCT member) mb, "
            "MIN(tx_date) lo, MAX(tx_date) hi FROM congress_trade").fetchone()
        f = conn.execute(
            "SELECT chamber, COUNT(*) n, SUM(unparsed_reason IS NOT NULL) bad "
            "FROM congress_filing GROUP BY chamber").fetchall()
        last = conn.execute(
            "SELECT MAX(synced_at) FROM congress_filing").fetchone()[0]
    chambers = {r["chamber"]: {"filings": r["n"], "unparsed": r["bad"] or 0} for r in f}
    total_f = sum(c["filings"] for c in chambers.values())
    total_bad = sum(c["unparsed"] for c in chambers.values())
    return {
        "trades": t["n"], "tickers": t["tk"], "members": t["mb"],
        "earliest_trade": t["lo"], "latest_trade": t["hi"],
        "filings": total_f, "unparsed_filings": total_bad,
        "unparsed_pct": round(total_bad / total_f * 100, 1) if total_f else 0.0,
        "by_chamber": chambers, "last_sync": last, "db_path": db.DB_PATH,
        "note": "未解析的申报绝大多数是纸质扫描件（整份为图片），"
                "无 OCR 读不出明细 —— 它们确实存在，只是不在上面的交易数里。",
    }


def unparsed_filings(limit: int = 100) -> list[dict]:
    """列出读不了的申报（给用户直达原件链接，不是一句"没数据"了事）。"""
    _init()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT chamber, doc_id, member, state_district, filing_date, "
            "       source_url, unparsed_reason "
            "FROM congress_filing WHERE unparsed_reason IS NOT NULL "
            "ORDER BY filing_date DESC LIMIT ?", (limit,))]
