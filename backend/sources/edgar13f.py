"""SEC 13F —— 机构持仓数据源。

合规级与 Form 4 相同（S 级：EDGAR 公开记录，只限速率不限商用）。
复用 `edgar.py` 的限速器与 `contact.py` 的 UA 配置。

━━━ ⭐ 13F 到底覆盖什么：这是本分栏最要紧的一件事 ━━━

「机构持仓」这个叫法本身就容易误导。13F 报的是
**季末时点、对 13(f) 证券的多头持仓**，而且：

| 不包含 | 依据 |
|---|---|
| **空头头寸** | SEC 2023 年专门另立 Rule 13f-2 / Form SHO 报空头（2025-01 生效）—— 正因为 13F 不覆盖 |
| 现金、债券、大宗商品、外汇 | 不在 13(f) 证券清单内 |
| 仅在境外上市的股票 | 同上 |
| 未上市/私募持仓 | 同上 |
| 获保密豁免的持仓 | 可申请暂缓披露（Rule 24b-2） |

⚠️ **期权以「标的证券」的形态出现**（Form 13F 特别说明第 10 条）：
看跌期权会被标成 `PUT` 但列在标的名下。实测 2026Q1 窗口：
普通持股 $74.9 万亿 / Call $2.95 万亿 / **Put $3.66 万亿** ——
把 PUT 当成持股加总，等于**把 3.66 万亿的看空头寸算成看多**。

━━━ 数据源：季度结构化数据集（无需逐份解析 XML）━━━

⚠️ 命名不是自然季度，而是**三个月的申报日窗口**：
`01mar2026-31may2026_form13f.zip`（2026-06-01 发布）。
一个窗口里混着多个报告期 —— 实测该窗口：
2026-03-31 报告 10,776 份，但也有一路到 2008 年的补报与修订。
**所以必须按 `PERIODOFREPORT` 过滤**，不能拿整个窗口当一个季度。

⚠️ 规模比 Form 345 大一个量级：95MB 压缩 / INFOTABLE 396MB / 380 万行。

━━━ 另外两个坑 ━━━
1. **13F-NT 是「通知件」，不含任何持仓**（"我的持仓由别人申报"）。
   实测该窗口 2,045 份 —— 占 18%。不排除就会报出两千家"零持仓"的机构。
2. **只有 CUSIP 没有 ticker**。SEC 不提供 CUSIP→ticker 映射（那是商业数据）。
   用发行人名称去 `company_tickers.json` 匹配实测命中率仅 **42.8%**
   （未命中的多是 ETF/基金）—— 所以 ticker 只能当尽力而为的辅助字段，
   **主键必须是 CUSIP**。
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from typing import Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter

DATASET_INDEX = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
DATASET_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"

#: 我们要用的表
DATASET_TABLES = ("SUBMISSION", "COVERPAGE", "INFOTABLE")

#: 含持仓的 **SUBMISSIONTYPE**（EDGAR 申报类型）。NT 是「通知件」，明确不含持仓。
#:
#: ⚠️ 别把 `13F COMBINATION REPORT` 写进来 —— 它是 **COVERPAGE.REPORTTYPE** 的值，
#: 不是申报类型。实测 2026Q1 窗口：441 份组合报告的 SUBMISSIONTYPE 全是
#: `13F-HR`(410) 或 `13F-HR/A`(31)，**已经被下面这两个值覆盖**。
#: 早前把它混进本常量，害得代码审查误判成"组合报告被丢掉了"（实际一条没丢）。
HOLDINGS_TYPES = frozenset({"13F-HR", "13F-HR/A"})

_WINDOW_RE = re.compile(
    r"(\d{2}[a-z]{3}\d{4}-\d{2}[a-z]{3}\d{4})_form13f\.zip", re.I)


def list_windows(limit: int = 12) -> list[str]:
    """列出可用的数据集窗口（新→旧），如 `01mar2026-31may2026`。

    ⚠️ 只能从索引页解析 —— 命名是三个月申报日窗口，无法用日期推算
    （不像 Form 345 的 `2026q1`）。
    """
    _limiter.wait()
    try:
        r = requests.get(DATASET_INDEX, headers={"User-Agent": user_agent()},
                         timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"13F 数据集索引请求失败: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"13F 数据集索引 HTTP {r.status_code}")
    seen: list[str] = []
    for m in _WINDOW_RE.finditer(r.text):
        w = m.group(1).lower()
        if w not in seen:
            seen.append(w)
    if not seen:
        raise RuntimeError("13F 数据集索引页未解析出任何窗口（页面结构可能已变更）")
    return seen[:limit]


def download_window(window: str) -> zipfile.ZipFile:
    """下载一个窗口的 ZIP 并返回句柄（**不解析 INFOTABLE**）。

    ⚠️ 必须流式处理：把 INFOTABLE 全量读成字典列表，实测峰值内存
    **5.3 GB**（下载后 4.1GB + 解析成对象后再 1.2GB）——
    8GB 的机器会疯狂 swap 甚至被 OOM 杀掉，直接违背"clone 下来就能跑"。
    所以这里只把 ZIP 拿在手上，小表（SUBMISSION/COVERPAGE 各约 1.2 万行）
    可以全读，**INFOTABLE 只能用 `iter_table()` 边读边过滤**。
    """
    url = f"{DATASET_BASE}/{window}_form13f.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=600)
    except requests.RequestException as e:
        raise RuntimeError(f"13F 数据集下载失败: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"13F 数据集窗口不存在: {window}")
    if r.status_code != 200:
        raise RuntimeError(f"13F 数据集 HTTP {r.status_code}: {window}")
    try:
        return zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"{window} 返回的不是 ZIP（可能是错误页）") from e


def iter_table(zf: zipfile.ZipFile, table: str):
    """逐行迭代 ZIP 里的某张 TSV（**不驻留内存**）。"""
    names = {n.upper(): n for n in zf.namelist()}
    real = names.get(f"{table.upper()}.TSV")
    if real is None:
        raise RuntimeError(f"数据集缺少 {table}.tsv（结构可能已变更）")
    with zf.open(real) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        header = next(text).rstrip("\n").split("\t")
        for line in text:
            vals = line.rstrip("\n").split("\t")
            if len(vals) < len(header):
                vals += [""] * (len(header) - len(vals))
            yield dict(zip(header, vals))


def read_table(zf: zipfile.ZipFile, table: str) -> list[dict]:
    """整表读入 —— **只用于小表**（SUBMISSION / COVERPAGE 各约 1.2 万行）。

    ⛔ 绝不要拿它读 INFOTABLE（380 万行 = 4GB 内存）。
    """
    return list(iter_table(zf, table))


def window_dataset(window: str) -> dict[str, list[dict]]:
    """⚠️ **已弃用** —— 会把 INFOTABLE 全量读进内存（实测峰值 5.3GB）。

    保留仅为兼容既有调用；新代码请用
    `download_window()` + `read_table()`（小表）+ `iter_table()`（INFOTABLE）。
    """
    url = f"{DATASET_BASE}/{window}_form13f.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=600)
    except requests.RequestException as e:
        raise RuntimeError(f"13F 数据集下载失败: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"13F 数据集窗口不存在: {window}")
    if r.status_code != 200:
        raise RuntimeError(f"13F 数据集 HTTP {r.status_code}: {window}")

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"{window} 返回的不是 ZIP（可能是错误页）") from e

    names = {n.upper(): n for n in zf.namelist()}
    out: dict[str, list[dict]] = {}
    for table in DATASET_TABLES:
        real = names.get(f"{table}.TSV")
        if real is None:
            raise RuntimeError(f"{window} 数据集缺少 {table}.tsv（结构可能已变更）")
        with zf.open(real) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            header = next(text).rstrip("\n").split("\t")
            rows = []
            for line in text:
                vals = line.rstrip("\n").split("\t")
                if len(vals) < len(header):
                    vals += [""] * (len(header) - len(vals))
                rows.append(dict(zip(header, vals)))
            out[table] = rows
    return out


def periods_in(tables: dict[str, list[dict]]) -> list[tuple[str, int]]:
    """窗口里各报告期的申报份数（多→少）。

    用来让用户看清「这个窗口主要是哪一期」——
    实测 `01mar2026-31may2026` 里 2026-03-31 有 10,776 份，
    但也混着一路到 2008 年的补报。
    """
    counts: dict[str, int] = {}
    for r in tables.get("SUBMISSION", []):
        p = (r.get("PERIODOFREPORT") or "").strip()
        if p:
            counts[p] = counts.get(p, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


def window_end(window: str) -> Optional[date]:
    """`01mar2026-31may2026` → date(2026, 5, 31)（窗口截止日）。"""
    m = re.match(r"\d{2}[a-z]{3}\d{4}-(\d{2})([a-z]{3})(\d{4})", window, re.I)
    if not m:
        return None
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    try:
        return date(int(m.group(3)), months[m.group(2).lower()], int(m.group(1)))
    except (KeyError, ValueError):
        return None


# ─────────────────────── 官方 13F 证券清单 ───────────────────────

#: SEC 每季发布的「Section 13(f) 证券官方清单」——
#: **CUSIP → 规范发行人名称**的权威来源。
SEC_LIST_BASE = "https://www.sec.gov/files/investment"


def securities_list(quarter: str) -> list[dict]:
    """下载官方 13(f) 证券清单（定宽文本）。`quarter` 形如 `2026q2`。

    ⭐ 为什么必须要它：INFOTABLE 里的 `NAMEOFISSUER` 是**申报人自由填写**的，
    实测苹果那个 CUSIP 有 **61 种不同写法**，其中还混着
    `VANGUARD WHITEHALL FDS` 这种完全填错的（别家公司的名字挂在苹果的 CUSIP 上）。
    拿这些名字做展示或分组，会把标的张冠李戴。

    定宽格式（SEC 页面明示）：
        CUSIP 1-9 / 期权标记 10 / 发行人名 11-40 / 类别描述 41-67 / 状态 68-70
    """
    url = f"{SEC_LIST_BASE}/13flist{quarter}-txt.txt"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"13F 证券清单请求失败: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"13F 证券清单不存在: {quarter}")
    if r.status_code != 200:
        raise RuntimeError(f"13F 证券清单 HTTP {r.status_code}: {quarter}")

    out: list[dict] = []
    for line in r.text.splitlines():
        if len(line) < 40:
            continue
        cusip = line[0:9].strip().upper()
        if not cusip or not cusip[0].isalnum():
            continue
        out.append({
            "cusip": cusip,
            "has_option": line[9:10].strip() == "*",
            "issuer": line[10:40].strip(),
            "class": line[40:67].strip(),
            "status": line[67:70].strip(),
        })
    if not out:
        raise RuntimeError(f"13F 证券清单 {quarter} 未解析出任何行（格式可能已变更）")
    return out


def list_quarters_for(period: date, back: int = 4) -> list[str]:
    """给定报告期，返回可能对应的清单季度标识（新→旧，用于回退尝试）。"""
    y, q = period.year, (period.month - 1) // 3 + 1
    out = []
    for _ in range(back):
        out.append(f"{y}q{q}")
        q += 1
        if q > 4:
            q, y = 1, y + 1
    return out
