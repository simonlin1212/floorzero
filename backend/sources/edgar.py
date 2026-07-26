"""SEC EDGAR —— 内部人 Form 4 数据源。

━━━ 合规级 S：美国政府公开记录，商用 ✅ 再分发 ✅ ━━━
与国会披露同级，是本项目里可以放心对外展示的数据。

━━━ ⭐ 两条路径，缺一不可（2026-07-26 实测）━━━

| | 季度结构化数据集 | 每日申报 XML |
|---|---|---|
| 内容 | SEC 预解析好的 TSV | 逐份 Form 4 原文 |
| 覆盖 | 完整历史，一个 ZIP 一个季度 | 当日全量 |
| 成本 | 1 次请求 13MB ≈ 10 万笔交易 | **每份申报 1 次请求**（单日 645 份） |
| 时效 | ⚠️ **滞后 7~49 天不等** | 实时 |

实测滞后：2025Q4 发布于 2026-01-07（+7 天）、2026Q1 于 2026-04-07（+7 天）、
2025Q3 于 2025-11-18（**+49 天**）—— 没有稳定规律。
2026-07-26 当天最新可用仅到 2026Q1，**缺口 116 天**。

→ 所以架构必须是「**ZIP 补历史（便宜）+ XML 补近期（贵但窗口小）**」。
只用 ZIP 会永远看不到最近几个月；只用 XML 则回补一年要几万次请求。

━━━ 限速 ━━━
SEC 官方要求：≤10 请求/秒，且必须带可识别的 User-Agent（含联系方式）。
这里自律设 8/秒。
"""
from __future__ import annotations

import io
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import requests

from sources.contact import user_agent

#: ⚠️ UA 由**部署者自行配置**（FZ_CONTACT），绝不硬编码任何人的邮箱 ——
#: 否则开源后每个用户的流量都以作者身份发出，限流封禁都算在他头上。
#: 详见 sources/contact.py。

ARCHIVES = "https://www.sec.gov/Archives"
DATASET_BASE = ("https://www.sec.gov/files/structureddata/data/"
                "insider-transactions-data-sets")


class NoXmlInFiling(RuntimeError):
    """该申报**文档本身没有 XML**（2003 年前的纯文本格式）—— 终态，重试也没用。

    ⚠️ 与「对象取不到」严格区分：后者可能只是刚索引、正文还没同步过来，
    过一会儿就有了。两者都用 DataNotAvailable 的话，
    一次传播延迟就会让那份申报被永久登记成"死件"、再也不抓。
    """


class DataNotAvailable(RuntimeError):
    """该日/该季度确实没有数据（非交易日、季度尚未发布）—— 调用方可安全跳过。

    ⚠️ 与「被拒绝 / 限流 / 网络故障」严格区分：后者必须冒泡。
    S3 托管的 SEC Archives 在对象不存在时可能返回 **403 AccessDenied（XML）而非 404**
    （无 ListBucket 权限时的标准行为）—— 这个坑 global-stock-data v2.0.1 踩过，
    所以下面用 `_is_object_missing()` 靠响应体区分，不能只看状态码。
    """


class _RateLimiter:
    """线程安全的最小间隔节流器。"""

    def __init__(self, max_per_sec: float) -> None:
        self._interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_limiter = _RateLimiter(8)          # SEC 上限 10/秒，留余量


#: 旁证 URL：收到 403 时用它判断"是不是我被封了"。
#: 必须是**长期稳定存在**的轻量资源 —— 用某份具体申报当旁证是错的
#: （申报会过期、路径会猜错，一旦它 404，所有正常的 403 都会被误判成"被封"）。
#: 这个目录索引只有 3.7KB 且始终存在。
_CANARY = f"{ARCHIVES}/edgar/daily-index/index.json"


