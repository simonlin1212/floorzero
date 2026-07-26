import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/market.py 对齐）── */
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
  /** ⚠️ 拉取失败的年份 —— 后端仍会把库里的旧数据给出来，
   *  所以这个字段**必须显示**，否则用户会把旧缓存当成最新的。 */
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

  // ⚠️ 每类请求各一个序号：曲线和 COT 是两条独立的加载线，
  //    共用一个计数器的话，切市场会把在途的曲线响应也判成"过期"丢掉。
  const curveSeq = useRef(0);
  const cotSeq = useRef(0);
  const mktSeq = useRef(0);

  /* ── 收益率曲线 ── */
  // `force` 只在用户主动点「刷新」时为 true。
  // ⚠️ 不传它的话后端会直接复用缓存 —— 按钮写着"刷新"却什么都不做，
  //    而 Treasury 是**会修订历史值**的，用户拿不到修订后的数字。
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

  /* ── COT 市场清单（只加载一次）── */
  // ⚠️ 这里**不能吞错**：清单取不到时下拉框会是空的，
  //    而"空下拉框"和"CFTC 连不上"在界面上长得一模一样。
  // ⚠️ 「重试」按钮可以连点，所以这里也要序号：先发的请求后返回时，
  //    会把后发那次的成功结果重新清空成失败态。
  const loadMarkets = useCallback(async () => {
    const seq = ++mktSeq.current;
    setMarketsErr(null);
    try {
      const r = await fetch("/api/market/cot/markets");
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as MarketList;
      if (seq !== mktSeq.current) return;
      setMarkets(d);
      // 默认选一个多数人认得的：优先 E-MINI S&P 500
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

  /* ── COT 时间序列 ── */
  const loadCot = useCallback(async () => {
    if (!picked) return;
    const seq = ++cotSeq.current;
    setCotLoading(true);
    setCotErr(null);
    // ⚠️ 必须先清空：下拉框已经显示合约 B 了，指标卡/图/表还留着 A 的数字，
    //    看上去就是"B 的持仓"。慢网络下这个错配能持续到请求超时。
    setCot(null);
    try {
      // ⚠️ exact=true：模糊匹配会把不同合约揉成一条锯齿线
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

  /* ── 图：利差时间序列 ── */
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
                v === null ? "" : v < 0 ? " <span style='color:#ff5a1f'>倒挂</span>" : "";
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
        name: "利差 %",
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
        // 零轴：低于它就是倒挂。画成标线而不是靠肉眼估。
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

  /* ── 图：当日期限结构 ── */
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

  /* ── 图：COT 净持仓 ── */
  const cotOption = useMemo(() => {
    if (!cot || !cot.rows.length) return null;
    // 后端按日期倒序返回；画图要正序
    const rows = [...cot.rows].reverse();
    const dates = rows.map((r) => r.report_date ?? "");
    return {
      backgroundColor: "transparent",
      grid: { left: 62, right: 18, top: 34, bottom: 46 },
      legend: {
        top: 0,
        textStyle: { color: "#8e8a83", fontSize: 11 },
        data: ["杠杆基金净持仓", "资产管理净持仓"],
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number | null }[]) =>
          `<b>${esc(ps[0]?.axisValue)}</b>（周二持仓）<br/>` +
          ps
            .map(
              (p) =>
                `${esc(p.seriesName)}: ${p.data === null ? "—" : p.data.toLocaleString()} 手`,
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
        name: "净持仓（手）",
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
          name: "杠杆基金净持仓",
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
          name: "资产管理净持仓",
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
      <PageHead kicker="Market · 宏观" title="收益率曲线与持仓报告">
        美国财政部与 CFTC 的公开数据 —— 全项目
        <b className="text-ink"> 最干净的一条线</b>：政府作品，不限商用、可自由再分发，
        没有 OPRA、没有 §13107、没有 FINRA 条款。
      </PageHead>

      {/* ── 合规与口径 ── */}
      <div className="mb-5 rounded-2xl border border-line bg-card2/60 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          S 级 · 口径说明
        </div>
        <p className="mb-1.5">
          <b className="text-ink">倒挂看哪条？</b>{" "}
          <Emph>
            {notes?.inversion ??
              "10Y−2Y 与 10Y−3M 是两条不同的利差，倒挂时点可以差好几个月。本页两条都显示。"}
          </Emph>
        </p>
        <p>
          <b className="text-ink">COT 的三天时滞：</b>{" "}
          <Emph>{notes?.cot_lag ?? "报告的是周二收盘持仓，周五下午才发布。"}</Emph>{" "}
          <Emph>{notes?.cot_scope}</Emph>
        </p>
      </div>

      {/* ═══ 收益率曲线 ═══ */}
      <Card
        title="美债收益率曲线"
        sub={
          curve
            ? `${curve.years[0]}–${curve.year} · ${curve.dates.length} 个交易日` +
              (curve.missing_years.length ? ` · 缺 ${curve.missing_years.join("/")}` : "")
            : "U.S. Treasury 官方日度数据"
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
                  近 {y} 年
                </option>
              ))}
            </select>
            <button
              onClick={() => void loadCurve(true)}
              disabled={curveLoading}
              title="强制重拉 Treasury（官方会修订历史值），不吃本地缓存"
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         transition hover:border-brand disabled:opacity-40"
            >
              {curveLoading ? "拉取中…" : "重新拉取"}
            </button>
          </div>
        }
      >
        {curveErr && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {curveErr}
          </div>
        )}

        {/* ⚠️ 部分年份拉失败时，下面显示的是**本地旧数据**。
            必须说出来 —— 否则"最新 2026-07-24"会被当成今天刚取的。 */}
        {curve && Object.keys(curve.failed).length > 0 && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            <div className="font-semibold text-brand">
              以下年份取数失败，图表用的是本地已有数据，可能不是最新的：
            </div>
            <ul className="mt-1 space-y-0.5 text-dim">
              {Object.entries(curve.failed).map(([y, msg]) => (
                <li key={y}>
                  · <span className="font-mono text-ink">{y}</span>：{msg}
                </li>
              ))}
            </ul>
          </div>
        )}
        {curve && curve.missing_years.length > 0 && (
          <div className="mb-3 text-[10px] text-dim">
            Treasury 没有 {curve.missing_years.join("、")} 年的数据（早年缺报是常态，
            与取数失败不同）。
          </div>
        )}

        {latest && (
          <>
            <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
              <Stat label={`最新 ${latest.date}`} value={pct(latest.yields["10Y"])} sub="10Y" />
              {SPREAD_KEYS.map((k) => {
                const v = latest.spreads[k];
                const inv = latest.inverted[k];
                return (
                  <Stat
                    key={k}
                    label={k}
                    value={v === null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`}
                    sub={inv === null ? "数据缺失" : inv ? "倒挂" : "未倒挂"}
                    warn={inv === true}
                  />
                );
              })}
            </div>

            <div className="mb-1 text-xs text-dim">利差走势（虚线为零轴，低于它即倒挂）</div>
            {spreadOption && (
              <ReactECharts option={spreadOption} style={{ height: 300 }} notMerge />
            )}

            <div className="mb-1 mt-4 text-xs text-dim">
              {latest.date} 的期限结构（横轴是期限，不是时间）
            </div>
            {shapeOption && (
              <ReactECharts option={shapeOption} style={{ height: 220 }} notMerge />
            )}

            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-line text-dim">
                  <tr>
                    <Th>期限</Th>
                    {curve!.tenors.map((t) => (
                      <Th key={t}>{t}</Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-b border-line/50">
                    <Td className="text-dim">收益率</Td>
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
          <div className="py-8 text-center text-xs text-dim">无数据</div>
        )}
      </Card>

      {/* ═══ COT ═══ */}
      <Card
        title="CFTC 持仓报告（TFF · 金融期货）"
        sub={
          markets
            ? `${markets.active} 个在报合约 · 最新一期 ${markets.latest_report ?? "—"}（周二持仓）`
            : marketsErr
              ? "合约清单取数失败"
              : "Traders in Financial Futures"
        }
        right={
          <button
            onClick={() => void loadCot()}
            disabled={cotLoading || !picked}
            className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                       transition hover:border-brand disabled:opacity-40"
          >
            {cotLoading ? "加载中…" : "刷新"}
          </button>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="筛合约名（S&P / TREASURY / VIX…）"
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
                {marketsErr ? "清单取数失败" : markets ? "无匹配合约" : "加载中…"}
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
            <span className="font-semibold text-brand">合约清单取不到</span>
            <span className="text-dim">
              {" "}
              —— {marketsErr}。这是<b className="text-ink">取数失败</b>，
              不是"CFTC 没有合约"。
            </span>
            <button
              onClick={() => void loadMarkets()}
              className="ml-2 rounded border border-line px-2 py-0.5 text-[10px] text-ink
                         hover:border-brand"
            >
              重试
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
                  <Stat label="报告日（周二）" value={r.report_date ?? "—"} sub="周五发布" />
                  <Stat
                    label="杠杆基金净持仓"
                    value={num(r.lev_net)}
                    sub={`多 ${num(r.lev_long)} / 空 ${num(r.lev_short)}`}
                  />
                  <Stat
                    label="资产管理净持仓"
                    value={num(r.asset_net)}
                    sub={`多 ${num(r.asset_long)} / 空 ${num(r.asset_short)}`}
                  />
                  <Stat label="总持仓量" value={num(r.open_interest)} sub="open interest" />
                </div>
              );
            })()}

            <div className="mb-1 text-xs text-dim">
              净持仓走势（正=净多，负=净空；虚线为零轴）
            </div>
            {cotOption && <ReactECharts option={cotOption} style={{ height: 300 }} notMerge />}

            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-line text-dim">
                  <tr>
                    <Th>报告日</Th>
                    <Th>杠杆多</Th>
                    <Th>杠杆空</Th>
                    <Th>杠杆净</Th>
                    <Th>资管多</Th>
                    <Th>资管空</Th>
                    <Th>资管净</Th>
                    <Th>交易商多</Th>
                    <Th>交易商空</Th>
                    <Th>总持仓</Th>
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
                  表只列最近 30 期，图为全部 {cot.rows.length} 期
                </div>
              )}
            </div>
          </>
        )}
        {cot && cot.rows.length === 0 && !cotErr && (
          <div className="py-8 text-center text-xs text-dim">该合约无记录</div>
        )}
      </Card>

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        数据源：U.S. Department of the Treasury（Daily Treasury Par Yield Curve Rates）·
        U.S. Commodity Futures Trading Commission（Traders in Financial Futures）。
        均为美国政府作品，公有领域。本页只呈现数值，不做任何判断与预测。
        今年为 {thisYear}。
      </p>
    </>
  );
}

/** 小指标块。 */
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
