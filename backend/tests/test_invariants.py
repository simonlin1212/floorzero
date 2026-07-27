"""铁律三：其余那些"一改就悄悄错掉"的地方。

- 暗池的四种记录类型混在一个返回里，加错一次数字就翻倍
- 个股页九条线，一条挂掉不该带倒整页
- 原因码有七种，只有 `no_data` 才是"这只票没有那类活动"
- 希腊字母的符号与量纲（用有限差分对照，不是抄公式）
- 联系方式未配置必须 fail-fast
"""
from __future__ import annotations

import math

import pytest

from modules import darkpool as dp
from modules import stock as st


# ─────────────────── 暗池：不能把同一笔量算两遍 ───────────────────

def _row(**kw):
    base = dict(summaryTypeCode="ATS_W_SMBL_FIRM", weekStartDate="2260-01-05",
                issueSymbolIdentifier="X", MPID="AAA",
                marketParticipantName="A", tierDescription="T1",
                totalWeeklyShareQuantity="1000", totalWeeklyTradeCount="10",
                totalNotionalSum="5000")
    base.update(kw)
    return base


def test_聚合行与明细行不能相加():
    """`*_SMBL` 是聚合、`*_SMBL_FIRM` 是同一笔量按机构拆分。
    全部 SUM 出来恰好是真值的两倍 —— 实测 NVDA 那周正是如此。"""
    rows = [
        _row(MPID="AAA", totalWeeklyShareQuantity="600"),
        _row(MPID="BBB", totalWeeklyShareQuantity="400"),
        _row(summaryTypeCode="ATS_W_SMBL", MPID="",
             totalWeeklyShareQuantity="1000"),          # 聚合行
    ]
    w = dp.week_summary(dp.parse(rows), "2260-01-05")
    assert w["ats"]["shares"] == 1000.0, "只用明细合计，不能把聚合行也加进去"
    assert w["ats"]["reconcile"]["matches"] is True


def test_对账容差是一股而不是相对误差():
    """成交股数是整数。写成 `max(1.0, ref*1e-6)` 取的是**较大者** ——
    2.68 亿股时容差高达 268 股，足以放过真实的漏行/重行。"""
    rows = [_row(totalWeeklyShareQuantity="268000000"),
            _row(summaryTypeCode="ATS_W_SMBL", MPID="",
                 totalWeeklyShareQuantity="268000200")]      # 差 200 股
    w = dp.week_summary(dp.parse(rows), "2260-01-05")
    assert w["ats"]["reconcile"]["matches"] is False, "差 200 股就该报对不上"


def test_ats_与非ats_始终分开():
    """"暗池"不等于"场外"：非 ATS 是批发商内部化，实测比 ATS 大一倍以上。
    模块**刻意不提供** `dark_pool_shares` 这种字段，免得诱使调用方去加。"""
    rows = [_row(totalWeeklyShareQuantity="100"),
            _row(summaryTypeCode="OTC_W_SMBL_FIRM", MPID="",
                 totalWeeklyShareQuantity="240")]
    w = dp.week_summary(dp.parse(rows), "2260-01-05")
    assert w["ats"]["shares"] == 100.0
    assert w["otc"]["shares"] == 240.0
    assert "dark_pool_shares" not in w, "不能给一个诱人相加的字段名"
    assert w["ats_over_otc"] == pytest.approx(100 / 240)


def test_未知记录类型被计数而不是默默丢掉():
    """FINRA 加一个新类型时，我们会悄无声息地漏掉一整类成交。"""
    parsed = dp.parse([_row(summaryTypeCode="BRAND_NEW_TYPE")])
    assert parsed["unknown_types"] == {"BRAND_NEW_TYPE": 1}
    assert parsed["parsed_any"] is False, "认得的类型一行都没有 → 解析不兼容"


def test_平均每笔股数在笔数为零时算不出():
    rows = dp.parse([_row(totalWeeklyTradeCount="0")])["ats_firm"]
    assert rows[0].avg_trade_size is None, "「没有成交」不是「平均每笔 0 股」"


# ─────────────────── 个股页：一条线挂掉不带倒整页 ───────────────────

def test_lane_的时点解析坏掉时不炸整页():
    """`lag_days` 是个 property，读起来像字段、执行起来会做日期解析 ——
    而它在 `assemble()` 里被求值，那已经在各 lane 的兜底之外。"""
    lanes = [st.lane("quote", as_of="不是日期", data={}),
             st.lane("gex", as_of="2260-01-05", data={})]
    out = st.assemble(lanes)                       # 不该抛
    got = {l["key"]: l["lag_days"] for l in out["lanes"]}
    assert got["quote"] is None
    assert isinstance(got["gex"], int)


