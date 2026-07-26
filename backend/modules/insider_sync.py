"""内部人交易的同步编排（季度批量 + 每日增量）。

━━━ 两条路径的成本差了两个数量级，必须让用户看清 ━━━

| | 季度数据集 | 每日 XML |
|---|---|---|
| 一次拿到 | ~10 万笔（整季度） | ~700 笔（单日） |
| 请求数 | **1 次** | **每份申报 1 次**（单日 645 次） |
| 耗时 | ~3 秒 | ~90 秒/天（限速 8/秒） |
| 时效 | 滞后 7~49 天 | 实时 |

→ 默认动作：**先补最近几个季度（几秒钟拿到几十万笔），再逐日补缺口**。
   缺口当前 117 天 —— 全补要 3 小时以上，所以默认只补最近 5 个工作日，
   要更多让用户自己选，**不替他默默跑几小时**。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Optional

from sources import edgar as src
from modules import insider as parse
from modules import insider_store as store

#: 距今几天以内的「无索引」不算定论 —— SEC 当日索引通常美东晚间才发布。
#: 超过这个天数还没有，才认定是非交易日。
INDEX_SETTLE_DAYS = 3


class SyncState:
    """同步进度（单用户自部署，单实例够用）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.rows = 0
        self.stage = "idle"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.coverage: Optional[str] = None

    def _snapshot_locked(self) -> dict:
        """⚠️ 调用方必须**已持有** self.lock。

        `threading.Lock` 不可重入，在持锁状态下再调 `snapshot()` 会死锁 ——
        Congress 分栏就是在这里踩过（第二次 POST 永久卡死）。
        """
        return {
            "running": self.running, "done": self.done, "total": self.total,
            "rows": self.rows, "stage": self.stage,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "errors": self.errors[-12:], "error_count": len(self.errors),
            "coverage": self.coverage,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _bump(done: int = 0, rows: int = 0) -> None:
    with STATE.lock:
        STATE.done += done
        STATE.rows += rows


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)


def _sync_quarters(quarters: list[str]) -> None:
    known = store.known_batches("quarter")
    todo = [q for q in quarters if q not in known]
    with STATE.lock:
        STATE.total += len(todo)
    for q in todo:
        STATE.stage = f"季度数据集 {q}"
        try:
            tables = src.quarter_dataset(q)
            trades = parse.parse_dataset(tables)
            n = store.save_trades(parse.to_dicts(trades), source="dataset")
            # ⚠️ 批次记录写**该批次解析出的行数**，不写"新增行数"：
            # 后者是去重后的增量，重叠导入或重试时会严重偏低，
            # 让「已导入 2026q1 = 3 行」这种明显失真的数字进库存表。
            store.mark_batch("quarter", q, rows=len(trades))
            _bump(rows=n)
        except src.DataNotAvailable:
            # 季度尚未发布 —— **不是错误**，也不标记为已同步（下次还要再试）
            _err(f"{q} 数据集尚未发布（SEC 滞后 7~49 天不等），跳过")
        except Exception as e:
            _err(f"季度 {q}: {type(e).__name__}: {e}")
        finally:
            _bump(done=1)


def _sync_one_day(day: date) -> tuple[int, int, int]:
    """抓一天的 Form 4。返回 (交易数, 申报数, 错误数)。"""
    refs = src.form4_filings(day)
    # 已知读不了的（老格式无 XML）直接跳过：它们是**终态**，重下多少次都一样。
    # 不跳的话，一份坏申报会让这一天永远无法标记完成，每次同步重下 600-700 份。
    dead = store.dead_accessions()
    total_refs = len(refs)
    refs = [r for r in refs if r.accession not in dead]
    rows: list[dict] = []
    errors = 0

    def one(r):
        # 每份申报单独分配主键（计数上下文就是这一份申报）
        return parse.to_dicts(parse.parse_form4_xml(src.filing_xml(r.txt_url), r))

    # SEC 允许 10 请求/秒；源层限速器已经把节奏卡住，这里并发只是为了填满带宽
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(one, r): r for r in refs}
        for fu in as_completed(futs):
            r = futs[fu]
            try:
                rows.extend(fu.result())
            except src.NoXmlInFiling as e:
                # 文档确实没有 XML = 终态，登记下来不再重试
                store.mark_dead(r.accession, day.isoformat(), str(e)[:120])
            except src.DataNotAvailable as e:
                # ⚠️ 「对象取不到」不是终态：新索引的申报正文可能还没同步过来。
                # 登记成死件的话，一次传播延迟就让那份申报永远缺失。
                # 计入 errors → 该日不标记完成 → 下次重试。
                errors += 1
                _err(f"{day} {r.company[:20]} {r.accession}: 正文暂不可得（{e}），将重试")
            except Exception as e:
                errors += 1
                _err(f"{day} {r.company[:20]} {r.accession}: {type(e).__name__}: {e}")
    n = store.save_trades(rows, source="daily")
    # ⚠️ **只有全部申报都成功才标记该日已完成**。
    # 有失败仍标完成的话，`known_batches("day")` 会让后续同步永久跳过这一天 ——
    # 一次网络抖动就让那天的数据永远缺一块，而且跑完不报错、看不出来。
    # 只剩**暂时性**失败（网络等）才不标记完成；终态失败已登记，不该拖住整天
    if errors == 0:
        # 记 len(rows)（该日实际解析出的交易数），不记去重后的新增数 ——
        # 部分失败后重试成功时，新增数只剩"这次补回来的那点"，会把批次记录写错。
        store.mark_batch("day", day.isoformat(), rows=len(rows),
                         filings=total_refs, errors=0)
    return n, total_refs, errors


