import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/congress.py) ── */
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
  scope: { truncated: boolean; sampled: number; limit: number | null };
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
  { v: null as string | null, label: "Both chambers" },
  { v: "house", label: "House" },
  { v: "senate", label: "Senate" },
];
const SIDES = [
  { v: null as string | null, label: "All" },
  { v: "buy", label: "Buy" },
  { v: "sell", label: "Sell" },
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

type TradeScope = { limit: number; returned: number; truncated: boolean };

  const [trades, setTrades] = useState<Trade[]>([]);
  // The listing's **own** truncation, not the summary's — they answer different queries
  const [tradeScope, setTradeScope] = useState<TradeScope | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [unparsed, setUnparsed] = useState<Unparsed[]>([]);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [yearsBack, setYearsBack] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  // ⚠️ A request sequence number: switching filters quickly leaves several load() calls in flight,
  // and a slow one returning last lays **the old filter's results** over the new (the control says NVDA while the table is market-wide).
  // Only the most recently issued request counts.
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    setErr(null);
    const since = daysAgo(range);
    // ⚠️ Detail and summary **share one query string**. The summary once carried only chamber/since,
    // so filtering by ticker left the detail showing NVDA while the cards and charts above stayed market-wide —
    // two views on one screen contradicting each other. Do not split them into two again.
    const q = new URLSearchParams();
    if (chamber) q.set("chamber", chamber);
    if (side) q.set("tx_type", side);
    if (since) q.set("since", since);
    if (ticker) q.set("ticker", ticker);
    try {
      // ⚠️ allSettled: one auxiliary view failing must not empty the main data already fetched
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
      const tPayload = (await t.value.json()) as { trades: Trade[]; scope: TradeScope };
      const nextTrades = tPayload.trades;
      const nextSummary =
        s.status === "fulfilled" && s.value.ok ? ((await s.value.json()) as Summary) : null;
      const nextUnparsed =
        u.status === "fulfilled" && u.value.ok
          ? ((await u.value.json()) as { filings: Unparsed[] }).filings
          : [];
      if (seq !== reqRef.current) return;              // superseded by a newer filter; discard
      setTrades(nextTrades);
      setTradeScope(tPayload.scope ?? null);
      setSummary(nextSummary);
      setUnparsed(nextUnparsed);
    } catch (e) {
      if (seq !== reqRef.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      // ⚠️ Old results must be cleared on failure: otherwise the market-wide table and charts carry on
      // displaying under an "NVDA" filter label, and what the user sees does not match what it says.
      setTrades([]);
      setTradeScope(null);
      setSummary(null);
      setUnparsed([]);
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [chamber, side, range, ticker]);

  useEffect(() => {
    void load();
  }, [load]);

  /* Poll sync progress; reload the data automatically when it finishes */
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
        /* a failed poll should not interrupt the page; the next round tries again */
      }
    }, 2500);
  }, [load]);

  useEffect(() => {
    // On entering the page, check whether a sync is already running (another tab may have started one)
    fetch("/api/congress/sync")
      .then((r) => (r.ok ? r.json() : null))
      .then((s: SyncState | null) => {
        if (!s) return;
        setSync(s);
        if (s.running) pollSync();
      })
      .catch(() => {});
    return () => {
      // ⚠️ The ref has to be nulled after clearing the timer:
      // changing a filter mid-sync gives load/pollSync a new identity → this cleanup runs,
      // and a still non-null ref makes the new pollSync() return immediately — the progress bar freezes,
      // and the data never refreshes on completion short of reopening the page.
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
      setErr(`Could not start the sync: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── Most active tickers ── */
  const tickerOption = useMemo(() => {
    // The backend's by_ticker is already sorted by trade count (matching "most active" and the chart), so there is no re-sort here —
    // sorting in two places drifts apart sooner or later.
    const rows = (summary?.by_ticker ?? []).slice(0, 14).reverse();
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 60, top: 26, bottom: 30 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#ffffff",
        borderColor: "#e2ddd4",
        textStyle: { color: "#1a1815", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(r.ticker)}</b> ${esc(r.asset_name.slice(0, 34))}<br/>` +
            `${r.buys} buys · ${r.sells} sells<br/>` +
            `${r.member_count} members · estimated ${money(r.est_amount)}`
          );
        },
      },
      legend: {
        data: ["Buys", "Sells"],
        textStyle: { color: "#6b665e", fontSize: 11 },
        top: 0,
        right: 4,
        itemWidth: 12,
        itemHeight: 8,
      },
      xAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#ece7dd" } },
        axisLabel: { color: "#6b665e", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.ticker),
        axisLine: { lineStyle: { color: "#e2ddd4" } },
        axisLabel: { color: "#1a1815", fontSize: 11, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          name: "Buys",
          type: "bar",
          stack: "x",
          itemStyle: { color: "#15803d" },
          data: rows.map((r) => r.buys),
        },
        {
          name: "Sells",
          type: "bar",
          stack: "x",
          itemStyle: { color: "#b91c1c" },
          data: rows.map((r) => r.sells),
        },
      ],
    };
  }, [summary]);

  /* ── Disclosure delay distribution ── */
  const delayOption = useMemo(() => {
    // ⭐ Uses the backend summary's own buckets: **the same sample** as the median and over-45-days cards above.
    // This once bucketed the detail table's 300 rows here while the cards used the summary's 2,000 —
    // two controls on one screen reporting different distributions. Bucketing does not belong in the frontend.
    // (Date anomalies are already excluded on the backend, and still listed and flagged in the detail table.)
    const buckets = summary?.delay.buckets ?? [];
    const counts = buckets.map((b) => b.count);
    if (counts.reduce((a, b) => a + b, 0) < 3) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#ffffff",
        borderColor: "#e2ddd4",
        textStyle: { color: "#1a1815", fontSize: 12 },
      },
      xAxis: {
        type: "category",
        data: buckets.map((b) => b.label),
        axisLine: { lineStyle: { color: "#e2ddd4" } },
        axisLabel: { color: "#6b665e", fontSize: 11 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#ece7dd" } },
        axisLabel: { color: "#6b665e", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          type: "bar",
          data: counts.map((c, i) => ({
            value: c,
            // Only the last bucket (>45d) gets the warning colour — and that is the fact of being late, not a finding of violation
            itemStyle: { color: i === 4 ? "#d4400d" : "#1d4ed8" },
          })),
          barMaxWidth: 54,
        },
      ],
    };
  }, [summary]);

  const st = summary?.stats ?? sync?.stats;
  // ⚠️ "No local data" and "nothing matches the current filter" are different things:
  // the first should point the user at the sync, the second need only say the filter matched nothing.
  // Judge it on the global cache count (st.trades), not on the current result being empty.
  const cacheEmpty = !loading && (st?.trades ?? 0) === 0;
  const filterEmpty = !loading && !cacheEmpty && trades.length === 0;
  const empty = cacheEmpty || filterEmpty;

  return (
    <>
      <PageHead kicker="Congress Trading · STOCK Act" title="Congressional trading">
        Members of Congress must disclose their trades publicly within set deadlines under the STOCK Act. The data comes
        straight from the<b className="text-ink"> Clerk of the House </b>and
        <b className="text-ink"> Senate eFD </b>— freely obtainable by the public,
        but <b className="text-ink">commercial use is prohibited by statute</b> (see the notes below).
      </PageHead>

      {/* The sync bar: first use has to fill the cache, and that should be said outright */}
      <Card
        title="Local data"
        sub={
          st
            ? `${st.trades} trades · ${st.tickers} tickers · ${st.members} members · ` +
              `${st.filings} filings${st.last_sync ? ` · last synced ${st.last_sync.replace("T", " ")}` : ""}`
            : "Not synced yet"
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
              <option value={0}>This year only</option>
              <option value={1}>Plus 1 year back</option>
              <option value={3}>Plus 3 years back</option>
              <option value={5}>Plus 5 years back</option>
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5
                         font-mono text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "Syncing…" : "Sync filings"}
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
              A first sync downloads every filing individually (300+ House PDFs for a single year), taking 2-4 minutes;
              after that it only picks up new ones. Reaching further back multiplies the time.
            </div>
          </div>
        )}

        {/* ⚠️ The Senate being unavailable is an environment problem, and must never read as "senators did not trade" */}
        {sync?.senate_status && sync.senate_status !== "available" && (
          <div className="mb-3 rounded-lg border border-brand/30 bg-brand/5 px-3.5 py-2.5 text-xs leading-relaxed">
            <b className="text-brand">The Senate source is currently unavailable</b>
            <div className="mt-1 text-dim">{sync.senate_status}</div>
            <div className="mt-1 text-dim">
              This means <b className="text-ink">the data could not be fetched</b>, not that senators did not trade —
              the results below cover the House only.
            </div>
          </div>
        )}

        {sync && sync.error_count > 0 && (
          <details className="text-xs text-dim">
            <summary className="cursor-pointer">{sync.error_count} errors during the sync</summary>
            <ul className="mt-1.5 space-y-0.5 font-mono text-[10px]">
              {sync.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </details>
        )}

        {st && st.unparsed_filings > 0 && (
          <div className="text-[11px] leading-relaxed text-dim">
            ⚠️ A further <b className="text-ink">{st.unparsed_filings}</b> filings (
            {st.unparsed_pct}%) could not be parsed automatically and are
            <b className="text-ink"> not counted in the trade total above</b> — almost all are paper scans (the whole filing an image).
            They are listed at the foot of this page.
          </div>
        )}
      </Card>

      {/* Filters */}
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
            placeholder="Filter by ticker"
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

      {filterEmpty && !err && (
        <Card title="Nothing matches the current filter" sub={`${st?.trades ?? 0} trades are held locally — the data is there, just not under these conditions`}>
          <div className="text-sm leading-relaxed text-dim">
            Try widening the date range, switching chamber, or clearing the ticker filter.
          </div>
        </Card>
      )}

      {cacheEmpty && !err && (
        <Card title="No local data yet" sub="Congressional disclosures come with their whole history; one sync fills it in">
          <div className="text-sm leading-relaxed text-dim">
            Press "Sync filings" at the top right to start. Unlike the options chain,
            <b className="text-ink"> congressional history can be backfilled at any time</b> —
            there is no "install late and lose a stretch forever" here.
            <br />
            The House archive is <b className="text-ink">split by year</b>, though:
            the default <b className="text-ink">syncs the current year only</b>, and earlier years come from the dropdown
            (each extra year is several hundred more PDFs and multiplies the time, so the choice is yours).
          </div>
        </Card>
      )}

      {!empty && (
        <>
          {summary && (
            <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="Trades" value={String(summary.total_trades)} />
              <Stat
                label="Buy / sell"
                value={`${summary.buys} / ${summary.sells}`}
                tone={summary.buys > summary.sells ? "up" : "down"}
              />
              <Stat
                label="Median disclosure delay"
                value={summary.delay.median_days != null ? `${summary.delay.median_days} days` : "—"}
              />
              <Stat
                label="Over 45 days"
                value={`${summary.delay.over_45d_count} trades`}
                tone={summary.delay.over_45d_count > 0 ? "warn" : undefined}
              />
            </div>
          )}

          <Card
            title="Most active tickers"
            sub={
              summary
                ? `Ordered by trade count · green = buys, red = sells · ${summary.by_ticker.length} tickers`
                : ""
            }
          >
            {summary?.by_ticker.length ? (
              <ReactECharts option={tickerOption} style={{ height: 380 }} notMerge />
            ) : (
              <div className="py-8 text-center text-sm text-dim">No trades carrying a ticker under this filter</div>
            )}
          </Card>

          <Card
            title="Disclosure delay distribution"
            /* ⚠️ Say the gap here rather than leaving it to the footnote. The card above reads
                832 and this chart 830, and two numbers a hair apart on one screen look like an
                error in the software until you find the note explaining the two excluded rows. */
            sub={(() => {
              const shown = summary?.delay.buckets.reduce((a, b) => a + b.count, 0) ?? 0;
              const bad = summary?.delay.anomaly_count ?? 0;
              return `Days from trade date to filing date · ${shown} trades`
                + (bad ? ` — ${bad} more are excluded, their filing date preceding the trade date`
                       + ` (an error in the original, still listed below)` : "");
            })()}
          >
            {Object.keys(delayOption).length ? (
              <ReactECharts option={delayOption} style={{ height: 240 }} notMerge />
            ) : (
              <div className="py-6 text-center text-sm text-dim">Not enough of a sample</div>
            )}
            <div className="mt-2 space-y-1 text-[11px] leading-relaxed text-dim">
              <div>⚠️ {summary?.delay.note ?? ""}</div>
              {!!summary?.delay.anomaly_count && (
                <div>
                  ⚠️ <b className="text-ink">{summary.delay.anomaly_count}</b> filings carry a
                  filing date earlier than the trade date (an error in the original). They are excluded from the delay statistics above,
                  and still listed and flagged in the detail.
                </div>
              )}
            </div>
          </Card>

          <Card
            title="Transaction detail"
            sub={`Newest ${trades.length} trades${tradeScope?.truncated ? " (the return limit was reached; this is not everything)" : ""}`}
          >
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[880px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>Trade date</Th>
                    <Th>Member</Th>
                    <Th>Chamber / district</Th>
                    <Th>Ticker</Th>
                    <Th>Direction</Th>
                    <Th>Amount range</Th>
                    <Th>Owner</Th>
                    <Th>Delay</Th>
                    <Th>Original</Th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {trades.map((t, i) => (
                    <tr key={i} className="border-b border-line/50 hover:bg-card2/60">
                      <Td>{t.tx_date ?? "—"}</Td>
                      <Td className="font-sans text-ink">{t.member}</Td>
                      <Td>
                        {t.chamber === "house" ? "H" : "S"}
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
                      <Td>{t.owner === "self" ? "Self" : t.owner}</Td>
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
                          ? "⚠ date doubtful"
                          : t.delay_days != null
                            ? `${t.delay_days}d`
                            : "—"}
                      </Td>
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

          <Card title="Members trading most" sub="Ordered by estimated amount">
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>Member</Th>
                    <Th>Chamber / district</Th>
                    <Th>Trades</Th>
                    <Th>Buy / sell</Th>
                    <Th>Tickers</Th>
                    <Th>Estimated amount</Th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {(summary?.by_member ?? []).slice(0, 18).map((m) => (
                    <tr key={m.member} className="border-b border-line/50 hover:bg-card2/60">
                      <Td className="font-sans text-ink">{m.member}</Td>
                      <Td>
                        {m.chamber === "house" ? "H" : "S"}
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
              title={`Filings that could not be read (${unparsed.length})`}
              sub="They do exist; their detail simply cannot be parsed automatically — listed here so you do not take the above for everything"
            >
              <div className="max-h-64 overflow-y-auto">
                <table className="w-full text-left text-xs">
                  <tbody className="font-mono">
                    {unparsed.map((u, i) => (
                      <tr key={i} className="border-b border-line/50">
                        <Td>{u.filing_date ?? "—"}</Td>
                        <Td className="font-sans text-ink">{u.member}</Td>
                        <Td>{u.chamber === "house" ? "H" : "S"}</Td>
                        <Td className="font-sans text-dim">{u.unparsed_reason.slice(0, 30)}</Td>
                        <Td>
                          <a
                            href={u.source_url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="text-dim underline decoration-dotted hover:text-brand"
                          >
                            Original
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

      {/* Definitions and disclaimer — the same approach as the GEX section: assumptions laid out, nothing hidden */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          Definitions and limits
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          <li>
            · <b className="text-ink">Amounts are ranges, not exact figures</b>: the STOCK Act only requires banded disclosure
            (such as $1,001-$15,000). Every "estimated amount" is a sum of range midpoints,
            <b className="text-ink">good for comparison between rows and not a real trade size</b>.
          </li>
          <li>
            · <b className="text-ink">"Over 45 days" is a fact, not a finding of violation</b>: the statutory deadline is
            30 days after becoming aware and no later than 45 days after the trade, rolling over weekends and holidays,
            and amendments and late broker notifications exist too.
          </li>
          <li>
            · <b className="text-ink">Coverage is not complete</b>: about one filing in ten is a paper scan,
            unreadable without OCR. Those filings are listed separately, and are not in the statistics.
          </li>
          <li>
            · <b className="text-ink">Disclosure lags by nature</b>: what you see are trades made tens of days ago.
            This page presents filed public data and is not investment advice.
          </li>
          <li>
            · <b className="text-brand">⚠️ Usage restriction</b>: under
            <span className="font-mono"> 5 U.S.C. §13107(c)(1)(B) </span>
            (the Ethics in Government Act),
            <b className="text-ink">obtaining or using these reports for any commercial purpose is unlawful</b>{" "}
            (with an exception for news and communications media disseminating to the public); §13107(c)(2) lets the Attorney General bring
            a civil action with a maximum fine of $10,000. The restriction <b className="text-ink">applies to both chambers</b>.
            <br />
            So: public inspection, personal research, academic and journalistic use ✅;
            <b className="text-ink">using this page's data in any paid product or commercial service ❌</b>.
            FloorZero is free, open-source and run by you, and personal research falls inside what is allowed.
            <br />
            <span className="text-dim">
              Note: this differs from SEC EDGAR — EDGAR restricts the request rate and not commercial use,
              and the two lanes' compliance tiers must not be conflated.
            </span>
          </li>
          <li>
            · Sources: the Clerk of the House · Senate eFD (US government public record).
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


