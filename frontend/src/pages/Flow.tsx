import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/flow.py) ── */
type Limits = {
  no_tape: string;
  no_direction: string;
  vol_oi: string;
  oi_lag: string;
  delayed: string;
};
type Row = {
  symbol: string;
  expiry: string;
  type: string;
  strike: number;
  dte: number;
  volume: number;
  open_interest: number;
  vol_oi: number | null;
  zero_prior_oi: boolean;
  mid: number | null;
  bid_zero: boolean;
  last: number | null;
  notional: number | null;
  iv: number | null;
  delta: number | null;
  gamma: number | null;
  unusual: boolean;
};
type Leg = {
  // null when a side has nothing computable at all — **"not computable" is not "zero"**
  call: number | null;
  put: number | null;
  pc: number | null;
  basis: string;
  counted_call?: number;
  counted_put?: number;
};
type Flow = {
  ticker: string;
  spot: number;
  timestamp: string | null;
  session: string | null;
  counts: {
    traded_contracts: number;
    scope_contracts: number;
    unusual: number;
    zero_prior_oi: number;
    total_volume: number;
    total_oi: number;
  };
  ratios: {
    by_volume: Leg;
    by_oi: Leg;
    by_notional: Leg;
    notional_excluded: number;
    one_sided_quotes: number;
  };
  exposure: {
    // null when every contract lacks its greeks — **"not computable" is not "zero exposure"**
    call_delta_shares: number | null;
    put_delta_shares: number | null;
    total_delta_shares: number | null;
    call_delta_notional: number | null;
    put_delta_notional: number | null;
    gamma_notional_per_1pct: number | null;
    missing_delta_call: number;
    missing_delta_put: number;
    missing_gamma: number;
    counted_delta_call: number;
    counted_delta_put: number;
    counted_gamma: number;
    note: string;
  };
  unusual_rows: Row[];
  biggest_rows: Row[];
  by_expiry: {
    expiry: string;
    dte: number;
    call_volume: number;
    put_volume: number;
    volume: number;
    open_interest: number;
    notional: number | null;
    notional_counted: number;
    contracts: number;
  }[];
  by_strike: {
    rows: {
      strike: number;
      call_volume: number;
      put_volume: number;
      call_oi: number;
      put_oi: number;
    }[];
    window_pct: number;
    low: number;
    high: number;
    dropped_contracts: number;
    dropped_volume: number;
  };
  thresholds: { unusual_ratio: number; min_volume: number };
  limits: Limits;
  expiries: string[];
  history: { snapshot_date: string; contracts: number; spot: number }[];
};
type OiChange = {
  enough: boolean;
  have?: number;
  dates: string[];
  date_from?: string;
  date_to?: string;
  span_days?: number | null;
  is_consecutive?: boolean;
  snapshots_between?: number;
  expired_excluded?: number;
  expired_oi?: number;
  incomplete_excluded?: number;
  incomplete_oi?: number;
  new_listings?: number;
  contracts_from?: number;
  contracts_to?: number;
  totals?: { call_change: number; put_change: number; contracts: number };
  gained?: OiRow[];
  lost?: OiRow[];
  note: string;
};
type OiRow = {
  expiry: string;
  type: string;
  strike: number;
  oi_from: number;
  oi_to: number;
  volume_to: number;
  change: number;
  change_pct: number | null;
};

function money(n: number | null): string {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
  return `${s}$${a.toFixed(0)}`;
}
function num(n: number | null): string {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}