def _is_object_missing(resp: requests.Response) -> bool:
    """区分「对象不存在」与「被拒绝」—— 实测后确认**光看响应做不到**。

    2026-07-26 实测 SEC Archives（S3 托管）：

    | 情况 | 响应 |
    |---|---|
    | 缺失的**文件对象** | `404` + `<Code>NoSuchKey</Code>` |
    | 缺失的**每日索引**（周末/未来日期） | `403` + `<Code>AccessDenied</Code>` |
    | **客户端被封** | `403` + `<Code>AccessDenied</Code>`（**响应体一模一样**）|

    最后两种在响应层面无法区分 —— 无 ListBucket 权限时，S3 对不存在的
    key 就回 AccessDenied。所以这里对 403 **不靠响应体下结论**，
    而是去探一个必定存在的资源当旁证：
    - 旁证通 → 我们没被封 → 这个 403 是"该资源确实没有"
    - 旁证也挂 → **是被封了** → 必须冒泡

    早前只按 `403 + AccessDenied` 判"没有"，会在被封时把每一天都记成
    "当天无申报"并标记完成 —— 之后**永远不再重试**，而且全程不报错。
    """
    if resp.status_code == 404:
        return True                      # NoSuchKey，明确
    if resp.status_code != 403:
        return False
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "xml" not in ctype or "AccessDenied" not in (resp.text or "")[:500]:
        return False                     # 非 S3 式 403 → 一律当被拒绝
    return _canary_ok()


_canary_state: dict[str, float] = {}
_canary_lock = threading.Lock()


def _canary_ok(ttl: float = 60.0) -> bool:
    """探测"我们还能正常访问 SEC 吗"（结果缓存 60 秒，403 本身很少见）。"""
    with _canary_lock:
        ts = _canary_state.get("t", 0.0)
        if time.monotonic() - ts < ttl:
            return bool(_canary_state.get("ok"))
    ok = False
    try:
        _limiter.wait()
        r = requests.get(_CANARY, headers={"User-Agent": user_agent()},
                         timeout=30, stream=True)
        ok = r.status_code == 200
        r.close()
    except requests.RequestException:
        ok = False
    with _canary_lock:
        _canary_state["t"] = time.monotonic()
        _canary_state["ok"] = ok
    return ok


def _get(url: str, timeout: int = 60, binary: bool = False):
    """统一取数：正向识别「没有」，其余一律冒泡。"""
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"EDGAR 网络故障: {type(e).__name__}: {e}") from e
    if _is_object_missing(r):
        raise DataNotAvailable(f"EDGAR 无此资源: {url[-80:]}")
    if r.status_code == 429:
        raise RuntimeError(f"EDGAR 限流 (429)：降低请求频率。{url[-60:]}")
    if r.status_code == 403:
        raise RuntimeError(
            f"EDGAR 拒绝访问 (403)，且旁证请求同样失败 —— **这是被拒绝不是没数据**。"
            f"常见原因：User-Agent 不合规、请求过快被临时封禁。{url[-60:]}")
    if r.status_code != 200:
        raise RuntimeError(f"EDGAR HTTP {r.status_code}: {url[-80:]}")
    return r.content if binary else r.text


# ─────────────────────── 每日申报索引 ───────────────────────

@dataclass(frozen=True)
class FilingRef:
    """一份申报的索引条目（不含明细 —— 明细要再取正文）。"""

    form: str
    company: str
    cik: str
    filed: str                    # YYYY-MM-DD
    accession: str                # 0000066740-26-000255
    txt_url: str                  # 全文提交件（含 XML）


def _recent_weekdays(n: int, end: Optional[date] = None) -> Iterator[date]:
    d = end or date.today()
    got = 0
    while got < n:
        if d.weekday() < 5:                       # 周末没有索引
            yield d
            got += 1
        d -= timedelta(days=1)


def _norm_idx_date(raw: str) -> Optional[str]:
    """索引里的申报日 → `YYYY-MM-DD`。识别不了返回 None。"""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    return None


