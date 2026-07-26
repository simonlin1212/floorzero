"""国会议员交易披露（STOCK Act）—— 众议院 + 参议院官方源。

━━━ ⚠️ 合规：数据公开，但**商用被法律明文禁止** ━━━
**5 U.S.C. §13107(c)(1)**（Ethics in Government Act，2026-07-26 实读法条原文）：

    "It shall be unlawful for any person to obtain or use a report—
     (B) for any commercial purpose, other than by news and communications
         media for dissemination to the general public"

§13107(c)(2)：司法部长可提起民事诉讼，**罚款上限 $10,000**。
§13107(a) 明确涵盖「the Clerk of the House of Representatives, and the
Secretary of the Senate」—— **两院都适用**，不是只管参议院。

→ 因此本数据源的正确定性是：
  - 公众获取、个人研究、学术、新闻媒体面向公众传播 ✅
  - **任何商业用途 ❌**（卖服务、卖订阅、做付费产品的一部分都不行）

⚠️ 这与 SEC EDGAR **不同**：EDGAR 的官方条款只限速率（10 请求/秒）与要求声明
User-Agent，明写 "Anyone can access and download this information for free"，
没有商用限制。别把两条线的合规级混为一谈 ——
本文件早前曾错标为「商用 ✅ 再分发 ✅」，是错的。

→ 对 FloorZero 的含义：本项目是**免费开源、用户自部署**，用户自己跑来做个人研究
  落在允许范围内；但**这条线不能成为任何收费产品的一部分**，
  也不适合当作"可以放心商用"的对外招牌。

━━━ 两院的实况差异很大（2026-07-26 实测）━━━

| | 众议院 House | 参议院 Senate |
|---|---|---|
| 索引 | 年度 ZIP，内含 XML | POST 搜索接口，返回 JSON |
| 访问 | `requests` 直连 ✅ | ⚠️ **Akamai 按 TLS 指纹拦截** |
| 明细 | PDF（文字可提取） | HTML 表格（**自带 Ticker 列**） |
| 标的代码 | 夹在资产名的括号里 | 独立字段，干净 |

⚠️ **参议院必须用 `curl_cffi` 做 TLS 伪装**：实测 `requests` 无论怎么伪装
请求头都是 403（连 robots.txt 都拿不到），而真实 Chrome 能进 ——
拦的是 TLS 指纹不是 UA，也**不是地域封锁**（美国 IP 的服务器同样被挡）。
`curl_cffi` 是**可选依赖**：没装就如实报「参议院不可用 + 怎么装」，
绝不把「装不了依赖」伪装成「参议院没有数据」。

━━━ ⚠️ 解析上的两个真陷阱 ━━━
1. **众议院 PDF 里 `[XX]` 是资产类型代码，不是股票代码。**
   `Treasury Bill ... [GS]` 的 GS = Government Securities，**不是高盛**。
   真正的代码在圆括号里：`Abbott Laboratories Common Stock (ABT) [ST]`。
   用 `\\[([A-Z]{2})\\]` 抓代码会产出大量看着像真的垃圾数据。
2. **参议院有「纸质扫描件」**（链接是 `/view/paper/` 而非 `/view/ptr/`），
   那是图片，无 OCR 解析不了。必须如实标成「纸质件未解析」，不能静默丢弃 ——
   否则用户以为看到的是全部。
"""
from __future__ import annotations

import io
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import requests

from sources.contact import user_agent

# ⚠️ UA 由**部署者自行配置**（FZ_CONTACT），绝不硬编码任何人的邮箱。
# 详见 sources/contact.py。

HOUSE_BASE = "https://disclosures-clerk.house.gov/public_disc"
SENATE_BASE = "https://efdsearch.senate.gov"

#: 众议院 FilingType 代码 → 含义。P = 我们要的定期交易报告。
HOUSE_FILING_TYPES = {
    "P": "定期交易报告 (PTR)",
    "A": "年度报告",
    "C": "候选人报告",
    "D": "候选人报告（修订）",
    "W": "离职报告",
    "X": "延期申请",
    "H": "听证",
    "T": "终止报告",
}

