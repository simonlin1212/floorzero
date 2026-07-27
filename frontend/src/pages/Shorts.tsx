import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/shorts.py) ── */
type Fail = {
  settlement_date: string;
  cusip: string;
  symbol: string;
  description: string;
  quantity: number | null;
  price: number | null;
  value: number | null;
  tag: string;
};
type Batch = {
  tag: string;
  rows: number;
  symbols: number;
  date_from: string | null;
  date_to: string | null;
  synced_at: string;
};
type Stats = {
  rows: number;
  symbols: number;
  days: number;
  earliest: string | null;
  latest: string | null;
  tags: string[];
  batches: Batch[];
  last_sync: string | null;
  finra_enabled: boolean;
  note: string;
};
type Notes = {
  ftd_cumulative: string;
  ftd_not_naked: string;
  volume_not_interest: string;
  price_caveat: string;
};
type SymbolRow = {
  symbol: string;
  description: string;
  cusip: string;
  days: number;
  avg_quantity: number | null;
  max_quantity: number | null;
  avg_value: number | null;
  max_value: number | null;
};
type Summary = {
  counts: { n: number; syms: number; days: number; lo: string; hi: string };
  by_symbol: SymbolRow[];
  by_date: {
    settlement_date: string;
    symbols: number;
    total_quantity: number;
    total_value: number;
  }[];
  stats: Stats;
  notes: Notes;
  scope: { aggregation: string };
};
type SyncState = {
  running: boolean;
  stage: string;
  rows: number;
  errors: string[];
  error_count: number;
  stats: Stats;
};
type FinraStatus = {
  enabled: boolean;
  env_var: string;
  terms: {
    url: string;
    last_modified: string;
    permitted: string;
    restriction_d: string;
    restriction_e: string;
    ambiguity: string;
    our_stance: string;
  };
};

function money(n: number): string {
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
  return `${s}$${a.toFixed(0)}`;
}
function num(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return n.toFixed(0);
}

