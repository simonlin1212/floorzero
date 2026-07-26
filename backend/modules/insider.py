"""内部人交易（Form 4）解析、分类与聚合。

━━━ ⭐ 这个分栏的全部价值，在于把交易类型分清楚 ━━━

**Form 4 里绝大多数行不是「内部人看好自家股票所以买入」。**
2026Q1 全市场 103,733 笔非衍生品交易，实测代码分布：

    F 代扣税 27,019 │ A 授予 24,690 │ S 卖出 22,822 │ M 行权 16,300
    P 公开市场买入 **5,935（仅 5.6%）** │ 其余 D/J/G/C/L/X/U/I/W 合计 ~7k

而按 SEC 的「取得/处置」标志（`TRANS_ACQUIRED_DISP_CD`）统计，
标为「取得」的有 48,849 笔 —— **是真实公开市场买入的 8 倍**。
把授予、行权、赠与当成「内部人买入」，是这类数据最经典的误读。

**实测样本（3M CO，2026-07-23，一份申报两行）：**

    M 行权 7,880 股 @ $154.69  → 标记「取得」
    S 卖出 7,880 股 @ $170.44  → 标记「处置」

同日行权即卖出。笼统统计会说「内部人买入 120 万美元」，
**实际他在公开市场一股没买、拿到手全卖了** —— 是薪酬变现，不是看多。

→ 所以本模块的第一原则：**按交易代码分类，不按取得/处置标志**。
   `is_open_market`（P/S）才是有信号含义的那部分。

━━━ 另一个维度：10b5-1 预设交易计划 ━━━
按 Rule 10b5-1 预先制定的计划卖出，是几个月前就排好的，
与「临时决定卖出」的信号强度完全不同。SEC 从 2023 年起要求在申报里勾选。
⚠️ 该字段编码混乱，实测同一季度里 `0/1`、`false/true`、空值三套并存，必须归一。

━━━ 免责 ━━━
本模块只做分类与统计，**不打「看涨/看跌」标签、不给评分**。
内部人买卖的预测力在学术上有争议，且披露有滞后；呈现事实即可。
"""
from __future__ import annotations

import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

#: SEC Form 4 官方交易代码表（2026-07-26 实读 https://www.sec.gov/files/form4.pdf §8）
TX_CODES: dict[str, str] = {
    # General
    "P": "公开市场买入", "S": "公开市场卖出", "V": "自愿提前申报",
    # Rule 16b-3（薪酬相关，多数不含主动买卖意图）
    "A": "授予/奖励", "D": "向公司处置", "F": "代扣税/抵行权价",
    "I": "16b-3(f) 自主交易", "M": "期权行权/转换",
    # 衍生品
    "C": "衍生品转换", "E": "空头衍生品到期", "H": "多头衍生品到期(有对价)",
    "O": "价外期权行权", "X": "价内/平价期权行权",
    # 豁免与小额
    "G": "赠与", "L": "16a-6 小额取得", "W": "继承/遗嘱", "Z": "表决权信托存取",
    # 其他
    "J": "其他（需说明）", "K": "权益互换", "U": "控制权变更要约",
}

#: ⭐ **只有这两个代码是公开市场主动买卖** —— 唯一有信号含义的部分
OPEN_MARKET = frozenset({"P", "S"})

#: 薪酬/机械性交易：授予、行权、代扣税、向公司处置。
#: 单列出来是因为它们**数量最多**，混进统计会淹没真正的信号。
COMPENSATION = frozenset({"A", "M", "F", "D", "I"})


def code_label(code: str) -> str:
    """交易代码 → 中文说明。未知代码原样返回，不硬塞进已知分类。"""
    if not code:
        return "未知"
    # 复合代码如 "S/K"（互换）取主码
    return TX_CODES.get(code.split("/")[0].strip().upper(), code)


def code_group(code: str) -> str:
    """交易代码 → 大类：open_market / compensation / other。"""
    c = (code or "").split("/")[0].strip().upper()
    if c in OPEN_MARKET:
        return "open_market"
    if c in COMPENSATION:
        return "compensation"
    return "other"


