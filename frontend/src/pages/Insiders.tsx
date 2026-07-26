import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/insider.py 对齐）── */
type Trade = {
  ticker: string | null;
  company: string;
  owner: string;
  officer_title: string;
  is_officer: boolean;
  is_director: boolean;
  is_ten_pct: boolean;
  tx_code: string;
  tx_code_label: string;
  group: string;
  is_open_market: boolean;
  direction: "buy" | "sell" | null;
  tx_date: string | null;
  filing_date: string | null;
  shares: number | null;
  price: number | null;
  value: number | null;
  is_10b5_1: boolean | null;
  price_implausible: boolean;
  date_anomaly: string | null;
  delay_days: number | null;
  source_url: string;
};
type Stats = {
  trades: number;
  tickers: number;
  owners: number;
  earliest: string | null;
  latest: string | null;
  by_group: Record<string, number>;
  open_market_pct: number;
  quarters: string[];
  days: string[];
  last_sync: string | null;
  note: string;
};
type TickerRow = {
  ticker: string;
  company: string;
  buys: number;
  sells: number;
  buy_value: number;
  sell_value: number;
  net_value: number;
  insider_count: number;
  insiders: string[];
};
type Summary = {
  total_rows: number;
  open_market: {
    count: number;
    buys: number;
    sells: number;
    buy_value: number;
    sell_value: number;
  };
  compensation_count: number;
  other_count: number;
  open_market_pct: number;
  by_ticker: TickerRow[];
  cluster_buys: TickerRow[];
  by_owner: {
    owner: string;
    ticker: string | null;
    title: string;
    buys: number;
    sells: number;
    buy_value: number;
    sell_value: number;
  }[];
  plan_sells: number;
  implausible_price: number;
  date_anomaly_count: number;
  delay: { median_days: number | null; over_2d: number; note: string };
  notes: { forms: string; classification: string; plan: string; price: string; disclaimer: string };
  scope: { truncated: boolean; sampled: number };
  stats: Stats;
};
type SyncState = {
  running: boolean;
  done: number;
  total: number;
  rows: number;
  stage: string;
  errors: string[];
  error_count: number;
  coverage: string | null;
  stats: Stats;
};

const GROUPS = [
  { v: "open_market", label: "公开市场", hint: "代码 P/S — 唯一含主动买卖意图的部分" },
  { v: "compensation", label: "薪酬类", hint: "授予/行权/代扣税，不代表买卖决策" },
  { v: "all", label: "全部", hint: "含薪酬类，会淹没真实买卖信号" },
];
const SIDES = [
  { v: null as string | null, label: "全部" },
  { v: "buy", label: "买入" },
  { v: "sell", label: "卖出" },
];
const ROLES = [
  { v: null as string | null, label: "不限身份" },
  { v: "officer", label: "高管" },
  { v: "director", label: "董事" },
  { v: "ten_pct", label: "10%股东" },
];
const PLANS = [
  { v: null as string | null, label: "不限计划" },
  { v: "yes", label: "10b5-1 计划内" },
  { v: "no", label: "非计划内" },
  // 三态分开：NULL 是「申报未标注」（2023 年前无此字段），不是「确认非计划内」
  { v: "unknown", label: "未标注" },
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
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
  return `${s}$${a.toFixed(0)}`;
}