export default function Flow() {
  const [tickerInput, setTickerInput] = useState("SPY");
  const [ticker, setTicker] = useState("SPY");
  const [dte, setDte] = useState<number | null>(7);
  const [tab, setTab] = useState<"unusual" | "biggest">("unusual");

  const [flow, setFlow] = useState<Flow | null>(null);
  const [oi, setOi] = useState<OiChange | null>(null);
  const [oiErr, setOiErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recordMsg, setRecordMsg] = useState<string | null>(null);

  const seq = useRef(0);

  const load = useCallback(async () => {
    const s = ++seq.current;
    setLoading(true);
    setErr(null);
    setFlow(null);
    setOi(null);
    setOiErr(null);
    try {
      const q = dte === null ? "" : `?dte_max=${dte}`;
      const r = await fetch(`/api/flow/${encodeURIComponent(ticker)}${q}`);
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as Flow;
      if (s !== seq.current) return;
      setFlow(d);
      // The OI change is an **enhanced view**: a failure affects that card alone and does not clear the main data.
      // ⚠️ But **it must not be silent**: leaving oi as null when the endpoint 500s makes the card
      //    read "not enough accrued yet" — reporting a server fault as an absence of history.
      try {
        const o = await fetch(`/api/flow/${encodeURIComponent(ticker)}/oi-change`);
        if (s !== seq.current) return;
        if (o.ok) {
          setOi((await o.json()) as OiChange);
        } else {
          setOiErr((await o.json()).detail ?? `HTTP ${o.status}`);
        }
      } catch (e2) {
        if (s !== seq.current) return;
        setOiErr(e2 instanceof Error ? e2.message : String(e2));
      }
    } catch (e) {
      if (s !== seq.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setFlow(null);
    } finally {
      if (s === seq.current) setLoading(false);
    }
  }, [ticker, dte]);

  useEffect(() => {
    void load();
  }, [load]);

  // ⚠️ Archiving is a **long request**, and the user may have switched symbol by the time it returns.
  //    The `load()` in the closure is bound to the old ticker — calling it directly would judge the in-flight
  //    new symbol's request stale and reload the old one: the box says QQQ while the cards show SPY.
  //    So the ticker is checked on return, and on a mismatch only the result is reported, with no write-back to the main data.
  const tickerRef = useRef(ticker);
  useEffect(() => {
    tickerRef.current = ticker;
  }, [ticker]);

  const record = useCallback(async () => {
    const mine = ticker;
    setRecording(true);
    setRecordMsg(null);
    try {
      const r = await fetch(`/api/flow/${encodeURIComponent(mine)}/record`, {
        method: "POST",
      });
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as { snapshot_date: string; recorded: number };
      if (tickerRef.current !== mine) {
        setRecordMsg(`The ${d.snapshot_date} snapshot for ${mine} was archived (${d.recorded} contracts), `
          + `but ${tickerRef.current} is on screen now, so this page was not refreshed.`);
        return;
      }
      setRecordMsg(`Archived ${d.recorded} contracts for the ${d.snapshot_date} trading session`);
      await load();
    } catch (e) {
      setRecordMsg(`Archiving failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRecording(false);
    }
  }, [ticker, load]);

  const rows = tab === "unusual" ? (flow?.unusual_rows ?? []) : (flow?.biggest_rows ?? []);
  const L = flow?.limits;

  /* Distribution by expiry */
  const expiryOption = useMemo(() => {
    if (!flow?.by_expiry.length) return null;
    const b = flow.by_expiry;
    return {
      backgroundColor: "transparent",
      grid: { left: 58, right: 18, top: 30, bottom: 40 },
      legend: { top: 0, textStyle: { color: "#6b665e", fontSize: 11 } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#1a1815",
        borderColor: "#e2ddd4",
        textStyle: { color: "#1a1815", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number }[]) =>
          `<b>${esc(ps[0]?.axisValue)}</b><br/>` +
          ps.map((p) => `${esc(p.seriesName)}: ${p.data.toLocaleString()} contracts`).join("<br/>"),
      },
      xAxis: {
        type: "category",
        data: b.map((x) => x.expiry),
        axisLine: { lineStyle: { color: "#e2ddd4" } },
        axisLabel: { color: "#6b665e", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#6b665e", fontSize: 10, formatter: (v: number) => num(v) },
        splitLine: { lineStyle: { color: "#e2ddd4", type: "dashed" } },
      },
      series: [
        {
          name: "Calls",
          type: "bar",
          stack: "v",
          data: b.map((x) => x.call_volume),
          itemStyle: { color: "#d4400d" },
        },
        {
          name: "Puts",
          type: "bar",
          stack: "v",
          data: b.map((x) => x.put_volume),
          itemStyle: { color: "#2563eb" },
        },
      ],
    };
  }, [flow]);

  /* Distribution by strike (puts drawn on the negative axis, to read the shape) */
  const strikeOption = useMemo(() => {
    if (!flow?.by_strike.rows.length) return null;
    const b = flow.by_strike.rows;
    return {
      backgroundColor: "transparent",
      grid: { left: 58, right: 18, top: 30, bottom: 40 },
      legend: { top: 0, textStyle: { color: "#6b665e", fontSize: 11 } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#1a1815",
        borderColor: "#e2ddd4",
        textStyle: { color: "#1a1815", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number }[]) =>
          `<b>Strike ${esc(ps[0]?.axisValue)}</b><br/>` +
          ps
            .map(
              (p) =>
                `${esc(p.seriesName)}: ${Math.abs(p.data).toLocaleString()} contracts`,
            )
            .join("<br/>"),
      },
      xAxis: {
        type: "category",
        data: b.map((x) => x.strike),
        axisLine: { lineStyle: { color: "#e2ddd4" } },
        axisLabel: { color: "#6b665e", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        axisLabel: {
          color: "#6b665e",
          fontSize: 10,
          formatter: (v: number) => num(Math.abs(v)),
        },
        splitLine: { lineStyle: { color: "#e2ddd4", type: "dashed" } },
      },
      series: [
        {
          name: "Call volume",
          type: "bar",
          data: b.map((x) => x.call_volume),
          itemStyle: { color: "#d4400d" },
        },
        {
          // Drawn on the negative axis purely to read the symmetry of the shape; the values themselves are positive (the tooltip takes the absolute)
          name: "Put volume",
          type: "bar",
          data: b.map((x) => -x.put_volume),
          itemStyle: { color: "#2563eb" },
        },
      ],
      markLine: undefined,
    };
  }, [flow]);

  return (
    <>
      <PageHead kicker="Flow · options flow" title="Unusual activity and positioning">
        Cboe's official delayed options chain. <b className="text-ink">It runs on your own machine only</b> —
        this is a tier C source, and showing it externally in any form triggers OPRA redistributor status.
      </PageHead>

      {/* ⭐ The limits, stated up front rather than buried in a footnote */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          What this section cannot do, said plainly first
        </div>
        <p className="mb-1.5">
          <Emph>{L?.no_tape}</Emph>
        </p>
        <p className="mb-1.5">
          <Emph>{L?.no_direction}</Emph>
        </p>
        <p>
          <Emph>{L?.delayed}</Emph>
        </p>
      </div>

      <Card
        title={flow ? `${flow.ticker} · $${flow.spot.toFixed(2)}` : "Options flow"}
        sub={
          flow
            ? `Trading session ${flow.session ?? "—"} · published by Cboe at ${flow.timestamp ?? "—"}`
            : "Enter a ticker to load"
        }
        right={
          <div className="flex items-center gap-2">
            <input
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value.toUpperCase())}
              onKeyDown={(e) => {
                if (e.key === "Enter") setTicker(tickerInput.trim() || "SPY");
              }}
              placeholder="SPY"
              className="w-24 rounded-lg border border-line bg-card2 px-2.5 py-1.5
                         text-xs uppercase text-ink placeholder:text-dim"
            />
            <select
              value={dte === null ? "all" : String(dte)}
              onChange={(e) =>
                setDte(e.target.value === "all" ? null : Number(e.target.value))
              }
              className="rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-ink"
            >
              <option value="0">0DTE</option>
              <option value="7">Within 7 days</option>
              <option value="30">Within 30 days</option>
              <option value="90">Within 90 days</option>
              <option value="all">Whole chain</option>
            </select>
            <button
              onClick={() => setTicker(tickerInput.trim() || "SPY")}
              disabled={loading}
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         transition hover:border-brand disabled:opacity-40"
            >
              {loading ? "Loading…" : "Query"}
            </button>
          </div>
        }
      >
        {err && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {err}
          </div>
        )}

        {flow && (
          <>
            <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
              <Stat
                label="Contracts traded"
                value={num(flow.counts.traded_contracts)}
                sub={`${num(flow.counts.total_volume)} contracts traded in total`}
              />
              <Stat
                label="Unusual contracts"
                value={num(flow.counts.unusual)}
                sub={`vol/OI ≥ ${flow.thresholds.unusual_ratio} and volume ≥ ${flow.thresholds.min_volume}`}
              />
              <Stat
                label="Zero prior open interest"
                value={num(flow.counts.zero_prior_oi)}
                sub="No open position at yesterday's close, and volume today"
              />
              <Stat
                label="Total open interest"
                value={num(flow.counts.total_oi)}
                sub={`All ${num(flow.counts.scope_contracts)} contracts in range · settled overnight`}
              />
            </div>

            {/* The three P/C bases */}
            <div className="mb-2 text-xs text-dim">
              Put/call ratio — <b className="text-ink">all three bases are given, and none is crowned "the" P/C</b>{" "}
              (they measure different things, and disagreeing is normal)
            </div>
            <div className="mb-4 grid grid-cols-1 gap-2.5 sm:grid-cols-3">
              {(
                [
                  ["by_volume", "On volume", ""],
                  ["by_oi", "On open interest", ""],
                  ["by_notional", "On premium (estimated)", ""],
                ] as const
              ).map(([k, title, hint]) => {
                const d = flow.ratios[k];
                return (
                  <div key={k} className="rounded-xl border border-line bg-card2/60 px-3 py-2.5">
                    <div className="text-[10px] uppercase tracking-wide text-dim">{title}</div>
                    <div className="mt-0.5 font-mono text-lg text-ink">
                      {d.pc === null ? "—" : d.pc.toFixed(3)}
                      {/* ⚠️ The direction has to be on the number itself. "Put/call ratio" is in the
                          heading above, but a bare 1.366 beside "calls $470M / puts $642M" leaves the
                          reader dividing the two to work out which way round it is. */}
                      {d.pc !== null && (
                        <span className="ml-1.5 font-sans text-[10px] text-dim">puts ÷ calls</span>
                      )}
                    </div>
                    <div className="mt-0.5 text-[10px] leading-relaxed text-dim">
                      Calls {k === "by_notional" ? money(d.call) : num(d.call)} / puts{" "}
                      {k === "by_notional" ? money(d.put) : num(d.put)}
                      <br />
                      <span className="opacity-70">
                        Basis: <Emph>{d.basis}</Emph>
                      </span>
                      {hint}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="mb-4 text-[10px] leading-relaxed text-dim">
              ⚠️ <b className="text-ink">"Premium" is an estimate, not the money actually traded</b>:
              a chain snapshot carries no per-print price, so this computes cumulative volume for the day × the mid
              <b className="text-ink"> at capture time</b> × 100. If 1,000 contracts traded at $1 in the morning and the mid is $5 by capture,
              this shows $500k against roughly $100k. Without a tape it cannot be exact, so it is only ever called an estimate.
              {flow.ratios.notional_excluded > 0 && (
                <>
                  {" "}A further {flow.ratios.notional_excluded} contracts have no quote at all, so not even an estimate is possible;
                  they are excluded from this basis (rather than counted as 0).
                </>
              )}
              {flow.ratios.one_sided_quotes > 0 && (
                <>
                  {" "}Of those, {flow.ratios.one_sided_quotes} have a
                  <b className="text-ink"> zero bid</b> (an ask with no bid = nobody buying),
                  and the mid reads optimistically for them — they are counted anyway, so this basis does not tilt
                  towards the in-the-money side, but it is worth knowing.
                </>
              )}
            </div>

            {/* Exposure */}
            <div className="mb-4 rounded-xl border border-line bg-card2/40 px-3 py-2.5">
              <div className="mb-1.5 flex flex-wrap gap-x-6 gap-y-1 text-xs">
                <span className="text-dim">
                  Absolute call |delta| exposure{" "}
                  <b className="font-mono text-ink">
                    {num(flow.exposure.call_delta_shares)} shares
                  </b>
                  <span className="text-dim">
                    {" "}
                    ({money(flow.exposure.call_delta_notional)})
                  </span>
                </span>
                <span className="text-dim">
                  Absolute put |delta| exposure{" "}
                  <b className="font-mono text-ink">
                    {num(flow.exposure.put_delta_shares)} shares
                  </b>
                  <span className="text-dim">
                    {" "}
                    ({money(flow.exposure.put_delta_notional)})
                  </span>
                </span>
              </div>
              <div className="text-[10px] leading-relaxed text-dim">
                <Emph>{flow.exposure.note}</Emph>
                {/* ⚠️ With everything missing the backend gives null and "—" is shown above.
                    This says explicitly that it is "not computable" rather than "zero exposure". */}
                {/* ⚠️ Reported per side: with puts complete and calls all missing,
                    one combined number reads as though the call side had "zero exposure". */}
                {(["call", "put"] as const).map((side) => {
                  const n =
                    side === "call"
                      ? flow.exposure.counted_delta_call
                      : flow.exposure.counted_delta_put;
                  const m =
                    side === "call"
                      ? flow.exposure.missing_delta_call
                      : flow.exposure.missing_delta_put;
                  const label = side === "call" ? "call" : "put";
                  if (n === 0 && m > 0)
                    return (
                      <b key={side} className="text-brand">
                        {" "}
                        All {m} contracts on the {label} side lack a delta, so the exposure
                        <b className="text-ink"> cannot be computed</b> (which is not zero).
                      </b>
                    );
                  if (m > 0)
                    return (
                      <span key={side}>
                        {" "}
                        ({m} on the {label} side lack a delta and are not counted; {n} are.)
                      </span>
                    );
                  return null;
                })}
              </div>
            </div>

            {/* Detail table */}
            <div className="mb-2 flex items-center gap-2">
              {(
                [
                  ["unusual", `Unusual (${flow.counts.unusual})`],
                  ["biggest", "Largest notional"],
                ] as const
              ).map(([k, label]) => (
                <button
                  key={k}
                  onClick={() => setTab(k)}
                  className={`rounded-lg px-2.5 py-1 text-xs transition ${
                    tab === k
                      ? "bg-brand/12 font-semibold text-brand"
                      : "text-dim hover:text-ink"
                  }`}
                >
                  {label}
                </button>
              ))}
              <span className="ml-auto text-[10px] text-dim">
                Both tables descend by estimated premium — "zero prior OI" is a badge, not a sort key
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-line text-dim">
                  <tr>
                    <Th>Expiry</Th>
                    <Th>DTE</Th>
                    <Th>Type</Th>
                    <Th>Strike</Th>
                    <Th>Volume</Th>
                    <Th>Open interest</Th>
                    <Th>vol/OI</Th>
                    <Th>Mid</Th>
                    <Th>Premium (est.)</Th>
                    <Th>IV</Th>
                    <Th>delta</Th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr
                      key={`${r.expiry}-${r.type}-${r.strike}`}
                      className="border-b border-line/50"
                    >
                      <Td className="font-mono">{r.expiry}</Td>
                      <Td className="text-dim">{r.dte}</Td>
                      <Td className={r.type === "put" ? "text-[#2563eb]" : "text-brand"}>
                        {r.type === "put" ? "Put" : "Call"}
                      </Td>
                      <Td className="font-mono">{r.strike.toFixed(1)}</Td>
                      <Td className="font-mono">{r.volume.toLocaleString()}</Td>
                      <Td className="font-mono">{r.open_interest.toLocaleString()}</Td>
                      <Td className="font-mono">
                        {r.zero_prior_oi ? (
                          <span
                            className="rounded bg-brand/15 px-1.5 py-0.5 text-[10px] text-brand"
                            title="Prior open interest was 0, so the ratio does not exist (it is not infinity)"
                          >
                            new
                          </span>
                        ) : r.vol_oi === null ? (
                          "—"
                        ) : (
                          r.vol_oi.toFixed(2)
                        )}
                      </Td>
                      <Td className="font-mono">{r.mid === null ? "—" : r.mid.toFixed(2)}</Td>
                      <Td className="font-mono">{money(r.notional)}</Td>
                      <Td className="font-mono text-dim">
                        {r.iv === null ? "—" : `${(r.iv * 100).toFixed(1)}%`}
                      </Td>
                      <Td className="font-mono text-dim">
                        {r.delta === null ? "—" : r.delta.toFixed(3)}
                      </Td>
                    </tr>
                  ))}
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={11} className="py-8 text-center text-dim">
                        No contracts match
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="mt-4 text-[10px] leading-relaxed text-dim">
              <Emph>{L?.vol_oi}</Emph> <Emph>{L?.oi_lag}</Emph>
            </div>
          </>
        )}
      </Card>

      {flow && (
        <Card title="Volume distribution" sub="Puts are drawn on the negative axis only to read the shape; the values themselves are positive">
          <div className="mb-1 text-xs text-dim">
            By expiry — <b className="text-ink">all strikes</b>
          </div>
          {expiryOption && <ReactECharts option={expiryOption} style={{ height: 240 }} notMerge />}
          {strikeOption && (
            <div className="mt-4">
              {/* ⚠️ The two charts cover different ranges, so their totals **necessarily** disagree.
                  Left unsaid, the user simply assumes one of them is miscomputed. */}
              <div className="mb-1 text-xs text-dim">
                By strike — drawn within ±{(flow.by_strike.window_pct * 100).toFixed(0)}% of spot only
                ({flow.by_strike.low.toFixed(0)} to {flow.by_strike.high.toFixed(0)})
                {flow.by_strike.dropped_contracts > 0 && (
                  <span className="text-dim">
                    ; the {flow.by_strike.dropped_contracts} contracts outside the window
                    ({num(flow.by_strike.dropped_volume)} contracts) are not drawn,
                    <b className="text-ink"> so its total does not match the chart above</b>
                  </span>
                )}
              </div>
              <ReactECharts option={strikeOption} style={{ height: 260 }} notMerge />
            </div>
          )}
        </Card>
      )}

      {/* ⭐ Locally accrued OI history */}
      <Card
        title="Open interest change (locally accrued)"
        sub={
          oi?.enough
            ? `${oi.date_from} → ${oi.date_to} (${oi.span_days} days apart)`
            : "This history cannot be backfilled; it only accrues, day by day, from installation"
        }
        right={
          <button
            onClick={() => void record()}
            disabled={recording || !flow}
            className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                       transition hover:border-brand disabled:opacity-40"
          >
            {recording ? "Archiving…" : "Archive this snapshot"}
          </button>
        }
      >
        {recordMsg && <div className="mb-3 text-xs text-dim">{recordMsg}</div>}

        {oiErr && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            <b className="text-brand">Could not fetch the open-interest change</b>
            <span className="text-dim">
              {" "}
              — {oiErr}. This is <b className="text-ink">an endpoint error</b>,
              not "not enough accrued yet".
            </span>
          </div>
        )}

        {!oiErr && !oi?.enough && (
          <div className="rounded-lg border border-line bg-card2/40 px-3 py-3 text-xs leading-relaxed text-dim">
            <Emph>{oi?.note}</Emph>
            {oi?.dates && oi.dates.length > 0 && (
              <div className="mt-2 font-mono text-[10px]">
                Snapshots held: {oi.dates.join(", ")}
              </div>
            )}
            <div className="mt-2 text-[10px]">
              ⚠️ What is shown here is <b className="text-ink">not enough accrued yet</b>,
              not "open interest did not change" — the two must be distinguishable in the interface.
            </div>
          </div>
        )}

        {oi?.enough && (
          <>
            {oi.is_consecutive === false && (
              <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
                The two snapshots are {oi.span_days} days apart
                {(oi.snapshots_between ?? 0) > 0 &&
                  `, with ${oi.snapshots_between} observations in between`}
                ,
                <span className="text-dim">
                  {" "}
                  so what follows is the <b className="text-ink">cumulative</b> change over that stretch, not a single day's.
                </span>
              </div>
            )}
            {(oi.incomplete_excluded ?? 0) > 0 && (
              <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
                <b className="text-brand">{oi.incomplete_excluded} contracts are not yet expired, yet absent from the end snapshot</b>
                <span className="text-dim">
                  ({num(oi.incomplete_oi ?? 0)} of open interest) — Cboe lists contracts through to expiry,
                  so this can only mean <b className="text-ink">that pull was incomplete</b>. They are excluded.
                  Contract counts across the two snapshots: {num(oi.contracts_from ?? 0)} → {num(oi.contracts_to ?? 0)};
                  a wide gap means this comparison cannot be trusted.
                </span>
              </div>
            )}
            {(oi.expired_excluded ?? 0) > 0 && (
              <div className="mb-3 text-[10px] leading-relaxed text-dim">
                Excluded {oi.expired_excluded} contracts that <b className="text-ink">expired during the period</b>{" "}
                ({num(oi.expired_oi ?? 0)} of open interest) — they left the chain because they expired,
                not because anyone closed out.
              </div>
            )}
            <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-3">
              <Stat
                label="Net change in call open interest"
                value={`${(oi.totals?.call_change ?? 0) > 0 ? "+" : ""}${num(oi.totals?.call_change ?? 0)}`}
                sub="contracts"
              />
              <Stat
                label="Net change in put open interest"
                value={`${(oi.totals?.put_change ?? 0) > 0 ? "+" : ""}${num(oi.totals?.put_change ?? 0)}`}
                sub="contracts"
              />
              <Stat label="Contracts involved" value={num(oi.totals?.contracts ?? 0)} sub="union of the two days" />
            </div>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {(
                [
                  ["Largest increases", oi.gained ?? []],
                  ["Largest decreases", oi.lost ?? []],
                ] as const
              ).map(([title, list]) => (
                <div key={title}>
                  <div className="mb-1 text-xs text-dim">{title}</div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <thead className="border-b border-line text-dim">
                        <tr>
                          <Th>Expiry</Th>
                          <Th>Type</Th>
                          <Th>Strike</Th>
                          <Th>Before</Th>
                          <Th>After</Th>
                          <Th>Change</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {list.slice(0, 12).map((r) => (
                          <tr
                            key={`${r.expiry}-${r.type}-${r.strike}`}
                            className="border-b border-line/50"
                          >
                            <Td className="font-mono">{r.expiry}</Td>
                            <Td className={r.type === "put" ? "text-[#2563eb]" : "text-brand"}>
                              {r.type === "put" ? "Put" : "Call"}
                            </Td>
                            <Td className="font-mono">{r.strike.toFixed(1)}</Td>
                            <Td className="font-mono text-dim">{num(r.oi_from)}</Td>
                            <Td className="font-mono text-dim">{num(r.oi_to)}</Td>
                            <Td
                              className={`font-mono ${r.change > 0 ? "text-ink" : "text-brand"}`}
                              title={
                                r.change_pct === null
                                  ? "Prior open interest was 0, so no percentage can be computed"
                                  : `${r.change_pct.toFixed(1)}%`
                              }
                            >
                              {r.change > 0 ? "+" : ""}
                              {num(r.change)}
                            </Td>
                          </tr>
                        ))}
                        {list.length === 0 && (
                          <tr>
                            <td colSpan={6} className="py-6 text-center text-dim">
                              None
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-3 text-[10px] leading-relaxed text-dim">
              <Emph>{oi.note}</Emph>
            </div>
          </>
        )}
      </Card>

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        Source: Cboe Global Markets delayed options quotes. ⛔ This page's data is for personal research on your own machine only;
        showing it externally makes you an OPRA redistributor ($1,500/month). This page presents values and
        makes no directional judgement or prediction.
      </p>
    </>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-line bg-card2/60 px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wide text-dim">{label}</div>
      <div className="mt-0.5 font-mono text-lg text-ink">{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-dim">{sub}</div>}
    </div>
  );
}
