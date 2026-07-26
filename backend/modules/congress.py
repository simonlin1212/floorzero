"""国会议员交易解析与聚合。

━━━ 解析这件事为什么不能想当然 ━━━
众议院给的是 PDF，参议院给的是 HTML，两边字段还不一样。
实测 12 份真实 PDF 后确认了 5 种必须处理的形态（都真踩过，别删这些分支）：

1. **10.5% 是纸质扫描件**（313 份 PTR 里 33 份，DocID 为 7 位）——
   43 页纯图片、0 个字符。**必须如实标注「未解析」**，
   静默返回空列表等于告诉用户"这位议员没交易"。
2. **金额会跨行**：`$15,001 -` 换行后才是 `$50,000`。
3. **两个日期粘连**：`06/12/202607/08/2026`（交易日 + 通知日，中间无分隔）。
4. **资产名与交易行分离**：交易行常常只有 `S 07/20/2026...`，
   资产名在它前面几行 —— 所以按「交易锚点」切块，锚点前的文本才是资产。
5. **PDF 提取会混入 `\\x00`**（某些字形），必须清洗。

⚠️ **最大的坑：`[XX]` 是资产类型代码，不是股票代码。**
`Treasury Bill ... [GS]` 的 GS = Government Securities，**不是高盛**。
股票代码在圆括号里：`Abbott Laboratories Common Stock (ABT) [ST]`。
拿方括号当代码会产出一堆看着很像真的垃圾。
"""
from __future__ import annotations

import io
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sources.congress import STOCK_ACT_HARD_CAP_DAYS, Filing

#: 众议院资产类型代码（完整表：https://fd.house.gov/reference/asset-type-codes.aspx）
#: 只列常见的；未知代码原样保留，不硬塞进已知分类。
ASSET_TYPES = {
    "ST": "股票", "EF": "ETF", "OP": "期权", "CS": "公司债",
    "GS": "政府债/机构债", "MF": "共同基金", "CT": "加密货币",
    "OT": "其他", "OL": "其他证券", "BA": "银行账户", "AB": "资产支持证券",
    "ET": "ETN", "FU": "期货", "HN": "对冲基金/PE", "HE": "对冲基金/PE(EIF)",
    "PS": "非公开股权", "RP": "房地产", "MA": "共同基金账户",
}

#: 交易类型代码
TX_TYPES = {
    "P": "买入", "S": "卖出", "S (partial)": "部分卖出", "E": "交换",
}

#: 交易锚点：类型 + 交易日 + 通知日 + 金额。
#: ⚠️ 金额有两种形态，都必须认：
#:   区间（绝大多数）`$1,001 - $15,000`  ——  STOCK Act 只要求按档披露
#:   精确值（少数议员自愿填）`$2,722.50`  ——  实测 Wasserman Schultz 2026-07-14 就是这种
#: 只写区间那一种，会把这类申报整份判成"未识别到交易行"（静默丢数据）。
_MONEY = r"\$[\d,]+(?:\.\d{2})?"
_TX_ANCHOR = re.compile(
    r"\b(?P<type>S \(partial\)|[PSE])\s+"
    r"(?P<tx_date>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<notify_date>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<amount>" + _MONEY + r"(?:\s*-\s*" + _MONEY + r")?)"
)

#: 资产块里的「(代码) [类型]」。代码允许 1-5 位字母 + 可选点号（如 BRK.B）。
_TICKER_TYPE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z0-9]{2})\]")
#: 有些条目只有资产类型没有代码
_TYPE_ONLY = re.compile(r"\[([A-Z0-9]{2})\]")
#: 持有人：SP=配偶 DC=受抚养子女 JT=共同
_OWNER = re.compile(r"\b(SP|DC|JT)\b")

#: PDF 里的噪音行（表头、脚注、声明），不属于资产名
_NOISE = re.compile(
    r"(F\s*S\s*:|S\s*O\s*:|D\s*:|Digitally Signed|"
    r"For the complete list of asset type|CERTIFY|Filing ID|"
    r"^\s*ID\s+Owner\s+Asset|Transaction\s+Date|Notification|Cap\.|Gains\s*>)",
    re.I)