#: STOCK Act 法定期限（众议院 PTR 表格原文，2026-07-26 实读）：
#: "30 days from when you became aware of the transaction,
#:  but no later than 45 days after the transaction."
#: ⚠️ 周末/假日会顺延，所以「>45 天」是**事实观察**不是违规认定 —— 见 modules/congress.py
STOCK_ACT_HARD_CAP_DAYS = 45


class DataNotAvailable(RuntimeError):
    """该年份/该文件确实没有 —— 调用方可安全跳过。

    与「被拒绝 / 缺依赖 / 网络故障」区分开：后者必须冒泡。
    这条边界在本项目已经踩错过两次（global-stock-data v2.0.1 的 403、
    FloorZero 九轮审计的 #6），不要再犯第三次。
    """


class SenateUnavailable(RuntimeError):
    """参议院数据源当前拿不到 —— **不是**「参议院没有交易」。

    单独立一个类型，就是为了让前端能把「装不了 curl_cffi」「被 Akamai 拦」
    这类**环境问题**显示成环境问题，而不是显示成一张空表让用户误以为
    参议员这段时间没交易。
    """


class _RateLimiter:
    """线程安全的最小间隔节流器（用锁，避免并发下被击穿）。"""

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


# 政府站点，自律放慢：这不是高频接口，跑快了没好处
_limiter = _RateLimiter(3)


@dataclass(frozen=True)
class Filing:
    """一份披露文件的**元数据**（不含交易明细 —— 那要再抓正文）。"""

    chamber: str                  # "house" | "senate"
    name: str                     # 显示用姓名
    last: str
    first: str
    state_district: str           # 众议院 "IN02"；参议院是州或空
    filing_type: str              # 原始代码（众议院）或报告标题（参议院）
    filing_date: Optional[date]   # 归档日
    year: str
    doc_id: str                   # 众议院 DocID / 参议院 UUID
    detail_url: str
    is_paper: bool = False        # ⚠️ 纸质扫描件：无法解析明细

    @property
    def is_ptr(self) -> bool:
        """是否定期交易报告（我们唯一关心的类型）。"""
        if self.chamber == "house":
            return self.filing_type == "P"
        return "periodic transaction" in self.filing_type.lower()


