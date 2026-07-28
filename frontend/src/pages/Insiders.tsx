import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/insider.py) ── */
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
  { v: "open_market", label: "Open market", hint: "Codes P/S — the only part carrying an intent to trade" },
  { v: "compensation", label: "Compensation", hint: "Grants, exercises, tax withholding; no decision to trade" },
  { v: "all", label: "All", hint: "Includes compensation, which drowns the real trading signal" },
];
const SIDES = [
  { v: null as string | null, label: "All" },
  { v: "buy", label: "Buy" },
  { v: "sell", label: "Sell" },
];
const ROLES = [
  { v: null as string | null, label: "Any role" },
  { v: "officer", label: "Officer" },
  { v: "director", label: "Director" },
  { v: "ten_pct", label: "10% holder" },
];
const PLANS = [
  { v: null as string | null, label: "Any plan status" },
  { v: "yes", label: "Under a 10b5-1 plan" },
  { v: "no", label: "Not under a plan" },
  // The three states are kept apart: NULL is "the filing did not mark it" (no such field before 2023), not "confirmed not under a plan"
  { v: "unknown", label: "Not marked" },
];
const RANGES = [
  { d: 30, label: "Last 30 days" },
  { d: 90, label: "Last 90 days" },
  { d: 365, label: "Last year" },
  { d: 0, label: "All" },
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

type TradeScope = { limit: number; returned: number; truncated: boolean };

  const [trades, setTrades] = useState<Trade[]>([]);
  // The listing's **own** truncation, not the summary's — they answer different queries
  const [tradeScope, setTradeScope] = useState<TradeScope | null>(null);
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
    // ⚠️ Detail and summary **share one query string** — split into two and they will draw different scopes sooner or later
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
      const tPayload = (await t.value.json()) as { trades: Trade[]; scope: TradeScope };
      const nextTrades = tPayload.trades;
      const nextSummary =
        s.status === "fulfilled" && s.value.ok ? ((await s.value.json()) as Summary) : null;
      if (seq !== reqRef.current) return; // superseded by a newer filter
      setTrades(nextTrades);
      setTradeScope(tPayload.scope ?? null);
      setSummary(nextSummary);
    } catch (e) {
      if (seq !== reqRef.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      // ⚠️ Old results must be cleared on failure: otherwise the market-wide table and charts carry on
      // displaying under an "NVDA" filter label, and what the user sees does not match what it says.
      setTrades([]);
      setTradeScope(null);
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
        /* a failed poll should not interrupt the page */
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
      // The ref has to be nulled after clearing the timer, or a new pollSync() returns immediately and progress freezes for good
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
      setErr(`Could not start the sync: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── Cluster buying: how many distinct insiders are buying the same name ── */
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
            `<b>${r.insider_count}</b> insiders bought · ${r.buys} trades<br/>` +
            `Bought ${money(r.buy_value)} · Sold ${money(r.sell_value)}<br/>` +
            `<span style="color:#8e8a83">${esc(r.insiders.slice(0, 4).join(", "))}</span>`
          );
        },
      },
      xAxis: {
        type: "value",
        name: "Insiders buying",
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

  /* ── Net buy and sell value ── */
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
            `<b>${esc(r.ticker)}</b><br/>Bought ${money(r.buy_value)} (${r.buys} trades)<br/>` +
            `Sold ${money(r.sell_value)} (${r.sells} trades)<br/>Net ${money(r.net_value)}`
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
      <PageHead kicker="Insider Trading · SEC Form 4" title="Insider trading">
        Officers, directors and holders of more than 10% who trade their own company's stock must file
        Form 4 with the SEC<b className="text-ink"> within two business days </b>under Section 16(a).
        The data comes straight from<b className="text-ink"> SEC EDGAR </b>— US government public record,
        and <b className="text-ink">freely redistributable</b>.
      </PageHead>

      {/* ⭐ The one thing this section most needs to make clear */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/[0.06] p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          Understand this first, or the data reads backwards
        </div>
        <p className="text-sm leading-relaxed text-dim">
          <b className="text-ink">Most records in a Form 4 are not "an insider likes the stock, so they bought".</b>{" "}
          Across 103,733 non-derivative transactions market-wide in 2026Q1: tax withheld 27,019 / grants 24,690 /
          sales 22,822 / option exercises 16,300, while
          <b className="text-ink"> real open-market buying came to just 5,935 (5.6%)</b>.
          Counted bluntly by the SEC's acquired/disposed flag, "acquired" runs to 48,849 —{" "}
          <b className="text-ink">eight times the real buying</b>.
          <br />
          <span className="mt-1.5 inline-block">
            The classic shape: an option exercise and an immediate sale on the same day. The exercise is flagged "acquired",
            but the insider <b className="text-ink">did not buy a single share on the open market, and sold every share they received</b> —
            that is compensation being cashed out, not conviction. So this page shows <b className="text-ink">open market (P/S) only</b> by default.
          </span>
        </p>
      </div>

      {/* Sync */}
      <Card
        title="Local data"
        sub={
          st
            ? `${st.trades.toLocaleString()} trades · ${st.tickers.toLocaleString()} tickers · ` +
              `${st.owners.toLocaleString()} insiders · ${st.open_market_pct}% open market` +
              (st.last_sync ? ` · last synced ${st.last_sync.replace("T", " ")}` : "")
            : "Not synced yet"
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
                  {n === 0 ? "Skip quarters" : `Backfill ${n} quarter${n > 1 ? "s" : ""}`}
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
                  {n === 0 ? "Skip days" : `Backfill ${n} day${n > 1 ? "s" : ""}`}
                </option>
              ))}
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                         text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "Syncing…" : "Sync"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-3">
            <div className="mb-1.5 flex justify-between font-mono text-[11px] text-dim">
              <span>{sync.stage}</span>
              <span>
                {sync.done}/{sync.total || "?"} · {sync.rows.toLocaleString()} stored
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

        {/* ⚠️ The coverage boundary has to be spelled out: the quarterly dataset lags, and the recent stretch comes only from daily fetching */}
        {(sync?.coverage || st) && (
          <div className="space-y-1 text-[11px] leading-relaxed text-dim">
            {sync?.coverage && <div>ℹ️ {sync.coverage}</div>}
            <div>
              The two sources differ in cost by two orders of magnitude:
              the <b className="text-ink">quarterly dataset</b> takes about 3 seconds for a whole quarter (~100k trades),
              while <b className="text-ink">fetching day by day</b> takes about 90 seconds a day (600-700 filings,
              each its own request). So quarters fill in history and the daily fetch covers only the last few days.
            </div>
            {st && st.quarters.length > 0 && (
              <div>
                Quarters imported: {st.quarters.join(", ")}
                {st.days.length > 0 && ` · ${st.days.length} trading days fetched`}
                {st.earliest && ` · covering ${st.earliest} to ${st.latest}`}
              </div>
            )}
          </div>
        )}

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
        {GROUPS.map((g) => (
          <button
            key={g.v}
            onClick={() => {
              setGroup(g.v);
              // ⚠️ Leaving "open market" must clear the direction: compensation rows have a NULL direction,
              // so leaving direction=buy empties the page while the button is disabled and the user cannot clear it.
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
            title={group !== "open_market" ? "Direction is only meaningful for open-market transactions" : undefined}
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
            placeholder="Filter by ticker"
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
        <Card title="No local data yet" sub="Sync first: the quarterly dataset gives a hundred thousand trades in seconds">
          <div className="text-sm leading-relaxed text-dim">
            Press Sync at the top right. Start with 2 quarters (about 6 seconds, 200k trades) to build history,
            then add the last few days for the newest filings — the SEC's quarterly dataset lags by a month or two,
            and that recent stretch can only be fetched day by day.
          </div>
        </Card>
      )}

      {filterEmpty && !err && (
        <Card
          title="Nothing matches the current filter"
          sub={`${(st?.trades ?? 0).toLocaleString()} trades are held locally — the data is there, just not under these conditions`}
        >
          <div className="text-sm leading-relaxed text-dim">
            Try widening the date range, switching the transaction type to "All", or clearing the ticker filter.
          </div>
        </Card>
      )}

      {/* ⚠️ The summary cards and charts depend on summary; **the detail table does not** —
          hiding detail that already arrived just because the summary request failed wastes the allSettled isolation. */}
      {!cacheEmpty && !filterEmpty && summary && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Open-market buying" value={money(summary.open_market.buy_value)} tone="up"
                  hint={`${summary.open_market.buys} trades`} />
            <Stat label="Open-market selling" value={money(summary.open_market.sell_value)} tone="down"
                  hint={`${summary.open_market.sells} trades`} />
            <Stat
              label="Net"
              value={money(summary.open_market.buy_value - summary.open_market.sell_value)}
              tone={summary.open_market.buy_value >= summary.open_market.sell_value ? "up" : "down"}
            />
            <Stat
              label="10b5-1 plan sales"
              value={`${summary.plan_sells} trades`}
              hint="Arranged in advance, not decided on the day"
            />
          </div>

          <Card
            title="Cluster buying"
            sub="Ordered by how many distinct insiders bought the same name · bars labelled with the amount bought"
          >
            {summary.cluster_buys.length ? (
              <>
                <ReactECharts option={clusterOption} style={{ height: 360 }} notMerge />
                <div className="mt-2 text-[11px] leading-relaxed text-dim">
                  One large trade by one person may be personal finance; several insiders buying the same name
                  in the same period is harder to put down to coincidence — the shape this data is most watched for.{" "}
                  <b className="text-ink">But it is a description of a shape, and no recommendation whatsoever.</b>
                </div>
              </>
            ) : (
              <div className="py-8 text-center text-sm text-dim">No open-market buying under this filter</div>
            )}
          </Card>

          <Card title="Net buy and sell value" sub="Green = net buying, red = net selling · open-market transactions only">
            {summary.by_ticker.length ? (
              <ReactECharts option={netOption} style={{ height: 360 }} notMerge />
            ) : (
              <div className="py-8 text-center text-sm text-dim">No data</div>
            )}
          </Card>

        </>
      )}

      {/* ⚠️ The detail table **does not depend on summary**: hiding detail that already arrived because the
          summary request failed wastes the allSettled isolation. Only the cards and charts above depend on it. */}
      {!cacheEmpty && !filterEmpty && (
        <Card
          title="Transaction detail"
          sub={`Newest ${trades.length} trades${tradeScope?.truncated ? " (the return limit was reached; this is not everything)" : ""}`}
        >
          <div className="-mx-1 overflow-x-auto">
            <table className="w-full min-w-[980px] text-left text-xs">
              <thead className="text-dim">
                <tr className="border-b border-line">
                  <Th>Trade date</Th>
                  <Th>Ticker</Th>
                  <Th>Insider</Th>
                  <Th>Role</Th>
                  <Th>Type</Th>
                  <Th>Shares</Th>
                  <Th>Price</Th>
                  <Th>Value</Th>
                  <Th>10b5-1</Th>
                  <Th>Filing delay</Th>
                  <Th>Original</Th>
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
                        t.is_officer && (t.officer_title || "Officer"),
                        t.is_director && "Director",
                        t.is_ten_pct && "10% holder",
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
                          ? "The filed price per share is implausible (the total value was probably entered in the price field), so it is excluded from the value totals"
                          : undefined
                      }
                    >
                      {t.price_implausible ? "⚠ price doubtful" : t.value != null ? money(t.value) : "—"}
                    </Td>
                    <Td className={t.is_10b5_1 ? "text-brand" : "text-dim"}>
                      {t.is_10b5_1 === null ? "—" : t.is_10b5_1 ? "Yes" : "No"}
                    </Td>
                    <Td>{t.delay_days != null ? `${t.delay_days}d` : "—"}</Td>
                    <Td>
                      <a
                        href={t.source_url}
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


      {/* Definitions and disclaimer */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          Definitions and limits
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          <li>
            · <b className="text-ink">Classified by transaction code, not by the acquired/disposed flag</b>:
            only P (open-market purchase) and S (open-market sale) carry an intent to trade;
            A grants / M exercises / F tax withholding / D dispositions to the issuer are compensation and represent no decision to trade.
            {summary && (
              <>
                {" "}The current sample holds {summary.total_rows.toLocaleString()} rows, of which{" "}
                {summary.open_market.count.toLocaleString()} are open market ({summary.open_market_pct}%).
              </>
            )}
          </li>
          <li>
            · <b className="text-ink">Form 4 only</b>: the same SEC dataset also carries
            Form 3 (an initial statement of holdings, not a transaction) and Form 5 (the annual catch-up filing, median delay
            <b className="text-ink"> 274 days</b>, three in ten over a year) — both excluded,
            or they would inflate the filing delay across the board. 4/A amendments are excluded by default too
            (they usually restate the original's transactions, so counting both double-counts;
            <b className="text-ink">this project does not pair originals with amendments and substitute one for the other</b>).
            Separated out, Form 4's median delay is 2 days, matching the statute.
          </li>
          <li>
            · <b className="text-ink">10b5-1 plan trades are distinguished</b>:
            a sale plan adopted in advance under that rule was usually arranged months earlier, which means something different from a sale decided on the day.
            The SEC has required the box to be ticked since 2023; earlier filings have no such field and show "—".
            In the filter, <b className="text-ink">"Not marked" and "Not under a plan" are different things</b> —
            the first means we do not know, the second that the filer explicitly said no. They must not be conflated.
          </li>
          <li>
            · <b className="text-ink">Values are approximate</b>: some filings put the total value into
            the price-per-share field (one reads <span className="font-mono">$24m per share</span>,
            and a single row like that pushes market-wide buying into the quadrillions). Provably wrong entries are excluded
            (over $1m per share, or over $200bn for a single trade — the first exceeds BRK.A's all-time high,
            the second any US individual's holding).{" "}
            <b className="text-ink">But subtler mis-entries cannot be caught</b>:
            one stock actually trading around $2 filed $14,561 per share, and without an external quote there is no way to tell.
            So value totals should be read as an indication of magnitude only.
            {summary && summary.implausible_price > 0 && (
              <> The current sample has {summary.implausible_price} flagged with a doubtful price.</>
            )}
          </li>
          <li>
            · <b className="text-ink">Coverage comes in two parts</b>: the SEC's quarterly dataset is complete but lags
            (measured at anywhere from 7 to 49 days), and the recent stretch is filled in day by day. What has been imported is shown under "Local data" above.
          </li>
          <li>
            · <b className="text-ink">Filing delay</b>: Section 16(a) requires filing within two business days of the trade.
            Counted here in calendar days without deducting weekends and holidays: a statement of fact, not a finding of violation.
            Trade dates are typed by the filer and contain year typos (one was filed as 2028),
            and <b className="text-ink">the physically impossible ones — a trade date after the filing date — are flagged</b>;
            but "filed a whole year late" is not legally impossible and cannot be confirmed row by row,
            so a few typos remain inside the delay statistics. The coverage range above uses the
            <b className="text-ink"> filing date</b> (assigned by EDGAR, and so free of typing errors).
            {summary && summary.date_anomaly_count > 0 && (
              <> {summary.date_anomaly_count} in the current sample are flagged.</>
            )}
          </li>
          <li>
            · This page presents facts already publicly filed, and
            <b className="text-ink"> attaches no bullish or bearish label, produces no score, and is not investment advice</b>.
            Source: SEC EDGAR (US government public record).
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


