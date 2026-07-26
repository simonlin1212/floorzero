import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/darkpool.py 对齐）── */
type Notes = {
  ats_vs_otc: string;
  no_double_count: string;
  otc_anonymous: string;
  lag: string;
  share_needs_local: string;
};
type Terms = {
  url: string;
  last_modified: string;
  permitted: string;
  restriction_d: string;
  restriction_e: string;
  ambiguity: string;
  our_stance: string;
};
type Status = {
  enabled: boolean;
  env_var: string;
  terms: Terms;
  notes: Notes;
  why_gated: string;
};
type Venue = {
  symbol: string;
  week: string;
  kind: string;
  mpid: string | null;
  name: string | null;
  tier: string | null;
  shares: number | null;
  trades: number | null;
  notional: number | null;
  avg_trade_size: number | null;
};
type Recon = {
  reference: number | null;
  diff?: number;
  matches: boolean | null;
  note: string;
};
type Side = {
  shares: number;
  // ⚠️ `records` 是行数、`firms` 是按 MPID 去重的机构数。非 ATS 那边
  //    MPID 全为空 —— 32 条记录点不出一家，两个数不能混用。
  records: number;
  firms: number;
  anonymous_records: number;
  null_share_records: number;
  null_trade_records: number;
  trades: number;
  reconcile: Recon;
  named_firms?: number;
};
type Dark = {
  ticker: string;
  week: string;
  weeks: string[];
  ats: Side;
  otc: Side;
  off_exchange_shares: number;
  ats_over_otc: number | null;
  // 分母凑不齐时为 null —— **不拿近似值顶替**
  share: {
    consolidated: number;
    ats_pct: number;
    otc_pct: number;
    off_exchange_pct: number;
    covered_days: string[];
  } | null;
  share_note: string;
  missing_days: string[];
  venues: { ats: Venue[]; otc: Venue[] };
  series: {
    week: string;
    ats_shares: number;
    otc_shares: number;
    ats_share_of_off_exchange: number | null;
  }[];
  unknown_types: Record<string, number>;
  null_shares: Record<string, number>;
  // 取满行数上限 = 周序列可能不全，而该接口不支持排序，截掉哪几周未知
  truncated: boolean;
  weekdays: string[];
  locally_observed_days: string[];
  // 本地观测不到整周时的说明（分不清休市还是没扫）
  calendar_note: string | null;
  notes: Notes;
};

function num(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e9) return `${s}${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(0)}K`;
  return `${s}${a.toFixed(0)}`;
}