def test_未来时点不会被排到最前():
    """时点在未来只可能是数据或时区出了问题；算出负数会顶到"最新"那一格。"""
    out = st.assemble([st.lane("quote", as_of="2999-01-01", data={})])
    assert out["lanes"][0]["lag_days"] == 0


def test_只有_no_data_才表示这只票没有那类活动():
    """七种原因码里，其余六种说的都是"我们拿不到"。
    混为一谈会让读者以为"这只票没有内部人交易"。

    ⚠️ 早先这个用例是靠**在文案里找"没有"两个字**来判断的 —— 那是启发式，
    随便换个措辞就能骗过去（`no_data: "数据获取失败"` 也能通过）。
    正确做法是让**代码自己**带上分类（`MEANS_ABSENT`），
    UI / MCP / 测试都引用同一个常量，而不是各自去读文案猜。
    """
    assert st.MEANS_ABSENT == {"no_data"}
    # 两个集合必须不相交、且合起来覆盖全部原因码 —— 不能有归不了类的
    assert not (st.MEANS_ABSENT & st.MEANS_UNAVAILABLE)
    assert st.MEANS_ABSENT | st.MEANS_UNAVAILABLE == set(st.REASON_LABEL)


def test_每条_lane_都带上是否真的没有():
    """前端与工具层直接用这个字段，不必自己判断哪条算"没有"。"""
    absent = st.to_dict(st.lane("insider", reason="no_data", detail=""))
    cant = st.to_dict(st.lane("darkpool", reason="disabled", detail=""))
    fine = st.to_dict(st.lane("quote", as_of="2020-01-01", data={}))
    assert absent["means_absent"] is True
    assert cant["means_absent"] is False
    assert fine["means_absent"] is None, "有数据的块无所谓"


def test_时间轴按滞后排序而不是按写死的顺序():
    # ⚠️ 用**过去**的日期：未来时点会被 clamp 成 0，两块并列、
    #    稳定排序保留原顺序，那样这个用例测的就不是排序了。
    lanes = [st.lane("institution", as_of="2020-01-01", data={}),
             st.lane("quote", as_of="2020-06-01", data={})]
    out = st.assemble(lanes)
    assert [l["key"] for l in out["lanes"]] == ["quote", "institution"]
    assert out["lag_spread_days"]["oldest"] > out["lag_spread_days"]["newest"]


def test_取不到的块沉到最后():
    lanes = [st.lane("darkpool", reason="disabled", detail="关着"),
             st.lane("quote", as_of="2020-06-01", data={})]
    out = st.assemble(lanes)
    assert out["lanes"][-1]["key"] == "darkpool"
    assert out["available"] == 1 and out["unavailable"] == 1


# ─────────────────── 希腊字母：用有限差分对照 ───────────────────

def _call_delta(S, K, t, sigma, r):
    """从 d1 现算 delta，用来给 gamma 做有限差分对照。

    ⚠️ 刻意**不引用模块里的实现** —— 拿被测代码自己的中间量去验它自己，
    验的是"我抄得一致"，不是"公式对"。
    """
    from statistics import NormalDist
    d1 = ((math.log(S / K) + (r + 0.5 * sigma ** 2) * t)
          / (sigma * math.sqrt(t)))
    return NormalDist().cdf(d1)


def test_gamma_是_delta_对现价的导数():
    """用有限差分对照，不是抄公式核对公式。"""
    from modules import bs
    S, K, t, v, r = 100.0, 100.0, 0.25, 0.30, 0.04
    h = 1e-3
    numeric = (_call_delta(S + h, K, t, v, r)
               - _call_delta(S - h, K, t, v, r)) / (2 * h)
    assert bs.bs_gamma(S, K, t, v, r) == pytest.approx(numeric, rel=1e-5)


def test_gamma_的峰在平值附近且两侧单调衰减():
    """形状错了，符号对也没用。

    ⚠️ 只比"平值 vs 很远的两点"是**假阳性**：峰跑到 110 也照样比 70/130 高。
    所以这里扫一遍行权价网格，验两件事 ——
    ① 峰确实落在现价附近（带利率时未必**恰好**在 K=S，所以给一个窄带）
    ② 从峰往两边是单调下降的
    """
    from modules import bs
    S = 100.0
    ks = [70 + 2 * i for i in range(31)]              # 70 … 130
    gs = [bs.bs_gamma(S, float(k), 0.25, 0.3) for k in ks]
    peak = ks[gs.index(max(gs))]
    assert abs(peak - S) <= 6, f"峰落在 {peak}，不在现价附近"
    top = gs.index(max(gs))
    assert all(gs[i] < gs[i + 1] for i in range(top)), "峰左侧应递增"
    assert all(gs[i] > gs[i + 1] for i in range(top, len(gs) - 1)), "峰右侧应递减"