def _parse_us_date(s: Optional[str]) -> Optional[date]:
    """解析 M/D/YYYY 或 MM/DD/YYYY；解析不了返回 None（不抛，源数据本来就有脏值）。"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


# ────────────────────────────── 众议院 ──────────────────────────────

def house_filings(year: int) -> list[Filing]:
    """拉某年度的众议院披露索引（年度 ZIP → 内含 XML）。

    ⚠️ 这里只有**元数据**（谁、什么类型、哪天归档），交易明细在各自的 PDF 里。
    """
    url = f"{HOUSE_BASE}/financial-pdfs/{year}FD.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=45)
        if r.status_code == 404:
            raise DataNotAvailable(f"众议院无 {year} 年度披露文件（年份太早或尚未发布）")
        r.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {403: "被拒绝（限流或封禁）", 429: "请求过快"}.get(code, "")
        raise RuntimeError(f"众议院 HTTP {code} {hint}: {url}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"众议院网络故障: {type(e).__name__}: {e}") from e

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise RuntimeError(f"众议院 {year} ZIP 内无 XML（结构可能已变更）")
        raw = zf.read(xml_names[0])
    except zipfile.BadZipFile as e:
        # 上游返回错误页而非 ZIP 时会走到这里 —— 别让它伪装成「没数据」
        raise RuntimeError(f"众议院 {year} 返回的不是 ZIP（可能是错误页）") from e

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(f"众议院 {year} XML 解析失败: {e}") from e

    out: list[Filing] = []
    for m in root:
        doc_id = (m.findtext("DocID") or "").strip()
        if not doc_id:
            continue
        last = (m.findtext("Last") or "").strip()
        first = (m.findtext("First") or "").strip()
        yr = (m.findtext("Year") or str(year)).strip()
        out.append(Filing(
            chamber="house",
            name=" ".join(x for x in (first, last) if x),
            last=last, first=first,
            state_district=(m.findtext("StateDst") or "").strip(),
            filing_type=(m.findtext("FilingType") or "").strip(),
            filing_date=_parse_us_date(m.findtext("FilingDate")),
            year=yr, doc_id=doc_id,
            detail_url=f"{HOUSE_BASE}/ptr-pdfs/{yr}/{doc_id}.pdf",
        ))
    if not out:
        raise DataNotAvailable(f"众议院 {year} 索引为空")
    return out


def house_ptr_pdf(year: str, doc_id: str) -> bytes:
    """下载一份众议院 PTR 的 PDF 原件。"""
    url = f"{HOUSE_BASE}/ptr-pdfs/{year}/{doc_id}.pdf"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=45)
        if r.status_code == 404:
            raise DataNotAvailable(f"众议院无此 PTR 文件: {year}/{doc_id}")
        r.raise_for_status()
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {403: "被拒绝（限流或封禁）", 429: "请求过快"}.get(code, "")
        raise RuntimeError(f"众议院 PDF HTTP {code} {hint}: {url}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"众议院 PDF 网络故障: {type(e).__name__}: {e}") from e

    if not r.content.startswith(b"%PDF"):
        # 正经报错好过把一个 HTML 错误页喂给 PDF 解析器
        raise RuntimeError(f"众议院 {year}/{doc_id} 返回的不是 PDF（可能是错误页）")
    return r.content


# ────────────────────────────── 参议院 ──────────────────────────────

_SENATE_HINT = (
    "参议院 eFD 由 Akamai 按 TLS 指纹拦截，普通 HTTP 客户端一律 403。"
    "需安装可选依赖：pip install curl_cffi"
)

_senate_session = None
_senate_lock = threading.Lock()


def _senate_client():
    """建立通过 Akamai 的会话（含接受条款那一步）。

    ⚠️ 每一步失败都要报清「是哪一步、为什么」——
    这条链路有 4 个环节（装依赖 / 首页 / CSRF / 接受条款），
    含糊一句「参议院失败」会让用户完全无从下手。
    """
    global _senate_session
    with _senate_lock:
        if _senate_session is not None:
            return _senate_session
        try:
            from curl_cffi import requests as cr
        except ImportError as e:
            raise SenateUnavailable(f"缺少 curl_cffi。{_SENATE_HINT}") from e

        s = cr.Session(impersonate="chrome")
        try:
            home = s.get(f"{SENATE_BASE}/search/home/", timeout=30)
        except Exception as e:
            raise SenateUnavailable(f"参议院首页请求失败: {type(e).__name__}: {e}") from e
        if home.status_code == 403:
            raise SenateUnavailable(f"参议院首页 403。{_SENATE_HINT}")
        if home.status_code != 200:
            raise SenateUnavailable(f"参议院首页 HTTP {home.status_code}")

        tok = re.search(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)", home.text)
        if not tok:
            raise SenateUnavailable("参议院首页未找到 CSRF token（页面结构可能已变更）")

        # eFD 要求先勾选「不得用于商业用途以外的禁止用途」声明才放行搜索
        r = s.post(f"{SENATE_BASE}/search/home/",
                   data={"prohibition_agreement": "1",
                         "csrfmiddlewaretoken": tok.group(1)},
                   headers={"Referer": f"{SENATE_BASE}/search/home/"}, timeout=30)
        if r.status_code != 200:
            raise SenateUnavailable(f"参议院条款确认失败 HTTP {r.status_code}")
        if not s.cookies.get("csrftoken"):
            raise SenateUnavailable("参议院会话未建立（无 csrftoken cookie）")
        _senate_session = s
        return s


#: eFD 服务端把每页条数**硬压到 100**（实测请求 250 只回 100）——
#: 不翻页就会静默丢数据：自 2020 起共 949 份 PTR，只取第一页等于丢掉 89%。
SENATE_PAGE_SIZE = 100


def senate_filings(start: str, end: str = "", limit: int = 500) -> list[Filing]:
    """搜索参议院 PTR（自动翻页直到取满或取完）。

    start / end 用 `MM/DD/YYYY`。**按归档日筛选**（不是交易日）。

    ⚠️ 必须翻页：服务端每页最多给 100 条，而 `recordsTotal` 常常远大于此。
    只读第一页的话，更早的申报**永远进不了缓存**（后续同步又只会拿到同一页，
    再按 known_doc_ids 全部过滤掉），且整个过程一声不吭。
    """
    out: list[Filing] = []
    offset = 0
    total: Optional[int] = None
    while True:
        page, total = _senate_page(start, end, offset)
        out.extend(page)
        offset += SENATE_PAGE_SIZE
        if not page or len(out) >= limit or (total is not None and offset >= total):
            break
    return out[:limit]


def _senate_page(start: str, end: str, offset: int) -> tuple[list[Filing], Optional[int]]:
    """取一页，返回 (本页 Filing, 总记录数)。"""
    s = _senate_client()
    csrf = s.cookies.get("csrftoken")
    _limiter.wait()
    try:
        r = s.post(
            f"{SENATE_BASE}/search/report/data/",
            data={"start": str(offset), "length": str(SENATE_PAGE_SIZE),
                  "report_types": "[11]",              # 11 = Periodic Transaction Report
                  "filer_types": "[]",
                  "submitted_start_date": f"{start} 00:00:00",
                  "submitted_end_date": f"{end} 23:59:59" if end else "",
                  "candidate_state": "", "senator_state": "", "office_id": "",
                  "first_name": "", "last_name": "", "csrfmiddlewaretoken": csrf},
            headers={"Referer": f"{SENATE_BASE}/search/",
                     "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf},
            timeout=45)
    except Exception as e:
        raise SenateUnavailable(f"参议院搜索请求失败: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise SenateUnavailable(f"参议院搜索 HTTP {r.status_code}")
    try:
        payload = r.json()
    except Exception as e:
        raise SenateUnavailable("参议院搜索返回非 JSON（会话可能已过期）") from e

    rows_total = payload.get("recordsTotal")
    out: list[Filing] = []
    for row in payload.get("data", []):
        if len(row) < 5:
            continue
        first, last, office, title_html, filed = row[0], row[1], row[2], row[3], row[4]
        link = re.search(r'href="([^"]+)"', title_html or "")
        href = link.group(1) if link else ""
        title = re.sub(r"<[^>]+>", "", title_html or "").strip()
        # ⚠️ /view/paper/ = 纸质扫描件（图片），明细解析不了
        is_paper = "/paper/" in href
        doc_id = href.rstrip("/").rsplit("/", 1)[-1] if href else ""
        out.append(Filing(
            chamber="senate",
            name=re.sub(r"\s+", " ", f"{first} {last}").strip().rstrip(","),
            last=(last or "").strip(), first=(first or "").strip(),
            state_district=re.sub(r"<[^>]+>", "", office or "").strip(),
            filing_type=title,
            filing_date=_parse_us_date(re.sub(r"<[^>]+>", "", filed or "")),
            year=str(_parse_us_date(re.sub(r"<[^>]+>", "", filed or "")) or "")[:4],
            doc_id=doc_id,
            detail_url=f"{SENATE_BASE}{href}" if href else "",
            is_paper=is_paper,
        ))
    return out, rows_total if isinstance(rows_total, int) else None


def senate_ptr_html(detail_url: str) -> str:
    """取参议院 PTR 明细页 HTML（电子申报件才有表格）。"""
    if "/paper/" in detail_url:
        raise DataNotAvailable(
            "这是纸质扫描件（图片），无 OCR 无法解析明细 —— 只能人工打开原件查看")
    s = _senate_client()
    _limiter.wait()
    try:
        r = s.get(detail_url, timeout=40)
    except Exception as e:
        raise SenateUnavailable(f"参议院明细页请求失败: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"参议院无此申报: {detail_url}")
    if r.status_code != 200:
        raise SenateUnavailable(f"参议院明细页 HTTP {r.status_code}")
    return r.text


def senate_available() -> tuple[bool, str]:
    """探测参议院这条线当前能不能用（供 UI 如实显示状态，而不是画一张空表）。"""
    try:
        _senate_client()
        return True, "可用"
    except SenateUnavailable as e:
        return False, str(e)
