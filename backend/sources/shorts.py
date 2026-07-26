"""做空数据源 —— SEC 交割失败（主）+ FINRA 场外空头成交量（可选）。

━━━ ⚠️ 两个源的合规级**完全不同**，别混为一谈 ━━━

| 源 | 级别 | 条款实况（2026-07-26 实读原文） |
|---|---|---|
| **SEC FTD** | **S** | 与 EDGAR 同源：只限速率（10 请求/秒）+ 要求声明 UA，**不限商用** |
| **FINRA Reg SHO** | **B ⚠️** | 见下，**默认关闭** |

FINRA Terms of Use（https://www.finra.org/terms-of-use，2023-11-09 版）原文：

    Permitted Uses: "the content and material provided through the FINRA Website
    shall be used ONLY for your own non-commercial personal or professional use."

    Restrictions (d): "develop or create a database of data using the FINRA
    Website, except as expressly permitted by any other terms of use..."

    Restrictions (e): "use any process to monitor or copy the FINRA Website in
    bulk, or use any data mining, scraping or harvesting tools (including robots)"

⚠️ **存在真实的模糊地带，本项目不替用户解释**：
- 条款开头把范围写成 "the use of the FINRA.**ORG** site"，而 Reg SHO 数据文件在
  `cdn.finra.org`（不同主机名）—— 是否涵盖，条款没说清。
- 限制 (d) 禁止"建数据库"，而本项目每个分栏都是「下载 → 落本地 SQLite」。
- FINRA 另有一套 **API Terms of Service**（developer.finra.org），是**点击同意式许可**，
  需要每个用户自己注册并接受 —— 我们无法代为接受。

→ 所以：**FINRA 这条默认关闭**，要用必须显式开启（`VF_ENABLE_FINRA=1`），
  开启处会把上述原文摆出来。判断由用户自己做，我们只保证他看得到条款。
  本分栏的**主源是 SEC FTD**，不开 FINRA 也完全可用。
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date, timedelta
from typing import Iterator, Optional

import requests

from sources.contact import user_agent
from sources.edgar import DataNotAvailable, _limiter

FTD_BASE = "https://www.sec.gov/files/data/fails-deliver-data"
FINRA_CDN = "https://cdn.finra.org/equity/regsho/daily"

#: FINRA Terms of Use 原文（展示给用户看，不要改写）
FINRA_TERMS = {
    "url": "https://www.finra.org/terms-of-use",
    "last_modified": "2023-11-09",
    "permitted": ("the content and material provided through the FINRA Website "
                  "shall be used ONLY for your own non-commercial personal or "
                  "professional use."),
    "restriction_d": ("develop or create a database of data using the FINRA "
                      "Website, except as expressly permitted by any other terms "
                      "of use on the FINRA Website"),
    "restriction_e": ("use any process to monitor or copy the FINRA Website in "
                      "bulk, or use any data mining, scraping or harvesting tools "
                      "(including robots), or any similar data-gathering or "
                      "extraction tools"),
    "ambiguity": ("条款范围写的是「the use of the FINRA.ORG site」，而 Reg SHO "
                  "数据文件在 cdn.finra.org（不同主机名）—— 是否涵盖条款没说清。"
                  "限制 (d) 禁止「建数据库」，而本工具会把数据落进本地 SQLite。"
                  "另有一套需注册接受的 API Terms of Service（developer.finra.org），"
                  "我们无法代为接受。"),
    "our_stance": ("本项目不替你解释这些条款：FINRA 这条**默认关闭**，"
                   "要用请设 VF_ENABLE_FINRA=1 并自行判断你的用途是否合规。"
                   "本分栏主源是 SEC FTD（S 级、不限商用），不开 FINRA 也完全可用。"),
}


def finra_enabled() -> bool:
    """FINRA 源是否被用户显式开启。"""
    return (os.environ.get("VF_ENABLE_FINRA") or "").strip().lower() in (
        "1", "true", "yes", "on")


class FinraDisabled(RuntimeError):
    """FINRA 源未开启 —— **这是配置状态，不是「没有数据」**。

    单独立一个类型，是为了让 UI 能显示成「你没开这个源」，
    而不是显示成一张空表让用户以为市场上没有空头成交。
    """


# ─────────────────────── SEC 交割失败（S 级 · 主源）───────────────────────

def ftd_files(back: int = 6, today: Optional[date] = None) -> list[str]:
    """最近 N 个半月档的文件标识（新→旧），如 `202606b`。

    SEC 每月发两个文件：`a` = 上半月、`b` = 下半月。
    上半月的文件月底才发，下半月的次月 15 号左右才发 —— 所以最新一档常常还没有。
    """
    d = today or date.today()
    out: list[str] = []
    y, m = d.year, d.month
    for _ in range((back + 1) // 2 + 1):
        for half in ("b", "a"):
            out.append(f"{y}{m:02d}{half}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out[:back]


def ftd_rows(tag: str) -> Iterator[dict]:
    """流式产出某半月档的 FTD 记录。

    ⚠️ 流式：单档约 6 万行，虽然不算大，但保持与 13F 同样的习惯 ——
    这个项目已经因为「整表驻留」踩过 5.3GB 的坑。
    """
    url = f"{FTD_BASE}/cnsfails{tag}.zip"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=180)
    except requests.RequestException as e:
        raise RuntimeError(f"SEC FTD 网络故障: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"SEC 尚未发布该档 FTD 数据: {tag}")
    if r.status_code != 200:
        raise RuntimeError(f"SEC FTD HTTP {r.status_code}: {tag}")

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"FTD {tag} 返回的不是 ZIP（可能是错误页）") from e

    names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
    if not names:
        raise RuntimeError(f"FTD {tag} 的 ZIP 内无 txt（结构可能已变更）")

    with zf.open(names[0]) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        header = next(text).rstrip("\n").split("|")
        for line in text:
            vals = line.rstrip("\n").split("|")
            if len(vals) < len(header):
                continue                    # 文件尾常有说明行，跳过而不是猜
            yield dict(zip(header, vals))


# ─────────────────── FINRA 场外空头成交量（B 级 · 默认关闭）───────────────────

def finra_short_volume(day: date, market: str = "CNMS") -> Iterator[dict]:
    """某日的 FINRA 场外空头成交量。

    ⚠️ **默认不可用**：需用户设 `VF_ENABLE_FINRA=1` 显式开启（见模块文档的条款原文）。

    `market`：CNMS = 综合（NMS 证券，最常用）/ FNSQ / FNYX / FNRA 为各设施明细。
    """
    if not finra_enabled():
        raise FinraDisabled(
            "FINRA 数据源未开启。它的条款限「非商用个人/专业用途」，"
            "且禁止「建数据库」与批量抓取，而本工具会把数据落进本地 SQLite —— "
            "是否合规请你自行判断。确认后设 VF_ENABLE_FINRA=1 开启。")

    url = f"{FINRA_CDN}/{market}shvol{day:%Y%m%d}.txt"
    _limiter.wait()
    try:
        r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"FINRA 网络故障: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise DataNotAvailable(f"FINRA 无该日数据（非交易日或尚未发布）: {day}")
    if r.status_code != 200:
        raise RuntimeError(f"FINRA HTTP {r.status_code}: {day}")

    lines = r.text.splitlines()
    if not lines:
        raise DataNotAvailable(f"FINRA {day} 文件为空")
    header = lines[0].split("|")
    for line in lines[1:]:
        vals = line.split("|")
        if len(vals) < len(header):
            continue                        # 文件尾的汇总行
        yield dict(zip(header, vals))


def recent_trading_days(n: int, end: Optional[date] = None) -> list[date]:
    """最近 N 个工作日（新→旧）。是否真有数据要取数时才知道。"""
    d = end or date.today()
    out: list[date] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out
