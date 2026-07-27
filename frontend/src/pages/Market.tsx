import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/market.py) ── */
type Notes = { inversion: string; cot_lag: string; cot_scope: string };
type CurveLatest = {
  date: string;
  yields: Record<string, number | null>;
  raw: Record<string, number | null>;
  spreads: Record<string, number | null>;
  inverted: Record<string, boolean | null>;
};
type Curve = {
  year: number;
  years: number[];
  missing_years: number[];
  /** ⚠️ Years that failed to fetch — the backend still serves whatever is in the database,
   *  so this field **must be displayed**, or the user takes an old cache for the latest. */
  failed: Record<string, string>;
  fetched: Record<string, number>;
  cache: { rows: number; earliest: string | null; latest: string | null;
           years: number; last_fetch: string | null };
  dates: string[];
  spreads: Record<string, (number | null)[]>;
  latest: CurveLatest | null;
  tenors: string[];
  notes: Notes;
};
type MarketRow = { market: string; last_date: string | null; reports: number };
type MarketList = {
  markets: MarketRow[];
  total: number;
  active: number;
  latest_report: string | null;
  active_only: boolean;
};
type Cot = {
  market: string;
  report_date: string | null;
  lev_long: number | null;
  lev_short: number | null;
  lev_net: number | null;
  asset_long: number | null;
  asset_short: number | null;
  asset_net: number | null;
  dealer_long: number | null;
  dealer_short: number | null;
  open_interest: number | null;
};
type CotResp = {
  rows: Cot[];
  count: number;
  markets: string[];
  notes: Notes;
  scope: { market: string | null; exact: boolean; limit: number; truncated: boolean };
};

const SPREAD_KEYS = ["10Y-2Y", "10Y-3M", "30Y-10Y"];
const SPREAD_COLOR: Record<string, string> = {
  "10Y-2Y": "#ff5a1f",
  "10Y-3M": "#5b9cf7",
  "30Y-10Y": "#8e8a83",
};

function num(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}
function pct(n: number | null | undefined, digits = 2): string {
  return n === null || n === undefined ? "—" : `${n.toFixed(digits)}%`;
}

