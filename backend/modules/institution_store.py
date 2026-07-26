"""13F 机构持仓的本地存储。

━━━ 规模 ━━━
单个报告期实测 **332 万条**持仓（8,741 家机构 × 31,464 个 CUSIP）。
全量入库约 600MB-1GB —— 对"clone 下来就跑"的自部署工具偏重。

⭐ 所以默认设**金额门槛 $100 万**：实测保留 37.5% 的行数、
覆盖 **99.37%** 的金额。这是个很划算的交换，但**必须如实报出丢了什么**
（`stats()` 会返回门槛与丢弃量）—— 悄悄截断会让"谁持有某只股票"
这类查询漏掉小持仓，而用户毫不知情。门槛可设为 0 收全量。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from modules import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS f13_holding (
    accession    TEXT NOT NULL,
    holding_key  TEXT NOT NULL,      -- cusip|kind|discretion#序号（见 institution.assign_keys）
    manager      TEXT NOT NULL,
    manager_cik  TEXT,
    period       TEXT NOT NULL,      -- 报告期（季末）
    filing_date  TEXT,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    cusip        TEXT NOT NULL,      -- ⭐ 聚合与跨季比对的主键（不是 ticker）
    issuer       TEXT,
    title_of_class TEXT,
    kind         TEXT NOT NULL,      -- share / call / put ⚠️ 必须分开统计
    value        REAL,               -- 美元（已按申报期把千美元换算过）
    shares       REAL,
    shares_type  TEXT,               -- SH / PRN
    discretion   TEXT,
    voting_sole  REAL,
    voting_shared REAL,
    voting_none  REAL,
    source_url   TEXT,
    PRIMARY KEY (accession, holding_key)
);
CREATE INDEX IF NOT EXISTS idx_f13_cusip   ON f13_holding (cusip, period, kind);
CREATE INDEX IF NOT EXISTS idx_f13_manager ON f13_holding (manager_cik, period);
CREATE INDEX IF NOT EXISTS idx_f13_period  ON f13_holding (period, kind);

-- 导入暂存表：与 f13_holding 同构，但**独立主键空间**。
-- ⚠️ 不能把暂存数据写进 f13_holding 再靠 period 区分 ——
-- 主键是 (accession, holding_key) 不含 period，暂存行会与正式行撞键
-- 被 INSERT OR IGNORE 静默丢弃（实测 7 条只进去 4 条）。
CREATE TABLE IF NOT EXISTS f13_holding_staging (
    accession    TEXT NOT NULL,
    holding_key  TEXT NOT NULL,
    manager      TEXT NOT NULL,
    manager_cik  TEXT,
    period       TEXT NOT NULL,
    filing_date  TEXT,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    cusip        TEXT NOT NULL,
    issuer       TEXT,
    title_of_class TEXT,
    kind         TEXT NOT NULL,
    value        REAL,
    shares       REAL,
    shares_type  TEXT,
    discretion   TEXT,
    voting_sole  REAL,
    voting_shared REAL,
    voting_none  REAL,
    source_url   TEXT,
    PRIMARY KEY (accession, holding_key)
);

-- ⭐ CUSIP → 规范发行人名称（来自 SEC 官方 13(f) 证券清单）。
-- 申报里的 NAMEOFISSUER 是自由填写的：实测苹果那个 CUSIP 有 61 种写法，
-- 还混着别家公司的名字。展示/分组一律以本表为准，查不到才退回申报里的众数。
CREATE TABLE IF NOT EXISTS f13_security (
    cusip      TEXT PRIMARY KEY,
    issuer     TEXT NOT NULL,
    class      TEXT,
    has_option INTEGER NOT NULL DEFAULT 0,
    quarter    TEXT,
    synced_at  TEXT NOT NULL
);

-- 已导入的报告期批次
CREATE TABLE IF NOT EXISTS f13_batch (
    period       TEXT PRIMARY KEY,
    window       TEXT,
    rows         INTEGER NOT NULL,   -- 实际入库条数
    parsed_rows  INTEGER,            -- 解析出的总条数（含被门槛滤掉的）
    dropped_rows INTEGER,            -- 被门槛滤掉的条数
    dropped_value REAL,              -- 被滤掉的金额
    min_value    REAL,               -- 本次使用的门槛
    managers     INTEGER,
    synced_at    TEXT NOT NULL
);
"""