def form4_filings(day: date) -> list[FilingRef]:
    """某一天的全部 Form 4 申报索引。

    ⚠️ 只返回索引，**明细在各自的正文里**（每份要单独取一次）。
    """
    q = (day.month - 1) // 3 + 1
    url = f"{ARCHIVES}/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"
    raw = _get(url)

    lines = raw.splitlines()
    start = next((i + 1 for i, L in enumerate(lines) if L.startswith("---")), 11)
    out: list[FilingRef] = []
    for line in lines[start:]:
        # ⚠️ **从右往左切，不要按列位切**。
        # 表头本身就跨两行（"Form Type Company Name CIK" / "Date Filed File Name"），
        # 按固定字节偏移解析实测直接切错（把 "4    edgar/data/..." 当成路径），
        # 而且 SEC 历史上改过列宽。路径/日期/CIK 都不含空格、公司名可能含空格 ——
        # 所以按空白从右侧取 3 个字段最稳，剩下的首 token 是表单类型、中间是公司名。
        parts = line.split()
        if len(parts) < 4:
            continue
        form = parts[0]
        # 4/A 是修订件：**要抓下来**（否则逐日与季度两条路径覆盖范围不一致），
        # 但入库后带 form_type 标记，聚合时默认排除以免与原件重复计数。
        if form not in ("4", "4/A"):
            continue
        path, filed_raw, cik = parts[-1], parts[-2], parts[-3]
        if not path.endswith(".txt") or not cik.isdigit():
            continue                               # 行格式不符预期，跳过而不是猜
        # 日期实测 2020-2026 六年始终是 YYYYMMDD；同时兼容 ISO，
        # 万一 SEC 改格式也不至于整天数据无声消失
        filed = _norm_idx_date(filed_raw)
        if filed is None:
            continue
        company = " ".join(parts[1:-3])
        # edgar/data/66740/0000066740-26-000255.txt → accession
        acc = path.rsplit("/", 1)[-1].removesuffix(".txt")
        out.append(FilingRef(form=form, company=company, cik=cik, filed=filed,
                             accession=acc, txt_url=f"{ARCHIVES}/{path}"))
    if not out:
        # ⚠️ 要区分「当天真的没有申报」与「解析器读不懂这个索引」。
        # 都返回 DataNotAvailable 的话，SEC 一改格式就会表现为
        # "每天都没有申报" —— 跑完不报错、数据一片空白，最难查的那种故障。
        data_lines = [L for L in lines[start:] if L.strip()]
        if data_lines:
            raise RuntimeError(
                f"{day} 的索引有 {len(data_lines)} 行数据，却一条 Form 4 都没解析出来 —— "
                f"索引格式可能已变更。首行样例：{data_lines[0][:90]!r}")
        raise DataNotAvailable(f"{day} 无 Form 4 申报（可能是非交易日）")
    return out


def recent_form4_days(days: int, end: Optional[date] = None) -> list[date]:
    """最近 N 个工作日（不判断是否真有索引，取数时才知道）。"""
    return list(_recent_weekdays(days, end))


def pending_form4_days(count: int, settled: set[str], max_back: int = 400,
                       end: Optional[date] = None,
                       floor: Optional[date] = None) -> list[date]:
    """往回走，跳过已完成的日子，取前 `count` 个**待处理**工作日。

    ⚠️ 不能用 `recent_form4_days(N)`：那永远只看最近 N 个工作日。
    季度数据集与今天之间的缺口实测有 117 天（≈83 个工作日），
    一旦最近这批都同步完，重复跑也只是把同一批再过滤一遍 ——
    更早的部分**永远补不上**，而且跑完不报错，看不出来还缺着。
    这样改之后，反复点同步就能一段段把缺口填满。

    `floor`：**不要走过这一天**（含）。传已导入季度的截止日 ——
    缺口补完后若继续往回走，就会去逐份重下季度 ZIP 里已有的数据
    （600-700 份/天、约 90 秒/天），一路走进好几年的重复历史。
    """
    out: list[date] = []
    d = end or date.today()
    walked = 0
    while len(out) < count and walked < max_back:
        if floor is not None and d <= floor:
            break                                  # 已进入季度数据集覆盖区，停
        if d.weekday() < 5 and d.isoformat() not in settled:
            out.append(d)
        d -= timedelta(days=1)
        walked += 1
    return out


def filing_xml(txt_url: str) -> str:
    """从全文提交件里抽出 ownershipDocument XML。

    Form 4 的 `.txt` 是 SGML 多文档容器，XML 夹在 `<XML>…</XML>` 里。
    取 `.txt` 只需 **1 次请求**（比先取目录再取 xml 省一半）。
    """
    raw = _get(txt_url)
    m = re.search(r"<XML>(.*?)</XML>", raw, re.S)
    if not m:
        # 文档确实没有 XML（2003 年前的纯文本格式）—— 这是**终态**
        raise NoXmlInFiling(f"该申报不含 XML（早期纯文本格式）: {txt_url[-60:]}")
    return m.group(1).strip()


# ─────────────────────── 季度结构化数据集 ───────────────────────