export default function Darkpool() {
  const [tickerInput, setTickerInput] = useState("NVDA");
  const [ticker, setTicker] = useState("NVDA");
  const [week, setWeek] = useState<string>("");
  const [tab, setTab] = useState<"ats" | "otc">("ats");

  const [status, setStatus] = useState<Status | null>(null);
  const [data, setData] = useState<Dark | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [gated, setGated] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showTerms, setShowTerms] = useState(false);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    // ⚠️ 这个请求失败**不能吞**：关闭卡要求 `gated && status`、主卡要求 `!gated`，
    //    status 拿不到时两块都不渲染 —— 页面变一片空白，看不出发生了什么。
    fetch("/api/darkpool-status")
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        setStatus((await r.json()) as Status);
      })
      .catch((e) => setStatusErr(e instanceof Error ? e.message : String(e)));
  }, []);

  const load = useCallback(async () => {
    const s = ++seq.current;
    setLoading(true);
    setErr(null);
    setGated(null);
    try {
      const q = week ? `?week=${encodeURIComponent(week)}` : "";
      const r = await fetch(`/api/darkpool/${encodeURIComponent(ticker)}${q}`);
      if (r.status === 409) {
        // ⚠️ 409 = **这一栏被关着**（配置状态），不是"没有数据"。
        //    这两件事在界面上必须长得完全不一样。
        // ⚠️ 序号要在 `await r.json()` **之后**再对一次 —— 解析期间用户可能
        //    已经发起新查询，先检查再 await 挡不住旧响应覆盖新状态。
        const detail = (await r.json()).detail ?? "FINRA 源已关闭";
        if (s !== seq.current) return;
        setGated(detail);
        setData(null);
        return;
      }
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as Dark;
      if (s !== seq.current) return;
      setData(d);
      if (!week) setWeek(d.week);
    } catch (e) {
      if (s !== seq.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      if (s === seq.current) setLoading(false);
    }
  }, [ticker, week]);

  useEffect(() => {
    void load();
  }, [load]);

  const N = data?.notes ?? status?.notes;
  const rows = tab === "ats" ? (data?.venues.ats ?? []) : (data?.venues.otc ?? []);

  const seriesOption = useMemo(() => {
    if (!data?.series.length) return null;
    const s = data.series;
    return {
      backgroundColor: "transparent",
      grid: { left: 58, right: 18, top: 30, bottom: 44 },
      legend: { top: 0, textStyle: { color: "#8e8a83", fontSize: 11 } },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1b1b20",
        borderColor: "#2a2a31",
        textStyle: { color: "#f2efe9", fontSize: 11 },
        formatter: (ps: { axisValue: string; seriesName: string; data: number }[]) =>
          `<b>${esc(ps[0]?.axisValue)} 起那周</b><br/>` +
          ps
            .map((p) => `${esc(p.seriesName)}: ${p.data.toLocaleString()} 股`)
            .join("<br/>"),
      },
      xAxis: {
        type: "category",
        data: s.map((x) => x.week),
        axisLine: { lineStyle: { color: "#2a2a31" } },
        axisLabel: { color: "#8e8a83", fontSize: 10 },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#8e8a83", fontSize: 10, formatter: (v: number) => num(v) },
        splitLine: { lineStyle: { color: "#2a2a31", type: "dashed" } },
      },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 6 }],
      series: [
        {
          name: "ATS（暗池）",
          type: "line",
          data: s.map((x) => x.ats_shares),
          showSymbol: false,
          lineStyle: { color: "#ff5a1f", width: 1.8 },
          itemStyle: { color: "#ff5a1f" },
        },
        {
          name: "非 ATS 场外（内部化）",
          type: "line",
          data: s.map((x) => x.otc_shares),
          showSymbol: false,
          lineStyle: { color: "#5b9cf7", width: 1.8 },
          itemStyle: { color: "#5b9cf7" },
        },
      ],
    };
  }, [data]);

  return (
    <>
      <PageHead kicker="Darkpool · 暗池" title="场外成交：ATS 与内部化">
        FINRA 场外成交透明度。这一栏
        <b className="text-ink">默认关闭</b> —— B 级源，条款限非商业用途且
        明文禁止「用本站数据建立数据库」，而本项目正是下载→落 SQLite。
      </PageHead>

      {/* ⭐ 最重要的一条：暗池 ≠ 场外 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          先说清楚这一栏最容易被算错的地方
        </div>
        <p className="mb-1.5">
          <Emph>{N?.ats_vs_otc}</Emph>
        </p>
        <p className="mb-1.5">
          <Emph>{N?.no_double_count}</Emph>
        </p>
        <p>
          <Emph>{N?.lag}</Emph> <Emph>{N?.otc_anonymous}</Emph>
        </p>
      </div>

      {statusErr && (
        <div className="mb-5 rounded-2xl border border-brand/40 bg-brand/8 px-4 py-3 text-xs">
          <b className="text-brand">取不到本栏的开关状态</b>
          <span className="text-dim"> —— {statusErr}。下面的内容可能不完整。</span>
        </div>
      )}

      {/* status 拿不到时的降级关闭卡 —— 别让页面变空白 */}
      {gated && !status && (
        <Card title="这一栏当前是关着的" sub="设置环境变量 VF_ENABLE_FINRA=1 才启用">
          <p className="text-xs leading-relaxed text-dim">{gated}</p>
        </Card>
      )}

      {/* 关闭态 */}
      {gated && status && (
        <Card
          title="这一栏当前是关着的"
          sub={`设置环境变量 ${status.env_var}=1 才启用`}
          right={
            <button
              onClick={() => setShowTerms((v) => !v)}
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         hover:border-brand"
            >
              {showTerms ? "收起条款" : "看条款原文"}
            </button>
          }
        >
          <p className="mb-3 text-xs leading-relaxed text-dim">
            <Emph>{status.why_gated}</Emph>
          </p>
          <pre className="mb-3 overflow-x-auto rounded-lg border border-line bg-card2/60 p-3 font-mono text-[11px] text-ink">
            VF_ENABLE_FINRA=1 python -m uvicorn app:app --host 127.0.0.1 --port 8920
          </pre>
          {showTerms && (
            <div className="space-y-2 rounded-lg border border-line bg-card2/40 p-3 text-[11px] leading-relaxed text-dim">
              <div>
                条款出处：{" "}
                <span className="font-mono text-ink">{status.terms.url}</span>
                （{status.terms.last_modified} 版）
              </div>
              {(
                [
                  ["允许的用途", status.terms.permitted],
                  ["限制 (d)", status.terms.restriction_d],
                  ["限制 (e)", status.terms.restriction_e],
                ] as const
              ).map(([k, v]) => (
                <div key={k}>
                  <b className="text-ink">{k}：</b>
                  <span className="italic">“{v}”</span>
                </div>
              ))}
              <div>
                <b className="text-ink">模糊之处：</b>
                {status.terms.ambiguity}
              </div>
              <div>
                <b className="text-ink">我们的立场：</b>
                {status.terms.our_stance}
              </div>
            </div>
          )}
        </Card>
      )}

      {!gated && (
        <Card
          title={data ? `${data.ticker} · ${data.week} 起那周` : "场外成交"}
          sub={
            data
              ? `本地可选 ${data.weeks.length} 周 · 最新 ${data.weeks[data.weeks.length - 1]}`
              : "输入代码后加载"
          }
          right={
            <div className="flex items-center gap-2">
              <input
                value={tickerInput}
                onChange={(e) => setTickerInput(e.target.value.toUpperCase())}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    setWeek("");
                    setTicker(tickerInput.trim() || "NVDA");
                  }
                }}
                className="w-24 rounded-lg border border-line bg-card2 px-2.5 py-1.5
                           text-xs uppercase text-ink"
              />
              {data && (
                <select
                  value={week}
                  onChange={(e) => setWeek(e.target.value)}
                  className="rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-ink"
                >
                  {[...data.weeks].reverse().map((w) => (
                    <option key={w} value={w}>
                      {w}
                    </option>
                  ))}
                </select>
              )}
              <button
                onClick={() => {
                  setWeek("");
                  setTicker(tickerInput.trim() || "NVDA");
                }}
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

          {data && (
            <>
              <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
                <Stat
                  label="ATS（真暗池）"
                  value={`${num(data.ats.shares)} 股`}
                  sub={`${data.ats.firms} 家 / ${data.ats.records} 条 · ${num(data.ats.trades)} 笔`}
                />
                <Stat
                  label="非 ATS 场外（内部化）"
                  value={`${num(data.otc.shares)} 股`}
                  sub={`${data.otc.records} 条记录 · 能点名 ${data.otc.firms} 家`}
                  warn
                />
                <Stat
                  label="ATS / 非 ATS"
                  value={
                    data.ats_over_otc === null ? "—" : `${data.ats_over_otc.toFixed(2)}×`
                  }
                  sub="小于 1 = 内部化更大"
                />
                <Stat
                  label="场外合计"
                  value={`${num(data.off_exchange_shares)} 股`}
                  sub="⚠️ 这不叫「暗池成交量」"
                />
              </div>

              {/* 对账结果 */}
              <div className="mb-4 space-y-1 text-[11px] leading-relaxed text-dim">
                {(
                  [
                    ["ATS", data.ats.reconcile],
                    ["非 ATS 场外", data.otc.reconcile],
                  ] as const
                ).map(([k, rc]) => (
                  <div key={k}>
                    <b className={rc.matches === false ? "text-brand" : "text-ink"}>
                      {k} 对账：
                    </b>{" "}
                    <Emph>{rc.note}</Emph>
                  </div>
                ))}
                {data.truncated && (
                  <div className="text-brand">
                    ⚠️ 本次取满了行数上限，
                    <span className="text-dim">
                      {" "}
                      周序列<b className="text-ink">可能不全</b> —— 该接口不支持排序，
                      截掉了哪几周无从得知。
                    </span>
                  </div>
                )}
                {data.ats.null_share_records + data.otc.null_share_records > 0 && (
                  <div className="text-brand">
                    ⚠️ 有 {data.ats.null_share_records + data.otc.null_share_records}{" "}
                    条记录成交量为空，
                    <span className="text-dim">
                      {" "}
                      已<b className="text-ink">排除</b>而不是当成 0 —— 合计因此偏小，
                      占比也因此不给。
                    </span>
                  </div>
                )}
                {data.ats.null_trade_records + data.otc.null_trade_records > 0 && (
                  <div className="text-dim">
                    ⚠️ 有 {data.ats.null_trade_records + data.otc.null_trade_records}{" "}
                    条记录笔数为空，已排除（未当成 0），笔数合计偏小。
                  </div>
                )}
                {data.calendar_note && (
                  <div className="text-dim">
                    <Emph>{data.calendar_note}</Emph>
                  </div>
                )}
                {Object.keys(data.unknown_types).length > 0 && (
                  <div className="text-brand">
                    ⚠️ 出现了未知的记录类型：{JSON.stringify(data.unknown_types)} ——
                    <span className="text-dim">
                      {" "}
                      FINRA 可能加了新分类，这部分成交量没有被计入，需要更新解析。
                    </span>
                  </div>
                )}
              </div>

              {/* 占比 */}
              <div className="mb-4 rounded-xl border border-line bg-card2/40 px-3 py-2.5 text-xs leading-relaxed">
                {data.share ? (
                  <>
                    <div className="mb-1 flex flex-wrap gap-x-6 gap-y-1">
                      <span className="text-dim">
                        ATS 占比{" "}
                        <b className="font-mono text-ink">
                          {data.share.ats_pct.toFixed(2)}%
                        </b>
                      </span>
                      <span className="text-dim">
                        非 ATS 场外占比{" "}
                        <b className="font-mono text-ink">
                          {data.share.otc_pct.toFixed(2)}%
                        </b>
                      </span>
                      <span className="text-dim">
                        场外合计{" "}
                        <b className="font-mono text-ink">
                          {data.share.off_exchange_pct.toFixed(2)}%
                        </b>
                      </span>
                    </div>
                    <div className="text-[10px] text-dim">
                      <Emph>{data.share_note}</Emph>
                    </div>
                  </>
                ) : (
                  <div className="text-dim">
                    <b className="text-brand">场外占比算不出来</b> ——
                    分母是同期总成交量，本地那周
                    {data.missing_days.length > 0 && (
                      <>
                        {" "}
                        缺 {data.missing_days.length} 个交易日（
                        <span className="font-mono">{data.missing_days.join("、")}</span>
                        ）
                      </>
                    )}
                    。<Emph>{N?.share_needs_local}</Emph>
                    <div className="mt-1 text-[10px]">
                      去「扫描器」分栏扫一下 {data.ticker}，把那几天的行情攒上就能算了 ——
                      不过这份历史<b className="text-ink">补不回来</b>，
                      只能从装上那天起往后攒。
                    </div>
                  </div>
                )}
              </div>

              {seriesOption && (
                <>
                  <div className="mb-1 text-xs text-dim">
                    逐周走势 —— <b className="text-ink">两条线分开画</b>
                    ，因为它们是两类不同的成交
                  </div>
                  <ReactECharts option={seriesOption} style={{ height: 260 }} notMerge />
                </>
              )}

              <div className="mb-2 mt-4 flex items-center gap-2">
                {(
                  [
                    ["ats", `ATS 暗池 (${data.ats.records} 条)`],
                    ["otc", `非 ATS 场外 (${data.otc.records} 条)`],
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
                  「均每笔」= 成交量 ÷ 笔数，只是个除法结果
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-line text-dim">
                    <tr>
                      <Th>MPID</Th>
                      <Th>机构</Th>
                      <Th>层级</Th>
                      <Th>成交量</Th>
                      <Th>笔数</Th>
                      <Th>均每笔</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((v, i) => (
                      <tr key={`${v.mpid ?? "anon"}-${i}`} className="border-b border-line/50">
                        <Td className="font-mono">{v.mpid ?? "—"}</Td>
                        <Td className={v.name ? "" : "text-dim"}>
                          {v.name ?? "（该数据不披露机构名）"}
                        </Td>
                        <Td className="text-dim">{v.tier ?? "—"}</Td>
                        <Td className="font-mono">{num(v.shares)}</Td>
                        <Td className="font-mono">{num(v.trades)}</Td>
                        <Td className="font-mono">
                          {v.avg_trade_size === null
                            ? "—"
                            : `${v.avg_trade_size.toFixed(0)} 股`}
                        </Td>
                      </tr>
                    ))}
                    {rows.length === 0 && (
                      <tr>
                        <td colSpan={6} className="py-8 text-center text-dim">
                          该周无记录
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
                {tab === "otc" && (data.otc.named_firms ?? 0) === 0 && (
                  <div className="mt-2 text-[10px] leading-relaxed text-dim">
                    <Emph>{N?.otc_anonymous}</Emph>
                  </div>
                )}
              </div>
            </>
          )}
        </Card>
      )}

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        数据源：FINRA OTC Transparency（B 级，默认关闭）+ Cboe 延时行情（分母）。
        本页只呈现数值，不做任何判断与预测。
      </p>
    </>
  );
}

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
      <div className={`mt-0.5 font-mono text-lg ${warn ? "text-[#5b9cf7]" : "text-ink"}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[10px] text-dim">{sub}</div>}
    </div>
  );
}
