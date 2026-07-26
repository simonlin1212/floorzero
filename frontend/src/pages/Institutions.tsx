import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/institution.py 对齐）── */
type Holding = {
  manager: string;
  manager_cik: string;
  period: string;
  cusip: string;
  issuer: string;
  title_of_class: string;
  kind: string;
  kind_label: string;
  value: number | null;
  shares: number | null;
  shares_type: string;
  discretion: string;
  is_amendment: boolean;
  source_url: string;
};
type Batch = {
  period: string;
  window: string;
  rows: number;
  parsed_rows: number;
  dropped_rows: number;
  dropped_value: number;
  min_value: number;
  managers: number;
  synced_at: string;
};
type Stats = {
  holdings: number;
  managers: number;
  cusips: number;
  total_value: number;
  by_kind: Record<string, number>;
  periods: string[];
  batches: Batch[];
  last_sync: string | null;
  note: string;
};
type IssuerRow = {
  cusip: string;
  issuer: string;
  class: string | null;
  holders: number;
  value: number;
  shares: number;
};
type Summary = {
  counts: { n: number; mgrs: number; cusips: number; val: number };
  by_issuer: IssuerRow[];
  by_manager: { manager_cik: string; manager: string; positions: number; value: number }[];
  by_kind: Record<string, { rows: number; value: number }>;
  stats: Stats;
};
type ChangeRow = {
  cusip: string;
  issuer: string;
  class?: string | null;
  holders: number;
  value: number;
  prev_value: number;
  delta_value: number;
};
type Changes = {
  period: string;
  prev_period: string;
  new: ChangeRow[];
  increased: ChangeRow[];
  decreased: ChangeRow[];
  exited: ChangeRow[];
  counts: { new: number; increased: number; decreased: number; exited: number; unchanged: number };
  min_value: number;
  note: string;
  floor_note: string;
};
type SyncState = {
  running: boolean;
  stage: string;
  rows: number;
  errors: string[];
  error_count: number;
  windows: string[];
  // ⚠️ 后端返回的是数据集里的原始写法（如 31-MAR-2026），
  // 而 /sync 的 period 参数要 YYYY-MM-DD —— 送出前必须转换
  periods: [string, number][];
  stats: Stats;
};

const MONTHS: Record<string, string> = {
  JAN: "01", FEB: "02", MAR: "03", APR: "04", MAY: "05", JUN: "06",
  JUL: "07", AUG: "08", SEP: "09", OCT: "10", NOV: "11", DEC: "12",
};

/** `31-MAR-2026` → `2026-03-31`；已是 ISO 就原样返回。 */
function isoPeriod(raw: string): string {
  const m = /^(\d{2})-([A-Z]{3})-(\d{4})$/.exec(raw.trim().toUpperCase());
  if (!m) return raw;
  const mm = MONTHS[m[2]];
  return mm ? `${m[3]}-${mm}-${m[1]}` : raw;
}

const KINDS = [
  { v: "share", label: "普通持股", hint: "13(f) 证券的多头持仓" },
  { v: "call", label: "看涨期权", hint: "按标的列示" },
  { v: "put", label: "看跌期权", hint: "看空 —— 混进持仓统计会把看空算成看多" },
  { v: "all", label: "全部", hint: "含 put，会把看空混进来" },
];

/** 发行人 + 股份类别。同一发行人常有多个类别（Alphabet CL A / CL C 是两只不同证券），
 *  只显示名字会让榜单出现两行一模一样的字，看着像重复数据。 */
function label(r: { issuer: string; class?: string | null }): string {
  const c = (r.class || "").trim();
  // COM = 普通股，是默认情况，拼上去只会变长
  if (!c || c.toUpperCase() === "COM") return r.issuer;
  return `${r.issuer} · ${c}`;
}

function money(n: number): string {
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
  return `${s}$${a.toFixed(0)}`;
}