def _norm_bool(v: Optional[str]) -> Optional[bool]:
    """归一 SEC 数据里三套并存的布尔编码。

    ⚠️ 实测 2026Q1 的 `AFF10B5ONE` 字段同时存在 `0/1`、`false/true`、空值。
    只认其中一套会让大部分 10b5-1 计划交易识别不出来。
    """
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "y", "yes"):
        return True
    if s in ("0", "false", "n", "no"):
        return False
    return None


#: 申报人明确表示"没有代码"的写法
_NO_TICKER = {"NONE", "N/A", "NA", "N//A", "--", "-", "", "TBD", "NOT APPLICABLE",
              "NO SYMBOL", "NONE.", "0"}
#: 交易所前缀（`NYSE: KRC` / `ASX:LNW` / `NASDAQ: XYZ`）
_EXCHANGE_PREFIX = re.compile(
    r"^(?:NYSE|NASDAQ|NYSEAMERICAN|NYSE AMERICAN|AMEX|OTC|OTCQB|OTCQX|ASX|TSX|LSE)\s*[:：]\s*",
    re.I)


def clean_ticker(raw: Optional[str]) -> Optional[str]:
    """归一 `ISSUERTRADINGSYMBOL` —— 这是**申报人自由填写**的字段，很脏。

    实测 16 万行里的真实值：`NONE`(875) / `N/A`(209) / `MOGA/MOGB`(107) /
    `GEF, GEF-B`(99) / `Z AND ZG`(87) / `NYSE: KRC`(58) / `(SIRI)`(38) / `N O G`(44)。
    不清洗的话 `NONE` 会以 875 笔的量冲进"最活跃标的"榜首 —— 一个不存在的公司。

    处理约定（**都是有损的判断，所以写在这里而不是藏进正则**）：
    - 明确表示无代码的写法 → None
    - 去掉交易所前缀与括号、压掉内部空格（`N O G` → `NOG`）
    - **多代码（双重股权）取第一个**（`MOGA/MOGB` → `MOGA`）：
      同一家公司的不同股份类别，聚合到主代码比拆成两个虚假标的更贴近事实
    - 清洗后仍不像代码（>6 位或含非法字符）→ None，不硬塞
    """
    if raw is None:
        return None
    t = str(raw).strip().upper()
    if t in _NO_TICKER:
        return None
    t = _EXCHANGE_PREFIX.sub("", t).strip()
    t = t.strip("()[]{} ")
    # 多代码：/ 、逗号、AND 分隔 → 取第一个
    t = re.split(r"\s*(?:/|,|\bAND\b|\|)\s*", t)[0].strip()
    t = t.replace(" ", "")                      # `N O G` → `NOG`
    if t in _NO_TICKER or not t:
        return None
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", t):
        return None                             # 不像代码就不认，别造脏数据
    return t


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _parse_date(v: Optional[str]) -> Optional[date]:
    """解析 `YYYY-MM-DD`（XML）或 `01-APR-2026`（数据集 TSV）。"""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class InsiderTrade:
    """一笔内部人交易。"""

    accession: str
    seq: int
    ticker: Optional[str]
    company: str
    issuer_cik: str
    owner: str
    owner_cik: str
    is_officer: bool
    is_director: bool
    is_ten_pct: bool
    officer_title: str
    security: str
    tx_code: str
    tx_date: Optional[date]
    filing_date: Optional[date]
    shares: Optional[float]
    price: Optional[float]
    acquired_disposed: str            # A / D（SEC 原始标志，仅作参考）
    shares_after: Optional[float]
    is_direct: bool                   # D=直接持有 / I=间接
    is_10b5_1: Optional[bool]
    form_type: str                    # 4 / 4/A（本模块只收这两种，见 parse_dataset）
    source_url: str

    @property
    def group(self) -> str:
        return code_group(self.tx_code)

    @property
    def is_open_market(self) -> bool:
        """⭐ 是否公开市场主动买卖 —— 只有这部分有信号含义。"""
        return self.group == "open_market"

    @property
    def direction(self) -> Optional[str]:
        """买/卖方向，**只对公开市场交易有意义**。

        ⚠️ 刻意不给薪酬类交易返回方向：授予被标为「取得」、
        代扣税被标为「处置」，把它们当买卖会让统计彻底失真
        （实测「取得」笔数是真实买入的 8 倍）。
        """
        if not self.is_open_market:
            return None
        return "buy" if self.tx_code.split("/")[0].upper() == "P" else "sell"

    @property
    def date_anomaly(self) -> Optional[str]:
        """交易日晚于申报日 —— **物理上不可能**（不能先申报后交易）。

        实测 16.5 万行里 14 笔，全是年份笔误：
        PRCH 报「交易 2028-03-19、申报 2026-03-20」、
        NFRX 报「交易 2002-02-24、申报 2026-02-25」—— 月日都对得上，只有年份错。

        ⚠️ **同类笔误远不止这 14 笔**：延迟 366 天那批（1,270 笔）里，
        ASTS「交易 2025-03-17 / 申报 2026-03-18」月日仅差一天、年份差一年，
        显然也是笔误 —— 但「晚报一年」在法律上并非不可能，
        **无法逐笔确证，所以不标记**。故延迟统计里仍混有少量笔误，不宜过度解读。

        与价格问题一样：**不替申报人改数据**，只标出确凿不可能的那部分。
        """
        if self.tx_date and self.filing_date and self.tx_date > self.filing_date:
            return "交易日晚于申报日（原件如此，疑为年份笔误）"
        return None

    @property
    def price_implausible(self) -> bool:
        """每股价格明显不可能 —— **申报人把「总金额」填进了「每股价格」字段**。

        实测（2026-07-26，16.5 万行）：
        - REEMF 报 100,149,060 股 × "$24,035,774.40/股" → 单笔 **$2.4 千万亿**
          原始 XML 确实写在 `transactionPricePerShare` 里；
          而 $24,035,774.40 ÷ 100,149,060 = **恰好 $0.2400/股** —— 填的是总额无疑。
        - 另有 PSX 报 $2,110,482/股、LLY 报 $1,032,319/股（真实股价 $130 / $800）。

        阈值取 **$100 万/股**：BRK.A ~$70 万/股是美股史上最高价，
        超过这个数在物理上不可能，判定不会误伤真实交易。

        ⚠️ **这个过滤器不完整**：同样是错填，IHT 报 $14,561/股（真实股价约 $2）
        就落在阈值之下 —— 没有外部行情做参照就识别不出来。
        所以不能宣称"金额已清洗干净"，只能说"已剔除可确证的错填"。

        **不自动改正**（不拿总额除以股数）：那是在替申报人猜测意图。
        做法是剔出统计、在明细里保留并标注。
        """
        if self.price is not None and self.price > 1_000_000:
            return True
        # 第二条锚点：单笔金额超过任何美国个人持股规模。
        # 马斯克的 TSLA 持股约 $1,500 亿是已知最大个人持仓，
        # 所以单笔 > $2,000 亿必是错填（实测 MYNZ 报 $402,000/股 × 643,850 股
        # = $2,588 亿，而 Mainz Biomed 实际股价不到 $1）。
        # ⚠️ 这条不能设得更低：TSLA 有一笔 $1,416 亿、价格 $334.09 完全正常
        # （马斯克整个持仓的信托转移），把它误杀就是删真数据。
        if self.shares is not None and self.price is not None:
            if self.shares * self.price > 200_000_000_000:
                return True
        return False

    @property
    def value(self) -> Optional[float]:
        """成交金额 = 股数 × 单价。

        无价格、或价格明显错填时返回 None —— **宁可没有，不要一个假数字**：
        一行错填的记录就能把全市场买入总额顶到 4.8 千万亿美元。
        """
        if self.shares is None or self.price is None or self.price_implausible:
            return None
        return self.shares * self.price

    @property
    def delay_days(self) -> Optional[int]:
        """交易日 → 申报日 的天数。

        Section 16(a) 要求 **交易后两个工作日内** 申报（2003 年起）。
        这里只报事实天数，不做违规认定（含节假日、修订件等情形）。
        """
        if not self.tx_date or not self.filing_date:
            return None
        return (self.filing_date - self.tx_date).days