export default function Shorts() {
  const [symbol, setSymbol] = useState("");
  const [symbolInput, setSymbolInput] = useState("");
  const [day, setDay] = useState("");

  const [fails, setFails] = useState<Fail[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [finra, setFinra] = useState<FinraStatus | null>(null);
  const [showTerms, setShowTerms] = useState(false);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [back, setBack] = useState(2);
  const pollRef = useRef<number | null>(null);
  const reqRef = useRef(0);

  const stats = summary?.stats ?? sync?.stats;
  const notes = summary?.notes;
  const days = useMemo(
    () => (summary?.by_date ?? []).map((d) => d.settlement_date),
    [summary],
  );

  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    setErr(null);
    // ⚠️ Detail and summary share one query string
    const q = new URLSearchParams();
    if (symbol) q.set("symbol", symbol);
    if (day) q.set("settlement_date", day);
    try {
      const [f, s] = await Promise.allSettled([
        fetch(`/api/shorts/ftd?${q}&limit=200`),
        fetch(`/api/shorts/summary?${q}`),
      ]);
      if (f.status !== "fulfilled" || !f.value.ok) {
        throw new Error(
          f.status === "fulfilled"
            ? ((await f.value.json()).detail ?? `HTTP ${f.value.status}`)
            : String(f.reason),
        );
      }
      const nextF = ((await f.value.json()) as { fails: Fail[] }).fails;
      const nextS =
        s.status === "fulfilled" && s.value.ok ? ((await s.value.json()) as Summary) : null;
      if (seq !== reqRef.current) return;
      setFails(nextF);
      setSummary(nextS);
    } catch (e) {
      if (seq !== reqRef.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setFails([]);
      setSummary(null);
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [symbol, day]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    fetch("/api/shorts/finra-status")
      .then((r) => (r.ok ? r.json() : null))
      .then((s: FinraStatus | null) => s && setFinra(s))
      .catch(() => {});
  }, []);

  const pollSync = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const r = await fetch("/api/shorts/sync");
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
    fetch("/api/shorts/sync")
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
      const r = await fetch(`/api/shorts/sync?back=${back}`, { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const s = (await r.json()) as SyncState & { started: boolean; reason?: string };
      setSync(s);
      if (s.started) pollSync();
      else if (s.reason) setErr(s.reason);
    } catch (e) {
      setErr(`Could not start the sync: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── Symbols with the largest fail-to-deliver balances ── */
  const symbolOption = useMemo(() => {
    const rows = (summary?.by_symbol ?? []).slice(0, 14).reverse();
    if (!rows.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 82, right: 76, top: 20, bottom: 34 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(r.symbol)}</b> ${esc(r.description.slice(0, 30))}<br/>` +
            `Mean balance across settlement dates ${num(r.avg_quantity ?? 0)} shares<br/>` +
            `Peak ${num(r.max_quantity ?? 0)} shares<br/>` +
            `Appearing on ${r.days} settlement dates`
          );
        },
      },
      xAxis: {
        type: "value",
        name: "Shares",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: {
          color: "#8e8a83",
          fontSize: 10,
          fontFamily: "JetBrains Mono",
          formatter: (v: number) => num(v),
        },
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.symbol),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#f2efe9", fontSize: 11, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          type: "bar",
          itemStyle: { color: "#a78bfa" },
          data: rows.map((r) => r.avg_quantity ?? 0),
          label: {
            show: true,
            position: "right",
            color: "#8e8a83",
            fontSize: 10,
            fontFamily: "JetBrains Mono",
            // ⚠️ With no price, mark it "no quote" rather than $0: the SEC's price field is "."
            // when it is "unavailable or below one cent", and $0 reads as though the stock were worthless
            formatter: (p: any) => {
              const v = rows[p.dataIndex].avg_value;
              return v == null || v === 0 ? "no quote" : money(v);
            },
          },
        },
      ],
    };
  }, [summary]);

  /* ── Market-wide balance per settlement date ── */
  const dateOption = useMemo(() => {
    const rows = summary?.by_date ?? [];
    if (rows.length < 2) return {};
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 24, top: 22, bottom: 46 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const r = rows[ps[0].dataIndex];
          return (
            `<b>${esc(r.settlement_date)}</b><br/>` +
            `Market-wide undelivered balance ${num(r.total_quantity)} shares<br/>` +
            `${r.symbols.toLocaleString()} symbols with a balance`
          );
        },
      },
      xAxis: {
        type: "category",
        data: rows.map((r) => r.settlement_date.slice(5)),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono", rotate: 45 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: {
          color: "#8e8a83",
          fontSize: 10,
          fontFamily: "JetBrains Mono",
          formatter: (v: number) => num(v),
        },
      },
      series: [
        {
          // ⚠️ Bars, not a line: a line implies "continuous evolution", while the SEC states plainly that consecutive days'
          // balances "may have little or no relationship" — each day is an independent instant and should not be joined into a trend
          type: "bar",
          itemStyle: { color: "#3b82f6" },
          data: rows.map((r) => r.total_quantity),
        },
      ],
    };
  }, [summary]);

  const cacheEmpty = !loading && (stats?.rows ?? 0) === 0;
  const filterEmpty = !loading && !cacheEmpty && fails.length === 0;

  return (
    <>
      <PageHead kicker="Short Data · SEC Fails-to-Deliver" title="Short-sale data">
        The main source is <b className="text-ink">SEC fails-to-deliver (FTD)</b> data —
        US government public record, with no restriction on commercial use. FINRA's off-exchange short volume carries its own terms and is
        <b className="text-ink"> off by default</b> (see below).
      </PageHead>

      {/* ⭐ Three official quotations — this section's data is the easiest to read backwards */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/[0.06] p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          Understand these three first, or the data reads backwards
        </div>
        <ul className="space-y-2 text-sm leading-relaxed text-dim">
          <li>
            <b className="text-ink">① An FTD is not that day's additions; it is a cumulative balance.</b>{" "}
            The SEC's own words: "Fails to deliver on a given day are a cumulative number of all
            fails outstanding until that day... <b className="text-ink">The figure is not
            a daily amount of fails</b>... may have little or no relationship to
            yesterday's aggregate fails. Thus... <b className="text-ink">the age of fails
            cannot be determined</b> by looking at these numbers."
            <br />
            → So this page <b className="text-ink">computes no day-on-day change and speaks of no surge</b>,
            and the chart below uses bars by settlement date rather than a line — consecutive days are no continuous trend.
          </li>
          <li>
            <b className="text-ink">② An FTD is not evidence of naked shorting.</b> The SEC's own words:
            "fails-to-deliver can occur for a number of reasons on{" "}
            <b className="text-ink">both long and short sales</b>. Therefore,
            fails-to-deliver are <b className="text-ink">not necessarily the result of
            short selling, and are not evidence of abusive short selling or 'naked'
            short selling</b>."
            <br />
            → Which is precisely this data's most popular use. Measured, the largest balances sit on GOOG / AMD / XOM
            and names like them — highly liquid large caps rather than small caps, which does not fit the "naked shorting is crushing it" story.
          </li>
          <li>
            <b className="text-ink">③ Short volume ≠ short interest.</b> FINRA's own words:
            "short interest position data <b className="text-ink">does not—and is not
            intended to—equate to</b> the daily short sale volume data."
            And that file holds <b className="text-ink">off-exchange</b> trades only
            (not consolidated with exchange data) →
            using it for a market-wide short share is wrong.
          </li>
        </ul>
      </div>

      {/* Sync */}
      <Card
        title="Local data"
        sub={
          stats && stats.rows
            ? `${stats.rows.toLocaleString()} rows · ${stats.symbols.toLocaleString()} symbols · ` +
              `${stats.days} settlement dates · covering ${stats.earliest} to ${stats.latest}` +
              (stats.last_sync ? ` · last synced ${stats.last_sync.replace("T", " ")}` : "")
            : "Not imported yet"
        }
        right={
          <div className="flex shrink-0 items-center gap-2">
            <select
              value={back}
              onChange={(e) => setBack(Number(e.target.value))}
              disabled={sync?.running}
              className="rounded-lg border border-line bg-card2 px-2 py-1.5 font-mono text-xs
                         text-dim outline-none focus:border-brand/50 disabled:opacity-40"
            >
              {[2, 4, 6, 12].map((n) => (
                <option key={n} value={n}>
                  Last {n} files ({n / 2} month{n / 2 > 1 ? "s" : ""})
                </option>
              ))}
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                         text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "Importing…" : "Import FTD"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-2 font-mono text-[11px] text-dim">
            {sync.stage}
            {sync.rows > 0 && ` · ${sync.rows.toLocaleString()} stored`}
          </div>
        )}
        <div className="space-y-1 text-[11px] leading-relaxed text-dim">
          <div>
            The SEC publishes <b className="text-ink">two half-month files</b> a month: the first half at month end and
            the second half around the 15th of the next — so the latest one or two are often not out yet, which is normal.
          </div>
          {stats && stats.tags.length > 0 && <div>Files imported: {stats.tags.join(", ")}</div>}
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

      {/* ⚠️ FINRA compliance: the terms are laid out as written, and the judgement left to the user */}
      {finra && (
        <Card
          title={`FINRA off-exchange short volume · ${finra.enabled ? "enabled" : "off by default"}`}
          sub="The compliance judgement on this lane is yours — all we guarantee is that you can read the terms"
          right={
            <button
              onClick={() => setShowTerms((v) => !v)}
              className="shrink-0 rounded-lg border border-line bg-card2 px-3 py-1.5 font-mono
                         text-xs text-dim hover:text-ink"
            >
              {showTerms ? "Hide the terms" : "Read the terms as written"}
            </button>
          }
        >
          <div className="text-xs leading-relaxed text-dim">
            {finra.enabled ? (
              <span>
                Enabled via <span className="font-mono text-ink">{finra.env_var}=1</span>.
              </span>
            ) : (
              <span>
                Not enabled. To use it, set{" "}
                <span className="font-mono text-ink">{finra.env_var}=1</span> and restart the backend.
                <b className="text-ink">
                  {" "}
                  This section's main source is SEC FTD, and works perfectly well without this lane.
                </b>
              </span>
            )}
          </div>
          {showTerms && (
            <div className="mt-3 space-y-2 rounded-lg border border-line bg-card2 p-3.5 text-[11px] leading-relaxed">
              <div className="font-mono text-[10px] text-dim">
                FINRA Terms of Use · last modified {finra.terms.last_modified} ·{" "}
                <a
                  href={finra.terms.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="underline decoration-dotted hover:text-brand"
                >
                  {finra.terms.url}
                </a>
              </div>
              <div>
                <b className="text-ink">Permitted Uses:</b>
                <span className="text-dim">"{finra.terms.permitted}"</span>
              </div>
              <div>
                <b className="text-ink">Restrictions (d):</b>
                <span className="text-dim">"{finra.terms.restriction_d}"</span>
              </div>
              <div>
                <b className="text-ink">Restrictions (e):</b>
                <span className="text-dim">"{finra.terms.restriction_e}"</span>
              </div>
              <div className="border-t border-line pt-2">
                <b className="text-brand">⚠️ There is a genuinely ambiguous area:</b>
                <span className="text-dim"> {finra.terms.ambiguity}</span>
              </div>
              <div>
                <b className="text-ink">How this project handles it:</b>
                <span className="text-dim"> {finra.terms.our_stance}</span>
              </div>
            </div>
          )}
        </Card>
      )}

      {/* Filters */}
      {!cacheEmpty && (
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <button
            onClick={() => setDay("")}
            className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
              !day
                ? "border-brand/50 bg-brand/12 text-brand"
                : "border-line bg-card text-dim hover:text-ink"
            }`}
          >
            All settlement dates
          </button>
          {days.slice(-8).map((d) => (
            <button
              key={d}
              onClick={() => setDay(d)}
              className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition ${
                day === d
                  ? "border-brand/50 bg-brand/12 text-brand"
                  : "border-line bg-card text-dim hover:text-ink"
              }`}
            >
              {d.slice(5)}
            </button>
          ))}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setSymbol(symbolInput.trim().toUpperCase());
            }}
            className="ml-auto flex gap-2"
          >
            <input
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value)}
              placeholder="Filter by ticker"
              className="w-32 rounded-lg border border-line bg-card px-3 py-1.5 font-mono
                         text-xs uppercase outline-none focus:border-brand/50"
            />
            {symbol && (
              <button
                type="button"
                onClick={() => {
                  setSymbol("");
                  setSymbolInput("");
                }}
                className="rounded-lg border border-line bg-card px-3 py-1.5 font-mono text-xs text-dim"
              >
                Clear
              </button>
            )}
          </form>
        </div>
      )}

      {err && (
        <div className="mb-5 rounded-xl border border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-400">
          {err}
        </div>
      )}

      {cacheEmpty && !err && (
        <Card title="No local data yet" sub="Importing one file takes a few seconds">
          <div className="text-sm leading-relaxed text-dim">
            Press "Import FTD" at the top right. The SEC publishes two half-month files a month; start with the last 4 (two months).
          </div>
        </Card>
      )}

      {filterEmpty && !err && (
        <Card title="Nothing matches the current filter" sub={`${(stats?.rows ?? 0).toLocaleString()} rows are held locally`}>
          <div className="text-sm leading-relaxed text-dim">
            This ticker has no fail-to-deliver balance in the imported range —
            <b className="text-ink"> which is normal</b>: symbols with a zero balance never appear in the file.
          </div>
        </Card>
      )}

      {!cacheEmpty && !filterEmpty && summary && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Records" value={(summary.counts.n ?? 0).toLocaleString()} />
            <Stat label="Symbols" value={(summary.counts.syms ?? 0).toLocaleString()} />
            <Stat label="Settlement dates" value={String(summary.counts.days ?? 0)} />
            <Stat
              label="Range"
              value={`${(summary.counts.lo ?? "").slice(5)} ~ ${(summary.counts.hi ?? "").slice(5)}`}
            />
          </div>

          <Card
            title="Largest fail-to-deliver balances"
            sub="Ordered by the mean balance across settlement dates (not their sum) · bars labelled with notional value"
          >
            {summary.by_symbol.length ? (
              <>
                <ReactECharts option={symbolOption} style={{ height: 400 }} notMerge />
                <div className="mt-2 text-[11px] leading-relaxed text-dim">
                  ⚠️ The mean rather than the sum: an FTD is <b className="text-ink">a cumulative balance at a point in time</b>,
                  one undelivered trade reappears across consecutive settlement dates, and adding the days together means nothing.
                </div>
              </>
            ) : (
              <div className="py-8 text-center text-sm text-dim">No data</div>
            )}
          </Card>

          {days.length > 1 && (
            <Card title="Market-wide undelivered balance by settlement date" sub="Each bar is an independent instant and forms no trend">
              <ReactECharts option={dateOption} style={{ height: 280 }} notMerge />
              <div className="mt-2 text-[11px] leading-relaxed text-dim">
                ⚠️ Bars rather than a line, deliberately: the SEC states plainly that consecutive days' balances
                "may have little or no relationship" —
                and a line would imply a continuous evolution that does not exist.
              </div>
            </Card>
          )}

          <Card title="Detail" sub={`${fails.length} rows · by settlement date and value, descending`}>
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>Settlement date</Th>
                    <Th>Ticker</Th>
                    <Th>Name</Th>
                    <Th>CUSIP</Th>
                    <Th>Undelivered balance</Th>
                    <Th>Previous close</Th>
                    <Th>Notional</Th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {fails.map((f, i) => (
                    <tr key={i} className="border-b border-line/50 hover:bg-card2/60">
                      <Td>{f.settlement_date}</Td>
                      <Td className="font-bold text-ink">{f.symbol || "—"}</Td>
                      <Td className="font-sans text-dim">{f.description.slice(0, 28)}</Td>
                      <Td className="text-dim">{f.cusip}</Td>
                      <Td>{f.quantity != null ? f.quantity.toLocaleString() : "—"}</Td>
                      <Td className={f.price == null ? "text-dim" : ""}>
                        {f.price != null ? `$${f.price.toFixed(2)}` : "no quote"}
                      </Td>
                      <Td className="text-ink">
                        {f.value != null ? money(f.value) : <span className="text-dim">no quote</span>}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      {/* Definitions and limits */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          Definitions and limits
        </div>
        <ul className="space-y-1.5 text-xs leading-relaxed text-dim">
          {notes && (
            <>
              <li>· <Emph>{notes.ftd_cumulative}</Emph></li>
              <li>· <Emph>{notes.ftd_not_naked}</Emph></li>
              <li>· <Emph>{notes.volume_not_interest}</Emph></li>
              <li>· <Emph>{notes.price_caveat}</Emph></li>
            </>
          )}
          <li>
            · <b className="text-ink">The price may be missing</b>: the SEC states that the price field is left empty
            when it is "unavailable or below one cent", and such records are shown here as "no quote" —
            which is not a value of zero.
          </li>
          <li>
            · <b className="text-ink">A zero balance never appears in the file</b>: finding no record for a symbol
            means it had no undelivered balance that day, not that data is missing.
          </li>
          <li>
            · <b className="text-ink">Publication lags</b>: the first half of a month appears at month end,
            and the second half around the 15th of the next. The SEC also states "We cannot guarantee the accuracy of the data".
          </li>
          <li>
            · Source: the SEC (US government public record, no restriction on commercial use). The FINRA lane carries its own terms and is off by default.
            This page presents public data, and
            <b className="text-ink"> attaches no "being shorted" label, produces no score, and is not investment advice</b>.
          </li>
        </ul>
      </div>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div className="mt-1 font-mono text-2xl font-bold text-ink">{value}</div>
    </div>
  );
}
