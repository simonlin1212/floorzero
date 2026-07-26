import { useEffect, useMemo, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, PageHead } from "../components/Shell";

/* ── 类型（与后端 greeks.to_dict 对齐）── */
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
    /** 稳定归档键（不含合约数）—— 拉历史必须用它，不能用 scope */
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
  { v: 7, label: "≤7天" },
  { v: 30, label: "≤30天" },
  { v: 0, label: "全部", all: true },
];

/** 取增强视图的 JSON；失败一律返回 null（由各卡片自行提示），不冒到外层清空主数据。 */
async function pickJson<T>(r: PromiseSettledResult<Response>): Promise<T | null> {
  if (r.status !== "fulfilled" || !r.value.ok) return null;
  try {
    return (await r.value.json()) as T;
  } catch {
    return null;
  }
}

/** 口径 → 查询串。**所有端点共用这一个函数**，避免各处各写一份而画岔口径。 */
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
        // ⚠️ 两个端点必须用**同一套过滤条件**，否则指标卡的 flip
        // 和曲线的零点会来自不同合约集合，曲线不在标注的 flip 处穿越 0。
        // 0DTE 档走 expiry 参数（后端两端点都支持），其余档走 dte_max
        const q = scopeQuery(dte);
        const [a, b] = await Promise.all([
          fetch(`/api/gex/${ticker}?strike_pct=0.05${q}`),
          fetch(`/api/gex/${ticker}/curve?points=60&strike_pct=0.05${q}`),
        ]);
        if (!a.ok) throw new Error((await a.json()).detail ?? `HTTP ${a.status}`);
        // 曲线请求失败也要报出来 —— 静默吞成空数组会让页面显示一张空图，
        // 却把整次加载呈现为成功，把网络/后端故障藏起来。
        if (!b.ok) throw new Error((await b.json()).detail ?? `曲线请求失败 HTTP ${b.status}`);
        const gex = (await a.json()) as GexData;
        const cv = (await b.json()).curve as CurvePoint[];
        if (!cancelled) {
          setData(gex);
          setCurve(cv);
        }
        // 曝险与曲面是**增强视图**：失败只置空该卡片并各自提示，
        // 不阻断已经取到的主数据（但也不静默——卡片会说明取不到）。
        // ⚠️ 用 allSettled 而非 all：这三个是**增强视图**，任一网络异常
        // （fetch reject / JSON 解析失败）若冒到外层 catch，会把已经取到的
        // 主数据一并清掉 —— 让一个可选卡片的故障掀翻整页。
        // ⚠️ 历史必须按 **scope_key**（稳定键）筛：用带合约数的 scope 会导致
        // 每天落进不同分组；不筛则会把 ≤7DTE 与全链混成无意义的锯齿。
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

  /* ── 采集一次快照 ── */
  async function snapshot() {
    setHistBusy(true);
    setHistMsg(null);
    try {
      // 与主端点共用同一套过滤参数，保证存进库的 scope 就是页面正在看的口径
      const r = await fetch(
        `/api/history/snapshot/${ticker}?strike_pct=0.05${scopeQuery(dte)}`,
        { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const j = await r.json();
      // 按**数据自身时点**入库，所以"没新增"= CBOE 还没发布新数据，而不是出错
      setHistMsg(
        j.created
          ? `已记录一条（数据时点 ${j.captured_at ?? "未知"}）`
          : `上游数据尚未更新（仍是 ${j.captured_at}），未重复记录`,
      );
      const h = await fetch(
        `/api/history/${ticker}?scope=${encodeURIComponent(j.scope_key)}`);
      if (h.ok) setHist((await h.json()).series as HistRow[]);
    } catch (e) {
      setHistMsg(`采集失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setHistBusy(false);
    }
  }

  /* ── 历史序列（旧→新）── */
  const histOption = useMemo(() => {
    // 少于 2 点画不出趋势：单点会让双 Y 轴退化成荒谬的自动量程（一个点撑开 300~1200），
    // 而"第一天只有 1 条"正是自部署用户必然遇到的状态 —— 这里改用文字如实呈现。
    if (hist.length < 2) return {};
    const rows = [...hist].reverse();
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 66, right: 62, top: 30, bottom: 44 },
      tooltip: { trigger: "axis", backgroundColor: "#131316",
        borderColor: "#2a2a31", textStyle: { color: "#f2efe9", fontSize: 12 } },
      legend: { data: ["总 GEX", "标的价"], textStyle: { color: "#8e8a83", fontSize: 11 },
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
        // ⚠️ 不用 smooth：快照点之间可能隔几十分钟，样条会在两点之间
        // 拟合出实际没发生过的走势（冲高/回落），直线连才是如实呈现。
        { name: "总 GEX", type: "line", symbol: "circle", symbolSize: 5,
          lineStyle: { color: "#F35D2B", width: 2 }, itemStyle: { color: "#F35D2B" },
          data: rows.map((r) => +(r.total_gex / 1e9).toFixed(3)),
          markLine: { silent: true, symbol: "none",
            // 关掉默认标签：它落在右轴刻度上，和标的价刻度叠字
            label: { show: false },
            data: [{ yAxis: 0, lineStyle: { color: "#8e8a83", type: "dashed" } }] } },
        { name: "标的价", type: "line", yAxisIndex: 1, symbol: "circle", symbolSize: 3,
          lineStyle: { color: "#3b82f6", width: 1.5, opacity: 0.7 },
          data: rows.map((r) => r.spot) },
      ],
    };
  }, [hist]);

  /* ── 按行权价的 GEX 柱状图 ── */
  const strikeOption = useMemo(() => {
    if (!data) return {};
    const xs = data.by_strike.map((r) => r.strike);
    return {
      backgroundColor: "transparent",
      animation: false,   // 金融图表不需要动画；开着会让首屏/截图出现空图
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
                label: { formatter: "现价", color: "#ff5a1f", fontSize: 10 },
              },
            ],
          },
        },
      ],
    };
  }, [data]);

  /* ── GEX 随股价变化的曲线（gamma flip 可视化）── */
  const curveOption = useMemo(() => {
    if (!data || !curve.length) return {};
    return {
      backgroundColor: "transparent",
      animation: false,   // 金融图表不需要动画；开着会让首屏/截图出现空图
      grid: { left: 62, right: 24, top: 30, bottom: 42 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#131316",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 12 },
        formatter: (ps: any[]) => {
          const [px, gex] = ps[0].data as [number, number];
          return `股价 <b>$${px.toFixed(2)}</b><br/>总 GEX <b>${gex.toFixed(3)} B</b>`;
        },
      },
      xAxis: {
        // ⚠️ 必须用数值轴而非类目轴：类目轴要把价格转成字符串标签，
        // 低价股（如 $10）±6% 四舍五入后只剩两三个不同标签，
        // 曲线间距会失真、FLIP 标线也会落到错误的采样点上。
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
          // 不用 visualMap 分段着色：ECharts 6 下 pieces 对一维 line 数据不生效，
          // 会导致整条线不渲染。改用 markArea 区分正负区，线本身用品牌色。
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
              // ⚠️ flip 可能落在曲线绘制范围（±span_pct）之外 ——
              // 那时 reduce 会选到最边缘的点，把图表边界谎报成穿零处。范围外就不画。
              ...(data.gamma_flip &&
              data.gamma_flip >= curve[0].price &&
              data.gamma_flip <= curve[curve.length - 1].price
                ? [
                    {
                      // 数值轴下可直接用真实 flip 价格，不必再找最近采样点
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

  /* ── Vanna / Charm 按行权价 ── */
  const expOption = useMemo(() => {
    // ⚠️ 必须挡空数组：合约存在但 IV 缺失/OI 全为 0 时，后端会如实返回一个
    // "成功但没有可用行"的画像。下面的 reduce 没有初值，空数组会抛
    // "Reduce of empty array"，而这段在 useMemo 里 —— 崩的是整页，不是这张卡。
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
            label: { formatter: "现价", color: "#ff5a1f", fontSize: 10 } }] } },
      ],
    };
  }, [exp, data]);

  /* ── expiry × strike 二维曲面 ── */
  const surfaceOption = useMemo(() => {
    if (!surface || surface.data.length === 0) return {};
    // ⚠️ GEX 曲面是极端长尾分布（实测 SPY 中位 0.002 / 最大 0.89，差 400 倍）。
    // 线性色阶会让 95% 的格子退化成同一个颜色 —— 图看着"有数据"，实则读不出结构。
    // 取 p95 封顶，并在图例上明示"已封顶"，不把裁剪偷偷藏起来。
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
          const over = Math.abs(v) > bound ? '  <span style="color:#ff5a1f">超出色阶</span>' : "";
          return `到期 <b>${surface.expiries[x]}</b><br/>行权价 <b>$${surface.strikes[y]}</b><br/>GEX <b>${v.toFixed(3)} B</b>${over}`;
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
        // 中间色 = 卡片底色，让近零格子自然隐去，只剩真正有敞口的地方显形
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
      <PageHead kicker="Gamma Exposure" title="GEX 伽马敞口">
        做市商卖出期权后必须对冲，股价每动一点就要买卖正股。GEX 衡量的是
        <b className="text-ink"> 股价每变动 1%，做市商需要买卖多少名义金额的正股</b>。
        正 GEX 抑制波动，负 GEX 放大波动。
      </PageHead>

        {/* 控制条 */}
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
              placeholder="其他代码"
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
            <b className="text-neg">取数失败</b>
            <div className="mt-1 text-dim">{err}</div>
          </div>
        )}

        {loading && !data && <div className="py-20 text-center text-dim">加载中…</div>}

        {data && (
          <>
            {/* 关键指标 */}
            <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-5">
              <Stat label="现价" value={`$${data.spot}`} />
              <Stat
                label="总 GEX"
                value={`${data.total_gex_bn > 0 ? "+" : ""}${data.total_gex_bn.toFixed(2)} B`}
                tone={neutral ? undefined : neg ? "neg" : "pos"}
                hint={
                  neutral
                    ? "零敞口 · 无可测量的 gamma"
                    : neg
                      ? "负 gamma · 放大波动"
                      : "正 gamma · 抑制波动"
                }
              />
              <Stat
                label="Gamma Flip"
                value={data.gamma_flip ? `$${data.gamma_flip}` : "—"}
                tone="brand"
                hint={
                  data.gamma_flip
                    ? data.spot < data.gamma_flip
                      ? "现价在 flip 之下"
                      : "现价在 flip 之上"
                    : "区间内未翻转"
                }
              />
              <Stat label="Call Wall" value={data.call_wall ? `$${data.call_wall}` : "—"} hint="常表现为阻力" />
              <Stat label="Put Wall" value={data.put_wall ? `$${data.put_wall}` : "—"} hint="常表现为支撑" />
            </div>

            {/* 图表 */}
            <Card title="GEX 随股价变化" sub="曲线穿越 0 处即 gamma flip · 每点均用 Black-Scholes 重算 gamma">
              <ReactECharts option={curveOption} style={{ height: 300 }} notMerge />
            </Card>

            <Card title="按行权价分布" sub="绿=Call GEX · 红=Put GEX · 堆叠后即净敞口">
              <ReactECharts option={strikeOption} style={{ height: 340 }} notMerge />
            </Card>

            {/* 到期日分布 */}
            <Card title="按到期日" sub="哪些到期日贡献了主要敞口">
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
              title="Vanna / Charm 曝险"
              sub={
                exp && exp.vanna_by_strike.length > 0
                  ? `蓝=Vanna（${exp.meta.vanna_unit}）· 紫=Charm（${exp.meta.charm_unit}）`
                  : exp
                    ? "该口径下所有合约缺 IV 或持仓量为 0，算不出二阶希腊字母"
                    : "取数失败"
              }
            >
              {exp && exp.vanna_by_strike.length > 0 ? (
                <>
                  <div className="mb-3 flex flex-wrap gap-3">
                    <MiniStat label="总 Vanna" value={`${exp.total_vanna_mm > 0 ? "+" : ""}${exp.total_vanna_mm.toFixed(0)} MM`} />
                    <MiniStat label="总 Charm" value={`${exp.total_charm_mm > 0 ? "+" : ""}${exp.total_charm_mm.toFixed(0)} MM`} />
                  </div>
                  <ReactECharts option={expOption} style={{ height: 300 }} notMerge />
                  <div className="mt-2 text-[11px] leading-relaxed text-dim">⚠️ {exp.meta.note}</div>
                </>
              ) : (
                <div className="py-8 text-center text-sm text-dim">暂无数据</div>
              )}
            </Card>

            {/* 二维曲面 */}
            <Card
              title="GEX 曲面（到期日 × 行权价）"
              sub={surface ? `绿=正 gamma · 红=负 gamma · ${surface.scope} · 色阶按 p95 封顶（极值 ${surface.min.toFixed(2)}~${surface.max.toFixed(2)} B）` : "取数失败或无数据"}
            >
              {surface && surface.data.length > 0 ? (
                <ReactECharts option={surfaceOption} style={{ height: 420 }} notMerge />
              ) : (
                <div className="py-8 text-center text-sm text-dim">暂无数据</div>
              )}
            </Card>

            {/* 本地历史沉淀 */}
            <Card
              title="本地历史"
              sub={
                data
                  ? `口径 ${data.meta.scope} · 已积累 ${hist.length} 条 · 落在你自己机器上（~/.vibe-flow/history.db）`
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
                  {histBusy ? "采集中…" : "采集一次快照"}
                </button>
                {histMsg && <span className="text-xs text-dim">{histMsg}</span>}
              </div>
              {hist.length >= 2 ? (
                <ReactECharts option={histOption} style={{ height: 260 }} notMerge />
              ) : hist.length === 1 ? (
                <div className="rounded-lg border border-line bg-card2 px-4 py-3 text-sm">
                  <div className="text-dim">已记录 1 条，再采集一次就能画出趋势线。</div>
                  <div className="mt-1.5 font-mono text-xs text-ink">
                    {hist[0].captured_at} · GEX{" "}
                    {(hist[0].total_gex / 1e9).toFixed(3)} B · 标的 $
                    {hist[0].spot.toFixed(2)}
                  </div>
                </div>
              ) : (
                <div className="py-6 text-center text-sm text-dim">
                  该口径下还没有历史 —— 点上面的按钮开始积累。
                </div>
              )}
              <div className="mt-2 text-[11px] leading-relaxed text-dim">
                ⚠️ 期权链的历史快照<b className="text-ink">补不回来</b>（CBOE 只给当下），
                自部署第一天必然是零历史。这正是 Unusual Whales 真正的护城河 ——
                他们跑了很多年。装上就开始攒，越用越值钱。
              </div>
            </Card>

            {/* 口径与免责 —— 这是我们的差异化，不藏着 */}
            <div className="mt-6 rounded-xl border border-line bg-card px-5 py-4 text-[13px] leading-relaxed text-dim">
              <div className="mb-1.5 font-mono text-[11px] uppercase tracking-wider text-brand">
                计算口径
              </div>
              <div>
                {data.meta.scope} · 单位 {data.meta.unit}
                {data.timestamp && ` · 数据时间 ${data.timestamp}`}
              </div>
              <div className="mt-1.5">
                ⚠️ {data.meta.convention_note}
              </div>
              <div className="mt-1.5">
                本页只呈现计算结果，不构成任何投资建议。数据源 CBOE 官方延时行情，仅供个人研究。
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

