import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/institution.py) ── */
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
  // ⚠️ The backend returns the dataset's own spelling (such as 31-MAR-2026),
  // while /sync's period parameter takes YYYY-MM-DD — it has to be converted before sending
  periods: [string, number][];
  stats: Stats;
};

const MONTHS: Record<string, string> = {
  JAN: "01", FEB: "02", MAR: "03", APR: "04", MAY: "05", JUN: "06",
  JUL: "07", AUG: "08", SEP: "09", OCT: "10", NOV: "11", DEC: "12",
};

/** `31-MAR-2026` → `2026-03-31`; already ISO, it comes back unchanged. */
function isoPeriod(raw: string): string {
  const m = /^(\d{2})-([A-Z]{3})-(\d{4})$/.exec(raw.trim().toUpperCase());
  if (!m) return raw;
  const mm = MONTHS[m[2]];
  return mm ? `${m[3]}-${mm}-${m[1]}` : raw;
}

const KINDS = [
  { v: "share", label: "Shares", hint: "Long positions in 13(f) securities" },
  { v: "call", label: "Calls", hint: "Listed under the underlying" },
  { v: "put", label: "Puts", hint: "Bearish — folded into the holdings totals, they get counted as bullish exposure" },
  { v: "all", label: "All", hint: "Includes puts, mixing bearish exposure in" },
];

/** Issuer plus share class. One issuer often has several classes (Alphabet CL A and CL C are two distinct securities),
 *  and showing the name alone puts two identical-looking rows in the table, which reads as duplicated data. */