_COLS = ("accession", "holding_key", "manager", "manager_cik", "period",
         "filing_date", "is_amendment", "cusip", "issuer", "title_of_class",
         "kind", "value", "shares", "shares_type", "discretion",
         "voting_sole", "voting_shared", "voting_none", "source_url")


def _init() -> None:
    db.ensure_schema("f13", _SCHEMA)


def _row_tuple(r: dict) -> tuple:
    """dict → 入库元组（正式表与暂存表共用，避免两处字段顺序漂移）。"""
    return (r["accession"], r["holding_key"], r["manager"], r["manager_cik"],
            r["period"], r["filing_date"], int(bool(r["is_amendment"])),
            r["cusip"], r["issuer"], r["title_of_class"], r["kind"], r["value"],
            r["shares"], r["shares_type"], r["discretion"], r["voting_sole"],
            r["voting_shared"], r["voting_none"], r["source_url"])


def clear_staging(period: Optional[str] = None) -> None:
    """清空暂存表（上次导入中途失败会留下数据）。"""
    _init()
    with db.connect() as conn:
        if period:
            conn.execute("DELETE FROM f13_holding_staging WHERE period = ?", (period,))
        else:
            conn.execute("DELETE FROM f13_holding_staging")


def save_staging(rows: list[dict]) -> int:
    """写入暂存表。"""
    _init()
    if not rows:
        return 0
    sql = (f"INSERT OR REPLACE INTO f13_holding_staging ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        conn.executemany(sql, [_row_tuple(r) for r in rows])
    return len(rows)


def commit_staging(period: str) -> int:
    """**原子切换**：删旧 → 暂存转正，同一个事务里完成。

    ⚠️ 不能"先删后写"：流式导入中途失败（网络中断、进程被杀、崩溃）
    会留下空的或半份数据，而 `f13_batch` 还写着旧的条数 ——
    之后所有查询都在悄悄提供残缺数据，且完全看不出来。
    先写暂存、全部成功了再切换，失败时旧数据分毫未动。
    """
    _init()
    cols = ",".join(_COLS)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM f13_holding_staging WHERE period = ?",
                         (period,)).fetchone()[0]
        # ⚠️ 暂存为空**也要**执行切换：门槛调得足够高时可能一行都不剩，
        # 直接返回的话旧持仓仍留在库里，而批次元数据已记成"0 条 + 新门槛" ——
        # 页面说没有数据、查询却照样返回旧数据。
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM f13_holding WHERE period = ?", (period,))
        conn.execute(f"INSERT OR REPLACE INTO f13_holding ({cols}) "
                     f"SELECT {cols} FROM f13_holding_staging WHERE period = ?",
                     (period,))
        conn.execute("DELETE FROM f13_holding_staging WHERE period = ?", (period,))
        conn.execute("COMMIT")
        return n


def save_holdings(rows: list[dict], replace_period: Optional[str] = None) -> int:
    """批量写入（幂等）。返回实际新增条数。

    `replace_period`：先把该报告期的旧数据清空再写。
    ⚠️ 直接用它是**破坏性**的（失败即毁数据）——
    流式导入请改用 `clear_staging()` + 写入 `<period>#staging` + `commit_staging()`。
    """
    _init()
    if replace_period:
        with db.connect() as conn:
            conn.execute("DELETE FROM f13_holding WHERE period = ?", (replace_period,))
    if not rows:
        return 0
    payload = [_row_tuple(r) for r in rows]
    sql = (f"INSERT OR IGNORE INTO f13_holding ({','.join(_COLS)}) "
           f"VALUES ({','.join('?' * len(_COLS))})")
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM f13_holding").fetchone()[0]
        conn.executemany(sql, payload)
        after = conn.execute("SELECT COUNT(*) FROM f13_holding").fetchone()[0]
    return after - before


