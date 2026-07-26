"""13F 同步编排。

一个窗口 95MB 压缩 / INFOTABLE 380 万行，下载+解析约 35 秒，
按报告期过滤后单季约 332 万条。所以：
- 一次只导**一个报告期**（用户选），不默认全导
- 默认金额门槛 $100 万（保留 37.5% 行数、覆盖 99.37% 金额）
- 丢弃量如实记进批次表并显示
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from sources import edgar13f as src
from modules import institution as parse
from modules import institution_store as store

#: 默认金额门槛。实测保留 37.5% 行数、覆盖 99.37% 金额。
DEFAULT_MIN_VALUE = 1_000_000.0


class SyncState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.rows = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.errors: list[str] = []
        self.windows: list[str] = []
        self.periods: list[list] = []

    def _snapshot_locked(self) -> dict:
        """⚠️ 调用方须**已持锁**（`threading.Lock` 不可重入，
        在持锁时再调 snapshot() 会死锁 —— Congress 分栏踩过）。"""
        return {
            "running": self.running, "stage": self.stage, "rows": self.rows,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "errors": self.errors[-10:], "error_count": len(self.errors),
            "windows": self.windows, "periods": self.periods,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()


STATE = SyncState()


def _err(msg: str) -> None:
    with STATE.lock:
        STATE.errors.append(msg)


def _run(window: Optional[str], period: Optional[str], min_value: float) -> None:
    try:
        STATE.stage = "探测可用窗口"
        windows = src.list_windows()
        with STATE.lock:
            STATE.windows = windows
        w = window or windows[0]

        STATE.stage = f"下载数据集 {w}（约 95MB）"
        # ⚠️ 只拿 ZIP 句柄，**不全量解析** —— INFOTABLE 380 万行全读进内存
        # 实测峰值 5.3GB，8GB 机器会被 OOM 杀掉。
        zf = src.download_window(w)

        periods = {}
        for r in src.read_table(zf, "SUBMISSION"):
            p_ = (r.get("PERIODOFREPORT") or "").strip()
            if p_:
                periods[p_] = periods.get(p_, 0) + 1
        ordered = sorted(periods.items(), key=lambda x: -x[1])
        with STATE.lock:
            STATE.periods = [[p_, n] for p_, n in ordered[:8]]
        if not ordered:
            _err(f"{w} 窗口内没有任何申报")
            return

        target = period
        if not target:
            d0 = parse._parse_date(ordered[0][0])
            target = d0.isoformat() if d0 else None
        if not target:
            _err(f"{w} 无法确定报告期")
            return

        # 官方证券清单：CUSIP → 规范发行人名称。
        # 不同步的话，展示会用申报里自由填写的名字 ——
        # 实测苹果的 CUSIP 有 61 种写法，其中还有别家公司的名字。
        STATE.stage = "同步官方 13(f) 证券清单"
        d = parse._parse_date(target)
        for q in (src.list_quarters_for(d) if d else []):
            try:
                n = store.save_securities(src.securities_list(q), q)
                STATE.stage = f"证券清单 {q}：{n:,} 条"
                break
            except src.DataNotAvailable:
                continue                      # 该季清单还没发布，试下一季
            except Exception as e:
                _err(f"证券清单 {q}: {type(e).__name__}: {e}")
                break

        STATE.stage = f"流式导入 {target} 的持仓"
        # ⚠️ 先写**暂存区**，全部成功后再原子切换 ——
        # 直接"先删后写"的话，中途失败会留下空的或半份数据，
        # 而批次元数据还写着旧条数，之后所有查询都在悄悄给残缺数据。
        store.clear_staging(target)

        batch, total_kept, dropped_rows, dropped_value, parsed = [], 0, 0, 0.0, 0
        managers: set[str] = set()
        for h, dropped, val in parse.iter_holdings(zf, src, target, min_value):
            parsed += 1
            if dropped:
                dropped_rows += 1
                dropped_value += val
                continue
            batch.append(parse.to_dict(h))     # 落暂存表（独立主键空间）
            managers.add(h.manager_cik)
            if len(batch) >= 50_000:          # 分批落库，内存恒定
                total_kept += len(batch)
                store.save_staging(batch)
                batch = []
                with STATE.lock:
                    STATE.rows = total_kept
                STATE.stage = f"流式导入 {target}：已写入 {total_kept:,} 条"
        if batch:
            total_kept += len(batch)
            store.save_staging(batch)
        with STATE.lock:
            STATE.rows = total_kept

        if not total_kept and not dropped_rows:
            store.clear_staging(target)         # 该期压根没申报 → 别动旧数据
            _err(f"{w} 中没有 {target} 的持仓申报（该窗口主体可能是别的报告期）")
            return
        if not total_kept:
            # 有申报、但全被门槛滤掉 —— 这是**成功的空导入**，
            # 旧数据必须一并清掉，否则元数据说 0 条、查询却还返回旧持仓
            _err(f"{target}: {dropped_rows:,} 条全部低于门槛 ${min_value:,.0f}，"
                 f"该期已清空（如需保留请降低门槛重导）")

        # ⭐ 到这里才切换：旧数据在整个导入过程中一直是完好可用的
        STATE.stage = f"切换 {target}（{total_kept:,} 条）"
        store.commit_staging(target)

        store.mark_batch(
            period=target, window=w, rows=total_kept, parsed_rows=parsed,
            dropped_rows=dropped_rows, dropped_value=dropped_value,
            min_value=min_value, managers=len(managers))
        STATE.stage = "完成"
    except src.DataNotAvailable as e:
        _err(f"数据不可得：{e}")
        STATE.stage = "完成（无数据）"
    except Exception as e:
        STATE.stage = f"中断：{type(e).__name__}: {e}"
        _err(f"同步中断：{type(e).__name__}: {e}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def start(window: Optional[str] = None, period: Optional[str] = None,
          min_value: float = DEFAULT_MIN_VALUE) -> dict:
    """启动一次导入（一次一个报告期）。"""
    with STATE.lock:
        if STATE.running:
            # 已持锁 —— 必须用 _snapshot_locked，调 snapshot() 会死锁
            return {"started": False, "reason": "已有同步在进行中",
                    **STATE._snapshot_locked()}
        STATE.running = True
        STATE.rows = 0
        STATE.errors = []
        STATE.stage = "启动中"
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None

    threading.Thread(target=_run, args=(window, period, min_value),
                     daemon=True).start()
    return {"started": True, **STATE.snapshot()}
