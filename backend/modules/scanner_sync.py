"""扫描器的后台作业。

一轮全市场扫描 = 6,049 次轻量行情请求。在自律限流 4 次/秒下约 **26 分钟** ——
所以它必须是后台任务 + 进度上报，而不是一个会转圈 26 分钟的接口。

━━━ ⚠️ 三个刻意的选择 ━━━

1. **并发线程数 > 1，但共用同一个限流器。**
   限流器决定真实速率（4/s），线程只是用来盖住网络往返延迟。
   把限流器绕开去追速度，等于拿封 IP 换几分钟。

2. **单只失败不中断整轮**，但**逐条记下来**并汇总。
   一轮里若有 800 只取不到，结果表看上去只是"少了些票"；
   不留痕的话，"取不到"就伪装成了"这些票没有数据"。

3. **可以只扫一个子集**（`symbols` 参数）。
   多数人只关心自己盯的几十只 —— 那是几十秒的事，
   没必要为了看三只票的 IV Rank 等 26 分钟。
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
from typing import Optional

from sources import cboe
from modules import scanner_store as store

#: 并发线程数。真实速率由 `cboe._limiter`（4/s）决定，
#: 线程只是用来盖住网络往返延迟，不是用来提速的。
WORKERS = 8


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.total = 0
        self.done = 0
        self.stored = 0
        self.failed = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.dropped = 0
        self.cancel = False

    def _snapshot_locked(self) -> dict:
        """⚠️ 调用方须**已持锁** —— `threading.Lock` 不可重入，
        持锁时再调 `snapshot()` 会死锁（Congress 分栏踩过）。"""
        pct = (self.done / self.total * 100.0) if self.total else 0.0
        # 剩余时间按已完成的实测速率外推，**不用固定值猜**
        return {
            "running": self.running, "stage": self.stage,
            "total": self.total, "done": self.done, "percent": round(pct, 1),
            "stored": self.stored, "failed": self.failed,
            "dropped_no_session": self.dropped,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "errors": self.errors[-10:], "error_count": len(self.errors),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = ScanState()


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)
        STATE.failed += 1


def _run(symbols: Optional[list[str]]) -> None:
    batch_id = 0
    try:
        with STATE.lock:
            STATE.stage = "取标的全集"
        universe = symbols or cboe.option_roots()
        with STATE.lock:
            STATE.total = len(universe)
            STATE.stage = f"扫描 {len(universe)} 只"
        batch_id = store.start_batch(len(universe))

        results: list[dict] = []
        res_lock = threading.Lock()

        attempted = 0
        att_lock = threading.Lock()

        def one(sym: str) -> None:
            nonlocal attempted
            # ⚠️ 取消时**照样把 done 加上**，否则进度条永远停在半路 ——
            #    作业其实已经结束，界面却像是卡住了。
            with STATE.lock:
                cancelled = STATE.cancel
            if cancelled:
                with STATE.lock:
                    STATE.done += 1
                return
            with att_lock:
                attempted += 1
            try:
                q = cboe.quote(sym)
            except cboe.DataNotAvailable:
                # 这只票确实没有行情 —— 是「没有」，不是「取不到」，不算失败
                with STATE.lock:
                    STATE.done += 1
                return
            except ValueError as e:                 # 代码本身不合法
                _err(f"{sym}: {e}")
                with STATE.lock:
                    STATE.done += 1
                return
            except RuntimeError as e:               # 网络/限流/上游异常 —— 必须留痕
                _err(f"{sym}: {e}")
                with STATE.lock:
                    STATE.done += 1
                return
            except Exception as e:                  # noqa: BLE001
                # ⚠️ **兜住所有意外**。上游若返回一个我们没预料到的结构，
                #    这里抛出的 AttributeError/TypeError 会从 `ex.map()` 冒出去，
                #    整轮直接中断 —— 连**已经抓成功的几千只**都不会入库。
                #    一只票的意外不该毁掉整轮，记下来接着跑。
                _err(f"{sym}: 未预料的异常 {type(e).__name__}: {e}")
                with STATE.lock:
                    STATE.done += 1
                return
            with res_lock:
                results.append({
                    "symbol": q.symbol, "session": q.session, "price": q.price,
                    "change_pct": q.change_pct, "volume": q.volume,
                    "iv30": q.iv30, "security_type": q.security_type,
                })
            with STATE.lock:
                STATE.done += 1

        with cf.ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, universe))

        with STATE.lock:
            STATE.stage = "写入本地历史"
        rec = store.record_quotes(results)
        sess = max((r["session"] for r in results if r.get("session")), default=None)
        # ⚠️ 上游给了行情却没给交易时段的，会被丢弃 —— **必须算进失败**，
        #    否则界面显示"全部成功"，而那些标的其实没进库。
        dropped = rec["dropped_no_session"]
        with STATE.lock:
            STATE.stored = rec["stored"]
            STATE.dropped = dropped
            if dropped:
                STATE.errors.append(
                    f"{dropped} 只有行情但**上游没给交易时段**，已丢弃"
                    f"（归档键错了会污染 IV 样本）")
                STATE.failed += dropped
            STATE.stage = "已完成" if not STATE.cancel else "已取消"
            failed = STATE.failed
        note = []
        if STATE.cancel:
            note.append("用户取消")
        if dropped:
            note.append(f"{dropped} 只缺交易时段被丢弃")
        # ⚠️ `scanned` 记的是**真实尝试数**，不是 `len(results)` ——
        #    后者把"尝试过但取不到"的排除在外，看上去就像根本没扫过它们。
        store.finish_batch(batch_id, attempted, rec["stored"], failed, sess,
                           note="；".join(note))
    except Exception as e:                          # noqa: BLE001 —— 后台线程兜底
        _err(f"扫描中断: {type(e).__name__}: {e}")
        with STATE.lock:
            STATE.stage = "中断"
        if batch_id:
            store.finish_batch(batch_id, STATE.done, STATE.stored, STATE.failed,
                               None, note=f"中断: {type(e).__name__}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = _now()


def start(symbols: Optional[list[str]] = None) -> dict:
    """启动一轮扫描（已在跑则原样返回当前状态，不排队第二个）。"""
    with STATE.lock:
        if STATE.running:
            return {**STATE._snapshot_locked(), "started": False,
                    "note": "已有一轮扫描在跑，先等它结束或取消。"}
        STATE.running = True
        STATE.cancel = False
        STATE.stage = "启动中"
        STATE.total = len(symbols) if symbols else 0
        STATE.done = STATE.stored = STATE.failed = STATE.dropped = 0
        STATE.errors = []
        STATE.started_at = _now()
        STATE.finished_at = None
        snap = STATE._snapshot_locked()
    threading.Thread(target=_run, args=(symbols,), daemon=True).start()
    return {**snap, "started": True}


def cancel() -> dict:
    with STATE.lock:
        STATE.cancel = True
        STATE.stage = "取消中"
        return STATE._snapshot_locked()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