def mark_batch(period: str, window: str, rows: int, parsed_rows: int,
               dropped_rows: int, dropped_value: float, min_value: float,
               managers: int) -> None:
    _init()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO f13_batch (period, window, rows, parsed_rows, "
            " dropped_rows, dropped_value, min_value, managers, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (period, window, rows, parsed_rows, dropped_rows, dropped_value,
             min_value, managers, datetime.now().isoformat(timespec="seconds")))


def save_securities(rows: list[dict], quarter: str) -> int:
    """写入官方证券清单（CUSIP → 规范名称）。"""
    _init()
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO f13_security "
            "(cusip, issuer, class, has_option, quarter, synced_at) VALUES (?,?,?,?,?,?)",
            [(r["cusip"], r["issuer"], r.get("class"), int(bool(r.get("has_option"))),
              quarter, now) for r in rows])
        return conn.execute("SELECT COUNT(*) FROM f13_security").fetchone()[0]


def security_count() -> int:
    _init()
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM f13_security").fetchone()[0]


def known_periods() -> list[str]:
    _init()
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT period FROM f13_batch ORDER BY period DESC")]


def _where(period: Optional[str] = None, cusip: Optional[str] = None,
           manager: Optional[str] = None, kind: Optional[str] = "share",
           min_value: Optional[float] = None,
           include_amendments: bool = False, alias: str = "") -> tuple[str, list]:
    """筛选条件 —— 明细与聚合**共用这一处**，避免两边口径漂移。

    ⚠️ `kind` 默认 `share`：把 put（看空）混进持仓统计，
    等于把看空算成看多（该季 put 规模 $2.66 万亿）。
    """
    # `alias` 让同一套条件能用在带表别名的 JOIN 查询上 ——
    # 早前是用字符串 replace 硬改别名，那种写法一改列名就会静默出错。
    p = f"{alias}." if alias else ""
    sql, args = "WHERE 1=1", []
    if not include_amendments:
        # 修订件会重述整份申报（Form 13F 说明第 3 条要求全文重述），
        # 与原件同时统计就是重复计数
        sql += f" AND {p}is_amendment = 0"
    if period:
        sql += f" AND {p}period = ?"; args.append(period)
    if cusip:
        sql += f" AND {p}cusip = ?"; args.append(cusip.upper())
    if manager:
        sql += f" AND {p}manager LIKE ?"; args.append(f"%{manager}%")
    if kind and kind != "all":
        sql += f" AND {p}kind = ?"; args.append(kind)
    if min_value:
        sql += f" AND {p}value >= ?"; args.append(min_value)
    return sql, args


def query(limit: int = 200, **filters) -> list[dict]:
    """持仓明细（按金额倒序）。

    ⚠️ 发行人名同样走官方清单：聚合视图用了、明细却用申报原值的话，
    同一个 CUSIP 会在图表里叫「APPLE INC」、在下面的表里叫别家公司的名字。
    """
    _init()
    wh, args = _where(**filters, alias="h")
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT h.*, COALESCE(s.issuer, h.issuer) issuer, s.class class "
            f"FROM f13_holding h LEFT JOIN f13_security s ON s.cusip = h.cusip "
            f"{wh} ORDER BY h.value IS NULL, h.value DESC LIMIT ?", args + [limit])]