@dataclass(frozen=True)
class Trade:
    """一笔已披露的交易。

    ⚠️ **金额是区间不是精确值** —— STOCK Act 只要求按档披露
    （$1,001-$15,000 / $15,001-$50,000 …）。任何"持仓市值""收益率"
    类计算都建立在区间上，必须标明是估算区间，不能报成确定数字。
    """

    chamber: str
    member: str
    state_district: str
    ticker: Optional[str]          # None = 该资产无公开代码（债券/基金/私募等）
    asset_name: str
    asset_type: Optional[str]      # 原始代码，如 "ST"
    asset_type_label: str
    tx_type: str                   # 原始代码
    tx_type_label: str
    tx_date: Optional[date]
    notification_date: Optional[date]
    filing_date: Optional[date]
    amount_low: Optional[int]
    amount_high: Optional[int]
    amount_raw: str
    owner: str                     # self / SP / DC / JT
    doc_id: str
    source_url: str

    @property
    def delay_days(self) -> Optional[int]:
        """交易日 → 归档日 的天数（**事实**，不是违规认定）。"""
        if not self.tx_date or not self.filing_date:
            return None
        return (self.filing_date - self.tx_date).days

    @property
    def date_anomaly(self) -> Optional[str]:
        """源数据本身的日期异常 —— **不替申报人改数据**，只如实标出来。

        实测 2026 年众议院有 2 笔归档日早于交易日（延迟为负），
        原件写的就是 `P 12/26/2026 01/21/2026`（交易日填成了未来），
        几乎可以肯定是填报笔误（应为 2025）。但"几乎肯定"不是依据 ——
        猜一个年份填进去就是在编数据。正确做法是标成异常、给出原件链接，
        并把它从延迟统计里剔除（否则 -320 天会把中位数和分布都带歪）。
        """
        d = self.delay_days
        if d is not None and d < 0:
            return "归档日早于交易日（原件如此，疑为填报笔误）"
        return None

    @property
    def over_45d(self) -> bool:
        """是否超过 STOCK Act 的 45 天硬上限。

        ⚠️ **这只是"超过 45 天"这个事实，不等于违规。**
        法条原文（众议院 PTR 表格）：期限为「知悉后 30 天内，但不得晚于交易后 45 天」，
        且**周末/假日顺延**。另有修订件、经纪商延迟通知等情形。
        本项目只呈现数据不下结论 —— UI 上必须同步显示这句话。
        """
        d = self.delay_days
        return d is not None and d > STOCK_ACT_HARD_CAP_DAYS


@dataclass(frozen=True)
class ParseResult:
    """解析结果。**解析不了要说清为什么**，不能返回空列表了事。"""

    trades: tuple[Trade, ...]
    unparsed_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.unparsed_reason is None


def _clean(text: str) -> str:
    """清洗 PDF 文本：去 \\x00、归一空白（金额/日期的跨行拼接靠这一步）。"""
    return re.sub(r"[ \t\u00a0]+", " ", text.replace("\x00", " ")).strip()


def _parse_amount(raw: str) -> tuple[Optional[int], Optional[int]]:
    """金额 → (下限, 上限)。

    `$1,001 - $15,000` → (1001, 15000)   区间
    `$2,722.50`        → (2722, 2722)    精确值：上下限相同，均值即真实金额
    带 "-" 但只解析出一个数（金额跨行被截断）→ (值, None)，不假装知道上限。
    """
    nums = re.findall(r"\$([\d,]+(?:\.\d{2})?)", raw)
    try:
        vals = [int(float(n.replace(",", ""))) for n in nums]
    except ValueError:
        return None, None
    if len(vals) >= 2:
        return vals[0], vals[1]
    if len(vals) == 1:
        # 无 "-" = 精确值（上下限相同）；有 "-" = 区间但上限没抓到
        return (vals[0], vals[0]) if "-" not in raw else (vals[0], None)
    return None, None


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


#: 表头最后一格。多页 PDF 每页都会重复整个表头，所以不能只在正文开头切一次。
_HEADER_TAIL = re.compile(r"\$\s*200\s*\?")