export default function Market() {
  const thisYear = new Date().getFullYear();
  const [years, setYears] = useState(3);
  const [curve, setCurve] = useState<Curve | null>(null);
  const [curveErr, setCurveErr] = useState<string | null>(null);
  const [curveLoading, setCurveLoading] = useState(false);

  const [markets, setMarkets] = useState<MarketList | null>(null);
  const [marketsErr, setMarketsErr] = useState<string | null>(null);
  const [picked, setPicked] = useState("");
  const [filter, setFilter] = useState("");
  const [cot, setCot] = useState<CotResp | null>(null);
  const [cotErr, setCotErr] = useState<string | null>(null);
  const [cotLoading, setCotLoading] = useState(false);

  // ⚠️ One sequence number per request type: the curve and COT are two independent loading lanes,
  //    and sharing a counter would judge an in-flight curve response "stale" when the market changes.
  const curveSeq = useRef(0);
  const cotSeq = useRef(0);
  const mktSeq = useRef(0);

  /* ── The yield curve ── */
  // `force` is true only when the user presses Refresh themselves.
  // ⚠️ Without it the backend simply reuses the cache — a button labelled "Refresh" that does nothing,
  //    while Treasury **does revise historical values**, so the user never gets the revised figures.
  const loadCurve = useCallback(async (force = false) => {
    const seq = ++curveSeq.current;
    setCurveLoading(true);
    setCurveErr(null);
    try {
      const r = await fetch(
        `/api/market/curve?years=${years}${force ? "&refresh=true" : ""}`);
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as Curve;
      if (seq !== curveSeq.current) return;
      setCurve(d);
    } catch (e) {
      if (seq !== curveSeq.current) return;
      setCurveErr(e instanceof Error ? e.message : String(e));
      setCurve(null);
    } finally {
      if (seq === curveSeq.current) setCurveLoading(false);
    }
  }, [years]);

  useEffect(() => {
    void loadCurve();
  }, [loadCurve]);

  /* ── The COT market list (loaded once) ── */
  // ⚠️ Errors **must not be swallowed** here: without the list the dropdown is empty,
  //    and "an empty dropdown" looks exactly like "CFTC is unreachable" in the interface.
  // ⚠️ The Retry button can be pressed repeatedly, so this needs a sequence number too: an earlier request
  //    returning later would clear a later request's success back into a failure state.
  const loadMarkets = useCallback(async () => {
    const seq = ++mktSeq.current;
    setMarketsErr(null);
    try {
      const r = await fetch("/api/market/cot/markets");
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as MarketList;
      if (seq !== mktSeq.current) return;
      setMarkets(d);
      // Default to something most people recognise: E-MINI S&P 500 first
      const pref =
        d.markets.find((m) => m.market.startsWith("E-MINI S&P 500")) ?? d.markets[0];
      if (pref) setPicked(pref.market);
    } catch (e) {
      if (seq !== mktSeq.current) return;
      setMarketsErr(e instanceof Error ? e.message : String(e));
      setMarkets(null);
    }
  }, []);

  useEffect(() => {
    void loadMarkets();
  }, [loadMarkets]);

  /* ── The COT time series ── */
  const loadCot = useCallback(async () => {
    if (!picked) return;
    const seq = ++cotSeq.current;
    setCotLoading(true);
    setCotErr(null);
    // ⚠️ It has to be cleared first: with the dropdown already showing contract B while the cards, chart and table
    //    still hold A's numbers, it reads as "B's positioning". On a slow connection that mismatch lasts until the request times out.
    setCot(null);
    try {
      // ⚠️ exact=true: a fuzzy match kneads different contracts into one sawtooth line
      const q = new URLSearchParams({ market: picked, exact: "true", limit: "160" });
      const r = await fetch(`/api/market/cot?${q}`);
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as CotResp;
      if (seq !== cotSeq.current) return;
      setCot(d);
    } catch (e) {
      if (seq !== cotSeq.current) return;
      setCotErr(e instanceof Error ? e.message : String(e));
      setCot(null);
    } finally {
      if (seq === cotSeq.current) setCotLoading(false);
    }
  }, [picked]);

  useEffect(() => {
    void loadCot();
  }, [loadCot]);

  const shown = useMemo(() => {
    const all = markets?.markets ?? [];
    const f = filter.trim().toUpperCase();
    return f ? all.filter((m) => m.market.toUpperCase().includes(f)) : all;
  }, [markets, filter]);

  /* ── Chart: the spread time series ── */
  const spreadOption = useMemo(() => {
    if (!curve) return null;
    return {
      backgroundColor: "transparent",
      grid: { left: 52, right: 18, top: 34, bottom: 46 },
      legend: {
        top: 0,
        textStyle: { color: "#8e8a83", fontSize: 11 },
        data: SPREAD_KEYS,
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number | null }[]) => {
          const head = `<b>${esc(ps[0]?.axisValue)}</b>`;
          const body = ps
            .map((p) => {
              const v = p.data;
              const tag =
                v === null ? "" : v < 0 ? " <span style='color:#ff5a1f'>inverted</span>" : "";
              return `${esc(p.seriesName)}: ${v === null ? "—" : `${v.toFixed(2)}%`}${tag}`;
            })
            .join("<br/>");
          return `${head}<br/>${body}`;
        },
      },
      xAxis: {
        type: "category",
        data: curve.dates,
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        name: "Spread %",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        axisLabel: { color: "#8e8a83", fontSize: 10, formatter: "{value}" },
        splitLine: { lineStyle: { color: "#2a2a31", type: "dashed" } },
      },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 8 }],
      series: SPREAD_KEYS.map((k) => ({
        name: k,
        type: "line",
        data: curve.spreads[k] ?? [],
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: k === "30Y-10Y" ? 1 : 1.6, color: SPREAD_COLOR[k] },
        itemStyle: { color: SPREAD_COLOR[k] },
        // The zero line: below it is inversion. Drawn as a mark line rather than left to the eye.
        markLine:
          k === "10Y-2Y"
            ? {
                silent: true,
                symbol: "none",
                label: { show: false },
                lineStyle: { color: "#8e8a83", type: "dashed", width: 1 },
                data: [{ yAxis: 0 }],
              }
            : undefined,
      })),
    };
  }, [curve]);

  /* ── Chart: the term structure on the day ── */
  const shapeOption = useMemo(() => {
    const L = curve?.latest;
    if (!L) return null;
    const labels = curve!.tenors;
    const vals = labels.map((t) => L.yields[t] ?? null);
    return {
      backgroundColor: "transparent",
      grid: { left: 48, right: 18, top: 22, bottom: 30 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; data: number | null }[]) =>
          `${esc(ps[0]?.axisValue)}: ${ps[0]?.data === null ? "—" : `${ps[0].data}%`}`,
      },
      xAxis: {
        type: "category",
        data: labels,
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: "#8e8a83", fontSize: 10, formatter: "{value}%" },
        splitLine: { lineStyle: { color: "#2a2a31", type: "dashed" } },
      },
      series: [
        {
          type: "line",
          data: vals,
          smooth: true,
          connectNulls: true,
          symbolSize: 6,
          lineStyle: { color: "#ff5a1f", width: 2 },
          itemStyle: { color: "#ff5a1f" },
          areaStyle: { color: "rgba(255,90,31,0.10)" },
        },
      ],
    };
  }, [curve]);

  /* ── Chart: COT net positioning ── */
  const cotOption = useMemo(() => {
    if (!cot || !cot.rows.length) return null;
    // The backend returns newest first; the chart needs oldest first
    const rows = [...cot.rows].reverse();
    const dates = rows.map((r) => r.report_date ?? "");
    return {
      backgroundColor: "transparent",
      grid: { left: 62, right: 18, top: 34, bottom: 46 },
      legend: {
        top: 0,
        textStyle: { color: "#8e8a83", fontSize: 11 },
        data: ["Leveraged funds net", "Asset managers net"],
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number | null }[]) =>
          `<b>${esc(ps[0]?.axisValue)}</b> (Tuesday's positions)<br/>` +
          ps
            .map(
              (p) =>
                `${esc(p.seriesName)}: ${p.data === null ? "—" : p.data.toLocaleString()} contracts`,
            )
            .join("<br/>"),
      },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        name: "Net position (contracts)",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        axisLabel: {
          color: "#8e8a83",
          fontSize: 10,
          formatter: (v: number) => num(v),
        },
        splitLine: { lineStyle: { color: "#2a2a31", type: "dashed" } },
      },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 8 }],
      series: [
        {
          name: "Leveraged funds net",
          type: "line",
          data: rows.map((r) => r.lev_net),
          showSymbol: false,
          connectNulls: false,
          lineStyle: { color: "#ff5a1f", width: 1.6 },
          itemStyle: { color: "#ff5a1f" },
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { color: "#8e8a83", type: "dashed", width: 1 },
            data: [{ yAxis: 0 }],
          },
        },
        {
          name: "Asset managers net",
          type: "line",
          data: rows.map((r) => r.asset_net),
          showSymbol: false,
          connectNulls: false,
          lineStyle: { color: "#5b9cf7", width: 1.6 },
          itemStyle: { color: "#5b9cf7" },
        },
      ],
    };
  }, [cot]);

  const latest = curve?.latest;
  const notes = curve?.notes ?? cot?.notes;

  return (
    <>
      <PageHead kicker="Market · macro" title="Yield curve and positioning report">
        Public data from the US Treasury and the CFTC — the
        <b className="text-ink"> cleanest lane in the project</b>: government works, no restriction on commercial use, freely redistributable,
        with no OPRA, no §13107 and no FINRA terms.
      </PageHead>

      {/* ── Compliance and definitions ── */}
      <div className="mb-5 rounded-2xl border border-line bg-card2/60 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          Tier S · definitions
        </div>
        <p className="mb-1.5">
          <b className="text-ink">Which inversion?</b>{" "}
          <Emph>
            {notes?.inversion ??
              "10Y−2Y and 10Y−3M are two different spreads and can invert months apart. This page shows both."}
          </Emph>
        </p>
        <p>
          <b className="text-ink">COT's three-day lag:</b>{" "}
          <Emph>{notes?.cot_lag ?? "It reports Tuesday's closing positions and is published on Friday afternoon."}</Emph>{" "}
          <Emph>{notes?.cot_scope}</Emph>
        </p>
      </div>

      {/* ═══ The yield curve ═══ */}
      <Card
        title="US Treasury yield curve"
        sub={
          curve
            ? `${curve.years[0]}–${curve.year} · ${curve.dates.length} trading days` +
              (curve.missing_years.length ? ` · missing ${curve.missing_years.join("/")}` : "")
            : "U.S. Treasury official daily data"
        }
        right={
          <div className="flex items-center gap-2">
            <select
              value={years}
              onChange={(e) => setYears(Number(e.target.value))}
              className="rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-ink"
            >
              {[1, 2, 3, 5, 10].map((y) => (
                <option key={y} value={y}>
                  Last {y} years
                </option>
              ))}
            </select>
            <button
              onClick={() => void loadCurve(true)}
              disabled={curveLoading}
              title="Force a refetch from Treasury (which does revise historical values), bypassing the local cache"
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         transition hover:border-brand disabled:opacity-40"
            >
              {curveLoading ? "Fetching…" : "Refetch"}
            </button>
          </div>
        }
      >
        {curveErr && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {curveErr}
          </div>
        )}

        {/* ⚠️ When some years fail to fetch, what is shown below is **local, older data**.
            That has to be said — or "latest 2026-07-24" is taken for something fetched just now. */}
        {curve && Object.keys(curve.failed).length > 0 && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            <div className="font-semibold text-brand">
              These years failed to fetch, so the chart uses local data that may not be current:
            </div>
            <ul className="mt-1 space-y-0.5 text-dim">
              {Object.entries(curve.failed).map(([y, msg]) => (
                <li key={y}>
                  · <span className="font-mono text-ink">{y}</span>: {msg}
                </li>
              ))}
            </ul>
          </div>
        )}
        {curve && curve.missing_years.length > 0 && (
          <div className="mb-3 text-[10px] text-dim">
            Treasury has no data for {curve.missing_years.join(", ")} (gaps in the early years are normal,
            and different from a failed fetch).
          </div>
        )}

        {latest && (
          <>
            <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
              <Stat label={`Latest ${latest.date}`} value={pct(latest.yields["10Y"])} sub="10Y" />
              {SPREAD_KEYS.map((k) => {
                const v = latest.spreads[k];
                const inv = latest.inverted[k];
                return (
                  <Stat
                    key={k}
                    label={k}
                    value={v === null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`}
                    sub={inv === null ? "data missing" : inv ? "inverted" : "not inverted"}
                    warn={inv === true}
                  />
                );
              })}
            </div>

            <div className="mb-1 text-xs text-dim">Spread history (the dashed line is zero; below it is inversion)</div>
            {spreadOption && (
              <ReactECharts option={spreadOption} style={{ height: 300 }} notMerge />
            )}

            <div className="mb-1 mt-4 text-xs text-dim">
              Term structure on {latest.date} (the x axis is maturity, not time)
            </div>
            {shapeOption && (
              <ReactECharts option={shapeOption} style={{ height: 220 }} notMerge />
            )}

            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-line text-dim">
                  <tr>
                    <Th>Maturity</Th>
                    {curve!.tenors.map((t) => (
                      <Th key={t}>{t}</Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-b border-line/50">
                    <Td className="text-dim">Yield</Td>
                    {curve!.tenors.map((t) => (
                      <Td key={t} className="font-mono">
                        {pct(latest.yields[t])}
                      </Td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>
          </>
        )}
        {!latest && !curveErr && !curveLoading && (
          <div className="py-8 text-center text-xs text-dim">No data</div>
        )}
      </Card>

      {/* ═══ COT ═══ */}
      <Card
        title="CFTC positioning report (TFF · financial futures)"
        sub={
          markets
            ? `${markets.active} contracts reporting · newest report ${markets.latest_report ?? "—"} (Tuesday's positions)`
            : marketsErr
              ? "Could not fetch the contract list"
              : "Traders in Financial Futures"
        }
        right={
          <button
            onClick={() => void loadCot()}
            disabled={cotLoading || !picked}
            className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                       transition hover:border-brand disabled:opacity-40"
          >
            {cotLoading ? "Loading…" : "Refresh"}
          </button>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter contract names (S&P / TREASURY / VIX…)"
            className="w-56 rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs
                       text-ink placeholder:text-dim"
          />
          <select
            value={picked}
            onChange={(e) => setPicked(e.target.value)}
            className="min-w-0 max-w-full flex-1 rounded-lg border border-line bg-card2
                       px-2.5 py-1.5 text-xs text-ink"
          >
            {shown.length === 0 && (
              <option value="">
                {marketsErr ? "Could not fetch the list" : markets ? "No matching contract" : "Loading…"}
              </option>
            )}
            {shown.map((m) => (
              <option key={m.market} value={m.market}>
                {m.market}
              </option>
            ))}
          </select>
          <span className="font-mono text-[10px] text-dim">
            {shown.length}/{markets?.active ?? 0}
          </span>
        </div>

        {marketsErr && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            <span className="font-semibold text-brand">The contract list could not be fetched</span>
            <span className="text-dim">
              {" "}
              — {marketsErr}. This is <b className="text-ink">a failed fetch</b>,
              not "CFTC has no contracts".
            </span>
            <button
              onClick={() => void loadMarkets()}
              className="ml-2 rounded border border-line px-2 py-0.5 text-[10px] text-ink
                         hover:border-brand"
            >
              Retry
            </button>
          </div>
        )}
        {cotErr && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {cotErr}
          </div>
        )}

        {cot && cot.rows.length > 0 && (
          <>
            {(() => {
              const r = cot.rows[0];
              return (
                <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
                  <Stat label="Report date (Tuesday)" value={r.report_date ?? "—"} sub="published Friday" />
                  <Stat
                    label="Leveraged funds net"
                    value={num(r.lev_net)}
                    sub={`${num(r.lev_long)} long / ${num(r.lev_short)} short`}
                  />
                  <Stat
                    label="Asset managers net"
                    value={num(r.asset_net)}
                    sub={`${num(r.asset_long)} long / ${num(r.asset_short)} short`}
                  />
                  <Stat label="Total open interest" value={num(r.open_interest)} sub="open interest" />
                </div>
              );
            })()}

            <div className="mb-1 text-xs text-dim">
              Net positioning over time (positive = net long, negative = net short; the dashed line is zero)
            </div>
            {cotOption && <ReactECharts option={cotOption} style={{ height: 300 }} notMerge />}

            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-line text-dim">
                  <tr>
                    <Th>Report date</Th>
                    <Th>Lev. long</Th>
                    <Th>Lev. short</Th>
                    <Th>Lev. net</Th>
                    <Th>AM long</Th>
                    <Th>AM short</Th>
                    <Th>AM net</Th>
                    <Th>Dealer long</Th>
                    <Th>Dealer short</Th>
                    <Th>Open interest</Th>
                  </tr>
                </thead>
                <tbody>
                  {cot.rows.slice(0, 30).map((r) => (
                    <tr key={r.report_date ?? Math.random()} className="border-b border-line/50">
                      <Td className="font-mono">{r.report_date ?? "—"}</Td>
                      <Td className="font-mono">{num(r.lev_long)}</Td>
                      <Td className="font-mono">{num(r.lev_short)}</Td>
                      <Td
                        className={`font-mono ${
                          (r.lev_net ?? 0) < 0 ? "text-brand" : "text-ink"
                        }`}
                      >
                        {num(r.lev_net)}
                      </Td>
                      <Td className="font-mono">{num(r.asset_long)}</Td>
                      <Td className="font-mono">{num(r.asset_short)}</Td>
                      <Td
                        className={`font-mono ${
                          (r.asset_net ?? 0) < 0 ? "text-brand" : "text-ink"
                        }`}
                      >
                        {num(r.asset_net)}
                      </Td>
                      <Td className="font-mono">{num(r.dealer_long)}</Td>
                      <Td className="font-mono">{num(r.dealer_short)}</Td>
                      <Td className="font-mono">{num(r.open_interest)}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {cot.rows.length > 30 && (
                <div className="mt-2 text-[10px] text-dim">
                  The table lists the last 30 reports; the chart shows all {cot.rows.length}
                </div>
              )}
            </div>
          </>
        )}
        {cot && cot.rows.length === 0 && !cotErr && (
          <div className="py-8 text-center text-xs text-dim">No records for this contract</div>
        )}
      </Card>

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        Sources: U.S. Department of the Treasury (Daily Treasury Par Yield Curve Rates) ·
        U.S. Commodity Futures Trading Commission (Traders in Financial Futures).
        Both are US government works in the public domain. This page presents values and makes no judgement or prediction.
        The current year is {thisYear}.
      </p>
    </>
  );
}

/** A small metric block. */
function Stat({
  label,
  value,
  sub,
  warn,
}: {
  label: string;
  value: string;
  sub?: string;
  warn?: boolean;
}) {
  return (
    <div className="rounded-xl border border-line bg-card2/60 px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wide text-dim">{label}</div>
      <div className={`mt-0.5 font-mono text-lg ${warn ? "text-brand" : "text-ink"}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[10px] text-dim">{sub}</div>}
    </div>
  );
}
