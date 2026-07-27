import { useEffect, useMemo, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead } from "../components/Shell";

/* ── Types (aligned with the backend's greeks.to_dict) ── */
type StrikeRow = {
  strike: number;
  call_gex_bn: number;
  put_gex_bn: number;
  net_gex_bn: number;
  call_oi: number;
  put_oi: number;
};
type GexData = {
  ticker: string;
  spot: number;
  total_gex_bn: number;
  gamma_flip: number | null;
  call_wall: number | null;
  put_wall: number | null;
  regime: "positive" | "negative" | "neutral";
  by_strike: StrikeRow[];
  by_expiry: { expiry: string; gex_bn: number }[];
  meta: {
    convention: string;
    convention_note: string;
    scope: string;
    /** The stable storage key (no contract count) — history must be fetched with this, never with scope */
    scope_key: string;
    unit: string;
  };
  timestamp?: string;
};
type CurvePoint = { price: number; gex_bn: number };
type Exposures = {
  total_vanna_mm: number;
  total_charm_mm: number;
  vanna_by_strike: { strike: number; vanna_mm: number }[];
  charm_by_strike: { strike: number; charm_mm: number }[];
  meta: { vanna_unit: string; charm_unit: string; note: string };
};
type Surface = {
  expiries: string[];
  strikes: number[];
  data: [number, number, number][];
  min: number;
  max: number;
  scope: string;
};

type HistRow = {
  captured_at: string;
  scope: string;
  spot: number;
  total_gex: number;
  gamma_flip: number | null;
  regime: string;
};

const PRESETS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "IWM"];
const DTE_OPTS = [
  { v: 0, label: "0DTE" },
  { v: 7, label: "≤7d" },
  { v: 30, label: "≤30d" },
  { v: 0, label: "All", all: true },
];

/** Fetch an enhanced view's JSON; failure always returns null (each card says so itself) rather than propagating and emptying the main data. */
async function pickJson<T>(r: PromiseSettledResult<Response>): Promise<T | null> {
  if (r.status !== "fulfilled" || !r.value.ok) return null;
  try {
    return (await r.value.json()) as T;
  } catch {
    return null;
  }
}

/** Scope → query string. **Every endpoint shares this one function**, so no two places can draw different scopes. */
function scopeQuery(dte: number | null): string {
  if (dte === null) return "";
  return dte === 0 ? "&expiry=0DTE" : `&dte_max=${dte}`;
}