def _asset_from_chunk(chunk: str) -> tuple[Optional[str], str, Optional[str], str]:
    """从交易锚点**之前**的文本块里提取 (代码, 资产名, 类型代码, 持有人)。"""
    # ⚠️ 多页申报每页都重印表头，它会落在下一笔的资产块里
    # （实测形如 "Type Date $200? SP Applied Materials..."）。
    # 取块内**最后一个** "$200?" 之后的部分，重复多少次都能剥干净。
    tails = list(_HEADER_TAIL.finditer(chunk))
    if tails:
        chunk = chunk[tails[-1].end():]
    owner_m = _OWNER.search(chunk[:40])
    owner = owner_m.group(1) if owner_m else "self"

    lines = [ln.strip() for ln in chunk.split("\n")]
    lines = [ln for ln in lines if ln and not _NOISE.search(ln)]
    block = " ".join(lines).strip()

    ticker = asset_type = None
    m = _TICKER_TYPE.search(block)
    if m:
        ticker, asset_type = m.group(1), m.group(2)
        name = block[:m.start()].strip()
    else:
        # 没有 (代码)：可能只有 [类型]（债券、基金等无公开代码的资产）
        t = _TYPE_ONLY.search(block)
        if t:
            asset_type = t.group(1)
            name = block[:t.start()].strip()
        else:
            name = block
    # 去掉行首的交易流水号与持有人标记
    name = re.sub(r"^\d{6,}\s*", "", name)
    name = re.sub(r"^(SP|DC|JT)\s+", "", name).strip(" -–—·")
    return ticker, name, asset_type, owner


