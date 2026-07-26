import { useEffect, useMemo, useState } from "react";
import ReactECharts from "echarts-for-react";

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
  meta: { convention: string; convention_note: string; scope: string; unit: string };
  timestamp?: string;
};
type CurvePoint = { price: number; gex_bn: number };

const PRESETS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "IWM"];
const DTE_OPTS = [
  { v: 0, label: "0DTE" },
  { v: 7, label: "≤7天" },
  { v: 30, label: "≤30天" },
  { v: 0, label: "全部", all: true },
];

export default function Gex() {
  const [ticker, setTicker] = useState("SPY");
  const [input, setInput] = useState("SPY");
  const [dte, setDte] = useState<number | null>(7);
  const [data, setData] = useState<GexData | null>(null);
  const [curve, setCurve] = useState<CurvePoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      try {
        // ⚠️ 两个端点必须用**同一套过滤条件**，否则指标卡的 flip
        // 和曲线的零点会来自不同合约集合，曲线不在标注的 flip 处穿越 0。
        // 0DTE 档走 expiry 参数（后端两端点都支持），其余档走 dte_max
        const q = dte === null ? "" : dte === 0 ? "&expiry=0DTE" : `&dte_max=${dte}`;
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

  const regime = data?.regime;
  const neg = regime === "negative";
  const neutral = regime === "neutral";

  return (
    <div className="min-h-screen grid-bg">
      <div className="mx-auto max-w-[1180px] px-6 py-8">
        {/* 头部 */}
        <header className="mb-7">
          <div className="mb-2 font-mono text-[11px] uppercase tracking-[0.25em] text-brand">
            ■ Gamma Exposure
          </div>
          <h1 className="text-3xl font-bold tracking-tight">GEX 伽马敞口</h1>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-dim">
            做市商卖出期权后必须对冲，股价每动一点就要买卖正股。GEX 衡量的是
            <b className="text-ink"> 股价每变动 1%，做市商需要买卖多少名义金额的正股</b>。
            正 GEX 抑制波动，负 GEX 放大波动。
          </p>
        </header>

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
      </div>
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

function Card({ title, sub, children }: { title: string; sub?: string; children: React.ReactNode }) {
  return (
    <div className="mb-5 rounded-2xl border border-line bg-card p-5">
      <div className="mb-3">
        <div className="text-base font-bold">{title}</div>
        {sub && <div className="mt-0.5 text-xs text-dim">{sub}</div>}
      </div>
      {children}
    </div>
  );
}
