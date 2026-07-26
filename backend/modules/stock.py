"""个股页：把九条线在一只票上汇合。

━━━━━━━━━━━━━━ ⚠️ 这一栏真正的难点不是聚合，是**时间轴对不齐** ━━━━━━━━━━━━━━

把九个来源摆在同一个屏幕上，最容易让人误以为它们说的是**同一时刻**的事。
实际上它们的新鲜度相差**两个数量级**：

| 线 | 数据所属时点 | 滞后 |
|---|---|---|
| 期权链 / GEX / 扫描器 | 上一个交易时段 | 延时 ~15 分钟 |
| 内部人 Form 4 | 交易日 | 法定 2 个工作日内申报 |
| 交割失败 FTD | 结算日 | 约 2 周 |
| 国会议员申报 | 交易日 | 法定 45 天，实际常更久 |
| 场外 / 暗池 | 周 | 约 4 周 |
| 机构 13F | **季末时点** | **至少 45 天** |

**所以「同一只票的全景」是个假象** —— 13F 说的是三个月前的季末，
期权链说的是昨天收盘。把它们并排放却不标时点，读者会自然而然
把它们当成一个连贯的故事，而那个故事是拼出来的。

本模块的做法：**每一块都必须带自己的 `as_of` 与滞后天数**，
界面按滞后从新到旧排，⛔ **不做任何跨源的"综合评分"**——
把不同时点的东西加权成一个数，是这一栏最容易犯也最难被发现的错。

━━━━━━━━━━━━━━ ⚠️ 缺一块 ≠ 那块是零 ━━━━━━━━━━━━━━

九条线里有四条依赖**本地已同步/已沉淀**的数据（内部人、机构、做空、扫描器），
一条**默认关闭**（暗池）。任何一块取不到，本页都给出**它自己的原因**：
「还没同步」/「本机没攒够」/「数据源被关着」/「取数失败」——
⛔ 绝不把这些一律显示成空白或 0，那会让读者以为"这只票没有内部人交易"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

#: 每条线的名字与它天然的滞后（用于排序与提示，不是精确值）
LANES = {
    "quote": ("行情与期权链", "CBOE 延时约 15 分钟"),
    "flow": ("期权流", "同上；持仓量为隔夜结算数，滞后一天"),
    "gex": ("伽马敞口", "同上"),
    "scanner": ("IV 排名", "本地逐日沉淀，需 60 个交易日才有 IV Rank"),
    "insider": ("内部人 Form 4", "法定 2 个工作日内申报"),
    "shorts": ("交割失败 FTD", "SEC 约两周后发布"),
    "congress": ("国会议员申报", "法定 45 天，实际常更久"),
    "darkpool": ("场外 / 暗池", "FINRA 约四周"),
    "institution": ("机构 13F", "季末时点，至少滞后 45 天"),
}

NOTES = {
    "timeline": (
        "**这九块数据的新鲜度相差两个数量级**：期权链是上一个交易时段，"
        "机构 13F 是三个月前的季末。并排摆着很容易被当成同一时刻的事 —— "
        "所以每一块都标了自己的时点与滞后，按从新到旧排。"),
    "no_score": (
        "⛔ **本页不做跨源综合评分。** 把不同时点、不同口径的数据加权成一个"
        "「多空分数」，是这一栏最容易犯也最难被发现的错 —— "
        "那个数字看着很干脆，但它把「三个月前的持仓」和「昨天的期权成交」"
        "当成了同一件事。本页只把各块并排放好，判断留给读者。"),
    "missing": (
        "某一块空着时，本页会说明**它自己的原因**：还没同步 / 本机没攒够 / "
        "数据源被关着 / 取数失败。⛔ 这些都不等于「这只票没有那类活动」。"),
}


@dataclass(frozen=True)
class Lane:
    """个股页上的一块。"""

    key: str
    title: str
    lag_note: str
    #: 这块数据**自己**的时点（YYYY-MM-DD 或 ISO 时间戳）
    as_of: Optional[str]
    ok: bool
    #: 取不到时的原因码：
    #: not_synced（还没同步）/ not_enough（本机没攒够）/ disabled（源被关着）/
    #: fetch_failed（取数失败）/ no_data（确实没有）/ no_mapping（定位不到）/
    #: bad_symbol（代码不合法）
    #: ⚠️ 六种在界面上必须分得开 —— 只有 no_data 才是"这只票没有那类活动"。
    reason: Optional[str]
    detail: Optional[str]
    data: Any

    @property
    def lag_days(self) -> Optional[int]:
        """距今多少天。

        ⚠️ 基准用**美东今日**，不是服务器本地日期。这些 `as_of` 全是美国市场的
        交易日/结算日/申报日；用本地日期做基准，同一时刻部署在奥克兰和洛杉矶
        会算出差一天的滞后 —— 一个跟着部署地漂移的"滞后天数"没有意义。
        """
        if not self.as_of:
            return None
        try:
            d = datetime.fromisoformat(self.as_of.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                d = date.fromisoformat(self.as_of[:10])
            except ValueError:
                return None
        # ⚠️ 连取"美东今日"都要兜：缺 tzdata 之类的环境问题会从这里抛出来，
        #    而 `lag_days` 是在 `assemble()` 里被访问的 —— 那已经在
        #    各 lane 的兜底之外，抛出去就是整页 500。
        try:
            from sources.cboe import et_today
            today = date.fromisoformat(et_today())
        except Exception:                            # noqa: BLE001
            today = date.today()
        # ⚠️ 未来日期会算出负数，排序时被顶到最前、看着像"最新的一块"。
        #    时点在未来只可能是数据或时区出了问题 —— 按 0 计并留痕。
        return max(0, (today - d).days)


REASON_LABEL = {
    "not_synced": "本地还没同步这条线的数据（不是这只票没有）",
    "not_enough": "本机攒的历史还不够（这份历史补不回来，只能逐日攒）",
    "disabled": "这个数据源默认关闭（配置状态，不是没有数据）",
    "fetch_failed": "取数失败（不是没有数据）",
    "no_data": "这只票在该数据源里确实没有记录",
    "bad_symbol": "代码不合法或不在覆盖范围内",
    # ⚠️ 与 no_data 严格区分：数据在，只是**我们没法从代码定位到它**
    "no_mapping": "数据源用的是别的标识（13F 只给 CUSIP），无法从股票代码可靠定位",
}


def lane(key: str, *, as_of: Optional[str] = None, data: Any = None,
         reason: Optional[str] = None, detail: Optional[str] = None) -> Lane:
    title, lag_note = LANES.get(key, (key, ""))
    return Lane(key=key, title=title, lag_note=lag_note, as_of=as_of,
                ok=reason is None, reason=reason, detail=detail, data=data)


def to_dict(l: Lane, lag_days: Optional[int] = ...) -> dict:
    if lag_days is ...:
        try:
            lag_days = l.lag_days
        except Exception:                            # noqa: BLE001
            lag_days = None
    return {
        "key": l.key, "title": l.title, "lag_note": l.lag_note,
        "as_of": l.as_of, "lag_days": lag_days,
        "ok": l.ok, "reason": l.reason,
        "reason_label": REASON_LABEL.get(l.reason or "", None),
        "detail": l.detail, "data": l.data,
    }


def assemble(lanes: list[Lane]) -> dict:
    """按时点从新到旧排。

    ⚠️ 排序键是**滞后天数**，不是我们写死的顺序 —— 让"哪块最旧"
    由数据自己说了算。没有时点的（取不到的那些）沉到最后。
    """
    def _age(l: "Lane") -> Optional[int]:
        # ⚠️ `lag_days` 是个 property，会做日期解析 —— 在这里抛异常
        #    同样是整页 500（它已经在各 lane 的兜底之外）。
        try:
            return l.lag_days
        except Exception:                            # noqa: BLE001
            return None

    ages = {l.key: _age(l) for l in lanes}
    ordered = sorted(lanes, key=lambda l: (ages[l.key] is None, ages[l.key] or 0))
    ok = [l for l in ordered if l.ok]
    missing = [l for l in ordered if not l.ok]
    spread = None
    days = [ages[l.key] for l in ok if ages[l.key] is not None]
    if len(days) >= 2:
        spread = {"newest": min(days), "oldest": max(days)}
    return {
        "lanes": [to_dict(l, ages[l.key]) for l in ordered],
        "available": len(ok), "unavailable": len(missing),
        # 最新与最旧差多少天 —— 这个数字本身就是对"全景"二字最好的提醒
        "lag_spread_days": spread,
        "notes": NOTES,
    }