def test_退化输入返回零而不是抛异常或_nan():
    """已到期合约、报价缺失的深度虚值合约，在真实期权链里天天出现。"""
    from modules import bs
    for args in ((100.0, 100.0, 0.0, 0.3),      # 到期
                 (100.0, 100.0, 0.25, 0.0),     # 零波动率
                 (0.0, 100.0, 0.25, 0.3),       # 现价为 0
                 (100.0, 0.0, 0.25, 0.3)):      # 行权价为 0
        g = bs.bs_gamma(*args)
        assert g == 0.0 and math.isfinite(g), args


def test_vanna_与_charm_在退化输入下也不炸():
    from modules import bs
    for fn in (bs.bs_vanna, bs.bs_charm):
        v = fn(100.0, 100.0, 0.0, 0.3)
        assert math.isfinite(v), f"{fn.__name__} 在 t=0 返回了 {v}"


def test_零dte_不会让_gamma_爆成无穷():
    """`years_to_expiry` 把 0DTE 按半个交易日算，就是为了这个。"""
    from modules import bs
    t = bs.years_to_expiry(0)
    assert t > 0
    assert math.isfinite(bs.bs_gamma(100.0, 100.0, t, 0.3))


# ─────────────────── 联系方式：必须 fail-fast ───────────────────

def test_未配置联系方式时直接报错(monkeypatch):
    """内置占位值会让每个用户的上游流量都以作者身份发出，
    而且被限流时毫无线索。"""
    from sources import contact
    monkeypatch.delenv("FZ_CONTACT", raising=False)
    with pytest.raises(contact.ContactNotConfigured):
        contact.user_agent()


def test_联系方式必须像个邮箱(monkeypatch):
    from sources import contact
    monkeypatch.setenv("FZ_CONTACT", "just a name")
    with pytest.raises(contact.ContactNotConfigured):
        contact.user_agent()


def test_配好之后_ua_带上联系方式(monkeypatch):
    from sources import contact
    monkeypatch.setenv("FZ_CONTACT", "Someone one@example.com")
    assert "one@example.com" in contact.user_agent()


# ─────────────────── 工具层：声明与实现不能漂移 ───────────────────

def test_每个工具声明都有实现():
    """工具定义只在 `tools.py` 一处 —— MCP server 自动继承。
    声明与实现对不上，就会出现"MCP 里有个调不动的工具"。"""
    import tools
    assert sorted(t["name"] for t in tools.TOOLS) == sorted(tools._IMPL)


def test_工具_schema_必要字段齐全():
    import tools
    for t in tools.TOOLS:
        assert t.get("name") and t.get("description"), t
        assert t["inputSchema"]["type"] == "object", t["name"]


def test_未知工具返回结构化错误而不是抛异常():
    """MCP 调用方需要拿到错误，不是断连。"""
    import tools
    assert "error" in tools.exec_tool("no_such_tool", {})


# ─────────────────── 国会：终态判定不能靠文案 ───────────────────

def test_扫描件的原因文案与终态判定是同一个常量():
    """一份纯图片的 PDF 解析不了，是**终态** —— 无 OCR 永远读不了，重试没有意义。

    同步层曾靠在这句话里找一个词来判定终态。翻译一改措辞，判定就失效：
    33 份扫描件重新变成"每次都重试"，吃光配额、更早的申报永远轮不到，
    而且同步照常报成功。所以这里断言的是**产出方与判定方用同一个常量**，
    不是断言这句话里有哪个词。
    """
    from modules import congress as parse

    writer = pytest.importorskip("pypdf").PdfWriter()
    writer.add_blank_page(width=612, height=792)      # 有效 PDF，零个可提取字符
    buf = __import__("io").BytesIO()
    writer.write(buf)

    res = parse.parse_house_ptr(buf.getvalue(), _filing())
    assert res.trades == ()
    assert res.unparsed_reason is not None
    assert parse.is_terminal(res.unparsed_reason), res.unparsed_reason


def test_可重试的失败不会被判成终态():
    """网络故障、临时取不到 —— 判成终态就再也不会重试了。"""
    from modules import congress as parse
    for reason in ("File unavailable: timeout", "No transaction lines recognised in the PDF",
                   None):
        assert not parse.is_terminal(reason), reason


def _filing():
    from datetime import date
    from sources.congress import Filing
    return Filing(chamber="house", name="X", last="X", first="X",
                  state_district="IN02", filing_type="P",
                  filing_date=date(2260, 1, 5), year="2260", doc_id="1",
                  detail_url="")
