"""国会披露的增量同步编排。

首次同步要抓几百份 PDF（众议院 2026 年 313 份，按限速约 100 秒），
之后每次只补新增申报。同步在后台线程跑，进度可查询 —— 因为
把一个两分钟的请求挂在那儿转圈，用户根本分不清是慢还是死了。

⚠️ **失败要分类**：
- 单份申报解析失败 → 记进库（附原因），继续跑下一份；
- 参议院整条线不可用（缺 curl_cffi / 被拦）→ 记成**环境问题**并显示出来，
  绝不能显示成"参议院没有交易"。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Optional

from sources import congress as src
from modules import congress as parse
from modules import congress_store as store


class SyncState:
    """同步进度（单实例，够用；本工具设计上就是单用户自部署）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.stage = "idle"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.senate_status: Optional[str] = None

    def _snapshot_locked(self) -> dict:
        """⚠️ 调用方必须**已持有** self.lock。

        单独拆出来，是因为 `threading.Lock` **不可重入**：
        在 `with self.lock:` 里再调一次 `snapshot()` 会永久死锁
        （实测：第二次 POST /api/congress/sync 卡死，吃掉一个 worker）。
        """
        return {
            "running": self.running, "done": self.done, "total": self.total,
            "stage": self.stage, "started_at": self.started_at,
            "finished_at": self.finished_at,
            "errors": self.errors[-12:], "error_count": len(self.errors),
            "senate_status": self.senate_status,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _sync_house(year: int, limit: Optional[int]) -> None:
    STATE.stage = f"众议院 {year} 索引"
    filings = [f for f in src.house_filings(year) if f.is_ptr]
    # ⚠️ 跳过「已定局」的 = 解析成功的 + 扫描件（终态，读不了就是读不了）。
    # 不能只跳过成功的：扫描件会把 limit 配额吃光，更早的申报永远轮不到。
    # 也不能把所有失败都当已完成：那样暂时性故障永远没机会自愈。
    known = store.settled_doc_ids("house")
    todo = [f for f in filings if f.doc_id not in known]
    todo.sort(key=lambda f: f.filing_date or date.min, reverse=True)
    if limit:
        todo = todo[:limit]
    with STATE.lock:
        STATE.total += len(todo)
    STATE.stage = f"众议院 {year}（新增 {len(todo)} 份 / 共 {len(filings)} 份）"

    for f in todo:
        try:
            res = parse.parse_house_ptr(src.house_ptr_pdf(f.year, f.doc_id), f)
            # 扫描件 = 终态（无 OCR 永远读不了）；其余失败留待下次重试
            store.save_filing(f, [parse.to_dict(t) for t in res.trades],
                              res.unparsed_reason,
                              terminal=bool(res.unparsed_reason
                                            and "扫描件" in res.unparsed_reason))
        except src.DataNotAvailable as e:
            store.save_filing(f, [], f"文件不可得：{e}")
        except Exception as e:                       # 单份失败不该拖垮整次同步
            with STATE.lock:
                STATE.errors.append(f"众议院 {f.doc_id} {f.name}: {type(e).__name__}: {e}")
        finally:
            with STATE.lock:
                STATE.done += 1


def _sync_senate(since: str, limit: Optional[int]) -> None:
    STATE.stage = "参议院索引"
    ok, msg = src.senate_available()
    STATE.senate_status = msg
    if not ok:
        # ⚠️ 环境问题 —— 记成错误显示出来，不是"参议院没有交易"
        with STATE.lock:
            STATE.errors.append(f"参议院不可用：{msg}")
        return

    # ⚠️ 顺序是关键：**先取全量索引 → 再滤掉已同步的 → 最后才套配额**。
    # 若把 limit 传给索引（只取最新 N 条），这 N 条一旦都已缓存，
    # 之后每次同步都拿同一批再全部丢弃 —— 更早的申报**永远同步不到**，
    # 而且表面上"跑完了、没出错"。众议院那条路径本来就是这个顺序，这里对齐。
    filings = src.senate_filings(since, limit=10000)
    known = store.settled_doc_ids("senate")
    todo = [f for f in filings if f.doc_id and f.doc_id not in known]
    todo.sort(key=lambda f: f.filing_date or date.min, reverse=True)
    if limit:
        todo = todo[:limit]
    with STATE.lock:
        STATE.total += len(todo)
    STATE.stage = f"参议院（新增 {len(todo)} 份 / 共 {len(filings)} 份）"

    for f in todo:
        try:
            if f.is_paper:
                store.save_filing(f, [], "纸质扫描件（图片），无 OCR 无法解析明细",
                                  terminal=True)
            else:
                res = parse.parse_senate_ptr(src.senate_ptr_html(f.detail_url), f)
                store.save_filing(f, [parse.to_dict(t) for t in res.trades],
                                  res.unparsed_reason)
        except src.DataNotAvailable as e:
            store.save_filing(f, [], str(e))
        except src.SenateUnavailable as e:
            with STATE.lock:
                STATE.errors.append(f"参议院中断：{e}")
            break                                    # 会话挂了，后面的也没意义
        except Exception as e:
            with STATE.lock:
                STATE.errors.append(f"参议院 {f.doc_id} {f.name}: {type(e).__name__}: {e}")
        finally:
            with STATE.lock:
                STATE.done += 1


def _run(years: list[int], since: str, limit: Optional[int],
         chambers: tuple[str, ...]) -> None:
    try:
        # ⚠️ limit 是**本次同步总额度**，两院共享。
        # 给每院各传一份会让 limit=N 实际处理最多 2N 份 —— 与参数说明不符。
        remaining = limit
        if "house" in chambers:
            for y in years:
                if remaining is not None and remaining <= 0:
                    break
                _sync_house(y, remaining)
                if remaining is not None:
                    remaining = max(0, limit - STATE.done)
        if "senate" in chambers and (remaining is None or remaining > 0):
            _sync_senate(since, remaining)
        STATE.stage = "完成"
    except Exception as e:
        STATE.stage = f"中断：{type(e).__name__}: {e}"
        with STATE.lock:
            STATE.errors.append(f"同步中断：{type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(year: Optional[int] = None, years_back: int = 0,
          since: Optional[str] = None, limit: Optional[int] = None,
          chambers: tuple[str, ...] = ("house", "senate")) -> dict:
    """启动一次增量同步（已在跑就直接返回当前进度，不叠开）。

    ⚠️ **众议院的归档是按年分卷的**，一次只能同步指定年份。
    默认只同步当年 —— 所以 UI 上不能说"一次同步就补齐全部历史"。
    要往前回补就传 `years_back`（如 3 = 当年 + 前 3 年，共 4 卷）。
    每多一年就多几百份 PDF，代价要让用户自己决定，不该默默替他跑。
    """
    with STATE.lock:
        if STATE.running:
            # 已持锁 —— 必须用 _snapshot_locked，调 snapshot() 会死锁
            return {"started": False, "reason": "已有同步在进行中",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.done = 0
        STATE.total = 0
        STATE.errors = []
        STATE.stage = "启动中"
        STATE.senate_status = None
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    base = year or date.today().year
    years = [base - i for i in range(max(0, years_back) + 1)]
    # ⚠️ 参议院起点必须跟随**年份边界**，不能用"今天往前 N 天"的滚动窗口：
    # 众议院按自然年分卷同步，参议院若用 180 天滚动窗，七月跑一次就会漏掉
    # 当年 1-2 月的参议院申报 —— 两院覆盖范围对不上，而 UI 却说这是"同步某年"。
    # 传了 year 却不影响参议院范围，更是直接与参数语义矛盾。
    s = since or f"01/01/{min(years)}"
    threading.Thread(target=_run, args=(years, s, limit, chambers), daemon=True).start()
    return {"started": True, **STATE.snapshot()}