export default function Institutions() {
  const [period, setPeriod] = useState<string>("");
  const [kind, setKind] = useState("share");
  const [manager, setManager] = useState("");
  const [managerInput, setManagerInput] = useState("");
  const [cusip, setCusip] = useState("");
  const [cusipInput, setCusipInput] = useState("");

  const [holdings, setHoldings] = useState<Holding[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [changes, setChanges] = useState<Changes | null>(null);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [minValue, setMinValue] = useState(1_000_000);
  // ⚠️ 必须能选报告期：页面写着"至少导入两个季度才能看环比"，
  // 但同步按钮若永远导同一期，UI 就做不到它自己写的事。
  const [syncWindow, setSyncWindow] = useState("");
  const [syncPeriod, setSyncPeriod] = useState("");
  const pollRef = useRef<number | null>(null);
  const reqRef = useRef(0);

  const stats = summary?.stats ?? sync?.stats;
  const periods = stats?.periods ?? [];
  const active = period || periods[0] || "";
  const prev = periods.find((p) => p < active) ?? "";

  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    setErr(null);
    // ⚠️ 明细与汇总共用同一份查询串
    const q = new URLSearchParams({ kind });
    if (active) q.set("period", active);
    if (manager) q.set("manager", manager);
    if (cusip) q.set("cusip", cusip);
    try {
      const cq = new URLSearchParams({ kind: kind === "all" ? "share" : kind });
      if (active) cq.set("period", active);
      if (prev) cq.set("prev_period", prev);
      if (manager) cq.set("manager", manager);
      const [h, s, c] = await Promise.allSettled([
        fetch(`/api/institution/holdings?${q}&limit=200`),
        fetch(`/api/institution/summary?${q}`),
        prev ? fetch(`/api/institution/changes?${cq}`) : Promise.resolve(null as any),
      ]);
      if (h.status !== "fulfilled" || !h.value.ok) {
        throw new Error(
          h.status === "fulfilled"
            ? ((await h.value.json()).detail ?? `HTTP ${h.value.status}`)
            : String(h.reason),
        );
      }
      const nextH = ((await h.value.json()) as { holdings: Holding[] }).holdings;
      const nextS =
        s.status === "fulfilled" && s.value?.ok ? ((await s.value.json()) as Summary) : null;
      const nextC =
        c.status === "fulfilled" && c.value?.ok ? ((await c.value.json()) as Changes) : null;
      if (seq !== reqRef.current) return;
      setHoldings(nextH);
      setSummary(nextS);
      setChanges(nextC);
    } catch (e) {
      if (seq !== reqRef.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setHoldings([]);
      setSummary(null);
      setChanges(null);
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [active, prev, kind, manager, cusip]);

  useEffect(() => {
    void load();
  }, [load]);

  const pollSync = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const r = await fetch("/api/institution/sync");
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
    }, 4000);
  }, [load]);

  useEffect(() => {
    fetch("/api/institution/sync")
      .then((r) => (r.ok ? r.json() : null))
      .then((s: SyncState | null) => {
        if (!s) return;
        setSync(s);
        if (s.running) pollSync();
      })
      .catch(() => {});
    return () => {
      // 清定时器后必须把 ref 置空，否则新的 pollSync() 会直接 return
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [pollSync]);

  async function startSync() {
    try {
      const q = new URLSearchParams({ min_value: String(minValue) });
      if (syncWindow) q.set("window", syncWindow);
      if (syncPeriod) q.set("period", isoPeriod(syncPeriod));
      const r = await fetch(`/api/institution/sync?${q}`, { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setSync((await r.json()) as SyncState);
      pollSync();
    } catch (e) {
      setErr(`同步启动失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── 持仓最大的标的 ── */
  const issuerOption = useMemo(() => {
    const rows = (summary?.by_issuer ?? []).slice(0, 14).reverse();
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 170, right: 70, top: 20, bottom: 32 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(label(r))}</b><br/>CUSIP ${esc(r.cusip)}<br/>` +
            `持仓市值 ${money(r.value)}<br/>${r.holders.toLocaleString()} 家机构持有`
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
        data: rows.map((r) => label(r).slice(0, 26)),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#f2efe9", fontSize: 10 },
      },
      series: [
        {
          type: "bar",
          itemStyle: { color: kind === "put" ? "#ef4444" : "#3b82f6" },
          data: rows.map((r) => r.value),
          label: {
            show: true,
            position: "right",
            color: "#8e8a83",
            fontSize: 10,
            fontFamily: "JetBrains Mono",
            formatter: (p: any) => `${rows[p.dataIndex].holders} 家`,
          },
        },
      ],
    };
  }, [summary, kind]);

  /* ── 环比变动 ── */
  const changeOption = useMemo(() => {
    if (!changes) return {};
    const inc = changes.increased.slice(0, 8);
    const dec = changes.decreased.slice(0, 8);
    const rows = [...dec].reverse().concat([...inc].reverse());
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 170, right: 40, top: 20, bottom: 32 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(label(r))}</b><br/>` +
            `${changes.prev_period} ${money(r.prev_value)} → ${changes.period} ${money(r.value)}<br/>` +
            `变动 ${money(r.delta_value)}`
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
        data: rows.map((r) => label(r).slice(0, 26)),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#f2efe9", fontSize: 10 },
      },
      series: [
        {
          type: "bar",
          data: rows.map((r) => ({
            value: r.delta_value,
            itemStyle: { color: r.delta_value >= 0 ? "#22c55e" : "#ef4444" },
          })),
        },
      ],
    };
  }, [changes]);

  const cacheEmpty = !loading && (stats?.holdings ?? 0) === 0;
  const filterEmpty = !loading && !cacheEmpty && holdings.length === 0;
  const batch = stats?.batches?.find((b) => b.period === active);

  return (
    <>
      <PageHead kicker="Institutional Holdings · SEC 13F" title="机构持仓">
        管理 1 亿美元以上的机构投资经理，须依 Section 13(f) 每季度申报其持仓。数据取自
        <b className="text-ink"> SEC 官方结构化数据集 </b>——
        美国政府公开记录，<b className="text-ink">不限商用</b>。
      </PageHead>

      {/* ⭐ 这个分栏最该先讲清楚的事 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/[0.06] p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          「机构持仓」这个说法本身就会误导
        </div>
        <p className="text-sm leading-relaxed text-dim">
          13F 报的是<b className="text-ink">季末那一个时点、13(f) 证券的多头持仓</b>。它
          <b className="text-ink">不含</b>：空头头寸、现金、债券、大宗商品、仅在境外上市的股票、
          私募持仓，以及获保密豁免暂缓披露的部分。所以「某机构持仓 X 亿」既不是它的全部资产，
          <b className="text-ink">也不代表净敞口</b>。
          <br />
          <span className="mt-1.5 inline-block">
            空头不在这里 ——{" "}
            <b className="text-ink">
              SEC 2023 年专门另立 Rule 13f-2 / Form SHO 报空头
            </b>
            ，正因为 13F 不覆盖。
          </span>
          <br />
          <span className="mt-1.5 inline-block">
            ⚠️ <b className="text-ink">期权按「标的证券」列示</b>（Form 13F 特别说明第 10 条）：
            一笔<b className="text-ink">看跌期权（看空）</b>会以「持有标的」的形态出现。
            实测该季 put 规模{" "}
            <b className="text-ink">
              {summary?.by_kind?.put ? money(summary.by_kind.put.value) : "$2.6T"}
            </b>{" "}
            —— 直接加总当「机构在买」，就是把这么大的看空头寸算成看多。
            所以本页默认<b className="text-ink">只看普通持股</b>。
          </span>
        </p>
      </div>

      {/* 同步 */}
      <Card
        title="本地数据"
        sub={
          stats && stats.holdings
            ? `${stats.holdings.toLocaleString()} 条持仓 · ${stats.managers.toLocaleString()} 家机构 · ` +
              `${stats.cusips.toLocaleString()} 个 CUSIP · 合计 ${money(stats.total_value)}` +
              (stats.last_sync ? ` · 上次同步 ${stats.last_sync.replace("T", " ")}` : "")
            : "尚未导入"
        }
        right={
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            {/* 数据集窗口：不选=最新。窗口里含哪些报告期由 sync 返回 */}
            <select
              value={syncWindow}
              onChange={(e) => {
                setSyncWindow(e.target.value);
                setSyncPeriod("");     // 换窗口后旧的报告期不再适用
              }}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              <option value="">最新窗口</option>
              {(sync?.windows ?? []).map((w) => (
                <option key={w} value={w}>
                  {w}
                </option>
              ))}
            </select>
            <select
              value={syncPeriod}
              onChange={(e) => setSyncPeriod(e.target.value)}
              disabled={sync?.running || !(sync?.periods ?? []).length}
              title={
                (sync?.periods ?? []).length
                  ? "该窗口内的报告期（括号内为申报份数）"
                  : "先点一次同步，才知道窗口里有哪些报告期"
              }
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              <option value="">主体报告期</option>
              {(sync?.periods ?? []).map(([p, n]) => (
                <option key={p} value={p}>
                  {isoPeriod(p)}（{n}）
                </option>
              ))}
            </select>
            <select
              value={minValue}
              onChange={(e) => setMinValue(Number(e.target.value))}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              <option value={1_000_000}>门槛 $1M（推荐）</option>
              <option value={10_000_000}>门槛 $10M</option>
              <option value={100_000}>门槛 $100K</option>
              <option value={0}>全量（约 580MB/季）</option>
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                         text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "导入中…" : "导入最新季度"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-3">
            <div className="font-mono text-[11px] text-dim">
              {sync.stage}
              {sync.rows > 0 && ` · 已入库 ${sync.rows.toLocaleString()} 条`}
            </div>
            <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-card2">
              <div className="h-full w-1/3 animate-pulse bg-brand" />
            </div>
          </div>
        )}

        <div className="space-y-1 text-[11px] leading-relaxed text-dim">
          <div>
            单季数据集 95MB 压缩、约 <b className="text-ink">332 万条</b>持仓。
            默认金额门槛 <b className="text-ink">$100 万</b> ——
            实测保留 37.5% 的行数、覆盖 <b className="text-ink">99.37%</b> 的金额，
            是很划算的交换；要精确到小持仓可选「全量」。
          </div>
          {batch && (
            <div>
              当前 {batch.period}（窗口 {batch.window}）：解析{" "}
              {batch.parsed_rows.toLocaleString()} 条 → 入库{" "}
              <b className="text-ink">{batch.rows.toLocaleString()}</b> 条；门槛 $
              {batch.min_value.toLocaleString()} 滤掉 {batch.dropped_rows.toLocaleString()}{" "}
              条 / {money(batch.dropped_value)}
              （占该季总额{" "}
              {(
                (batch.dropped_value / (batch.dropped_value + (stats?.total_value ?? 1))) *
                100
              ).toFixed(2)}
              %）。
            </div>
          )}
          {stats && stats.periods.length > 0 && (
            <div>
              已导入报告期：{stats.periods.join("、")}
              {stats.periods.length < 2 && (
                <b className="text-brand">
                  {" "}
                  —— 再导一个季度才能看环比变动（13F 的主要价值在变动，不在静态快照）
                </b>
              )}
            </div>
          )}
        </div>

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
        {periods.map((p) => (
          <button
            key={p}
            onClick={() => setPeriod(p)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              active === p
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {p}
          </button>
        ))}
        {periods.length > 0 && <span className="mx-1 h-4 w-px bg-line" />}
        {KINDS.map((k) => (
          <button
            key={k.v}
            onClick={() => setKind(k.v)}
            title={k.hint}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              kind === k.v
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            {k.label}
          </button>
        ))}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setManager(managerInput.trim());
            setCusip(cusipInput.trim().toUpperCase());
          }}
          className="ml-auto flex gap-2"
        >
          <input
            value={managerInput}
            onChange={(e) => setManagerInput(e.target.value)}
            placeholder="机构名"
            className="w-32 rounded-lg border border-line bg-card px-3 py-1.5 text-xs
                       outline-none focus:border-brand/50"
          />
          <input
            value={cusipInput}
            onChange={(e) => setCusipInput(e.target.value)}
            placeholder="CUSIP"
            className="w-28 rounded-lg border border-line bg-card px-3 py-1.5 font-mono
                       text-xs uppercase outline-none focus:border-brand/50"
          />
          <button
            type="submit"
            className="rounded-lg border border-line bg-card px-3 py-1.5 font-mono text-xs text-dim
                       hover:text-ink"
          >
            筛选
          </button>
          {(manager || cusip) && (
            <button
              type="button"
              onClick={() => {
                setManager("");
                setCusip("");
                setManagerInput("");
                setCusipInput("");
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
        <Card title="本地还没有数据" sub="导入一个季度约需 1-2 分钟">
          <div className="text-sm leading-relaxed text-dim">
            点右上角「导入最新季度」。13F 每季申报一次、法定期限为季末后 45 天，
            所以最新可得的通常是<b className="text-ink">上一个季度</b>的时点持仓。
            <br />
            建议至少导入<b className="text-ink">两个季度</b> ——
            单季持仓只是静态快照，<b className="text-ink">环比变动才有信息量</b>。
          </div>
        </Card>
      )}

      {filterEmpty && !err && (
        <Card
          title="当前筛选没有命中"
          sub={`本地共 ${(stats?.holdings ?? 0).toLocaleString()} 条持仓`}
        >
          <div className="text-sm leading-relaxed text-dim">
            试试换个报告期、换持仓类型，或清除机构/CUSIP 筛选。
          </div>
        </Card>
      )}

      {!cacheEmpty && !filterEmpty && summary && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="持仓市值" value={money(summary.counts.val ?? 0)} />
            <Stat label="申报机构" value={(summary.counts.mgrs ?? 0).toLocaleString()} />
            <Stat label="标的数" value={(summary.counts.cusips ?? 0).toLocaleString()} />
            <Stat
              label="看跌期权规模"
              value={money(summary.by_kind?.put?.value ?? 0)}
              hint="已单独归类，不计入持仓"
              tone="warn"
            />
          </div>

          <Card
            title={`持仓最大的标的 · ${KINDS.find((k) => k.v === kind)?.label}`}
            sub={`条上标注持有该标的的机构家数 · ${active}`}
          >
            {summary.by_issuer.length ? (
              <ReactECharts option={issuerOption} style={{ height: 400 }} notMerge />
            ) : (
              <div className="py-8 text-center text-sm text-dim">无数据</div>
            )}
          </Card>

          {changes && (
            <Card
              title="季度环比变动"
              sub={`${changes.prev_period} → ${changes.period} · 绿=加仓 红=减仓 · 按 CUSIP 比对`}
            >
              <div className="mb-3 flex flex-wrap gap-3">
                <MiniStat label="新建仓" value={String(changes.counts.new)} tone="up" />
                <MiniStat label="加仓" value={String(changes.counts.increased)} tone="up" />
                <MiniStat label="减仓" value={String(changes.counts.decreased)} tone="down" />
                <MiniStat label="清仓" value={String(changes.counts.exited)} tone="down" />
                {/* 未变动单列：早前它们被算进「减仓」，把"没动"显示成"在减" */}
                <MiniStat label="未变动" value={String(changes.counts.unchanged ?? 0)} />
              </div>
              <ReactECharts option={changeOption} style={{ height: 420 }} notMerge />
              <div className="mt-2 space-y-1 text-[11px] leading-relaxed text-dim">
                <div>⚠️ {changes.note}</div>
                <div>⚠️ {changes.floor_note}</div>
              </div>
            </Card>
          )}

          <Card title="持仓最大的机构" sub={`${active} · 仅统计当前持仓类型`}>
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>机构</Th>
                    <Th>持仓标的数</Th>
                    <Th>持仓市值</Th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {summary.by_manager.slice(0, 15).map((m) => (
                    <tr key={m.manager_cik} className="border-b border-line/50 hover:bg-card2/60">
                      <Td className="font-sans text-ink">{m.manager.slice(0, 42)}</Td>
                      <Td>{m.positions.toLocaleString()}</Td>
                      <Td>{money(m.value)}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      {!cacheEmpty && !filterEmpty && (
        <Card title="持仓明细" sub={`最大 ${holdings.length} 条 · 按市值倒序`}>
          <div className="-mx-1 overflow-x-auto">
            <table className="w-full min-w-[900px] text-left text-xs">
              <thead className="text-dim">
                <tr className="border-b border-line">
                  <Th>机构</Th>
                  <Th>标的</Th>
                  <Th>CUSIP</Th>
                  <Th>类型</Th>
                  <Th>市值</Th>
                  <Th>数量</Th>
                  <Th>裁量权</Th>
                  <Th>原件</Th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {holdings.map((h, i) => (
                  <tr key={i} className="border-b border-line/50 hover:bg-card2/60">
                    <Td className="font-sans text-ink">{h.manager.slice(0, 28)}</Td>
                    <Td className="font-sans">{h.issuer.slice(0, 26)}</Td>
                    <Td className="text-dim">{h.cusip}</Td>
                    <Td>
                      <span
                        className={
                          h.kind === "put"
                            ? "text-red-400"
                            : h.kind === "call"
                              ? "text-green-400"
                              : "text-dim"
                        }
                      >
                        {h.kind_label}
                      </span>
                    </Td>
                    <Td className="text-ink">{h.value != null ? money(h.value) : "—"}</Td>
                    <Td>
                      {h.shares != null ? h.shares.toLocaleString() : "—"}
                      <span className="text-dim"> {h.shares_type}</span>
                    </Td>
                    <Td className="text-dim">{h.discretion}</Td>
                    <Td>
                      <a
                        href={h.source_url}
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

      {/* 口径与边界 */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          口径与边界
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          <li>
            · <b className="text-ink">只有多头，且只有 13(f) 证券</b>：不含空头（另见 Form SHO）、
            现金、债券、大宗商品、仅境外上市的股票、私募持仓与获保密豁免的部分。
          </li>
          <li>
            · <b className="text-ink">期权按标的列示</b>，看跌期权是<b className="text-ink">看空</b>。
            本页按普通持股 / 看涨 / 看跌分开统计，默认只看普通持股。
          </li>
          <li>
            · <b className="text-ink">至少滞后 45 天</b>：13F 的法定申报期限是季末后 45 天，
            看到的是<b className="text-ink">一个半月前的时点</b>持仓，期间机构可能已大幅调仓。
          </li>
          <li>
            · <b className="text-ink">用 CUSIP 不用股票代码</b>：13F 只给 CUSIP，
            SEC 不提供 CUSIP→代码映射（那是商业数据）。按发行人名称去匹配 SEC 的
            company_tickers.json 实测命中率仅 <b className="text-ink">42.8%</b>
            （未命中的多为 ETF 与基金），所以本页以发行人名称 + CUSIP 为准。
            <br />
            发行人名称取自 <b className="text-ink">SEC 官方 13(f) 证券清单</b> ——
            申报里的名称是填报人自由填写的，实测苹果那个 CUSIP 有{" "}
            <b className="text-ink">61 种写法</b>，其中还有别家公司的名字。
          </li>
          <li>
            · <b className="text-ink">「清仓」不等于看空</b>：只代表该 CUSIP 不再出现在 13(f)
            多头持仓里 —— 可能转成了期权、移到无需申报的账户，或该证券已退出 13(f) 清单。
          </li>
          <li>
            · <b className="text-ink">修订件默认排除</b>：13F 修订要求全文重述整份申报，
            与原件同时统计会重复计数。
          </li>
          <li>
            · 本页只呈现已申报的事实，
            <b className="text-ink">不打「看涨/看跌」标签、不做评分、不构成任何投资建议</b>。
            数据源 SEC EDGAR 结构化数据集（美国政府公开记录，不限商用）。
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
  tone?: "warn";
}) {
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div
        className={`mt-1 font-mono text-2xl font-bold ${tone === "warn" ? "text-brand" : "text-ink"}`}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-[11px] text-dim">{hint}</div>}
    </div>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  const c = tone === "up" ? "text-green-400" : tone === "down" ? "text-red-400" : "text-ink";
  return (
    <div className="rounded-lg border border-line bg-card2 px-3.5 py-2">
      <div className="font-mono text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div className={`mt-0.5 font-mono text-lg font-bold ${c}`}>{value}</div>
    </div>
  );
}