def parse_house_ptr(pdf_bytes: bytes, filing: Filing) -> ParseResult:
    """解析一份众议院 PTR PDF。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:                         # 缺依赖 ≠ 没数据
        raise RuntimeError("缺少 pypdf，无法解析众议院 PDF：pip install pypdf") from e

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        return ParseResult((), f"PDF 读取失败：{type(e).__name__}: {e}")

    if len(text.strip()) < 80:
        # ⚠️ 实测 313 份 PTR 里 33 份是这种（43 页纯图片、0 字符）。
        # 返回空列表 + 不说明 = 让用户以为这位议员没交易。
        return ParseResult((), "纸质扫描件（整份为图片），无 OCR 无法解析明细 —— 请打开原件查看")

    clean = _clean(text)
    # ⚠️ 从交易表表头之后开始找：否则**每份 PDF 的第一笔**会把页眉
    # （"P T R Clerk of the House of Representatives…"、议员姓名、表头）
    # 当成资产名 —— 看着有数据，其实第一笔资产名全是错的。
    # 表头最后一格是 "Cap. Gains > $200?"，必须切到 "$200?" **之后**：
    # 只切到 "Cap. Gains" 会把 "> $200?" 漏进第一笔的资产名，
    # 连带让开头的持有人标记（JT/SP）也剥不掉。
    head = re.search(r"\$\s*200\s*\?", clean)
    body = clean[head.end():] if head else clean
    matches = list(_TX_ANCHOR.finditer(body))
    if not matches:
        return ParseResult((), "未在 PDF 中识别到交易行（格式可能已变更）")

    trades: list[Trade] = []
    prev_end = 0
    for m in matches:
        ticker, name, atype, owner = _asset_from_chunk(body[prev_end:m.start()])
        prev_end = m.end()
        lo, hi = _parse_amount(m.group("amount"))
        tx = m.group("type")
        trades.append(Trade(
            chamber="house", member=filing.name,
            state_district=filing.state_district,
            ticker=ticker, asset_name=name or "(未识别)",
            asset_type=atype, asset_type_label=ASSET_TYPES.get(atype or "", atype or "未知"),
            tx_type=tx, tx_type_label=TX_TYPES.get(tx, tx),
            tx_date=_parse_date(m.group("tx_date")),
            notification_date=_parse_date(m.group("notify_date")),
            filing_date=filing.filing_date,
            amount_low=lo, amount_high=hi,
            amount_raw=re.sub(r"\s+", " ", m.group("amount")).strip(),
            owner=owner, doc_id=filing.doc_id, source_url=filing.detail_url,
        ))
    return ParseResult(tuple(trades))


#: 参议院明细页的表格行
_SEN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_SEN_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def _strip_html(s: str) -> str:
    import html
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_senate_ptr(page_html: str, filing: Filing) -> ParseResult:
    """解析参议院电子 PTR（HTML 表格，**自带 Ticker 列**，比众议院干净）。"""
    rows = _SEN_ROW.findall(page_html)
    trades: list[Trade] = []
    for row in rows:
        cells = [_strip_html(c) for c in _SEN_CELL.findall(row)]
        # 表头：# / Transaction Date / Owner / Ticker / Asset Name / Asset Type / Type / Amount / Comment
        if len(cells) < 8 or not re.fullmatch(r"\d+", cells[0] or ""):
            continue
        raw_ticker = (cells[3] or "").strip()
        # 参议院用 "--" 表示无公开代码，别把它当成一个叫 "--" 的标的
        ticker = raw_ticker if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", raw_ticker) else None
        lo, hi = _parse_amount(cells[7])
        tx_label = cells[6] or ""
        # 参议院写全称（Purchase / Sale (Full) / Sale (Partial) / Exchange）→ 归一到众议院代码
        tx_code = ("P" if tx_label.lower().startswith("purchase")
                   else "S (partial)" if "partial" in tx_label.lower()
                   else "S" if tx_label.lower().startswith("sale")
                   else "E" if tx_label.lower().startswith("exchange") else tx_label)
        trades.append(Trade(
            chamber="senate", member=filing.name,
            state_district=filing.state_district,
            ticker=ticker, asset_name=cells[4] or "(未识别)",
            asset_type=None, asset_type_label=cells[5] or "未知",
            tx_type=tx_code, tx_type_label=TX_TYPES.get(tx_code, tx_label),
            tx_date=_parse_date(cells[1]),
            notification_date=None,        # 参议院表格不给通知日
            filing_date=filing.filing_date,
            amount_low=lo, amount_high=hi, amount_raw=cells[7],
            owner=cells[2] or "self", doc_id=filing.doc_id,
            source_url=filing.detail_url,
        ))
    if not trades:
        return ParseResult((), "未在页面中识别到交易行（可能是无交易的申报，或页面结构已变更）")
    return ParseResult(tuple(trades))


# ────────────────────────────── 聚合 ──────────────────────────────

def to_dict(t: Trade) -> dict:
    return {
        "chamber": t.chamber, "member": t.member, "state_district": t.state_district,
        "ticker": t.ticker, "asset_name": t.asset_name,
        "asset_type": t.asset_type, "asset_type_label": t.asset_type_label,
        "tx_type": t.tx_type, "tx_type_label": t.tx_type_label,
        "tx_date": t.tx_date.isoformat() if t.tx_date else None,
        "notification_date": t.notification_date.isoformat() if t.notification_date else None,
        "filing_date": t.filing_date.isoformat() if t.filing_date else None,
        "amount_low": t.amount_low, "amount_high": t.amount_high,
        "amount_raw": t.amount_raw, "owner": t.owner,
        "delay_days": t.delay_days, "over_45d": t.over_45d,
        "date_anomaly": t.date_anomaly,
        "doc_id": t.doc_id, "source_url": t.source_url,
    }


def _mid(t: Trade) -> float:
    """金额区间中值 —— 仅用于**排序/相对比较**，不是真实成交额。"""
    if t.amount_low is None:
        return 0.0
    if t.amount_high is None:
        return float(t.amount_low)
    return (t.amount_low + t.amount_high) / 2.0


#: 延迟分桶（上界含）。>45 天单列，因为 45 是法定硬上限。
_DELAY_BUCKETS = ((7, "≤7天"), (15, "8-15"), (30, "16-30"), (45, "31-45"),
                  (10 ** 9, ">45天"))


def _delay_buckets(delays: list[int]) -> list[dict]:
    out, lo = [], -(10 ** 9)
    for hi, label in _DELAY_BUCKETS:
        out.append({"label": label, "count": sum(1 for d in delays if lo <= d <= hi)})
        lo = hi + 1
    return out


def summarize(trades: list[Trade]) -> dict:
    """按标的 / 议员 / 买卖方向聚合。

    ⚠️ 所有金额都是**区间中值的加总**（STOCK Act 只按档披露），
    只能用于横向比较，不能当成真实资金量。输出里带 `amount_is_estimate` 标记。
    """
    by_ticker: dict[str, dict] = {}
    by_member: dict[str, dict] = {}
    buys = sells = 0

    for t in trades:
        is_buy = t.tx_type == "P"
        is_sell = t.tx_type.startswith("S")
        buys += is_buy
        sells += is_sell
        mid = _mid(t)

        if t.ticker:
            e = by_ticker.setdefault(t.ticker, {
                "ticker": t.ticker, "asset_name": t.asset_name,
                "trades": 0, "buys": 0, "sells": 0,
                "est_amount": 0.0, "members": set()})
            e["trades"] += 1
            e["buys"] += is_buy
            e["sells"] += is_sell
            e["est_amount"] += mid
            e["members"].add(t.member)

        m = by_member.setdefault(t.member, {
            "member": t.member, "chamber": t.chamber,
            "state_district": t.state_district,
            "trades": 0, "buys": 0, "sells": 0,
            "est_amount": 0.0, "tickers": set()})
        m["trades"] += 1
        m["buys"] += is_buy
        m["sells"] += is_sell
        m["est_amount"] += mid
        if t.ticker:
            m["tickers"].add(t.ticker)

    # ⚠️ by_ticker 按**笔数**排（"最活跃标的"就是这个意思，图表画的也是笔数）。
    # 曾经按 est_amount 排 → 出现 "2 笔排在 11 笔前面"，排序依据与标题/图形都对不上。
    # 金额只是区间中值的估算，本来也不适合当主排序键。
    tick = sorted(by_ticker.values(),
                  key=lambda x: (x["trades"], x["est_amount"]), reverse=True)
    for e in tick:
        e["members"] = sorted(e["members"])
        e["member_count"] = len(e["members"])
        e["est_amount"] = round(e["est_amount"])
    # by_member 保持按估算金额排（UI 上标的就是"按估算金额排序"）
    memb = sorted(by_member.values(), key=lambda x: x["est_amount"], reverse=True)
    for e in memb:
        e["ticker_count"] = len(e["tickers"])
        e["tickers"] = sorted(e["tickers"])[:12]
        e["est_amount"] = round(e["est_amount"])

    # ⚠️ 剔除日期异常的记录再算延迟统计：一笔 -320 天会把中位数和分布全带歪。
    # 剔除不等于隐藏 —— 数量单独报在 anomaly_count，明细里也照常显示并标注。
    delays = [t.delay_days for t in trades
              if t.delay_days is not None and t.date_anomaly is None]
    return {
        "total_trades": len(trades),
        "buys": buys, "sells": sells,
        "by_ticker": tick,
        "by_member": memb,
        # ⭐ 直方图在这里算，和上面的中位数/超期数**同一个样本**。
        # 曾经让前端拿明细表的 300 行自己分桶、卡片却用汇总的 2000 行 ——
        # 同一屏里两个控件报出不同的分布。同源计算才能从结构上杜绝。
        "delay": {
            "buckets": _delay_buckets(delays),
            # 用 statistics.median：`sorted(x)[len//2]` 在**偶数个样本**时
            # 取的是上中位数（[1,100] 会报 100 而不是 50.5），
            # 样本一小（按标的筛选后很常见）就明显失真。
            "median_days": round(statistics.median(delays), 1) if delays else None,
            "max_days": max(delays) if delays else None,
            "over_45d_count": sum(1 for t in trades
                                  if t.over_45d and t.date_anomaly is None),
            "anomaly_count": sum(1 for t in trades if t.date_anomaly),
            "anomaly_note": "另有若干笔申报的归档日早于交易日（原件填报有误）。"
                            "已从上面的延迟统计中剔除，但仍在明细里如实列出并标注。",
            "note": "「超 45 天」是事实统计，不是违规认定：法定期限为「知悉后 30 天内、"
                    "且不晚于交易后 45 天」，周末/假日顺延，另有修订件与经纪商延迟通知等情形。",
        },
        "amount_is_estimate": True,
        "amount_note": "STOCK Act 只要求按区间披露（如 $1,001-$15,000），"
                       "此处金额为区间中值加总，仅供横向比较，不是真实成交额。",
    }