export default function Gex() {
  const [ticker, setTicker] = useState("SPY");
  const [input, setInput] = useState("SPY");
  const [dte, setDte] = useState<number | null>(7);
  const [data, setData] = useState<GexData | null>(null);
  const [curve, setCurve] = useState<CurvePoint[]>([]);
  const [exp, setExp] = useState<Exposures | null>(null);
  const [surface, setSurface] = useState<Surface | null>(null);
  const [hist, setHist] = useState<HistRow[]>([]);
  const [histBusy, setHistBusy] = useState(false);
  const [histMsg, setHistMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      setExp(null);
      setSurface(null);
      try {
        // ⚠️ Both endpoints must use **the same filter conditions**, or the flip on the metric card
        // and the curve's zero come from different contract sets, and the curve does not cross 0 at the flip it is labelled with.
        // The 0DTE option goes through the expiry parameter (both endpoints support it); the rest through dte_max
        const q = scopeQuery(dte);
        const [a, b] = await Promise.all([
          fetch(`/api/gex/${ticker}?strike_pct=0.05${q}`),
          fetch(`/api/gex/${ticker}/curve?points=60&strike_pct=0.05${q}`),
        ]);
        if (!a.ok) throw new Error((await a.json()).detail ?? `HTTP ${a.status}`);
        // A failed curve request has to be reported too — swallowing it into an empty array shows an empty chart
        // while presenting the whole load as a success, hiding a network or backend fault.
        if (!b.ok) throw new Error((await b.json()).detail ?? `Curve request failed, HTTP ${b.status}`);
        const gex = (await a.json()) as GexData;
        const cv = (await b.json()).curve as CurvePoint[];
        if (!cancelled) {
          setData(gex);
          setCurve(cv);
        }
        // Exposure and surface are **enhanced views**: a failure empties that card and says so there,
        // without blocking the main data already fetched (and without silence — the card explains what could not be fetched).
        // ⚠️ allSettled rather than all: these three are **enhanced views**, and any network fault
        // (a rejected fetch, a JSON parse failure) reaching the outer catch would clear the main data
        // already fetched — letting one optional card's failure overturn the whole page.
        // ⚠️ History must be filtered on **scope_key** (the stable key): a scope carrying the contract count
        // lands in a different group every day, and no filter at all mixes ≤7DTE with the whole chain into a meaningless sawtooth.
        const [ex, sf, h] = await Promise.allSettled([
          fetch(`/api/exposures/${ticker}?strike_pct=0.05${q}`),
          fetch(`/api/gex/${ticker}/surface?dte_max=45`),
          fetch(`/api/history/${ticker}?scope=${encodeURIComponent(gex.meta.scope_key)}`),
        ]);
        if (!cancelled) {
          setExp(await pickJson<Exposures>(ex));
          setSurface(await pickJson<Surface>(sf));
          const hj = await pickJson<{ series: HistRow[] }>(h);
          setHist(hj?.series ?? []);
        }
      } catch (e) {
        if (!cancelled) {
          setErr(e instanceof Error ? e.message : String(e));
          setData(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ticker, dte]);

  /* ── Capture one snapshot ── */
  async function snapshot() {
    setHistBusy(true);
    setHistMsg(null);
    try {
      // It shares the main endpoint's filter parameters, so the scope stored is the scope on screen
      const r = await fetch(
        `/api/history/snapshot/${ticker}?strike_pct=0.05${scopeQuery(dte)}`,
        { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const j = await r.json();
      // Stored by **the data's own instant**, so "nothing added" means Cboe has not published anything new, not that something went wrong
      setHistMsg(
        j.created
          ? `One observation recorded (data instant ${j.captured_at ?? "unknown"})`
          : `Upstream has not updated yet (still ${j.captured_at}); nothing recorded twice`,
      );
      const h = await fetch(
        `/api/history/${ticker}?scope=${encodeURIComponent(j.scope_key)}`);
      if (h.ok) setHist((await h.json()).series as HistRow[]);
    } catch (e) {
      setHistMsg(`Capture failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setHistBusy(false);
    }
  }

  /* ── The historical series (oldest to newest) ── */
  const histOption = useMemo(() => {
    // Fewer than 2 points draws no trend: a single point degenerates the dual Y axes into an absurd auto-range (one point stretched over 300~1200),
    // and "one observation on day one" is exactly what a self-hosting user must go through — so this states it in words instead.
    if (hist.length < 2) return {};
    const rows = [...hist].reverse();
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 62, top: 30, bottom: 44 },
      tooltip: { trigger: "axis", backgroundColor: "#131316",
        borderColor: "#2a2a31", textStyle: { color: "#f2efe9", fontSize: 12 } },
      legend: { data: ["Total GEX", "Spot"], textStyle: { color: "#8e8a83", fontSize: 11 },
        top: 2, left: "center", itemWidth: 12, itemHeight: 8 },
      xAxis: { type: "category",
        data: rows.map((r) => r.captured_at.replace("T", " ").slice(5, 16)),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 9, fontFamily: "JetBrains Mono" } },
      yAxis: [
        { type: "value", name: "B$",
          nameTextStyle: { color: "#8e8a83", fontSize: 10 },
          splitLine: { lineStyle: { color: "#1e1e24" } },
          axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" } },
        { type: "value", name: "$", scale: true,
          nameTextStyle: { color: "#8e8a83", fontSize: 10 },
          splitLine: { show: false },
          axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" } },
      ],
      series: [
        // ⚠️ No smoothing: snapshots can be tens of minutes apart, and a spline fits movement between two points
        // that never happened (a spike and a fall). Straight segments are the honest rendering.
        { name: "Total GEX", type: "line", symbol: "circle", symbolSize: 5,
          lineStyle: { color: "#F35D2B", width: 2 }, itemStyle: { color: "#F35D2B" },
          data: rows.map((r) => +(r.total_gex / 1e9).toFixed(3)),
          markLine: { silent: true, symbol: "none",
            // The default label is off: it lands on the right-hand ticks and overlaps the spot scale
            label: { show: false },
            data: [{ yAxis: 0, lineStyle: { color: "#8e8a83", type: "dashed" } }] } },
        { name: "Spot", type: "line", yAxisIndex: 1, symbol: "circle", symbolSize: 3,
          lineStyle: { color: "#3b82f6", width: 1.5, opacity: 0.7 },
          data: rows.map((r) => r.spot) },
      ],
    };
  }, [hist]);

  /* ── GEX by strike ── */
  const strikeOption = useMemo(() => {
    if (!data) return {};
    const xs = data.by_strike.map((r) => r.strike);
    return {
      backgroundColor: "transparent",
      animation: false,   // financial charts need no animation; leaving it on gives an empty chart on first paint and in screenshots
      grid: { left: 62, right: 24, top: 34, bottom: 42 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const s = ps[0]?.axisValue;
          const row = data.by_strike.find((r) => String(r.strike) === String(s));
          if (!row) return "";
          return `<b>$${row.strike}</b><br/>
            Call GEX ${row.call_gex_bn.toFixed(3)} B<br/>
            Put GEX ${row.put_gex_bn.toFixed(3)} B<br/>
            <b>Net ${row.net_gex_bn.toFixed(3)} B</b><br/>
            <span style="color:#8e8a83">OI  C ${row.call_oi.toLocaleString()} / P ${row.put_oi.toLocaleString()}</span>`;
        },
      },
      legend: {
        data: ["Call GEX", "Put GEX"],
        textStyle: { color: "#8e8a83", fontSize: 11 },
        top: 4,
        right: 8,
        itemWidth: 12,
        itemHeight: 8,
      },
      xAxis: {
        type: "category",
        data: xs,
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      yAxis: {
        type: "value",
        name: "B$ / 1%",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          name: "Call GEX",
          type: "bar",
          stack: "gex",
          data: data.by_strike.map((r) => r.call_gex_bn),
          itemStyle: { color: "#22c55e" },
        },
        {
          name: "Put GEX",
          type: "bar",
          stack: "gex",
          data: data.by_strike.map((r) => r.put_gex_bn),
          itemStyle: { color: "#ef4444" },
          markLine: {
            silent: true,
            symbol: "none",
            data: [
              {
                xAxis: String(
                  xs.reduce((p, c) => (Math.abs(c - data.spot) < Math.abs(p - data.spot) ? c : p))
                ),
                lineStyle: { color: "#ff5a1f", width: 2, type: "solid" },
                label: { formatter: "Spot", color: "#ff5a1f", fontSize: 10 },
              },
            ],
          },
        },
      ],
    };
  }, [data]);

  /* ── The GEX curve against share price (visualising the gamma flip) ── */
  const curveOption = useMemo(() => {
    if (!data || !curve.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,   // financial charts need no animation; leaving it on gives an empty chart on first paint and in screenshots
      grid: { left: 62, right: 24, top: 30, bottom: 42 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const [px, gex] = ps[0].data as [number, number];
          return `Price <b>$${px.toFixed(2)}</b><br/>Total GEX <b>${gex.toFixed(3)} B</b>`;
        },
      },
      xAxis: {
        // ⚠️ It must be a value axis rather than a category axis: a category axis turns prices into string labels,
        // and on a low-priced stock (say $10) ±6% rounds to only two or three distinct labels,
        // distorting the spacing and putting the FLIP marker on the wrong sample point.
        type: "value",
        min: curve[0].price,
        max: curve[curve.length - 1].price,
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: {
          color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono",
          formatter: (v: number) => (v >= 100 ? v.toFixed(0) : v.toFixed(2)),
        },
      },
      yAxis: {
        type: "value",
        name: "B$ / 1%",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" },
      },
      series: [
        {
          type: "line",
          smooth: true,
          symbol: "none",
          // No visualMap for piecewise colouring: under ECharts 6, pieces do not take effect on one-dimensional line data
          // and the whole line fails to render. markArea distinguishes the positive and negative regions instead, with the line in the brand colour.
          lineStyle: { width: 3, color: "#ff5a1f" },
          areaStyle: {
            opacity: 0.15,
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(255,90,31,0.35)" },
                { offset: 1, color: "rgba(255,90,31,0.02)" },
              ],
            },
          },
          data: curve.map((p) => [p.price, p.gex_bn]),
          markLine: {
            silent: true,
            symbol: "none",
            data: [
              { yAxis: 0, lineStyle: { color: "#4a4a55", type: "dashed" }, label: { show: false } },
              // ⚠️ The flip may fall outside the curve's drawn range (±span_pct) —
              // reduce would then pick the outermost point and misreport the chart's edge as the crossing. Outside the range, it is not drawn.
              ...(data.gamma_flip &&
              data.gamma_flip >= curve[0].price &&
              data.gamma_flip <= curve[curve.length - 1].price
                ? [
                    {
                      // On a value axis the real flip price can be used directly, with no need to find the nearest sample
                      xAxis: data.gamma_flip,
                      lineStyle: { color: "#ff5a1f", width: 2 },
                      label: { formatter: "FLIP", color: "#ff5a1f", fontSize: 10 },
                    },
                  ]
                : []),
            ],
          },
        },
      ],
    };
  }, [data, curve]);

  /* ── Vanna / charm by strike ── */
  const expOption = useMemo(() => {
    // ⚠️ The empty array has to be guarded: where contracts exist but IV is missing or open interest is all 0, the backend honestly returns
    // a profile that succeeded with no usable rows. The reduce below has no initial value, and an empty array raises
    // "Reduce of empty array" — and since this sits inside useMemo, what crashes is the whole page, not this one card.
    if (!exp || !data || exp.vanna_by_strike.length === 0) return {};
    const strikes = exp.vanna_by_strike.map((r) => r.strike);
    const charmMap = new Map(exp.charm_by_strike.map((r) => [r.strike, r.charm_mm]));
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 24, top: 34, bottom: 42 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" },
        backgroundColor: "#131316", borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 } },
      legend: { data: ["Vanna", "Charm"], textStyle: { color: "#8e8a83", fontSize: 11 },
        top: 4, right: 8, itemWidth: 12, itemHeight: 8 },
      xAxis: { type: "category", data: strikes,
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" } },
      yAxis: { type: "value", name: "MM$",
        nameTextStyle: { color: "#8e8a83", fontSize: 10 },
        splitLine: { lineStyle: { color: "#1e1e24" } },
        axisLabel: { color: "#8e8a83", fontSize: 10, fontFamily: "JetBrains Mono" } },
      series: [
        { name: "Vanna", type: "bar", itemStyle: { color: "#3b82f6" },
          data: exp.vanna_by_strike.map((r) => r.vanna_mm) },
        { name: "Charm", type: "bar", itemStyle: { color: "#a78bfa" },
          data: strikes.map((k) => charmMap.get(k) ?? 0),
          markLine: { silent: true, symbol: "none", data: [{
            xAxis: String(strikes.reduce((p, c) =>
              Math.abs(c - data.spot) < Math.abs(p - data.spot) ? c : p)),
            lineStyle: { color: "#ff5a1f", width: 2 },
            label: { formatter: "Spot", color: "#ff5a1f", fontSize: 10 } }] } },
      ],
    };
  }, [exp, data]);

  /* ── The two-dimensional expiry × strike surface ── */
  const surfaceOption = useMemo(() => {
    if (!surface || surface.data.length === 0) return {};
    // ⚠️ The GEX surface is an extreme long-tail distribution (measured on SPY: median 0.002, max 0.89, a factor of 400).
    // A linear colour scale flattens 95% of the cells to one colour — the chart looks populated while no structure can be read from it.
    // It is capped at p95, with the legend saying so, rather than hiding the clipping.
    const mags = surface.data.map((d) => Math.abs(d[2])).sort((a, b) => a - b);
    const p95 = mags[Math.floor(mags.length * 0.95)] || 0;
    const peak = Math.max(Math.abs(surface.min), Math.abs(surface.max)) || 1;
    const bound = p95 > 0 ? p95 : peak;
    const clipped = bound < peak;
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 78, top: 20, bottom: 70 },
      tooltip: {
        backgroundColor: "#131316", borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (p: any) => {
          const [x, y, v] = p.data as [number, number, number];
          const over = Math.abs(v) > bound ? '  <span style="color:#ff5a1f">beyond the scale</span>' : "";
          return `Expiry <b>${surface.expiries[x]}</b><br/>Strike <b>$${surface.strikes[y]}</b><br/>GEX <b>${v.toFixed(3)} B</b>${over}`;
        },
      },
      xAxis: { type: "category", data: surface.expiries.map((e) => e.slice(5)),
        splitArea: { show: false },
        axisLabel: { color: "#8e8a83", fontSize: 9, fontFamily: "JetBrains Mono", rotate: 45 } },
      yAxis: { type: "category", data: surface.strikes.map(String),
        splitArea: { show: false },
        axisLabel: { color: "#8e8a83", fontSize: 9, fontFamily: "JetBrains Mono" } },
      visualMap: {
        min: -bound, max: bound, calculable: true, orient: "vertical",
        right: 8, top: "middle", itemHeight: 160,
        textStyle: { color: "#8e8a83", fontSize: 10 },
        // The midpoint colour is the card's own background, so near-zero cells fade out and only real exposure shows
        inRange: { color: ["#ef4444", "#131316", "#22c55e"] },
        formatter: (v: number) =>
          clipped && Math.abs(Math.abs(v) - bound) < 1e-9
            ? `${v > 0 ? "≥" : "≤"}${v.toFixed(2)}`
            : v.toFixed(2),
      },
      series: [{ type: "heatmap", data: surface.data, progressive: 2000,
        itemStyle: { borderWidth: 0 } }],
    };
  }, [surface]);

  const regime = data?.regime;
  const neg = regime === "negative";
  const neutral = regime === "neutral";

  return (
    <>
      <PageHead kicker="Gamma Exposure" title="GEX — gamma exposure">
        Having sold options, market makers must hedge, buying and selling stock on every move. GEX measures
        <b className="text-ink"> how much notional stock dealers have to buy or sell for each 1% move in the price</b>.
        Positive GEX suppresses volatility; negative GEX amplifies it.
      </PageHead>

        {/* Controls */}
        <div className="mb-5 flex flex-wrap items-center gap-2">
          {PRESETS.map((t) => (
            <button
              key={t}
              onClick={() => {
                setTicker(t);
                setInput(t);
              }}
              className={`rounded-lg border px-3.5 py-2 font-mono text-sm transition ${
                ticker === t
                  ? "border-brand bg-brand text-black font-bold"
                  : "border-line bg-card2 text-ink hover:border-[#3d3d46]"
              }`}
            >
              {t}
            </button>
          ))}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const v = input.trim().toUpperCase();
              if (v) setTicker(v);
            }}
            className="flex items-center gap-2"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Other ticker"
              className="w-28 rounded-lg border border-line bg-card2 px-3 py-2 font-mono text-sm outline-none focus:border-brand"
            />
          </form>

          <span className="mx-1 h-6 w-px bg-line" />

          {DTE_OPTS.map((o) => {
            const on = o.all ? dte === null : dte === o.v;
            return (
              <button
                key={o.label}
                onClick={() => setDte(o.all ? null : o.v)}
                className={`rounded-lg border px-3 py-2 text-sm transition ${
                  on
                    ? "border-brand bg-brand/10 text-brand"
                    : "border-line bg-card2 text-dim hover:border-[#3d3d46]"
                }`}
              >
                {o.label}
              </button>
            );
          })}
        </div>

        {err && (
          <div className="mb-5 rounded-xl border border-neg/40 bg-neg/[0.07] px-5 py-4 text-sm">
            <b className="text-neg">Could not fetch</b>
            <div className="mt-1 text-dim">{err}</div>
          </div>
        )}

        {loading && !data && <div className="py-20 text-center text-dim">Loading…</div>}

        {data && (
          <>
            {/* Key metrics */}
            <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-5">
              <Stat label="Spot" value={`$${data.spot}`} />
              <Stat
                label="Total GEX"
                value={`${data.total_gex_bn > 0 ? "+" : ""}${data.total_gex_bn.toFixed(2)} B`}
                tone={neutral ? undefined : neg ? "neg" : "pos"}
                hint={
                  neutral
                    ? "Zero exposure · no measurable gamma"
                    : neg
                      ? "Negative gamma · amplifies volatility"
                      : "Positive gamma · suppresses volatility"
                }
              />
              <Stat
                label="Gamma Flip"
                value={data.gamma_flip ? `$${data.gamma_flip}` : "—"}
                tone="brand"
                hint={
                  data.gamma_flip
                    ? data.spot < data.gamma_flip
                      ? "Spot is below the flip"
                      : "Spot is above the flip"
                    : "No flip within the range"
                }
              />
              <Stat label="Call Wall" value={data.call_wall ? `$${data.call_wall}` : "—"} hint="often acts as resistance" />
              <Stat label="Put Wall" value={data.put_wall ? `$${data.put_wall}` : "—"} hint="often acts as support" />
            </div>

            {/* Charts */}
            <Card title="GEX against share price" sub="Where the curve crosses 0 is the gamma flip · every point recomputes gamma with Black-Scholes">
              <ReactECharts option={curveOption} style={{ height: 300 }} notMerge />
            </Card>

            <Card title="By strike" sub="Green = call GEX · red = put GEX · stacked, they are the net exposure">
              <ReactECharts option={strikeOption} style={{ height: 340 }} notMerge />
            </Card>

            {/* By expiry */}
            <Card title="By expiry" sub="Which expiries contribute the bulk of the exposure">
              <div className="flex flex-wrap gap-2">
                {data.by_expiry.map((e) => (
                  <div
                    key={e.expiry}
                    className="rounded-lg border border-line bg-card2 px-3.5 py-2.5"
                  >
                    <div className="font-mono text-[11px] text-dim">{e.expiry}</div>
                    <div
                      className={`mt-0.5 font-mono text-base font-bold ${
                        e.gex_bn >= 0 ? "text-pos" : "text-neg"
                      }`}
                    >
                      {e.gex_bn > 0 ? "+" : ""}
                      {e.gex_bn.toFixed(2)} B
                    </div>
                  </div>
                ))}
              </div>
            </Card>

            {/* Vanna / Charm */}
            <Card
              title="Vanna / charm exposure"
              sub={
                exp && exp.vanna_by_strike.length > 0
                  ? `Blue = vanna (${exp.meta.vanna_unit}) · purple = charm (${exp.meta.charm_unit})`
                  : exp
                    ? "Every contract in this scope lacks IV or has zero open interest, so the second-order greeks cannot be computed"
                    : "Could not fetch"
              }
            >
              {exp && exp.vanna_by_strike.length > 0 ? (
                <>
                  <div className="mb-3 flex flex-wrap gap-3">
                    <MiniStat label="Total vanna" value={`${exp.total_vanna_mm > 0 ? "+" : ""}${exp.total_vanna_mm.toFixed(0)} MM`} />
                    <MiniStat label="Total charm" value={`${exp.total_charm_mm > 0 ? "+" : ""}${exp.total_charm_mm.toFixed(0)} MM`} />
                  </div>
                  <ReactECharts option={expOption} style={{ height: 300 }} notMerge />
                  <div className="mt-2 text-[11px] leading-relaxed text-dim">⚠️ {exp.meta.note}</div>
                </>
              ) : (
                <div className="py-8 text-center text-sm text-dim">No data</div>
              )}
            </Card>

            {/* The two-dimensional surface */}
            <Card
              title="GEX surface (expiry × strike)"
              sub={surface ? `Green = positive gamma · red = negative gamma · ${surface.scope} · colour scale capped at p95 (extremes ${surface.min.toFixed(2)} to ${surface.max.toFixed(2)} B)` : "Could not fetch, or no data"}
            >
              {surface && surface.data.length > 0 ? (
                <ReactECharts option={surfaceOption} style={{ height: 420 }} notMerge />
              ) : (
                <div className="py-8 text-center text-sm text-dim">No data</div>
              )}
            </Card>

            {/* Locally accrued history */}
            <Card
              title="Local history"
              sub={
                data
                  ? `Scope ${data.meta.scope} · ${hist.length} observations accrued · held on your own machine (~/.floorzero/history.db)`
                  : ""
              }
            >
              <div className="mb-3 flex flex-wrap items-center gap-3">
                <button
                  onClick={snapshot}
                  disabled={histBusy || !data}
                  className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                             text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
                >
                  {histBusy ? "Capturing…" : "Capture a snapshot"}
                </button>
                {histMsg && <span className="text-xs text-dim">{histMsg}</span>}
              </div>
              {hist.length >= 2 ? (
                <ReactECharts option={histOption} style={{ height: 260 }} notMerge />
              ) : hist.length === 1 ? (
                <div className="rounded-lg border border-line bg-card2 px-4 py-3 text-sm">
                  <div className="text-dim">One observation recorded; capture once more to draw a trend line.</div>
                  <div className="mt-1.5 font-mono text-xs text-ink">
                    {hist[0].captured_at} · GEX{" "}
                    {(hist[0].total_gex / 1e9).toFixed(3)} B · spot $
                    {hist[0].spot.toFixed(2)}
                  </div>
                </div>
              ) : (
                <div className="py-6 text-center text-sm text-dim">
                  No history under this scope yet — press the button above to start accruing.
                </div>
              )}
              <div className="mt-2 text-[11px] leading-relaxed text-dim">
                ⚠️ Historical snapshots of the option chain <b className="text-ink">cannot be backfilled</b> (Cboe gives only the present),
                so day one of a self-hosted install is necessarily zero history. This is Unusual Whales' real moat —
                they have been running for years. It starts accruing on installation, and grows more valuable with use.
              </div>
            </Card>

            {/* Definitions and disclaimer — this is our differentiator, and it is not hidden */}
            <div className="mt-6 rounded-xl border border-line bg-card px-5 py-4 text-[13px] leading-relaxed text-dim">
              <div className="mb-1.5 font-mono text-[11px] uppercase tracking-wider text-brand">
                How it is computed
              </div>
              <div>
                {data.meta.scope} · unit {data.meta.unit}
                {data.timestamp && ` · data timestamped ${data.timestamp}`}
              </div>
              <div className="mt-1.5">
                ⚠️ {data.meta.convention_note}
              </div>
              <div className="mt-1.5">
                This page presents computed results and is not investment advice. Source: Cboe's official delayed quotes, for personal research only.
              </div>
            </div>
          </>
        )}
    </>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-card2 px-3.5 py-2">
      <div className="font-mono text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div className="mt-0.5 font-mono text-lg font-bold text-ink">{value}</div>
    </div>
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
  tone?: "pos" | "neg" | "brand";
}) {
  const c = tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : tone === "brand" ? "text-brand" : "text-ink";
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3.5">
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-dim">{label}</div>
      <div className={`mt-1 font-mono text-xl font-bold ${c}`}>{value}</div>
      {hint && <div className="mt-1 text-[11px] leading-snug text-dim">{hint}</div>}
    </div>
  );
}

