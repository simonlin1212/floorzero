import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/congress.py 对齐）── */
type Trade = {
  chamber: string;
  member: string;
  state_district: string | null;
  ticker: string | null;
  asset_name: string;
  asset_type_label: string;
  tx_type: string;
  tx_type_label: string;
  tx_date: string | null;
  filing_date: string | null;
  amount_low: number | null;
  amount_high: number | null;
  amount_raw: string;
  owner: string;
  delay_days: number | null;
  date_anomaly: string | null;
  source_url: string;
};
type Stats = {
  trades: number;
  tickers: number;
  members: number;
  filings: number;
  unparsed_filings: number;
  unparsed_pct: number;
  earliest_trade: string | null;
  latest_trade: string | null;
  last_sync: string | null;
  db_path: string;
  note: string;
};
type Summary = {
  total_trades: number;
  buys: number;
  sells: number;
  by_ticker: {
    ticker: string;
    asset_name: string;
    trades: number;
    buys: number;
    sells: number;
    est_amount: number;
    member_count: number;
    members: string[];
  }[];
  by_member: {
    member: string;
    chamber: string;
    state_district: string;
    trades: number;
    buys: number;
    sells: number;
    est_amount: number;
    ticker_count: number;
  }[];
  delay: {
    median_days: number | null;
    max_days: number | null;
    over_45d_count: number;
    buckets: { label: string; count: number }[];
    anomaly_count: number;
    anomaly_note: string;
    note: string;
  };
  amount_note: string;
  scope: { truncated: boolean; sampled: number; limit: number };
  stats: Stats;
};
type SyncState = {
  running: boolean;
  done: number;
  total: number;
  stage: string;
  errors: string[];
  error_count: number;
  senate_status: string | null;
  stats: Stats;
};
type Unparsed = {
  chamber: string;
  member: string;
  state_district: string | null;
  filing_date: string | null;
  source_url: string;
  unparsed_reason: string;
};

const CHAMBERS = [
  { v: null as string | null, label: "两院" },
  { v: "house", label: "众议院" },
  { v: "senate", label: "参议院" },
];
const SIDES = [
  { v: null as string | null, label: "全部" },
  { v: "buy", label: "买入" },
  { v: "sell", label: "卖出" },
];
const RANGES = [
  { d: 30, label: "近 30 天" },
  { d: 90, label: "近 90 天" },
  { d: 365, label: "近 1 年" },
  { d: 0, label: "全部" },
];