function label(r: { issuer: string; class?: string | null }): string {
  const c = (r.class || "").trim();
  // COM = common stock, which is the default case; appending it only makes the label longer
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
  // ⚠️ The reporting period has to be selectable: the page says "import at least two quarters to see the change",
  // but if the sync button always imports the same period, the UI cannot do what it says.
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
    // ⚠️ Detail and summary share one query string
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
        /* a failed poll should not interrupt the page */
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
      // The ref has to be nulled after clearing the timer, or a new pollSync() returns immediately
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
      setErr(`Could not start the sync: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── Largest holdings by security ── */
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
            `Position value ${money(r.value)}<br/>held by ${r.holders.toLocaleString()} managers`
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
            formatter: (p: any) => `${rows[p.dataIndex].holders} managers`,
          },
        },
      ],
    };
  }, [summary, kind]);

  /* ── Quarter-on-quarter change ── */
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
            `Change ${money(r.delta_value)}`
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
      <PageHead kicker="Institutional Holdings · SEC 13F" title="Institutional holdings">
        Investment managers with over $100m in assets under management must report their holdings quarterly under Section 13(f). The data comes from
        <b className="text-ink"> the SEC's official structured dataset </b>—
        US government public record, with <b className="text-ink">no restriction on commercial use</b>.
      </PageHead>

      {/* ⭐ The thing this section most needs to say first */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/[0.06] p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          The phrase "institutional holdings" is misleading
        </div>
        <p className="text-sm leading-relaxed text-dim">
          A 13F reports <b className="text-ink">long positions in 13(f) securities at one instant, quarter-end</b>. It
          <b className="text-ink"> excludes</b>: short positions, cash, bonds, commodities, stocks listed only outside the US,
          private holdings, and anything granted confidential treatment. So "this manager holds $X bn" is neither their total assets
          <b className="text-ink"> nor their net exposure</b>.
          <br />
          <span className="mt-1.5 inline-block">
            Shorts are not in here —{" "}
            <b className="text-ink">
              the SEC created Rule 13f-2 / Form SHO in 2023 specifically to report shorts
            </b>
            , precisely because 13F does not cover them.
          </span>
          <br />
          <span className="mt-1.5 inline-block">
            ⚠️ <b className="text-ink">Options are listed under the underlying security</b> (Form 13F Special Instruction 10):
            a <b className="text-ink">put — a bearish position</b> — appears shaped exactly like holding the stock.
            Puts that quarter came to{" "}
            <b className="text-ink">
              {summary?.by_kind?.put ? money(summary.by_kind.put.value) : "$2.6T"}
            </b>{" "}
            — sum it in as "institutions are buying" and that much bearish exposure is counted as bullish.
            So this page shows <b className="text-ink">ordinary holdings only</b> by default.
          </span>
        </p>
      </div>

      {/* Sync */}
      <Card
        title="Local data"
        sub={
          stats && stats.holdings
            ? `${stats.holdings.toLocaleString()} holdings · ${stats.managers.toLocaleString()} managers · ` +
              `${stats.cusips.toLocaleString()} CUSIPs · totalling ${money(stats.total_value)}` +
              (stats.last_sync ? ` · last synced ${stats.last_sync.replace("T", " ")}` : "")
            : "Not imported yet"
        }
        right={
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            {/* The dataset window: unselected = the newest. Which reporting periods it holds comes back from sync */}
            <select
              value={syncWindow}
              onChange={(e) => {
                setSyncWindow(e.target.value);
                setSyncPeriod("");     // after changing window, the old reporting period no longer applies
              }}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              <option value="">Newest window</option>
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
                  ? "Reporting periods inside that window (filing counts in brackets)"
                  : "Sync once to find out which reporting periods the window holds"
              }
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              <option value="">Main reporting period</option>
              {(sync?.periods ?? []).map(([p, n]) => (
                <option key={p} value={p}>
                  {isoPeriod(p)} ({n})
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
              <option value={1_000_000}>Threshold $1M (recommended)</option>
              <option value={10_000_000}>Threshold $10M</option>
              <option value={100_000}>Threshold $100K</option>
              <option value={0}>Everything (about 580MB a quarter)</option>
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                         text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "Importing…" : "Import newest quarter"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-3">
            <div className="font-mono text-[11px] text-dim">
              {sync.stage}
              {sync.rows > 0 && ` · ${sync.rows.toLocaleString()} stored`}
            </div>
            <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-card2">
              <div className="h-full w-1/3 animate-pulse bg-brand" />
            </div>
          </div>
        )}

        <div className="space-y-1 text-[11px] leading-relaxed text-dim">
          <div>
            One quarter's dataset is 95MB compressed and about <b className="text-ink">3.32m</b> holdings.
            The default value threshold is <b className="text-ink">$1m</b> —
            measured, it keeps 37.5% of the rows and covers <b className="text-ink">99.37%</b> of the value,
            which is a good tradeoff. For precision down to small positions, choose "Everything".
          </div>
          {batch && (
            <div>
              Current {batch.period} (window {batch.window}): parsed{" "}
              {batch.parsed_rows.toLocaleString()} rows → stored{" "}
              <b className="text-ink">{batch.rows.toLocaleString()}</b>; the $
              {batch.min_value.toLocaleString()} threshold dropped {batch.dropped_rows.toLocaleString()}{" "}
              rows / {money(batch.dropped_value)}
              {" ("}
              {(
                (batch.dropped_value / (batch.dropped_value + (stats?.total_value ?? 1))) *
                100
              ).toFixed(2)}
              % of the quarter's total).
            </div>
          )}
          {stats && stats.periods.length > 0 && (
            <div>
              Reporting periods imported: {stats.periods.join(", ")}
              {stats.periods.length < 2 && (
                <b className="text-brand">
                  {" "}
                  — import one more quarter to see the change (13F's value is in the change, not the static snapshot)
                </b>
              )}
            </div>
          )}
        </div>

        {sync && sync.error_count > 0 && (
          <details className="mt-2 text-xs text-dim">
            <summary className="cursor-pointer">{sync.error_count} notices during the sync</summary>
            <ul className="mt-1.5 space-y-0.5 font-mono text-[10px]">
              {sync.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </details>
        )}
      </Card>

      {/* Filters */}
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
            placeholder="Manager name"
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
            Filter
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
              Clear
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
        <Card title="No local data yet" sub="Importing one quarter takes 1-2 minutes">
          <div className="text-sm leading-relaxed text-dim">
            Press "Import newest quarter" at the top right. 13F is filed quarterly with a statutory deadline 45 days after quarter-end,
            so the newest available is usually <b className="text-ink">the previous quarter's</b> positions.
            <br />
            Import at least <b className="text-ink">two quarters</b> —
            a single quarter is only a static snapshot, and <b className="text-ink">the change is what carries the information</b>.
          </div>
        </Card>
      )}

      {filterEmpty && !err && (
        <Card
          title="Nothing matches the current filter"
          sub={`${(stats?.holdings ?? 0).toLocaleString()} holdings are held locally`}
        >
          <div className="text-sm leading-relaxed text-dim">
            Try a different reporting period or position kind, or clear the manager and CUSIP filters.
          </div>
        </Card>
      )}

      {!cacheEmpty && !filterEmpty && summary && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Position value" value={money(summary.counts.val ?? 0)} />
            <Stat label="Managers filing" value={(summary.counts.mgrs ?? 0).toLocaleString()} />
            <Stat label="Securities" value={(summary.counts.cusips ?? 0).toLocaleString()} />
            <Stat
              label="Puts outstanding"
              value={money(summary.by_kind?.put?.value ?? 0)}
              hint="Classified separately and excluded from holdings"
              tone="warn"
            />
          </div>

          <Card
            title={`Largest holdings · ${KINDS.find((k) => k.v === kind)?.label}`}
            sub={`Bars labelled with the number of managers holding it · ${active}`}
          >
            {summary.by_issuer.length ? (
              <ReactECharts option={issuerOption} style={{ height: 400 }} notMerge />
            ) : (
              <div className="py-8 text-center text-sm text-dim">No data</div>
            )}
          </Card>

          {changes && (
            <Card
              title="Quarter-on-quarter change"
              sub={`${changes.prev_period} → ${changes.period} · green = added, red = trimmed · compared on CUSIP`}
            >
              <div className="mb-3 flex flex-wrap gap-3">
                <MiniStat label="New" value={String(changes.counts.new)} tone="up" />
                <MiniStat label="Added" value={String(changes.counts.increased)} tone="up" />
                <MiniStat label="Trimmed" value={String(changes.counts.decreased)} tone="down" />
                <MiniStat label="Exited" value={String(changes.counts.exited)} tone="down" />
                {/* Unchanged stands alone: these were once counted as trimmed, showing "did not move" as "is selling" */}
                <MiniStat label="Unchanged" value={String(changes.counts.unchanged ?? 0)} />
              </div>
              <ReactECharts option={changeOption} style={{ height: 420 }} notMerge />
              <div className="mt-2 space-y-1 text-[11px] leading-relaxed text-dim">
                <div>⚠️ {changes.note}</div>
                <div>⚠️ {changes.floor_note}</div>
              </div>
            </Card>
          )}

          <Card title="Largest managers" sub={`${active} · counting the current position kind only`}>
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>Manager</Th>
                    <Th>Securities held</Th>
                    <Th>Position value</Th>
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
        <Card title="Holding detail" sub={`Largest ${holdings.length} · by value, descending`}>
          <div className="-mx-1 overflow-x-auto">
            <table className="w-full min-w-[900px] text-left text-xs">
              <thead className="text-dim">
                <tr className="border-b border-line">
                  <Th>Manager</Th>
                  <Th>Security</Th>
                  <Th>CUSIP</Th>
                  <Th>Kind</Th>
                  <Th>Value</Th>
                  <Th>Quantity</Th>
                  <Th>Discretion</Th>
                  <Th>Original</Th>
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
                        View
                      </a>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Definitions and limits */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          Definitions and limits
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          <li>
            · <b className="text-ink">Long positions only, and 13(f) securities only</b>: no shorts (see Form SHO),
            cash, bonds, commodities, stocks listed only outside the US, private holdings, or anything granted confidential treatment.
          </li>
          <li>
            · <b className="text-ink">Options are listed under the underlying</b>, and a put is <b className="text-ink">bearish</b>.
            This page counts shares / calls / puts separately, and shows ordinary holdings by default.
          </li>
          <li>
            · <b className="text-ink">At least 45 days behind</b>: 13F's statutory deadline is 45 days after quarter-end,
            so what you see is a position <b className="text-ink">six weeks old</b>, and the manager may have moved a long way since.
          </li>
          <li>
            · <b className="text-ink">Keyed on CUSIP, not on ticker</b>: 13F gives CUSIPs only, and
            the SEC publishes no CUSIP→ticker mapping (that is commercial data). Matching issuer names against the SEC's
            company_tickers.json was measured hitting only <b className="text-ink">42.8%</b>{" "}
            (most misses being ETFs and funds), so this page keys on issuer name plus CUSIP.
            <br />
            Issuer names come from the <b className="text-ink">SEC's official 13(f) securities list</b> —
            names in the filings are typed freely by the filer, and Apple's CUSIP was measured carrying{" "}
            <b className="text-ink">61 spellings</b>, some of them other companies' names.
          </li>
          <li>
            · <b className="text-ink">An exit is not bearishness</b>: it means only that this CUSIP no longer appears among 13(f)
            long holdings — it may have moved into options, into an account that need not be reported, or the security may have left the 13(f) list.
          </li>
          <li>
            · <b className="text-ink">Amendments are excluded by default</b>: a 13F amendment must restate the filing whole,
            so counting it alongside the original double-counts.
          </li>
          <li>
            · This page presents facts already filed, and
            <b className="text-ink"> attaches no bullish or bearish label, produces no score, and is not investment advice</b>.
            Source: the SEC EDGAR structured dataset (US government public record, no restriction on commercial use).
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