def to_dicts(trades: list[InsiderTrade]) -> list[dict]:
    """批量转换并分配稳定主键 —— **入库一律走这个**，不要逐条 to_dict。"""
    keys = assign_trade_keys(trades)
    out = []
    for t, k in zip(trades, keys):
        d = to_dict(t)
        d["trade_key"] = k
        out.append(d)
    return out


def to_dict(t: InsiderTrade) -> dict:
    return {
        "accession": t.accession, "seq": t.seq,
        # trade_key 由 assign_trade_keys() 批量分配（需要同申报内的计数上下文），
        # 这里先占位，to_dicts() 会填上
        "trade_key": None,
        "ticker": t.ticker, "company": t.company, "issuer_cik": t.issuer_cik,
        "owner": t.owner, "owner_cik": t.owner_cik,
        "is_officer": t.is_officer, "is_director": t.is_director,
        "is_ten_pct": t.is_ten_pct, "officer_title": t.officer_title,
        "security": t.security,
        "tx_code": t.tx_code, "tx_code_label": code_label(t.tx_code),
        "group": t.group, "is_open_market": t.is_open_market,
        "direction": t.direction,
        "tx_date": t.tx_date.isoformat() if t.tx_date else None,
        "filing_date": t.filing_date.isoformat() if t.filing_date else None,
        "shares": t.shares, "price": t.price, "value": t.value,
        "price_implausible": t.price_implausible,
        "date_anomaly": t.date_anomaly,
        "acquired_disposed": t.acquired_disposed,
        "shares_after": t.shares_after, "is_direct": t.is_direct,
        "is_10b5_1": t.is_10b5_1, "form_type": t.form_type,
        "is_amendment": t.form_type.endswith("/A"),
        "delay_days": t.delay_days,
        "source_url": t.source_url,
    }