def aggregate(top: int = 20, **filters) -> dict:
    """在 SQL 里对**全部命中行**聚合（不是取前 N 行再算）。"""
    _init()
    w, a = _where(**filters)
    wh, ah = _where(**filters, alias="h")          # 带别名版本，供 JOIN 查询用
    with db.connect() as conn:
        tot = conn.execute(
            f"SELECT COUNT(*) n, COUNT(DISTINCT manager_cik) mgrs, "
            f" COUNT(DISTINCT cusip) cusips, SUM(value) val "
            f"FROM f13_holding {w}", a).fetchone()
        # ⚠️ 发行人名用**官方清单**，不能用 MAX(issuer)：
        # 申报里的名称是自由填写的，MAX 取的是字母序最大那个 ——
        # 实测苹果的 CUSIP 会被显示成 "VANGUARD WHITEHALL FDS"（别家公司），
        # 亚马逊显示成 "JOHNSON & JOHNSON COM"。张冠李戴且看不出来。
        # 官方清单查不到时才退回申报里**出现最多**的那个写法（众数，不是 MAX）。
        # ⚠️ 带上官方清单的 class：同一发行人常有多个股份类别
        # （Alphabet 的 CL A / CL C 是两个不同 CUSIP、两只不同证券）。
        # 不显示类别的话，榜单上会出现两行"ALPHABET INC"，看着像重复数据。
        by_issuer = [dict(r) for r in conn.execute(
            f"SELECT h.cusip, COALESCE(s.issuer, "
            f"  (SELECT issuer FROM f13_holding x WHERE x.cusip = h.cusip "
            f"   GROUP BY x.issuer ORDER BY COUNT(*) DESC LIMIT 1)) issuer, "
            f" s.class class, "
            f" COUNT(DISTINCT h.manager_cik) holders, "
            f" SUM(h.value) value, SUM(h.shares) shares "
            f"FROM f13_holding h LEFT JOIN f13_security s ON s.cusip = h.cusip "
            f"{wh} GROUP BY h.cusip ORDER BY value DESC LIMIT ?", ah + [top])]
        by_manager = [dict(r) for r in conn.execute(
            f"SELECT manager_cik, MAX(manager) manager, COUNT(DISTINCT cusip) positions, "
            f" SUM(value) value "
            f"FROM f13_holding {w} GROUP BY manager_cik "
            f"ORDER BY value DESC LIMIT ?", a + [top])]
        # ⭐ 三种持仓类型各自的规模 —— 让 put 的量级摆在明面上
        by_kind = {r["kind"]: {"rows": r["n"], "value": r["v"]}
                   for r in conn.execute(
                       f"SELECT kind, COUNT(*) n, SUM(value) v FROM f13_holding "
                       f"{_where(**{**filters, 'kind': 'all'})[0]} GROUP BY kind",
                       _where(**{**filters, "kind": "all"})[1])}
    return {"counts": dict(tot), "by_issuer": by_issuer,
            "by_manager": by_manager, "by_kind": by_kind}