#: 数据集里我们要用的三张表
DATASET_TABLES = ("SUBMISSION", "REPORTINGOWNER", "NONDERIV_TRANS")


def dataset_quarters(back: int = 8, today: Optional[date] = None,
                     start: Optional[str] = None) -> list[str]:
    """最近 N 个季度标识（新→旧），如 ['2026q2', '2026q1', …]。

    `start` 指定起点季度（如 '2026q1'）；不传则从当前自然季度算起。

    ⚠️ 数据集**永远滞后**，从当前自然季度往回数会把最前面 1-2 个槽位
    浪费在尚未发布的季度上 —— `back=2` 实测一笔都导不进来。
    要"最近 N 个**可用**季度"请用 `available_quarters()`。
    """
    if start:
        y, q = int(start.split("q")[0]), int(start.split("q")[1])
    else:
        d = today or date.today()
        y, q = d.year, (d.month - 1) // 3 + 1
    out = []
    for _ in range(back):
        out.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


def quarter_dataset(quarter: str) -> dict[str, list[dict]]:
    """下载并解析某季度的内部人交易数据集。

    返回 {表名: [行字典]}。⚠️ 单季度约 10 万笔交易，内存里放得下但不小。

    ⚠️ 季度尚未发布时抛 `DataNotAvailable`（**不是错误**）——
    最新一两个季度经常还没出，滞后 7~49 天不等，没有稳定规律。
    """
    url = f"{DATASET_BASE}/{quarter}_form345.zip"
    blob = _get(url, timeout=180, binary=True)
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"{quarter} 返回的不是 ZIP（可能是错误页）") from e

    out: dict[str, list[dict]] = {}
    names = {n.upper(): n for n in zf.namelist()}
    for table in DATASET_TABLES:
        real = names.get(f"{table}.TSV")
        if real is None:
            raise RuntimeError(f"{quarter} 数据集缺少 {table}.tsv（结构可能已变更）")
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


def latest_available_quarter(back: int = 8) -> tuple[Optional[str], list[str]]:
    """探测最新已发布的季度，返回 (最新季度, 已探测但未发布的季度列表)。

    用来在 UI 上如实告诉用户「数据集只到 X 季度，之后的要靠逐日抓取」。
    """
    missing: list[str] = []
    for q in dataset_quarters(back):
        url = f"{DATASET_BASE}/{q}_form345.zip"
        _limiter.wait()
        try:
            r = requests.head(url, headers={"User-Agent": user_agent()}, timeout=30)
        except requests.RequestException as e:
            raise RuntimeError(f"探测季度数据集失败: {type(e).__name__}: {e}") from e
        if r.status_code == 200:
            return q, missing
        # ⚠️ **只有 404 才算"尚未发布"**。
        # 实测该端点是 nginx/Drupal（不是 S3），未发布季度干净地返回 404 +
        # HTML 错误页；403 只可能是客户端/IP 被拒。
        # 把 403 也算成"未发布"，会在被封时让所有季度都"查无此档" ——
        # 同步报成功、一笔历史都没导入，正是最难排查的静默失败。
        if r.status_code == 404:
            missing.append(q)
            continue
        if r.status_code == 403:
            raise RuntimeError(
                f"探测 {q} 被拒绝 (403) —— 这是「被拒绝」不是「尚未发布」。"
                f"多为 User-Agent 不合规或 IP 被 SEC 限制，请检查后重试。")
        raise RuntimeError(f"探测 {q} 得到 HTTP {r.status_code}")
    return None, missing


def available_quarters(back: int = 4) -> tuple[list[str], list[str]]:
    """最近 N 个**已发布**季度（新→旧）+ 探测到的未发布季度。

    这才是同步该用的入口：直接用 `dataset_quarters()` 会把额度
    花在还没发布的季度上，结果"跑完了、没报错、一笔没导入"。
    """
    latest, missing = latest_available_quarter()
    if latest is None:
        return [], missing
    return dataset_quarters(back, start=latest), missing


def quarter_end(quarter: str) -> date:
    """'2026q1' → date(2026, 3, 31)。用于判断数据集覆盖到哪天。"""
    y, q = quarter.split("q")
    m = int(q) * 3
    last = {3: 31, 6: 30, 9: 30, 12: 31}[m]
    return date(int(y), m, last)
