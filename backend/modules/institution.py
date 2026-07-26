"""13F 机构持仓：解析、分类与季度环比。

━━━ ⭐ 「机构持仓」这个说法本身就会误导，先把边界讲清 ━━━

13F 报的是**季末时点、对 13(f) 证券的多头持仓**。它**不含**：
空头头寸（SEC 2023 年专门另立 Form SHO 就是因为 13F 不覆盖）、
现金、债券、大宗商品、仅境外上市的股票、私募持仓、获保密豁免的持仓。

所以「某机构持仓 X 亿」只是**它多头这一面里、恰好落在 13(f) 清单内的部分**，
既不是全部资产，也不代表净敞口。

━━━ ⚠️ 期权：最容易把数据读反的地方 ━━━

Form 13F 特别说明第 10 条要求把期权按**标的证券**列示，并标 `PUT` / `CALL`。
于是**一笔看跌期权（看空）会以「持有标的」的形态出现在表里**。
实测 2026Q1 窗口：普通持股 $74.9 万亿 / Call $2.95 万亿 / **Put $3.66 万亿**。

→ 直接把 VALUE 加总当「机构在买」，等于**把 3.66 万亿的看空头寸算成看多**。
   本模块按 `position_kind`（share / call / put）分开统计，
   默认口径只含普通持股。

━━━ 另外三个坑（都实测过）━━━
1. **13F-NT 是通知件，不含任何持仓**（该窗口 2,045 份，占 18%）。
2. **VALUE 单位**：2023 年起是**美元**（实测隐含股价中位 $53.30，合理）；
   更早的申报以**千美元**计 —— 回补历史时必须换算，否则差 1000 倍。
3. **只有 CUSIP 没有 ticker**。按发行人名称匹配 `company_tickers.json`
   实测命中率仅 **42.8%**（未命中多为 ETF/基金）→ ticker 只作辅助字段，
   **聚合与去重一律用 CUSIP**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from sources import edgar13f as src

#: VALUE 字段改用「美元」的申报期起点。更早的以千美元计。
#: （SEC 2022 年修订 Form 13F，2023 年起生效）
DOLLARS_FROM = date(2023, 1, 1)

#: 持仓类型 —— **必须分开**，见模块文档
POSITION_KINDS = {
    "share": "普通持股",
    "call": "看涨期权（标的）",
    "put": "看跌期权（标的·看空）",
}


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _parse_date(v: Optional[str]) -> Optional[date]:
    """解析 `31-MAR-2026`（数据集）或 `2026-03-31`。"""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def position_kind(putcall: Optional[str]) -> str:
    """PUTCALL 字段 → 持仓类型。空 = 普通持股。"""
    s = (putcall or "").strip().lower()
    if s.startswith("put"):
        return "put"
    if s.startswith("call"):
        return "call"
    return "share"


@dataclass(frozen=True)
class Holding:
    """一条 13F 持仓记录。"""

    accession: str
    holding_key: str              # 稳定主键（见 assign_keys）
    manager: str                  # 申报机构名
    manager_cik: str
    period: Optional[date]        # 报告期（季末）
    filing_date: Optional[date]
    is_amendment: bool
    cusip: str                    # ⭐ 主键用它，不用 ticker
    issuer: str
    title_of_class: str
    kind: str                     # share / call / put
    value: Optional[float]        # 美元（已按申报期换算）
    shares: Optional[float]
    shares_type: str              # SH（股数）/ PRN（面值）
    discretion: str               # SOLE / DEFINED / OTHER
    voting_sole: Optional[float]
    voting_shared: Optional[float]
    voting_none: Optional[float]
    source_url: str

    @property
    def kind_label(self) -> str:
        return POSITION_KINDS.get(self.kind, self.kind)


def to_dict(h: Holding) -> dict:
    return {
        "accession": h.accession, "holding_key": h.holding_key,
        "manager": h.manager, "manager_cik": h.manager_cik,
        "period": h.period.isoformat() if h.period else None,
        "filing_date": h.filing_date.isoformat() if h.filing_date else None,
        "is_amendment": h.is_amendment,
        "cusip": h.cusip, "issuer": h.issuer, "title_of_class": h.title_of_class,
        "kind": h.kind, "kind_label": h.kind_label,
        "value": h.value, "shares": h.shares, "shares_type": h.shares_type,
        "discretion": h.discretion,
        "voting_sole": h.voting_sole, "voting_shared": h.voting_shared,
        "voting_none": h.voting_none, "source_url": h.source_url,
    }


def accession_url(cik: str, accession: str) -> str:
    cik = (cik or "").lstrip("0")
    if not cik or not accession:
        return ""
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


def _value_dollars(raw: Optional[str], period: Optional[date]) -> Optional[float]:
    """VALUE → 美元。

    ⚠️ 2023 年前的申报以**千美元**计（SEC 2022 年修订 Form 13F）。
    不换算的话，回补 2022 年及更早的数据会整整差 1000 倍 ——
    而且因为数值"看起来还挺像"，不容易一眼发现。
    """
    v = _num(raw)
    if v is None:
        return None
    if period and period < DOLLARS_FROM:
        return v * 1000.0
    return v


def assign_keys(rows: Iterable[tuple[str, str, str, str]]) -> list[str]:
    """给持仓分配稳定主键：`cusip|kind|discretion#序号`（按申报内计数）。

    ⚠️ 不能只用 `(accession, cusip)`：同一份申报里，
    同一 CUSIP 会因**持仓类型**（普通股/看涨/看跌）与**投资裁量权**
    （SOLE / DEFINED / 不同 other manager）拆成多行 —— 都是合法的不同行。
    也不能用行序（数据集与逐份解析的顺序不保证一致）。
    """
    seen: dict[tuple[str, str], int] = {}
    out: list[str] = []
    for acc, cusip, kind, disc in rows:
        base = f"{cusip}|{kind}|{disc}"
        n = seen.get((acc, base), 0)
        seen[(acc, base)] = n + 1
        out.append(f"{base}#{n}")
    return out


def iter_holdings(zf, src, period: str, min_value: float = 0.0):
    """**流式**产出某报告期的持仓 —— 内存恒定。

    ⚠️ 必须流式：把 INFOTABLE（380 万行）全量读成字典再解析，
    实测峰值 **5.3 GB**，8GB 的机器会被 OOM 杀掉。
    这里只把小表（SUBMISSION/COVERPAGE 各约 1.2 万行）读进内存做索引，
    INFOTABLE 边读边过滤边产出。

    `min_value` 在这里就过滤掉，避免把注定要丢的行也构造成对象。
    产出 `(Holding, 是否被门槛滤掉, 该行金额)`，让调用方能如实统计丢弃量。
    """
    want = _parse_date(period)

    subs: dict[str, dict] = {}
    for r in src.read_table(zf, "SUBMISSION"):
        # 用 sources 里的共享常量，别在这里再写一份（两处定义早晚会漂移）
        if (r.get("SUBMISSIONTYPE") or "").strip().upper() not in src.HOLDINGS_TYPES:
            continue                       # 排除 13F-NT（通知件，不含持仓）
        if want and _parse_date(r.get("PERIODOFREPORT")) != want:
            continue
        subs[r["ACCESSION_NUMBER"]] = r
    if not subs:
        return

    covers = {r["ACCESSION_NUMBER"]: r
              for r in src.read_table(zf, "COVERPAGE")
              if r["ACCESSION_NUMBER"] in subs}

    seen: dict[tuple[str, str], int] = {}
    for r in src.iter_table(zf, "INFOTABLE"):
        acc = r["ACCESSION_NUMBER"]
        s = subs.get(acc)
        if s is None:
            continue
        p = _parse_date(s.get("PERIODOFREPORT"))
        val = _value_dollars(r.get("VALUE"), p)
        if min_value and (val or 0) < min_value:
            yield None, True, (val or 0.0)      # 被门槛滤掉，但要计数
            continue
        cusip = (r.get("CUSIP") or "").strip().upper()
        kind = position_kind(r.get("PUTCALL"))
        disc = (r.get("INVESTMENTDISCRETION") or "").strip().upper()
        base = f"{cusip}|{kind}|{disc}"
        n = seen.get((acc, base), 0)
        seen[(acc, base)] = n + 1
        cp = covers.get(acc, {})
        cik = (s.get("CIK") or "").lstrip("0")
        yield Holding(
            accession=acc, holding_key=f"{base}#{n}",
            manager=(cp.get("FILINGMANAGER_NAME") or "").strip() or "(未知)",
            manager_cik=cik, period=p, filing_date=_parse_date(s.get("FILING_DATE")),
            is_amendment=(s.get("SUBMISSIONTYPE") or "").strip().upper().endswith("/A"),
            cusip=cusip, issuer=(r.get("NAMEOFISSUER") or "").strip(),
            title_of_class=(r.get("TITLEOFCLASS") or "").strip(),
            kind=kind, value=val, shares=_num(r.get("SSHPRNAMT")),
            shares_type=(r.get("SSHPRNAMTTYPE") or "").strip().upper(),
            discretion=disc,
            voting_sole=_num(r.get("VOTING_AUTH_SOLE")),
            voting_shared=_num(r.get("VOTING_AUTH_SHARED")),
            voting_none=_num(r.get("VOTING_AUTH_NONE")),
            source_url=accession_url(cik, acc),
        ), False, (val or 0.0)


def parse_dataset(tables: dict[str, list[dict]],
                  period: Optional[str] = None) -> list[Holding]:
    """⚠️ **已弃用**（会把整表驻留内存）—— 新代码请用 `iter_holdings()`。

    把数据集三张表拼成持仓列表。

    `period`：只要某个报告期（`YYYY-MM-DD`）。**强烈建议传** ——
    一个窗口混着多个报告期（实测有一路到 2008 年的补报），
    不过滤的话「本季机构持仓」会掺进十几年的历史。

    ⚠️ 只收 13F-HR / 13F-HR/A：**13F-NT 是通知件、不含任何持仓**
    （实测该窗口 2,045 份，占 18%）。不排除的话，
    这些机构会以「零持仓」的形态出现在榜单里。
    """
    want = _parse_date(period) if period else None

    subs: dict[str, dict] = {}
    for r in tables.get("SUBMISSION", []):
        stype = (r.get("SUBMISSIONTYPE") or "").strip().upper()
        if stype not in src.HOLDINGS_TYPES:      # 与流式路径共用同一常量
            continue                       # 排除 13F-NT / 13F-NT/A
        p = _parse_date(r.get("PERIODOFREPORT"))
        if want and p != want:
            continue
        subs[r["ACCESSION_NUMBER"]] = r

    covers = {r["ACCESSION_NUMBER"]: r for r in tables.get("COVERPAGE", [])}

    raw: list[tuple] = []
    for r in tables.get("INFOTABLE", []):
        acc = r["ACCESSION_NUMBER"]
        s = subs.get(acc)
        if s is None:
            continue
        raw.append((r, s, covers.get(acc, {})))

    keys = assign_keys(
        (r["ACCESSION_NUMBER"], (r.get("CUSIP") or "").strip().upper(),
         position_kind(r.get("PUTCALL")),
         (r.get("INVESTMENTDISCRETION") or "").strip().upper())
        for r, _, _ in raw)

    out: list[Holding] = []
    for (r, s, cp), key in zip(raw, keys):
        p = _parse_date(s.get("PERIODOFREPORT"))
        cik = (s.get("CIK") or "").lstrip("0")
        acc = r["ACCESSION_NUMBER"]
        out.append(Holding(
            accession=acc, holding_key=key,
            manager=(cp.get("FILINGMANAGER_NAME") or "").strip() or "(未知)",
            manager_cik=cik,
            period=p, filing_date=_parse_date(s.get("FILING_DATE")),
            is_amendment=(s.get("SUBMISSIONTYPE") or "").strip().upper().endswith("/A"),
            cusip=(r.get("CUSIP") or "").strip().upper(),
            issuer=(r.get("NAMEOFISSUER") or "").strip(),
            title_of_class=(r.get("TITLEOFCLASS") or "").strip(),
            kind=position_kind(r.get("PUTCALL")),
            value=_value_dollars(r.get("VALUE"), p),
            shares=_num(r.get("SSHPRNAMT")),
            shares_type=(r.get("SSHPRNAMTTYPE") or "").strip().upper(),
            discretion=(r.get("INVESTMENTDISCRETION") or "").strip().upper(),
            voting_sole=_num(r.get("VOTING_AUTH_SOLE")),
            voting_shared=_num(r.get("VOTING_AUTH_SHARED")),
            voting_none=_num(r.get("VOTING_AUTH_NONE")),
            source_url=accession_url(cik, acc),
        ))
    return out


# ─────────────────────── 名称 → ticker（尽力而为）───────────────────────

_SUFFIX = re.compile(
    r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|SA|NV|AG|LLC|LP|"
    r"TRUST|GROUP|HOLDINGS?|THE|CLASS|COM|NEW|SE|CL|A|B)\b")


def normalize_issuer(name: str) -> str:
    """发行人名称归一（用于与 SEC company_tickers.json 匹配）。"""
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_ticker_map(company_tickers: dict) -> dict[str, str]:
    """SEC `company_tickers.json` → {归一名称: ticker}。

    ⚠️ **这只是尽力而为**：实测对 13F 发行人的命中率仅 **42.8%**，
    未命中的绝大多数是 ETF 与基金（`company_tickers.json` 只收运营公司）。
    所以 ticker 在本分栏里是**辅助显示字段**，
    聚合、去重、跨季度比对一律用 CUSIP。
    """
    out: dict[str, str] = {}
    for v in (company_tickers or {}).values():
        n = normalize_issuer(v.get("title", ""))
        if n and n not in out:
            out[n] = v.get("ticker", "")
    return out