function daysAgo(n: number): string | undefined {
  if (!n) return undefined;
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function money(n: number): string {
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n}`;
}

export default function Congress() {
  const [chamber, setChamber] = useState<string | null>(null);
  const [side, setSide] = useState<string | null>(null);
  const [range, setRange] = useState(90);
  const [ticker, setTicker] = useState("");
  const [tickerInput, setTickerInput] = useState("");

  const [trades, setTrades] = useState<Trade[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [unparsed, setUnparsed] = useState<Unparsed[]>([]);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [yearsBack, setYearsBack] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  // ⚠️ 请求序号：快速切筛选时会有多个 load() 并发，
  // 慢的那个后返回就会把**旧筛选的结果**盖在新筛选上（控件显示 NVDA、表里却是全市场）。
  // 只认最后一次发起的请求。
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    setErr(null);
    const since = daysAgo(range);
    // ⚠️ 明细与汇总**共用同一份查询串**。曾经汇总只带 chamber/since，
    // 结果按标的筛选时明细只剩 NVDA、上方统计卡与图表却还是全市场 ——
    // 同一屏里两个视图互相打架。别再拆成两份各写各的。
    const q = new URLSearchParams();
    if (chamber) q.set("chamber", chamber);
    if (side) q.set("tx_type", side);
    if (since) q.set("since", since);
    if (ticker) q.set("ticker", ticker);
    try {
      // ⚠️ 用 allSettled：任一辅助视图挂掉不该清空已取到的主数据
      const [t, s, u] = await Promise.allSettled([
        fetch(`/api/congress/trades?${q}&limit=300`),
        fetch(`/api/congress/summary?${q}`),
        fetch(`/api/congress/unparsed?limit=60`),
      ]);
      if (t.status !== "fulfilled" || !t.value.ok) {
        throw new Error(
          t.status === "fulfilled"
            ? ((await t.value.json()).detail ?? `HTTP ${t.value.status}`)
            : String(t.reason),
        );
      }
      const nextTrades = ((await t.value.json()) as { trades: Trade[] }).trades;
      const nextSummary =
        s.status === "fulfilled" && s.value.ok ? ((await s.value.json()) as Summary) : null;
      const nextUnparsed =
        u.status === "fulfilled" && u.value.ok
          ? ((await u.value.json()) as { filings: Unparsed[] }).filings
          : [];
      if (seq !== reqRef.current) return;              // 已被更新的筛选取代，丢弃
      setTrades(nextTrades);
      setSummary(nextSummary);
      setUnparsed(nextUnparsed);
    } catch (e) {
      if (seq !== reqRef.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      // ⚠️ 失败时必须清掉旧结果：否则「全市场」的表格和图会顶着
      // 「NVDA」的筛选标签继续显示，用户看到的范围与标注的完全不符。
      setTrades([]);
      setSummary(null);
      setUnparsed([]);
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [chamber, side, range, ticker]);

  useEffect(() => {
    void load();
  }, [load]);

  /* 同步进度轮询：跑完自动重载数据 */
  const pollSync = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const r = await fetch("/api/congress/sync");
        if (!r.ok) return;
        const s = (await r.json()) as SyncState;
        setSync(s);
        if (!s.running) {
          window.clearInterval(pollRef.current!);
          pollRef.current = null;
          void load();
        }
      } catch {
        /* 轮询失败不打断页面；下一轮再试 */
      }
    }, 2500);
  }, [load]);

  useEffect(() => {
    // 进页面先看一眼有没有同步在跑（可能是别的标签页发起的）
    fetch("/api/congress/sync")
      .then((r) => (r.ok ? r.json() : null))
      .then((s: SyncState | null) => {
        if (!s) return;
        setSync(s);
        if (s.running) pollSync();
      })
      .catch(() => {});
    return () => {
      // ⚠️ 清掉定时器后必须把 ref 也置空：
      // 同步进行中改一次筛选就会让 load/pollSync 变身份 → 触发本清理，
      // 而 ref 仍非空会让新的 pollSync() 直接 return —— 进度条从此不动、
      // 跑完也不会自动刷新数据，除非重开页面。
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [pollSync]);

  async function startSync() {
    try {
      const r = await fetch(`/api/congress/sync?years_back=${yearsBack}`, { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setSync((await r.json()) as SyncState);
      pollSync();
    } catch (e) {
      setErr(`同步启动失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── 最活跃标的 ── */
  const tickerOption = useMemo(() => {
    // 后端 by_ticker 已按笔数排序（与"最活跃"和图形一致），这里不再重排 ——
    // 两处各排一次早晚会漂移。
    const rows = (summary?.by_ticker ?? []).slice(0, 14).reverse();
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 60, top: 26, bottom: 30 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(r.ticker)}</b> ${esc(r.asset_name.slice(0, 34))}<br/>` +
            `买入 ${r.buys} 笔 · 卖出 ${r.sells} 笔<br/>` +
            `${r.member_count} 位议员 · 估算 ${money(r.est_amount)}`
          );
        },
      },
      legend: {
        data: ["买入", "卖出"],
        textStyle: { color: "#8e8a83", fontSize: 11 },
        top: 0,
        right: 4,
        itemWidth: 12,
        itemHeight: 8,
      },
      xAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.ticker),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#f2efe9", fontSize: 11, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          name: "买入",
          type: "bar",
          stack: "x",
          itemStyle: { color: "#22c55e" },
          data: rows.map((r) => r.buys),
        },
        {
          name: "卖出",
          type: "bar",
          stack: "x",
          itemStyle: { color: "#ef4444" },
          data: rows.map((r) => r.sells),
        },
      ],
    };
  }, [summary]);

  /* ── 披露延迟分布 ── */
  const delayOption = useMemo(() => {
    // ⭐ 直接用后端 summary 里的分桶：与上方"延迟中位数/超45天"卡片**同一个样本**。
    // 曾经在这里拿明细表的 300 行自己分桶，而卡片用的是汇总的 2000 行 ——
    // 同一屏两个控件报出不同分布。分桶不该在前端重算。
    // （日期异常的记录已在后端剔除，明细表里仍如实列出并标注。）
    const buckets = summary?.delay.buckets ?? [];
    const counts = buckets.map((b) => b.count);
    if (counts.reduce((a, b) => a + b, 0) < 3) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
      },
      xAxis: {
        type: "category",
        data: buckets.map((b) => b.label),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 11 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          type: "bar",
          data: counts.map((c, i) => ({
            value: c,
            // 只有最后一档（>45天）用警示色 —— 但那是"超期"的事实，不是违规认定
            itemStyle: { color: i === 4 ? "#ff5a1f" : "#3b82f6" },
          })),
          barMaxWidth: 54,
        },
      ],
    };
  }, [summary]);

  const st = summary?.stats ?? sync?.stats;
  // ⚠️ 「本地没数据」与「当前筛选无结果」是两回事：
  // 前者要引导去同步，后者只需说"这个筛选没命中"。
  // 用全局缓存量（st.trades）判断，不能只看当前结果为空。
  const cacheEmpty = !loading && (st?.trades ?? 0) === 0;
  const filterEmpty = !loading && !cacheEmpty && trades.length === 0;
  const empty = cacheEmpty || filterEmpty;

  return (
    <>
      <PageHead kicker="Congress Trading · STOCK Act" title="国会议员交易">
        美国国会议员依 STOCK Act 必须在交易后一定期限内公开申报。数据直取
        <b className="text-ink"> 众议院书记官办公室 </b>与
        <b className="text-ink"> 参议院 eFD </b>官方源 —— 公众可自由获取，
        但<b className="text-ink">法律明文禁止商业用途</b>（见下方口径说明）。
      </PageHead>

      {/* 同步条：首次使用必须先灌数据，这一点要直说 */}
      <Card
        title="本地数据"
        sub={
          st
            ? `${st.trades} 笔交易 · ${st.tickers} 个标的 · ${st.members} 位议员 · ` +
              `申报 ${st.filings} 份${st.last_sync ? ` · 上次同步 ${st.last_sync.replace("T", " ")}` : ""}`
            : "尚未同步"
        }
        right={
          <div className="flex shrink-0 items-center gap-2">
            <select
              value={yearsBack}
              onChange={(e) => setYearsBack(Number(e.target.value))}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono
                         text-xs text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              <option value={0}>仅当年</option>
              <option value={1}>回补 1 年</option>
              <option value={3}>回补 3 年</option>
              <option value={5}>回补 5 年</option>
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5
                         font-mono text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "同步中…" : "同步申报"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-3">
            <div className="mb-1.5 flex justify-between font-mono text-[11px] text-dim">
              <span>{sync.stage}</span>
              <span>
                {sync.done}/{sync.total || "?"}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-card2">
              <div
                className="h-full bg-brand transition-all"
                style={{ width: `${sync.total ? (sync.done / sync.total) * 100 : 0}%` }}
              />
            </div>
            <div className="mt-2 text-[11px] text-dim">
              首次同步要逐份下载申报原件（众议院单年 300+ 份 PDF），约需 2-4 分钟；
              之后只补新增。回补更多年份会成倍增加耗时。
            </div>
          </div>
        )}

        {/* ⚠️ 参议院不可用 = 环境问题，绝不能显示成"参议院没有交易" */}
        {sync?.senate_status && sync.senate_status !== "可用" && (
          <div className="mb-3 rounded-lg border border-brand/30 bg-brand/5 px-3.5 py-2.5 text-xs leading-relaxed">
            <b className="text-brand">参议院数据源当前不可用</b>
            <div className="mt-1 text-dim">{sync.senate_status}</div>
            <div className="mt-1 text-dim">
              这是<b className="text-ink">取不到数据</b>，不是参议员没有交易 ——
              下面的结果只覆盖众议院。
            </div>
          </div>
        )}

        {sync && sync.error_count > 0 && (
          <details className="text-xs text-dim">
            <summary className="cursor-pointer">同步中有 {sync.error_count} 条错误</summary>
            <ul className="mt-1.5 space-y-0.5 font-mono text-[10px]">
              {sync.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </details>
        )}

        {st && st.unparsed_filings > 0 && (
          <div className="text-[11px] leading-relaxed text-dim">
            ⚠️ 另有 <b className="text-ink">{st.unparsed_filings}</b> 份申报（
            {st.unparsed_pct}%）无法自动解析，
            <b className="text-ink">未计入上面的交易数</b> —— 绝大多数是纸质扫描件（整份为图片）。
            清单见页面底部。
          </div>
        )}
      </Card>

      {/* 筛选 */}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        {CHAMBERS.map((c) => (
          <button
            key={c.label}
            onClick={() => setChamber(c.v)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              chamber === c.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {c.label}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-line" />
        {SIDES.map((s) => (
          <button
            key={s.label}
            onClick={() => setSide(s.v)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              side === s.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {s.label}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-line" />
        {RANGES.map((r) => (
          <button
            key={r.label}
            onClick={() => setRange(r.d)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              range === r.d
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {r.label}
          </button>
        ))}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setTicker(tickerInput.trim().toUpperCase());
          }}
          className="ml-auto flex gap-2"
        >
          <input
            value={tickerInput}
            onChange={(e) => setTickerInput(e.target.value)}
            placeholder="按标的筛选"
            className="w-36 rounded-lg border border-line bg-card px-3 py-1.5 font-mono
                       text-xs uppercase outline-none focus:border-brand/50"
          />
          {ticker && (
            <button
              type="button"
              onClick={() => {
                setTicker("");
                setTickerInput("");
              }}
              className="rounded-lg border border-line bg-card px-3 py-1.5 font-mono text-xs text-dim"
            >
              清除
            </button>
          )}
        </form>
      </div>

      {err && (
        <div className="mb-5 rounded-xl border border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-400">
          {err}
        </div>
      )}

      {filterEmpty && !err && (
        <Card title="当前筛选没有命中任何交易" sub={`本地共缓存 ${st?.trades ?? 0} 笔 —— 数据是有的，只是这个条件下没有`}>
          <div className="text-sm leading-relaxed text-dim">
            试试放宽时间范围、换个院别，或清除标的筛选。
          </div>
        </Card>
      )}

      {cacheEmpty && !err && (
        <Card title="本地还没有数据" sub="国会披露是全量历史，同步一次就能补齐">
          <div className="text-sm leading-relaxed text-dim">
            点右上角「同步申报」开始灌数据。与期权链不同，
            <b className="text-ink">国会披露的历史随时可以回补</b> ——
            不存在"装晚了就永远缺一段"的问题。
            <br />
            不过众议院的归档是<b className="text-ink">按年分卷</b>的：
            默认<b className="text-ink">只同步当年</b>，要更早的年份请在下拉里选回补几年
            （每多一年就多几百份 PDF，耗时成倍增加，所以交给你自己定）。
          </div>
        </Card>
      )}

      {!empty && (
        <>
          {summary && (
            <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="交易笔数" value={String(summary.total_trades)} />
              <Stat
                label="买 / 卖"
                value={`${summary.buys} / ${summary.sells}`}
                tone={summary.buys > summary.sells ? "up" : "down"}
              />
              <Stat
                label="披露延迟中位数"
                value={summary.delay.median_days != null ? `${summary.delay.median_days} 天` : "—"}
              />
              <Stat
                label="超 45 天"
                value={`${summary.delay.over_45d_count} 笔`}
                tone={summary.delay.over_45d_count > 0 ? "warn" : undefined}
              />
            </div>
          )}

          <Card
            title="最活跃标的"
            sub={
              summary
                ? `按交易笔数排序 · 绿=买入 红=卖出 · 共 ${summary.by_ticker.length} 个标的`
                : ""
            }
          >
            {summary?.by_ticker.length ? (
              <ReactECharts option={tickerOption} style={{ height: 380 }} notMerge />
            ) : (
              <div className="py-8 text-center text-sm text-dim">该筛选下没有带代码的交易</div>
            )}
          </Card>

          <Card
            title="披露延迟分布"
            sub={`交易日 → 归档日 的天数（与上方统计卡同一样本，共 ${
      summary?.delay.buckets.reduce((a, b) => a + b.count, 0) ?? 0
    } 笔）`}
          >
            {Object.keys(delayOption).length ? (
              <ReactECharts option={delayOption} style={{ height: 240 }} notMerge />
            ) : (
              <div className="py-6 text-center text-sm text-dim">样本不足</div>
            )}
            <div className="mt-2 space-y-1 text-[11px] leading-relaxed text-dim">
              <div>⚠️ {summary?.delay.note ?? ""}</div>
              {!!summary?.delay.anomaly_count && (
                <div>
                  ⚠️ 有 <b className="text-ink">{summary.delay.anomaly_count}</b> 笔申报的
                  归档日早于交易日（原件填报有误）。已从上面的延迟统计中剔除，
                  但在明细里如实列出并标注。
                </div>
              )}
            </div>
          </Card>

          <Card
            title="交易明细"
            sub={`最新 ${trades.length} 笔${summary?.scope.truncated ? "（已达返回上限，非全量）" : ""}`}
          >
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[880px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>交易日</Th>
                    <Th>议员</Th>
                    <Th>院/选区</Th>
                    <Th>标的</Th>
                    <Th>方向</Th>
                    <Th>金额区间</Th>
                    <Th>持有人</Th>
                    <Th>延迟</Th>
                    <Th>原件</Th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {trades.map((t, i) => (
                    <tr key={i} className="border-b border-line/50 hover:bg-card2/60">
                      <Td>{t.tx_date ?? "—"}</Td>
                      <Td className="font-sans text-ink">{t.member}</Td>
                      <Td>
                        {t.chamber === "house" ? "众" : "参"}
                        {t.state_district ? ` ${t.state_district.slice(0, 12)}` : ""}
                      </Td>
                      <Td>
                        {t.ticker ? (
                          <span className="font-bold text-ink">{t.ticker}</span>
                        ) : (
                          <span className="text-dim" title={t.asset_name}>
                            —{" "}
                            <span className="font-sans">
                              {t.asset_type_label || t.asset_name.slice(0, 18)}
                            </span>
                          </span>
                        )}
                      </Td>
                      <Td>
                        <span
                          className={
                            t.tx_type === "P"
                              ? "text-green-400"
                              : t.tx_type.startsWith("S")
                                ? "text-red-400"
                                : "text-dim"
                          }
                        >
                          {t.tx_type_label}
                        </span>
                      </Td>
                      <Td>{t.amount_raw || "—"}</Td>
                      <Td>{t.owner === "self" ? "本人" : t.owner}</Td>
                      <Td
                        className={
                          t.date_anomaly
                            ? "text-yellow-500"
                            : t.delay_days != null && t.delay_days > 45
                              ? "text-brand"
                              : ""
                        }
                        title={t.date_anomaly ?? undefined}
                      >
                        {t.date_anomaly
                          ? "⚠ 日期存疑"
                          : t.delay_days != null
                            ? `${t.delay_days}天`
                            : "—"}
                      </Td>
                      <Td>
                        <a
                          href={t.source_url}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="text-dim underline decoration-dotted hover:text-brand"
                        >
                          查看
                        </a>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="交易最多的议员" sub="按估算金额排序">
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>议员</Th>
                    <Th>院/选区</Th>
                    <Th>笔数</Th>
                    <Th>买/卖</Th>
                    <Th>标的数</Th>
                    <Th>估算金额</Th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {(summary?.by_member ?? []).slice(0, 18).map((m) => (
                    <tr key={m.member} className="border-b border-line/50 hover:bg-card2/60">
                      <Td className="font-sans text-ink">{m.member}</Td>
                      <Td>
                        {m.chamber === "house" ? "众" : "参"}
                        {m.state_district ? ` ${m.state_district.slice(0, 12)}` : ""}
                      </Td>
                      <Td>{m.trades}</Td>
                      <Td>
                        <span className="text-green-400">{m.buys}</span>
                        <span className="text-dim"> / </span>
                        <span className="text-red-400">{m.sells}</span>
                      </Td>
                      <Td>{m.ticker_count}</Td>
                      <Td>{money(m.est_amount)}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-2 text-[11px] leading-relaxed text-dim">
              ⚠️ {summary?.amount_note ?? ""}
            </div>
          </Card>

          {unparsed.length > 0 && (
            <Card
              title={`读不了的申报（${unparsed.length} 份）`}
              sub="它们确实存在，只是明细无法自动解析 —— 列出来是为了不让你以为上面就是全部"
            >
              <div className="max-h-64 overflow-y-auto">
                <table className="w-full text-left text-xs">
                  <tbody className="font-mono">
                    {unparsed.map((u, i) => (
                      <tr key={i} className="border-b border-line/50">
                        <Td>{u.filing_date ?? "—"}</Td>
                        <Td className="font-sans text-ink">{u.member}</Td>
                        <Td>{u.chamber === "house" ? "众" : "参"}</Td>
                        <Td className="font-sans text-dim">{u.unparsed_reason.slice(0, 30)}</Td>
                        <Td>
                          <a
                            href={u.source_url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="text-dim underline decoration-dotted hover:text-brand"
                          >
                            原件
                          </a>
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
        </>
      )}

      {/* 口径与免责 —— 与 GEX 分栏同一套做法：把假设摊开，不藏 */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          口径与边界
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          <li>
            · <b className="text-ink">金额是区间不是精确值</b>：STOCK Act 只要求按档披露
            （如 $1,001-$15,000）。所有"估算金额"都是区间中值加总，
            <b className="text-ink">只能横向比较，不是真实成交额</b>。
          </li>
          <li>
            · <b className="text-ink">「超 45 天」是事实不是违规认定</b>：法定期限为
            「知悉后 30 天内、且不晚于交易后 45 天」，周末/假日顺延，
            另有修订件与经纪商延迟通知等情形。
          </li>
          <li>
            · <b className="text-ink">覆盖并不完整</b>：约一成申报是纸质扫描件，
            无 OCR 读不出明细；这些申报已单独列出，但不在统计口径内。
          </li>
          <li>
            · <b className="text-ink">披露天然滞后</b>：看到的是几十天前发生的交易，
            本页只呈现已公开的申报数据，不构成任何投资建议。
          </li>
          <li>
            · <b className="text-brand">⚠️ 使用限制</b>：依
            <span className="font-mono"> 5 U.S.C. §13107(c)(1)(B) </span>
            （Ethics in Government Act），
            <b className="text-ink">为任何商业目的获取或使用这些申报报告均属违法</b>
            （新闻与传播媒体面向公众传播除外）；§13107(c)(2) 规定司法部长可提起民事诉讼、
            罚款上限 $10,000。该限制<b className="text-ink">两院均适用</b>。
            <br />
            所以：公众查阅、个人研究、学术与新闻用途 ✅；
            <b className="text-ink">把本页数据用于任何收费产品或商业服务 ❌</b>。
            Vibe-Flow 免费开源、由你自己部署运行，用于个人研究落在允许范围内。
            <br />
            <span className="text-dim">
              注：这与 SEC EDGAR 不同 —— EDGAR 只限制请求速率、不限制商用，
              两条线的合规级别不可混用。
            </span>
          </li>
          <li>
            · 数据源：众议院书记官办公室 · 参议院 eFD（美国政府公开记录）。
          </li>
        </ul>
      </div>
    </>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "up" | "down" | "warn";
}) {
  const color =
    tone === "up"
      ? "text-green-400"
      : tone === "down"
        ? "text-red-400"
        : tone === "warn"
          ? "text-brand"
          : "text-ink";
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-bold ${color}`}>{value}</div>
    </div>
  );
}