def _sync_days(days: int) -> None:
    # 往回走跳过已完成的，而不是死盯最近 N 天 —— 否则缺口永远补不上。
    # 同时以「已导入季度的截止日」为下界：那之前的数据季度 ZIP 里已经有了，
    # 再逐份下载纯属重复（600-700 份/天、约 90 秒/天）。
    floor = None
    quarters = sorted(store.known_batches("quarter"))
    if quarters:
        floor = src.quarter_end(quarters[-1])
    todo = src.pending_form4_days(days, store.known_batches("day"), floor=floor)
    if not todo and floor:
        STATE.stage = f"逐日部分已补齐（{floor} 之前由季度数据集覆盖）"
    with STATE.lock:
        STATE.total += len(todo)
    for d in todo:
        STATE.stage = f"逐日抓取 {d}（单日约 600-700 份申报）"
        try:
            n, filings, errors = _sync_one_day(d)
            _bump(rows=n)
            if errors:
                _err(f"{d}: {filings} 份申报中 {errors} 份未取到/未解析，"
                     f"该日**未标记完成**，下次同步会重试")
        except src.DataNotAvailable:
            # ⚠️ 「取不到索引」有两种成因，必须分开：
            #   · 非交易日（周末/假期）→ 永远不会有，标记完成免得每次重试
            #   · **今天/昨天的索引 SEC 还没发**（通常美东晚间才发布）
            #     → 标记完成的话，这一天之后新增的申报**永远不会再补**
            if (date.today() - d).days >= INDEX_SETTLE_DAYS:
                store.mark_batch("day", d.isoformat(), rows=0, filings=0)
            else:
                _err(f"{d} 的索引尚未发布（SEC 通常美东晚间发布），"
                     f"该日未标记完成，下次同步会重试")
        except Exception as e:
            _err(f"{d}: {type(e).__name__}: {e}")
        finally:
            _bump(done=1)


def _update_coverage() -> None:
    """如实记录「数据集覆盖到哪天、之后靠逐日补了多少」。"""
    try:
        latest, _ = src.latest_available_quarter()
        if latest:
            end = src.quarter_end(latest)
            gap = (date.today() - end).days
            STATE.coverage = (f"SEC 季度数据集最新只到 {latest}（覆盖至 {end}），"
                              f"之后 {gap} 天只能靠逐日抓取补齐")
    except Exception as e:
        STATE.coverage = f"季度覆盖探测失败：{type(e).__name__}: {e}"


def _run(quarters_back: int, days: int) -> None:
    try:
        _update_coverage()
        if quarters_back:
            # ⚠️ 必须从**最新已发布**季度往回数：直接用自然季度会把额度
            # 花在还没发布的季度上（实测 quarters_back=2 一笔都导不进来）。
            STATE.stage = "探测可用季度"
            quarters, missing = src.available_quarters(quarters_back)
            if missing:
                _err(f"以下季度 SEC 尚未发布（滞后 7~49 天不等），已跳过：{'、'.join(missing)}")
            _sync_quarters(quarters)
        if days:
            _sync_days(days)
        STATE.stage = "完成"
    except Exception as e:
        STATE.stage = f"中断：{type(e).__name__}: {e}"
        _err(f"同步中断：{type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(quarters_back: int = 2, days: int = 5) -> dict:
    """启动同步。

    `quarters_back` = 补最近几个季度（便宜，几秒一个季度）
    `days`          = 逐日补最近几个工作日（贵，约 90 秒/天）

    ⚠️ 默认 (2, 5) 而不是"全补"：当前缺口 117 天，全补要 3 小时以上。
    代价大的事必须让用户自己选，不能默默替他跑。
    """
    with STATE.lock:
        if STATE.running:
            # 已持锁，必须用 _snapshot_locked（调 snapshot() 会死锁）
            return {"started": False, "reason": "已有同步在进行中",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.done = STATE.total = STATE.rows = 0
        STATE.errors = []
        STATE.stage = "启动中"
        STATE.coverage = None
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    threading.Thread(target=_run,
                     args=(max(0, quarters_back), max(0, days)), daemon=True).start()
    return {"started": True, **STATE.snapshot()}
