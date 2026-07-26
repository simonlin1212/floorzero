import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/flow.py 对齐）── */
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
  // 某一边一个都算不出时为 null —— **「算不出」不是「零」**
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
    // 全部合约都缺希腊字母时为 null —— **「算不出」不是「敞口为零」**
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
      // OI 变化是**增强视图**：失败只影响那张卡片，不清掉主数据。
      // ⚠️ 但**不能静默**：接口 500 时如果只是把 oi 留成 null，
      //    卡片会显示"还没攒够"—— 把服务故障说成了"没有历史"。
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

  // ⚠️ 归档是**长请求**，返回时用户可能已经切到别的标的了。
  //    闭包里的 `load()` 绑的是旧 ticker —— 直接调用会把在途的新标的请求
  //    判成过期、再把旧标的重新加载回来：输入框写着 QQQ、卡片显示 SPY。
  //    所以回来先对一次标的，不一致就只报结果、不回写主数据。
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
        setRecordMsg(`${mine} 的 ${d.snapshot_date} 快照已归档（${d.recorded} 个合约），`
          + `当前显示的是 ${tickerRef.current}，未刷新本页。`);
        return;
      }
      setRecordMsg(`已归档 ${d.snapshot_date} 交易时段的 ${d.recorded} 个合约`);
      await load();
    } catch (e) {
      setRecordMsg(`归档失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRecording(false);
    }
  }, [ticker, load]);

  const rows = tab === "unusual" ? (flow?.unusual_rows ?? []) : (flow?.biggest_rows ?? []);
  const L = flow?.limits;

  /* 按到期分布 */
  const expiryOption = useMemo(() => {
    if (!flow?.by_expiry.length) return null;
    const b = flow.by_expiry;
    return {
      backgroundColor: "transparent",
      grid: { left: 58, right: 18, top: 30, bottom: 40 },
      legend: { top: 0, textStyle: { color: "#8e8a83", fontSize: 11 } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number }[]) =>
          `<b>${esc(ps[0]?.axisValue)}</b><br/>` +
          ps.map((p) => `${esc(p.seriesName)}: ${p.data.toLocaleString()} 张`).join("<br/>"),
      },
      xAxis: {
        type: "category",
        data: b.map((x) => x.expiry),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#8e8a83", fontSize: 10, formatter: (v: number) => num(v) },
        splitLine: { lineStyle: { color: "#2a2a31", type: "dashed" } },
      },
      series: [
        {
          name: "认购",
          type: "bar",
          stack: "v",
          data: b.map((x) => x.call_volume),
          itemStyle: { color: "#ff5a1f" },
        },
        {
          name: "认沽",
          type: "bar",
          stack: "v",
          data: b.map((x) => x.put_volume),
          itemStyle: { color: "#5b9cf7" },
        },
      ],
    };
  }, [flow]);

  /* 按行权价分布（认沽画成负轴，看形状） */
  const strikeOption = useMemo(() => {
    if (!flow?.by_strike.rows.length) return null;
    const b = flow.by_strike.rows;
    return {
      backgroundColor: "transparent",
      grid: { left: 58, right: 18, top: 30, bottom: 40 },
      legend: { top: 0, textStyle: { color: "#8e8a83", fontSize: 11 } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number }[]) =>
          `<b>行权价 ${esc(ps[0]?.axisValue)}</b><br/>` +
          ps
            .map(
              (p) =>
                `${esc(p.seriesName)}: ${Math.abs(p.data).toLocaleString()} 张`,
            )
            .join("<br/>"),
      },
      xAxis: {
        type: "category",
        data: b.map((x) => x.strike),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        axisLabel: {
          color: "#8e8a83",
          fontSize: 10,
          formatter: (v: number) => num(Math.abs(v)),
        },
        splitLine: { lineStyle: { color: "#2a2a31", type: "dashed" } },
      },
      series: [
        {
          name: "认购成交",
          type: "bar",
          data: b.map((x) => x.call_volume),
          itemStyle: { color: "#ff5a1f" },
        },
        {
          // 画到负轴纯粹是为了看形状对称性，数值本身是正的（tooltip 取绝对值）
          name: "认沽成交",
          type: "bar",
          data: b.map((x) => -x.put_volume),
          itemStyle: { color: "#5b9cf7" },
        },
      ],
      markLine: undefined,
    };
  }, [flow]);

  return (
    <>
      <PageHead kicker="Flow · 期权流" title="期权异动与持仓结构">
        CBOE 官方延时期权链。<b className="text-ink">只在你自己机器上跑</b> ——
        这条线是 C 级源，任何对外展示都会触发 OPRA redistributor 认定。
      </PageHead>

      {/* ⭐ 能力边界：放在最前面，而不是藏进脚注 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          先说清楚这一栏做不了什么
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
        title={flow ? `${flow.ticker} · $${flow.spot.toFixed(2)}` : "期权流"}
        sub={
          flow
            ? `交易时段 ${flow.session ?? "—"} · CBOE 发布于 ${flow.timestamp ?? "—"}`
            : "输入代码后加载"
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
              <option value="7">7 天内</option>
              <option value="30">30 天内</option>
              <option value="90">90 天内</option>
              <option value="all">全链</option>
            </select>
            <button
              onClick={() => setTicker(tickerInput.trim() || "SPY")}
              disabled={loading}
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         transition hover:border-brand disabled:opacity-40"
            >
              {loading ? "加载中…" : "查询"}
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
                label="有成交合约"
                value={num(flow.counts.traded_contracts)}
                sub={`总成交 ${num(flow.counts.total_volume)} 张`}
              />
              <Stat
                label="异动合约"
                value={num(flow.counts.unusual)}
                sub={`vol/OI ≥ ${flow.thresholds.unusual_ratio} 且量 ≥ ${flow.thresholds.min_volume}`}
              />
              <Stat
                label="前收持仓为 0"
                value={num(flow.counts.zero_prior_oi)}
                sub="昨收无未平仓头寸，今日有成交"
              />
              <Stat
                label="持仓量合计"
                value={num(flow.counts.total_oi)}
                sub={`范围内全部 ${num(flow.counts.scope_contracts)} 个合约 · 隔夜结算数`}
              />
            </div>

            {/* P/C 三口径 */}
            <div className="mb-2 text-xs text-dim">
              认沽/认购比 —— <b className="text-ink">三个口径都给，不挑一个当「那个」P/C</b>
              （它们量的是不同的东西，结论不同是正常的）
            </div>
            <div className="mb-4 grid grid-cols-1 gap-2.5 sm:grid-cols-3">
              {(
                [
                  ["by_volume", "按成交量", ""],
                  ["by_oi", "按持仓量", ""],
                  ["by_notional", "按权利金（估算）", ""],
                ] as const
              ).map(([k, title, hint]) => {
                const d = flow.ratios[k];
                return (
                  <div key={k} className="rounded-xl border border-line bg-card2/60 px-3 py-2.5">
                    <div className="text-[10px] uppercase tracking-wide text-dim">{title}</div>
                    <div className="mt-0.5 font-mono text-lg text-ink">
                      {d.pc === null ? "—" : d.pc.toFixed(3)}
                    </div>
                    <div className="mt-0.5 text-[10px] leading-relaxed text-dim">
                      认购 {k === "by_notional" ? money(d.call) : num(d.call)} / 认沽{" "}
                      {k === "by_notional" ? money(d.put) : num(d.put)}
                      <br />
                      <span className="opacity-70">
                        口径：<Emph>{d.basis}</Emph>
                      </span>
                      {hint}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="mb-4 text-[10px] leading-relaxed text-dim">
              ⚠️ <b className="text-ink">「权利金」是估算值，不是实际成交金额</b>：
              链快照没有逐笔成交价，这里算的是「当日累计成交量 × <b className="text-ink">抓取时</b>
              中间价 × 100」。若 1,000 张在上午以 $1 成交、抓取时中间价已到 $5，
              这里会显示 $50 万而实际约 $10 万。没有 tape 就算不准，所以只叫它估算。
              {flow.ratios.notional_excluded > 0 && (
                <>
                  {" "}另有 {flow.ratios.notional_excluded} 个合约完全没有报价、连估算都做不了，
                  已排除在这个口径之外（不是当成 0）。
                </>
              )}
              {flow.ratios.one_sided_quotes > 0 && (
                <>
                  {" "}其中 {flow.ratios.one_sided_quotes} 个是
                  <b className="text-ink">零买价</b>（有卖价没买价 = 没人接盘），
                  中间价对它们偏乐观 —— 照算是为了不让这个口径偏向实值侧，
                  但知道这一点。
                </>
              )}
            </div>

            {/* 敞口 */}
            <div className="mb-4 rounded-xl border border-line bg-card2/40 px-3 py-2.5">
              <div className="mb-1.5 flex flex-wrap gap-x-6 gap-y-1 text-xs">
                <span className="text-dim">
                  认购 |delta| 敞口{" "}
                  <b className="font-mono text-ink">
                    {num(flow.exposure.call_delta_shares)} 股
                  </b>
                  <span className="text-dim">
                    {" "}
                    ({money(flow.exposure.call_delta_notional)})
                  </span>
                </span>
                <span className="text-dim">
                  认沽 |delta| 敞口{" "}
                  <b className="font-mono text-ink">
                    {num(flow.exposure.put_delta_shares)} 股
                  </b>
                  <span className="text-dim">
                    {" "}
                    ({money(flow.exposure.put_delta_notional)})
                  </span>
                </span>
              </div>
              <div className="text-[10px] leading-relaxed text-dim">
                <Emph>{flow.exposure.note}</Emph>
                {/* ⚠️ 全缺时后端给的是 null、上面显示"—"。
                    这里再说清是"算不出"而不是"敞口为零"。 */}
                {/* ⚠️ 按边分别提示：认沽全有、认购全缺时，
                    只报一个总数会让人以为认购那边"敞口是零"。 */}
                {(["call", "put"] as const).map((side) => {
                  const n =
                    side === "call"
                      ? flow.exposure.counted_delta_call
                      : flow.exposure.counted_delta_put;
                  const m =
                    side === "call"
                      ? flow.exposure.missing_delta_call
                      : flow.exposure.missing_delta_put;
                  const label = side === "call" ? "认购" : "认沽";
                  if (n === 0 && m > 0)
                    return (
                      <b key={side} className="text-brand">
                        {" "}
                        {label}侧 {m} 个合约全都没有 delta，敞口
                        <b className="text-ink">算不出来</b>（不是零）。
                      </b>
                    );
                  if (m > 0)
                    return (
                      <span key={side}>
                        {" "}
                        （{label}侧 {m} 个缺 delta 未计入，已计入 {n} 个。）
                      </span>
                    );
                  return null;
                })}
              </div>
            </div>

            {/* 明细表 */}
            <div className="mb-2 flex items-center gap-2">
              {(
                [
                  ["unusual", `异动 (${flow.counts.unusual})`],
                  ["biggest", "名义金额最大"],
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
                两张表都按权利金估算降序 —— 「前收 0」是徽章不是排序键
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-line text-dim">
                  <tr>
                    <Th>到期</Th>
                    <Th>DTE</Th>
                    <Th>类型</Th>
                    <Th>行权价</Th>
                    <Th>成交量</Th>
                    <Th>持仓量</Th>
                    <Th>vol/OI</Th>
                    <Th>中间价</Th>
                    <Th>权利金(估算)</Th>
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
                      <Td className={r.type === "put" ? "text-[#5b9cf7]" : "text-brand"}>
                        {r.type === "put" ? "认沽" : "认购"}
                      </Td>
                      <Td className="font-mono">{r.strike.toFixed(1)}</Td>
                      <Td className="font-mono">{r.volume.toLocaleString()}</Td>
                      <Td className="font-mono">{r.open_interest.toLocaleString()}</Td>
                      <Td className="font-mono">
                        {r.zero_prior_oi ? (
                          <span
                            className="rounded bg-brand/15 px-1.5 py-0.5 text-[10px] text-brand"
                            title="此前持仓量为 0，比值算不出来（不是无穷大）"
                          >
                            全新
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
                        无符合条件的合约
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
        <Card title="成交分布" sub="认沽画在负轴只为看形状，数值本身为正">
          <div className="mb-1 text-xs text-dim">
            按到期日 —— <b className="text-ink">全部行权价</b>
          </div>
          {expiryOption && <ReactECharts option={expiryOption} style={{ height: 240 }} notMerge />}
          {strikeOption && (
            <div className="mt-4">
              {/* ⚠️ 两张图的范围不同，合计对不上是**必然**的。
                  不说出来，用户只会以为其中一张算错了。 */}
              <div className="mb-1 text-xs text-dim">
                按行权价 —— 只画现价 ±{(flow.by_strike.window_pct * 100).toFixed(0)}%
                （{flow.by_strike.low.toFixed(0)} ~ {flow.by_strike.high.toFixed(0)}）
                {flow.by_strike.dropped_contracts > 0 && (
                  <span className="text-dim">
                    ，窗口外的 {flow.by_strike.dropped_contracts} 个合约
                    （{num(flow.by_strike.dropped_volume)} 张）未画，
                    <b className="text-ink">所以它与上图的合计对不上</b>
                  </span>
                )}
              </div>
              <ReactECharts option={strikeOption} style={{ height: 260 }} notMerge />
            </div>
          )}
        </Card>
      )}

      {/* ⭐ OI 历史沉淀 */}
      <Card
        title="持仓量变化（本地沉淀）"
        sub={
          oi?.enough
            ? `${oi.date_from} → ${oi.date_to}（相隔 ${oi.span_days} 天）`
            : "这份历史补不回来，只能从装上那天起逐日攒"
        }
        right={
          <button
            onClick={() => void record()}
            disabled={recording || !flow}
            className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                       transition hover:border-brand disabled:opacity-40"
          >
            {recording ? "归档中…" : "归档本次快照"}
          </button>
        }
      >
        {recordMsg && <div className="mb-3 text-xs text-dim">{recordMsg}</div>}

        {oiErr && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            <b className="text-brand">持仓量变化取数失败</b>
            <span className="text-dim">
              {" "}
              —— {oiErr}。这是<b className="text-ink">接口出错</b>，
              不是"还没攒够历史"。
            </span>
          </div>
        )}

        {!oiErr && !oi?.enough && (
          <div className="rounded-lg border border-line bg-card2/40 px-3 py-3 text-xs leading-relaxed text-dim">
            <Emph>{oi?.note}</Emph>
            {oi?.dates && oi.dates.length > 0 && (
              <div className="mt-2 font-mono text-[10px]">
                已有快照：{oi.dates.join("、")}
              </div>
            )}
            <div className="mt-2 text-[10px]">
              ⚠️ 这里显示的是<b className="text-ink">还没攒够</b>，
              不是"持仓没有变化" —— 两者在界面上必须能分清。
            </div>
          </div>
        )}

        {oi?.enough && (
          <>
            {oi.is_consecutive === false && (
              <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
                两个快照相隔 {oi.span_days} 天
                {(oi.snapshots_between ?? 0) > 0 &&
                  `、中间还夹着 ${oi.snapshots_between} 次观测`}
                ，
                <span className="text-dim">
                  {" "}
                  下面是这段时间的<b className="text-ink">累计</b>变化，不是单日变化。
                </span>
              </div>
            )}
            {(oi.incomplete_excluded ?? 0) > 0 && (
              <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
                <b className="text-brand">有 {oi.incomplete_excluded} 个合约尚未到期却不在结束快照里</b>
                <span className="text-dim">
                  （{num(oi.incomplete_oi ?? 0)} 张持仓）—— CBOE 会把合约挂到到期为止，
                  所以这只能说明<b className="text-ink">那次抓取不完整</b>，已排除。
                  两次快照合约数：{num(oi.contracts_from ?? 0)} → {num(oi.contracts_to ?? 0)}，
                  差得多就说明这次比较不可信。
                </span>
              </div>
            )}
            {(oi.expired_excluded ?? 0) > 0 && (
              <div className="mb-3 text-[10px] leading-relaxed text-dim">
                已排除 {oi.expired_excluded} 个<b className="text-ink">在此期间到期</b>的合约
                （合计 {num(oi.expired_oi ?? 0)} 张持仓）—— 它们从链里消失是因为到期，
                不是有人平仓。
              </div>
            )}
            <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-3">
              <Stat
                label="认购持仓净变化"
                value={`${(oi.totals?.call_change ?? 0) > 0 ? "+" : ""}${num(oi.totals?.call_change ?? 0)}`}
                sub="张"
              />
              <Stat
                label="认沽持仓净变化"
                value={`${(oi.totals?.put_change ?? 0) > 0 ? "+" : ""}${num(oi.totals?.put_change ?? 0)}`}
                sub="张"
              />
              <Stat label="涉及合约" value={num(oi.totals?.contracts ?? 0)} sub="两日并集" />
            </div>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {(
                [
                  ["净增持", oi.gained ?? []],
                  ["净减持", oi.lost ?? []],
                ] as const
              ).map(([title, list]) => (
                <div key={title}>
                  <div className="mb-1 text-xs text-dim">{title}</div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <thead className="border-b border-line text-dim">
                        <tr>
                          <Th>到期</Th>
                          <Th>类型</Th>
                          <Th>行权价</Th>
                          <Th>前</Th>
                          <Th>后</Th>
                          <Th>变化</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {list.slice(0, 12).map((r) => (
                          <tr
                            key={`${r.expiry}-${r.type}-${r.strike}`}
                            className="border-b border-line/50"
                          >
                            <Td className="font-mono">{r.expiry}</Td>
                            <Td className={r.type === "put" ? "text-[#5b9cf7]" : "text-brand"}>
                              {r.type === "put" ? "认沽" : "认购"}
                            </Td>
                            <Td className="font-mono">{r.strike.toFixed(1)}</Td>
                            <Td className="font-mono text-dim">{num(r.oi_from)}</Td>
                            <Td className="font-mono text-dim">{num(r.oi_to)}</Td>
                            <Td
                              className={`font-mono ${r.change > 0 ? "text-ink" : "text-brand"}`}
                              title={
                                r.change_pct === null
                                  ? "此前持仓为 0，算不出百分比"
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
                              无
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
        数据源：Cboe Global Markets 延时期权报价。⛔ 本页数据仅供在本机做个人研究，
        对外展示会被认定为 OPRA redistributor（$1,500/月）。本页只呈现数值，
        不做任何方向判断与预测。
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