def changes(period: str, prev_period: str, top: int = 20,
            kind: str = "share", manager: Optional[str] = None) -> dict:
    """季度环比变动：新建仓 / 加仓 / 减仓 / 清仓。

    ⭐ 这才是 13F 的主要价值 —— 单季持仓是静态快照，变动才有信息量。

    ⚠️ 按 **CUSIP** 比对，不用 ticker（ticker 只有四成能匹配上）。
    ⚠️ 「清仓」只代表**不再出现在 13(f) 持仓里**，不等于机构看空 ——
    可能转成了期权、转到不需申报的账户、或该证券已退出 13(f) 清单。
    """
    _init()
    # ⚠️ **金额门槛会污染「新建仓/清仓」的判定**：
    # 上季 $90 万（被门槛滤掉）、本季 $110 万（保留）的持仓，
    # 会被算成"新建仓"，实际只是加仓；反向则被算成"清仓"。
    # 所以要把两期的门槛取出来，在结果里如实标注这个失真区间。
    with db.connect() as conn:
        floors = {r[0]: r[1] for r in conn.execute(
            "SELECT period, min_value FROM f13_batch WHERE period IN (?,?)",
            (period, prev_period))}
    floor = max([v or 0 for v in floors.values()] or [0])
    m_sql, m_args = ("", [])
    if manager:
        m_sql, m_args = " AND manager LIKE ?", [f"%{manager}%"]
    # 发行人名同样取自官方清单（见 aggregate 的说明），退回众数而非 MAX
    base = (f"SELECT h.cusip cusip, COALESCE(s.issuer, "
            f"  (SELECT issuer FROM f13_holding x WHERE x.cusip = h.cusip "
            f"   GROUP BY x.issuer ORDER BY COUNT(*) DESC LIMIT 1)) issuer, "
            f"s.class class, SUM(h.value) value, SUM(h.shares) shares, "
            f"COUNT(DISTINCT h.manager_cik) holders "
            f"FROM f13_holding h LEFT JOIN f13_security s ON s.cusip = h.cusip "
            f"WHERE h.is_amendment = 0 AND h.kind = ? AND h.period = ?"
            f"{m_sql.replace(' manager ', ' h.manager ')} GROUP BY h.cusip")
    with db.connect() as conn:
        cur = {r["cusip"]: dict(r) for r in conn.execute(base, [kind, period] + m_args)}
        prv = {r["cusip"]: dict(r) for r in conn.execute(base, [kind, prev_period] + m_args)}

    new, exited, inc, dec, unchanged = [], [], [], [], []
    for c, r in cur.items():
        p = prv.get(c)
        if p is None:
            new.append({**r, "prev_value": 0.0, "delta_value": r["value"] or 0.0})
            continue
        d = (r["value"] or 0) - (p["value"] or 0)
        row = {**r, "prev_value": p["value"], "delta_value": d,
               "prev_shares": p["shares"],
               "delta_shares": (r["shares"] or 0) - (p["shares"] or 0)}
        # ⚠️ d == 0 是「持仓没变」，既不是加仓也不是减仓。
        # 早前写成 `inc if d > 0 else dec`，把 36 个完全未变动的标的
        # 算进了减仓 —— 数字虚高，还把"没动"显示成"在减"。
        if d > 0:
            inc.append(row)
        elif d < 0:
            dec.append(row)
        else:
            unchanged.append(row)
    for c, p in prv.items():
        if c not in cur:
            exited.append({**p, "prev_value": p["value"], "value": 0.0,
                           "delta_value": -(p["value"] or 0.0)})

    k = lambda rows, rev: sorted(rows, key=lambda x: x["delta_value"],
                                 reverse=rev)[:top]
    return {
        "period": period, "prev_period": prev_period, "kind": kind,
        "new": k(new, True), "increased": k(inc, True),
        "decreased": k(dec, False), "exited": k(exited, False),
        "counts": {"new": len(new), "increased": len(inc),
                   "decreased": len(dec), "exited": len(exited),
                   "unchanged": len(unchanged)},
        "min_value": floor,
        "note": "「清仓」只代表该 CUSIP 不再出现在 13(f) 多头持仓里 —— "
                "不等于机构看空：可能转成了期权、移到无需申报的账户、"
                "或该证券已退出 13(f) 清单。",
        "floor_note": (
            f"⚠️ 两期数据带 ${floor:,.0f} 的金额门槛：低于它的持仓没有入库。"
            f"因此**金额接近门槛的「新建仓」与「清仓」不可信** —— "
            f"上季 ${floor*0.9:,.0f}（被滤掉）、本季 ${floor*1.1:,.0f}（保留）"
            f"会显示成新建仓，实际只是加仓。要精确比对请以门槛 0 重新导入两期。"
            if floor else "两期均为全量导入（无金额门槛），新建仓/清仓判定可信。"),
    }


def stats() -> dict:
    """库存概览 —— **必须把门槛与丢弃量报出来**。"""
    _init()
    with db.connect() as conn:
        t = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT manager_cik) mgrs, "
            "COUNT(DISTINCT cusip) cusips, SUM(value) val FROM f13_holding "
            "WHERE is_amendment = 0").fetchone()
        b = [dict(r) for r in conn.execute(
            "SELECT * FROM f13_batch ORDER BY period DESC")]
        kinds = {r[0]: r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM f13_holding GROUP BY kind")}
    return {
        "holdings": t["n"] or 0, "managers": t["mgrs"] or 0,
        "cusips": t["cusips"] or 0, "total_value": t["val"] or 0.0,
        "by_kind": kinds,
        "periods": [x["period"] for x in b],
        "batches": b,
        "last_sync": b[0]["synced_at"] if b else None,
        "db_path": db.DB_PATH,
        "note": "13F 只含**季末时点、13(f) 证券的多头持仓** —— 不含空头、现金、"
                "债券、仅境外上市股票与私募持仓。看跌期权(put)按标的列示，"
                "已单独归类，默认口径不计入持仓。",
    }