# ─────────────────────── XML 解析（近期申报）───────────────────────

def trade_key(accession: str, security: str, tx_date, tx_code: str,
              shares, price, acquired_disposed: str, owner: str) -> str:
    """一笔交易的**内容指纹**，用作跨来源稳定主键。

    ⚠️ 不能用「在申报里的第几行」当主键：季度数据集按 TSV 行序、
    XML 按元素序，两边顺序不保证一致。一旦不一致，
    `(accession, seq)` 就会把**不同的交易**配成同一个键 ——
    `INSERT OR IGNORE` 于是静默丢掉正确的那条，且毫无迹象。

    改用内容指纹后，同一笔交易无论从哪条路进来都是同一个键，
    顺序不同也不会串位。

    ⚠️ 但**光有指纹会丢数据**：同一份申报里合法存在内容完全相同的多笔
    （实测 2026Q1 有 976 组重复、最多一组 8 笔 —— 例如同证券同日同价，
     只是分别落在 IRA 与 Roth IRA 两个账户）。只按指纹去重会少掉 1,120 笔。
    所以真正的主键是 `指纹 + 同指纹内的出现序号`（见 `assign_trade_keys`）：
    重复内容全部保留，而跨来源的顺序差异仍不会串位
    （只要两边解析出的是同一个多重集合）。
    """
    import hashlib

    d = tx_date.isoformat() if hasattr(tx_date, "isoformat") else str(tx_date or "")
    raw = "|".join([
        accession or "", (security or "").strip().upper(), d,
        (tx_code or "").strip().upper(),
        f"{float(shares):.4f}" if shares is not None else "",
        f"{float(price):.6f}" if price is not None else "",
        (acquired_disposed or "").strip().upper(), (owner or "").strip().upper(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def assign_trade_keys(trades: list["InsiderTrade"]) -> list[str]:
    """给一批交易分配稳定主键：`指纹#同指纹内序号`。

    按申报分组计数，所以同一份申报里的重复内容会得到 `#0`/`#1`/…，
    既不会互相覆盖，也不依赖遍历顺序（同一份申报的多重集合两边一致即可）。
    """
    seen: dict[tuple[str, str], int] = {}
    out: list[str] = []
    for t in trades:
        fp = trade_key(t.accession, t.security, t.tx_date, t.tx_code,
                       t.shares, t.price, t.acquired_disposed, t.owner)
        n = seen.get((t.accession, fp), 0)
        seen[(t.accession, fp)] = n + 1
        out.append(f"{fp}#{n}")
    return out


def accession_url(cik: str, accession: str) -> str:
    """申报原件链接（EDGAR 可读索引页）。两条导入路径共用，避免风格漂移。"""
    cik = (cik or "").lstrip("0")
    if not cik or not accession:
        return ""
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


def _txt(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    e = node.find(path)
    return (e.text or "").strip() if e is not None and e.text else None


def parse_form4_xml(xml: str, ref) -> list[InsiderTrade]:
    """解析一份 Form 4 的 ownershipDocument XML。

    ⚠️ 一份申报可能有**多个申报人**（实测 2026Q1 最多 10 个、927 份 >1）。
    这里把多个申报人的姓名合并展示，关系取并集 ——
    只取第一个会丢掉共同申报的信息。
    """
    root = ET.fromstring(xml)

    issuer = root.find("issuer")
    ticker = clean_ticker(_txt(issuer, "issuerTradingSymbol"))
    company = _txt(issuer, "issuerName") or ref.company
    issuer_cik = (_txt(issuer, "issuerCik") or ref.cik).lstrip("0") or ref.cik

    owners, ciks = [], []
    is_officer = is_director = is_ten_pct = False
    titles = []
    for ro in root.findall("reportingOwner"):
        owners.append(_txt(ro, "reportingOwnerId/rptOwnerName") or "")
        ciks.append(_txt(ro, "reportingOwnerId/rptOwnerCik") or "")
        rel = ro.find("reportingOwnerRelationship")
        if rel is not None:
            is_officer |= _norm_bool(_txt(rel, "isOfficer")) is True
            is_director |= _norm_bool(_txt(rel, "isDirector")) is True
            is_ten_pct |= _norm_bool(_txt(rel, "isTenPercentOwner")) is True
            t = _txt(rel, "officerTitle")
            if t:
                titles.append(t)

    # 10b5-1 计划标志（SEC 2023 起要求勾选）
    plan = _norm_bool(_txt(root, "aff10b5One"))

    filing_date = _parse_date(ref.filed)
    # 与季度数据集那条路径**统一用 `-index.htm`**：同一笔交易从不同来源导入
    # 不该给出两种风格的链接。（扁平的 `.txt` 实测同样可访问，只是给用户看的是
    # 原始 SGML 全文；`-index.htm` 是可读的申报索引页，体验更好。）
    url = accession_url(ref.cik, ref.accession)

    out: list[InsiderTrade] = []
    for i, tr in enumerate(root.iter("nonDerivativeTransaction")):
        out.append(InsiderTrade(
            accession=ref.accession, seq=i,
            ticker=ticker, company=company, issuer_cik=issuer_cik,
            owner=" / ".join(x for x in owners if x) or "(未知)",
            owner_cik=",".join(x for x in ciks if x),
            is_officer=is_officer, is_director=is_director, is_ten_pct=is_ten_pct,
            officer_title=" / ".join(dict.fromkeys(titles)),
            security=_txt(tr, "securityTitle/value") or "",
            tx_code=_txt(tr, "transactionCoding/transactionCode") or "",
            tx_date=_parse_date(_txt(tr, "transactionDate/value")),
            filing_date=filing_date,
            shares=_num(_txt(tr, "transactionAmounts/transactionShares/value")),
            price=_num(_txt(tr, "transactionAmounts/transactionPricePerShare/value")),
            acquired_disposed=_txt(
                tr, "transactionAmounts/transactionAcquiredDisposedCode/value") or "",
            shares_after=_num(_txt(
                tr, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            is_direct=(_txt(tr, "ownershipNature/directOrIndirectOwnership/value")
                       or "D").upper().startswith("D"),
            is_10b5_1=plan, form_type=ref.form, source_url=url,
        ))
    return out


# ─────────────────────── TSV 解析（季度数据集）───────────────────────

def parse_dataset(tables: dict[str, list[dict]]) -> list[InsiderTrade]:
    """把季度数据集的三张表拼成交易列表。

    ⚠️ REPORTINGOWNER 与 SUBMISSION 是**一对多**（一份申报最多 10 个申报人），
    所以先按 accession 聚合申报人，再关联交易 —— 直接 join 会让交易翻倍。
    """
    # ⚠️ **这个 ZIP 是 Form 3/4/5 合集，必须先按表单类型筛**。
    # 实测 2026Q1：Form 4 = 100,341 笔（延迟中位 **2 天**，正是法定要求）；
    # 而 **Form 5 = 2,168 笔，延迟中位 274 天、32.6% 超过一年** ——
    # 它是**年度补报**，按设计就该滞后，不是谁填错了。
    # 混进来会把"内部人申报延迟"整体拉高，还会把 Form 3（初始持股声明，
    # 根本不是交易）当成交易统计。
    # （这也纠正了本项目早前的一个误判：那批 366+ 天的延迟主要是 Form 5，
    #   不是"年份笔误"。）
    keep = {"4", "4/A"}
    subs = {r["ACCESSION_NUMBER"]: r for r in tables.get("SUBMISSION", [])
            if (r.get("DOCUMENT_TYPE") or "").strip() in keep}

    owners: dict[str, dict] = {}
    for r in tables.get("REPORTINGOWNER", []):
        acc = r["ACCESSION_NUMBER"]
        o = owners.setdefault(acc, {"names": [], "ciks": [], "titles": [],
                                    "officer": False, "director": False, "ten": False})
        if r.get("RPTOWNERNAME"):
            o["names"].append(r["RPTOWNERNAME"])
        if r.get("RPTOWNERCIK"):
            o["ciks"].append(r["RPTOWNERCIK"])
        if r.get("RPTOWNER_TITLE"):
            o["titles"].append(r["RPTOWNER_TITLE"])
        rel = (r.get("RPTOWNER_RELATIONSHIP") or "").lower()
        o["officer"] |= "officer" in rel
        o["director"] |= "director" in rel
        o["ten"] |= "10" in rel or "ten" in rel

    out: list[InsiderTrade] = []
    seq_by_acc: dict[str, int] = {}
    for r in tables.get("NONDERIV_TRANS", []):
        acc = r["ACCESSION_NUMBER"]
        s = subs.get(acc)
        if s is None:
            continue                       # 交易对不上申报头，跳过而不是编一个
        o = owners.get(acc, {})
        seq = seq_by_acc.get(acc, 0)
        seq_by_acc[acc] = seq + 1
        cik = (s.get("ISSUERCIK") or "").lstrip("0")
        out.append(InsiderTrade(
            accession=acc, seq=seq,
            ticker=clean_ticker(s.get("ISSUERTRADINGSYMBOL")),
            company=s.get("ISSUERNAME") or "",
            issuer_cik=cik,
            owner=" / ".join(o.get("names", [])) or "(未知)",
            owner_cik=",".join(o.get("ciks", [])),
            is_officer=bool(o.get("officer")), is_director=bool(o.get("director")),
            is_ten_pct=bool(o.get("ten")),
            officer_title=" / ".join(dict.fromkeys(o.get("titles", []))),
            security=r.get("SECURITY_TITLE") or "",
            tx_code=(r.get("TRANS_CODE") or "").strip().upper(),
            tx_date=_parse_date(r.get("TRANS_DATE")),
            filing_date=_parse_date(s.get("FILING_DATE")),
            shares=_num(r.get("TRANS_SHARES")),
            price=_num(r.get("TRANS_PRICEPERSHARE")),
            acquired_disposed=(r.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper(),
            shares_after=_num(r.get("SHRS_OWND_FOLWNG_TRANS")),
            is_direct=not (r.get("DIRECT_INDIRECT_OWNERSHIP") or "D").upper().startswith("I"),
            is_10b5_1=_norm_bool(s.get("AFF10B5ONE")),
            form_type=(s.get("DOCUMENT_TYPE") or "4").strip(),
            source_url=accession_url(cik, acc),
        ))
    return out


# ─────────────────────── 聚合 ───────────────────────

def summary_notes(library: Optional[dict]) -> dict:
    """口径说明（REST 与 MCP 共用，避免两处措辞/口径漂移）。"""
    return {
        "forms": "本页只含 Form 4（修订件 4/A 默认排除，避免与原件重复计数）。"
                 "同一个 SEC 数据集里还有 Form 3（初始持股声明，不是交易）与 "
                 "Form 5（年度补报，实测延迟中位 274 天、32.6% 超一年）—— "
                 "都已排除，否则会把「内部人申报延迟」整体拉高。"
                 "分离后 Form 4 的延迟中位是 2 天，与法定要求一致。",
        "classification": _classification_note(library, 0, 0, 0),
        "plan": "10b5-1 是预先制定的交易计划，卖出往往是几个月前排好的，"
                "与临时决定卖出的含义不同；「未标注」（2023 年前无此字段）"
                "与「明确非计划内」是两回事。",
        "price": "部分申报把「总金额」误填进「每股价格」字段（实测有报到 "
                 "$2,400 万/股 的）。每股价 > $100 万、或单笔 > $2,000 亿的记录"
                 "已剔出金额统计，但**更隐蔽的错填识别不出来** —— 金额仍应视为近似值。",
        "disclaimer": "本页只呈现已公开申报的事实，不构成任何投资建议。",
    }


def _classification_note(library: Optional[dict], om: int,
                         comp: int, other: int) -> str:
    """分类说明。有全库统计就用全库口径，没有才退回当前样本并注明。"""
    if library and library.get("trades"):
        g = library.get("by_group") or {}
        return (f"全库 {library['trades']:,} 行中，**公开市场主动买卖仅 "
                f"{g.get('open_market', 0):,} 行（{library.get('open_market_pct')}%）**，"
                f"其余为授予/行权/代扣税等薪酬类 {g.get('compensation', 0):,} 行、"
                f"其他 {g.get('other', 0):,} 行。买卖统计只含公开市场部分 —— "
                f"按 SEC 的「取得/处置」标志笼统统计会把买入夸大数倍。")
    return (f"当前样本 {om + comp + other:,} 行，其中公开市场 {om:,} 行、"
            f"薪酬类 {comp:,} 行、其他 {other:,} 行（未取到全库统计）。")


def summarize(trades: list[InsiderTrade], top: int = 20,
              library: Optional[dict] = None) -> dict:
    """按标的 / 内部人聚合。

    ⭐ **所有买卖统计都只算公开市场交易（P/S）**。
    薪酬类（授予/行权/代扣税）单独计数，不混进买卖 ——
    否则「内部人买入」会被夸大约 8 倍。
    """
    om = [t for t in trades if t.is_open_market]
    comp = [t for t in trades if t.group == "compensation"]
    other = [t for t in trades if t.group == "other"]

    by_ticker: dict[str, dict] = {}
    by_owner: dict[str, dict] = {}
    for t in om:
        buy = t.direction == "buy"
        val = t.value or 0.0
        if t.ticker:
            e = by_ticker.setdefault(t.ticker, {
                "ticker": t.ticker, "company": t.company, "buys": 0, "sells": 0,
                "buy_value": 0.0, "sell_value": 0.0, "insiders": set()})
            e["buys" if buy else "sells"] += 1
            e["buy_value" if buy else "sell_value"] += val
            # ⚠️ 只把**买入者**计入：这个集合驱动 `insider_count`，
            # 而榜单标题写的是"有多少位不同内部人**买入**"。
            # 混进只卖不买的人会凭空放大集群买入信号 —— 而这正是本页最被关注的指标。
            if buy:
                e["insiders"].add(t.owner)
        k = f"{t.owner}|{t.ticker or t.company}"
        m = by_owner.setdefault(k, {
            "owner": t.owner, "ticker": t.ticker, "company": t.company,
            "title": t.officer_title, "is_officer": t.is_officer,
            "is_director": t.is_director, "is_ten_pct": t.is_ten_pct,
            "buys": 0, "sells": 0, "buy_value": 0.0, "sell_value": 0.0})
        m["buys" if buy else "sells"] += 1
        m["buy_value" if buy else "sell_value"] += val

    ticks = []
    for e in by_ticker.values():
        e["insider_count"] = len(e["insiders"])          # = 不同**买入者**人数
        e["insiders"] = sorted(e["insiders"])[:10]
        e["net_value"] = round(e["buy_value"] - e["sell_value"])
        e["buy_value"] = round(e["buy_value"])
        e["sell_value"] = round(e["sell_value"])
        ticks.append(e)
    # ⭐ 按「有多少个不同内部人买入」排序 —— 集群买入是这类数据里最被关注的形态，
    #    单人一笔大额可能只是个人理财，多人同期买入更难用巧合解释。
    cluster = sorted([e for e in ticks if e["buys"] > 0],
                     key=lambda x: (x["insider_count"], x["buy_value"]), reverse=True)

    owners = sorted(by_owner.values(),
                    key=lambda x: max(x["buy_value"], x["sell_value"]), reverse=True)
    for m in owners:
        m["buy_value"] = round(m["buy_value"])
        m["sell_value"] = round(m["sell_value"])

    delays = [t.delay_days for t in trades
              if t.delay_days is not None and t.delay_days >= 0]
    plan_sells = sum(1 for t in om if t.direction == "sell" and t.is_10b5_1 is True)

    return {
        "total_rows": len(trades),
        "open_market": {
            "count": len(om),
            "buys": sum(1 for t in om if t.direction == "buy"),
            "sells": sum(1 for t in om if t.direction == "sell"),
            "buy_value": round(sum(t.value or 0 for t in om if t.direction == "buy")),
            "sell_value": round(sum(t.value or 0 for t in om if t.direction == "sell")),
        },
        "compensation_count": len(comp),
        "other_count": len(other),
        "open_market_pct": round(len(om) / len(trades) * 100, 1) if trades else 0.0,
        "by_ticker": sorted(ticks, key=lambda x: abs(x["net_value"]), reverse=True)[:top],
        "cluster_buys": cluster[:top],
        "by_owner": owners[:top],
        "plan_sells": plan_sells,
        "implausible_price": sum(1 for t in trades if t.price_implausible),
        "date_anomaly_count": sum(1 for t in trades if t.date_anomaly),
        "delay": {
            "median_days": round(statistics.median(delays), 1) if delays else None,
            "over_2d": sum(1 for d in delays if d > 2),
            "note": "Section 16(a) 要求交易后两个工作日内申报。"
                    "此处按自然日计算，未扣除周末与节假日，"
                    "「超 2 天」是事实统计不是违规认定。",
        },
        "notes": {
            # ⚠️ 分类比例要报**全库**的，不能报当前样本的：
            # 页面默认就筛了 open_market，样本里自然 100% 是公开市场，
            # 那句话就变成同义反复、什么也没说明。真正要让用户看见的是
            # 「整个 Form 4 里公开市场只占多少」——那是全库口径。
            "classification": _classification_note(library, len(om), len(comp), len(other)),
            "plan": "10b5-1 是预先制定的交易计划，卖出往往是几个月前排好的，"
                    "与临时决定卖出的含义不同。",
            "price": "部分申报把「总金额」误填进「每股价格」字段（实测有报到 "
                     "$2,400 万/股 的）。每股价 > $100 万的记录已剔出金额统计"
                     "（BRK.A ~$70 万/股是美股最高价，超过即不可能），"
                     "但**更隐蔽的错填识别不出来** —— 金额汇总仍应视为近似值。",
            "disclaimer": "本页只呈现已公开申报的事实，不构成任何投资建议。",
        },
    }