export default function Insiders() {
  const [group, setGroup] = useState("open_market");
  const [side, setSide] = useState<string | null>(null);
  const [role, setRole] = useState<string | null>(null);
  const [plan, setPlan] = useState<string | null>(null);
  const [range, setRange] = useState(90);
  const [ticker, setTicker] = useState("");
  const [tickerInput, setTickerInput] = useState("");

  const [trades, setTrades] = useState<Trade[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [quarters, setQuarters] = useState(2);
  const [days, setDays] = useState(5);
  const pollRef = useRef<number | null>(null);
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    setErr(null);
    // ⚠️ 明细与汇总**共用同一份查询串** —— 拆成两份迟早会画岔口径
    const q = new URLSearchParams({ group });
    if (side) q.set("direction", side);
    if (role) q.set("role", role);
    if (plan) q.set("plan", plan);
    const since = daysAgo(range);
    if (since) q.set("since", since);
    if (ticker) q.set("ticker", ticker);
    try {
      const [t, s] = await Promise.allSettled([
        fetch(`/api/insider/trades?${q}&limit=300`),
        fetch(`/api/insider/summary?${q}`),
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
      if (seq !== reqRef.current) return; // 已被更新的筛选取代
      setTrades(nextTrades);
      setSummary(nextSummary);
    } catch (e) {
      if (seq !== reqRef.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      // ⚠️ 失败时必须清掉旧结果：否则「全市场」的表格和图会顶着
      // 「NVDA」的筛选标签继续显示，用户看到的范围与标注的完全不符。
      setTrades([]);
      setSummary(null);
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [group, side, role, plan, range, ticker]);

  useEffect(() => {
    void load();
  }, [load]);

  const pollSync = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const r = await fetch("/api/insider/sync");
        if (!r.ok) return;
        const s = (await r.json()) as SyncState;
        setSync(s);
        if (!s.running) {
          window.clearInterval(pollRef.current!);
          pollRef.current = null;
          void load();
        }
      } catch {
        /* 轮询失败不打断页面 */
      }
    }, 3000);
  }, [load]);

  useEffect(() => {
    fetch("/api/insider/sync")
      .then((r) => (r.ok ? r.json() : null))
      .then((s: SyncState | null) => {
        if (!s) return;
        setSync(s);
        if (s.running) pollSync();
      })
      .catch(() => {});
    return () => {
      // 清定时器后必须把 ref 置空，否则新的 pollSync() 会直接 return、进度从此不动
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [pollSync]);

  async function startSync() {
    try {
      const r = await fetch(
        `/api/insider/sync?quarters_back=${quarters}&days=${days}`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setSync((await r.json()) as SyncState);
      pollSync();
    } catch (e) {
      setErr(`同步启动失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── 集群买入：多少个不同内部人在买同一只 ── */
  const clusterOption = useMemo(() => {
    const rows = (summary?.cluster_buys ?? []).slice(0, 14).reverse();
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 70, right: 70, top: 22, bottom: 30 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(r.ticker)}</b> ${esc(r.company.slice(0, 30))}<br/>` +
            `<b>${r.insider_count}</b> 位内部人买入 · 共 ${r.buys} 笔<br/>` +
            `买入 ${money(r.buy_value)}　卖出 ${money(r.sell_value)}<br/>` +
            `<span style="color:#8e8a83">${esc(r.insiders.slice(0, 4).join("、"))}</span>`
          );
        },
      },
      xAxis: {
        type: "value",
        name: "买入人数",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" },
        minInterval: 1,
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.ticker),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#f2efe9", fontSize: 11, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          type: "bar",
          itemStyle: { color: "#22c55e" },
          data: rows.map((r) => r.insider_count),
          label: {
            show: true,
            position: "right",
            color: "#8e8a83",
            fontSize: 10,
            fontFamily: "JetBrains Mono",
            formatter: (p: any) => money(rows[p.dataIndex].buy_value),
          },
        },
      ],
    };
  }, [summary]);

  /* ── 净买卖金额 ── */
  const netOption = useMemo(() => {
    const rows = (summary?.by_ticker ?? []).slice(0, 14).reverse();
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 70, right: 30, top: 22, bottom: 34 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(r.ticker)}</b><br/>买入 ${money(r.buy_value)}（${r.buys} 笔）<br/>` +
            `卖出 ${money(r.sell_value)}（${r.sells} 笔）<br/>净额 ${money(r.net_value)}`
          );
        },
      },
      xAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: {
          color: "#8e8a83",
          fontSize: 10,
          fontFamily: "JetBrains Mono",
          formatter: (v: number) => money(v),
        },
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.ticker),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#f2efe9", fontSize: 11, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          type: "bar",
          data: rows.map((r) => ({
            value: r.net_value,
            itemStyle: { color: r.net_value >= 0 ? "#22c55e" : "#ef4444" },
          })),
        },
      ],
    };
  }, [summary]);

  const st = summary?.stats ?? sync?.stats;
  const cacheEmpty = !loading && (st?.trades ?? 0) === 0;
  const filterEmpty = !loading && !cacheEmpty && trades.length === 0;

  return (
    <>
      <PageHead kicker="Insider Trading · SEC Form 4" title="内部人交易">
        上市公司高管、董事与 10% 以上股东买卖自家股票，须依 Section 16(a) 在
        <b className="text-ink"> 两个工作日内 </b>向 SEC 申报 Form 4。数据直取
        <b className="text-ink"> SEC EDGAR </b>官方源 —— 美国政府公开记录，
        <b className="text-ink">允许自由再分发</b>。
      </PageHead>

      {/* ⭐ 这个分栏最该讲清楚的一件事 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/[0.06] p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          先看懂这一点，否则数据会读反
        </div>
        <p className="text-sm leading-relaxed text-dim">
          <b className="text-ink">Form 4 里绝大多数记录不是「内部人看好所以买入」。</b>
          实测 2026Q1 全市场 103,733 笔非衍生品交易中，代扣税 27,019 / 授予 24,690 /
          卖出 22,822 / 期权行权 16,300，而
          <b className="text-ink"> 真正的公开市场买入只有 5,935 笔（5.6%）</b>。
          若按 SEC 的「取得/处置」标志笼统统计，「取得」有 48,849 笔 ——
          <b className="text-ink">是真实买入的 8 倍</b>。
          <br />
          <span className="mt-1.5 inline-block">
            典型形态：同日「期权行权 + 立即卖出」。行权那笔被标为「取得」，
            但内部人<b className="text-ink">在公开市场一股没买、拿到手全卖了</b> ——
            是薪酬变现，不是看多。所以本页默认<b className="text-ink">只看公开市场（P/S）</b>。
          </span>
        </p>
      </div>

      {/* 同步 */}
      <Card
        title="本地数据"
        sub={
          st
            ? `${st.trades.toLocaleString()} 笔 · ${st.tickers.toLocaleString()} 标的 · ` +
              `${st.owners.toLocaleString()} 位内部人 · 公开市场占 ${st.open_market_pct}%` +
              (st.last_sync ? ` · 上次同步 ${st.last_sync.replace("T", " ")}` : "")
            : "尚未同步"
        }
        right={
          <div className="flex shrink-0 items-center gap-2">
            <select
              value={quarters}
              onChange={(e) => setQuarters(Number(e.target.value))}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              {[0, 1, 2, 4, 8].map((n) => (
                <option key={n} value={n}>
                  {n === 0 ? "不补季度" : `补 ${n} 季度`}
                </option>
              ))}
            </select>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              {[0, 1, 5, 10, 20].map((n) => (
                <option key={n} value={n}>
                  {n === 0 ? "不补日" : `补 ${n} 天`}
                </option>
              ))}
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                         text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "同步中…" : "同步"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-3">
            <div className="mb-1.5 flex justify-between font-mono text-[11px] text-dim">
              <span>{sync.stage}</span>
              <span>
                {sync.done}/{sync.total || "?"} · 已入库 {sync.rows.toLocaleString()} 笔
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-card2">
              <div
                className="h-full bg-brand transition-all"
                style={{ width: `${sync.total ? (sync.done / sync.total) * 100 : 0}%` }}
              />
            </div>
          </div>
        )}

        {/* ⚠️ 覆盖边界必须说清：季度数据集滞后，近期只能靠逐日抓 */}
        {(sync?.coverage || st) && (
          <div className="space-y-1 text-[11px] leading-relaxed text-dim">
            {sync?.coverage && <div>ℹ️ {sync.coverage}</div>}
            <div>
              两条来源成本差两个数量级：
              <b className="text-ink">季度数据集</b>约 3 秒拿一整季（~10 万笔），
              <b className="text-ink">逐日抓取</b>约 90 秒/天（单日 600-700 份申报，
              每份都要单独请求）。所以季度用来补历史、逐日只补最近几天。
            </div>
            {st && st.quarters.length > 0 && (
              <div>
                已导入季度：{st.quarters.join("、")}
                {st.days.length > 0 && ` · 已抓取 ${st.days.length} 个交易日`}
                {st.earliest && ` · 覆盖 ${st.earliest} ~ ${st.latest}`}
              </div>
            )}
          </div>
        )}

        {sync && sync.error_count > 0 && (
          <details className="mt-2 text-xs text-dim">
            <summary className="cursor-pointer">同步中有 {sync.error_count} 条提示</summary>
            <ul className="mt-1.5 space-y-0.5 font-mono text-[10px]">
              {sync.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </details>
        )}
      </Card>

      {/* 筛选 */}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        {GROUPS.map((g) => (
          <button
            key={g.v}
            onClick={() => {
              setGroup(g.v);
              // ⚠️ 离开「公开市场」必须清掉方向：薪酬类的 direction 是 NULL，
              // 留着 direction=buy 会让页面空成一片，而按钮已被禁用、用户根本清不掉。
              if (g.v !== "open_market") setSide(null);
            }}
            title={g.hint}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              group === g.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {g.label}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-line" />
        {SIDES.map((s) => (
          <button
            key={s.label}
            onClick={() => setSide(s.v)}
            disabled={group !== "open_market"}
            title={group !== "open_market" ? "买卖方向只对公开市场交易有意义" : undefined}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition disabled:opacity-30 ${
              side === s.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {s.label}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-line" />
        {ROLES.map((r) => (
          <button
            key={r.label}
            onClick={() => setRole(r.v)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              role === r.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {r.label}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-line" />
        {PLANS.map((p) => (
          <button
            key={p.label}
            onClick={() => setPlan(p.v)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              plan === p.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {p.label}
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
            className="w-32 rounded-lg border border-line bg-card px-3 py-1.5 font-mono
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

      {cacheEmpty && !err && (
        <Card title="本地还没有数据" sub="先同步：季度数据集几秒就能拿到十万笔">
          <div className="text-sm leading-relaxed text-dim">
            点右上角「同步」。建议先补 2 个季度（约 6 秒、20 万笔）建立历史，
            再补最近几天补上最新申报 —— SEC 的季度数据集滞后一到两个月，
            最近这段只能逐日抓。
          </div>
        </Card>
      )}

      {filterEmpty && !err && (
        <Card
          title="当前筛选没有命中"
          sub={`本地共 ${(st?.trades ?? 0).toLocaleString()} 笔 —— 数据是有的，只是这个条件下没有`}
        >
          <div className="text-sm leading-relaxed text-dim">
            试试放宽时间范围、切到「全部」交易类型，或清除标的筛选。
          </div>
        </Card>
      )}

      {/* ⚠️ 汇总卡与图表依赖 summary；**明细表不依赖** ——
          汇总请求失败时把已经取到的明细一起藏掉，等于白费了 allSettled 的隔离。 */}
      {!cacheEmpty && !filterEmpty && summary && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="公开市场买入" value={money(summary.open_market.buy_value)} tone="up"
                  hint={`${summary.open_market.buys} 笔`} />
            <Stat label="公开市场卖出" value={money(summary.open_market.sell_value)} tone="down"
                  hint={`${summary.open_market.sells} 笔`} />
            <Stat
              label="净额"
              value={money(summary.open_market.buy_value - summary.open_market.sell_value)}
              tone={summary.open_market.buy_value >= summary.open_market.sell_value ? "up" : "down"}
            />
            <Stat
              label="10b5-1 计划卖出"
              value={`${summary.plan_sells} 笔`}
              hint="预先排定，非临时决定"
            />
          </div>

          <Card
            title="集群买入"
            sub="按「有多少位不同内部人买入同一只」排序 · 条上标注买入金额"
          >
            {summary.cluster_buys.length ? (
              <>
                <ReactECharts option={clusterOption} style={{ height: 360 }} notMerge />
                <div className="mt-2 text-[11px] leading-relaxed text-dim">
                  单人一笔大额可能只是个人理财；多位内部人在同一时期买入同一只，
                  更难用巧合解释 —— 这是内部人数据里最常被关注的形态。
                  <b className="text-ink">但这只是形态描述，不构成任何建议。</b>
                </div>
              </>
            ) : (
              <div className="py-8 text-center text-sm text-dim">该筛选下没有公开市场买入</div>
            )}
          </Card>

          <Card title="净买卖金额" sub="绿=净买入 红=净卖出 · 仅公开市场交易">
            {summary.by_ticker.length ? (
              <ReactECharts option={netOption} style={{ height: 360 }} notMerge />
            ) : (
              <div className="py-8 text-center text-sm text-dim">无数据</div>
            )}
          </Card>

        </>
      )}

      {/* ⚠️ 明细表**不依赖 summary**：汇总请求失败时把已取到的明细一起藏掉，
          等于白费了 allSettled 的隔离。上面的卡片与图表才依赖 summary。 */}
      {!cacheEmpty && !filterEmpty && (
        <Card
          title="交易明细"
          sub={`最新 ${trades.length} 笔${summary?.scope.truncated ? "（已达返回上限，非全量）" : ""}`}
        >
          <div className="-mx-1 overflow-x-auto">
            <table className="w-full min-w-[980px] text-left text-xs">
              <thead className="text-dim">
                <tr className="border-b border-line">
                  <Th>交易日</Th>
                  <Th>标的</Th>
                  <Th>内部人</Th>
                  <Th>身份</Th>
                  <Th>类型</Th>
                  <Th>股数</Th>
                  <Th>单价</Th>
                  <Th>金额</Th>
                  <Th>10b5-1</Th>
                  <Th>申报延迟</Th>
                  <Th>原件</Th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {trades.map((t, i) => (
                  <tr key={i} className="border-b border-line/50 hover:bg-card2/60">
                    <Td
                      className={t.date_anomaly ? "text-yellow-500" : ""}
                      title={t.date_anomaly ?? undefined}
                    >
                      {t.tx_date ?? "—"}
                      {t.date_anomaly && " ⚠"}
                    </Td>
                    <Td className="font-bold text-ink">{t.ticker ?? "—"}</Td>
                    <Td className="font-sans text-ink">{t.owner.slice(0, 26)}</Td>
                    <Td className="font-sans text-dim">
                      {[
                        t.is_officer && (t.officer_title || "高管"),
                        t.is_director && "董事",
                        t.is_ten_pct && "10%股东",
                      ]
                        .filter(Boolean)
                        .join(" · ")
                        .slice(0, 22) || "—"}
                    </Td>
                    <Td>
                      <span
                        className={
                          t.direction === "buy"
                            ? "text-green-400"
                            : t.direction === "sell"
                              ? "text-red-400"
                              : "text-dim"
                        }
                      >
                        {t.tx_code_label}
                      </span>
                    </Td>
                    <Td>{t.shares != null ? t.shares.toLocaleString() : "—"}</Td>
                    <Td>{t.price != null ? `$${t.price.toFixed(2)}` : "—"}</Td>
                    <Td
                      className={
                        t.price_implausible
                          ? "text-yellow-500"
                          : t.direction === "buy"
                            ? "text-green-400"
                            : ""
                      }
                      title={
                        t.price_implausible
                          ? "申报的每股价格不合理（疑为把总金额填进了价格字段），已剔出金额统计"
                          : undefined
                      }
                    >
                      {t.price_implausible ? "⚠ 价格存疑" : t.value != null ? money(t.value) : "—"}
                    </Td>
                    <Td className={t.is_10b5_1 ? "text-brand" : "text-dim"}>
                      {t.is_10b5_1 === null ? "—" : t.is_10b5_1 ? "是" : "否"}
                    </Td>
                    <Td>{t.delay_days != null ? `${t.delay_days}天` : "—"}</Td>
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
      )}


      {/* 口径与免责 */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          口径与边界
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          <li>
            · <b className="text-ink">分类按交易代码，不按「取得/处置」标志</b>：
            只有 P（公开市场买入）与 S（公开市场卖出）含主动买卖意图；
            A 授予 / M 行权 / F 代扣税 / D 向公司处置属薪酬类，不代表买卖决策。
            {summary && (
              <>
                {" "}当前样本共 {summary.total_rows.toLocaleString()} 行，其中公开市场{" "}
                {summary.open_market.count.toLocaleString()} 行（{summary.open_market_pct}%）。
              </>
            )}
          </li>
          <li>
            · <b className="text-ink">只含 Form 4</b>：同一个 SEC 数据集里还有
            Form 3（初始持股声明，不是交易）与 Form 5（年度补报，实测延迟中位
            <b className="text-ink"> 274 天</b>、三成超一年）—— 都已排除，
            否则会把申报延迟整体拉高。修订件 4/A 也默认排除
            （通常重述原申报的交易，与原件同时统计会重复计数；
            <b className="text-ink">本项目未做原件↔修订的配对替换</b>）。
            分离后 Form 4 的延迟中位是 2 天，与法定要求一致。
          </li>
          <li>
            · <b className="text-ink">10b5-1 计划交易另作区分</b>：
            按该规则预先制定的卖出计划，通常几个月前就已排定，与临时决定卖出含义不同。
            SEC 自 2023 年起要求在申报中勾选，更早的申报无此字段（显示为「—」）。
            筛选里<b className="text-ink">「未标注」与「非计划内」是两回事</b> ——
            前者是不知道，后者是申报人明确勾了否，不能混为一谈。
          </li>
          <li>
            · <b className="text-ink">金额是近似值</b>：部分申报把「总金额」误填进
            「每股价格」字段（实测有报到 <span className="font-mono">$2,400 万/股</span> 的，
            一行就能把全市场买入总额顶到千万亿量级）。已剔除可确证的错填
            （每股价 &gt; $100 万，或单笔金额 &gt; $2,000 亿 —— 前者超过 BRK.A 的历史最高价、
            后者超过任何美国个人的持股规模）。
            <b className="text-ink">但更隐蔽的错填识别不出来</b>：
            同样是错填，某只真实股价约 $2 的股票报 $14,561/股，没有外部行情就无从判断。
            所以金额汇总只应作量级参考。
            {summary && summary.implausible_price > 0 && (
              <> 当前样本中有 {summary.implausible_price} 笔已标注为「价格存疑」。</>
            )}
          </li>
          <li>
            · <b className="text-ink">数据覆盖有两段</b>：SEC 季度数据集完整但滞后
            （实测 7~49 天不等），最近这段只能逐日抓取补齐。已导入范围见上方「本地数据」。
          </li>
          <li>
            · <b className="text-ink">申报延迟</b>：Section 16(a) 要求交易后两个工作日内申报。
            此处按自然日计算、未扣周末与节假日，是事实统计而非违规认定。
            交易日由申报人手填、存在年份笔误（实测有报成 2028 年的），
            <b className="text-ink">「交易日晚于申报日」这种物理不可能的已标注</b>；
            但「晚报整一年」在法律上并非不可能，无法逐笔确证，
            所以延迟统计里仍混有少量笔误。上方「覆盖」区间用的是
            <b className="text-ink">申报日</b>（由 EDGAR 系统赋予，不受手填笔误影响）。
            {summary && summary.date_anomaly_count > 0 && (
              <> 当前样本中 {summary.date_anomaly_count} 笔已标注。</>
            )}
          </li>
          <li>
            · 本页只呈现已公开申报的事实，
            <b className="text-ink">不打「看涨/看跌」标签、不做评分、不构成任何投资建议</b>。
            数据源 SEC EDGAR（美国政府公开记录）。
          </li>
        </ul>
      </div>
    </>
  );
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "up" | "down";
}) {
  const c = tone === "up" ? "text-green-400" : tone === "down" ? "text-red-400" : "text-ink";
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-bold ${c}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-dim">{hint}</div>}
    </div>
  );
}


