import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── 类型（与后端 modules/shorts.py 对齐）── */
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
    // ⚠️ 明细与汇总共用同一份查询串
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
        /* 轮询失败不打断页面 */
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
      // 清定时器后必须把 ref 置空，否则新的 pollSync() 会直接 return
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
      setErr(`同步启动失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /* ── 交割失败余额最大的标的 ── */
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
            `各结算日余额均值 ${num(r.avg_quantity ?? 0)} 股<br/>` +
            `峰值 ${num(r.max_quantity ?? 0)} 股<br/>` +
            `出现在 ${r.days} 个结算日`
          );
        },
      },
      xAxis: {
        type: "value",
        name: "股",
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
            // ⚠️ 无价格时标「无报价」而不是 $0：SEC 的价格字段在
            // 「不可得或低于一美分」时是 "."，标成 $0 会让人以为这只股票不值钱
            formatter: (p: any) => {
              const v = rows[p.dataIndex].avg_value;
              return v == null || v === 0 ? "无报价" : money(v);
            },
          },
        },
      ],
    };
  }, [summary]);

  /* ── 各结算日的全市场余额 ── */
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
            `全市场未交割余额 ${num(r.total_quantity)} 股<br/>` +
            `${r.symbols.toLocaleString()} 只标的有余额`
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
          // ⚠️ 用柱不用折线：折线会暗示"连续演进"，而 SEC 明说相邻两日的余额
          // "may have little or no relationship" —— 各日是独立时点，不该连起来看趋势
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
      <PageHead kicker="Short Data · SEC Fails-to-Deliver" title="做空数据">
        主源是 <b className="text-ink">SEC 交割失败（FTD）</b>数据 ——
        美国政府公开记录，不限商用。FINRA 的场外空头成交量另有条款限制，
        <b className="text-ink">默认关闭</b>（见下方）。
      </PageHead>

      {/* ⭐ 三条官方原文 —— 这个分栏的数据最容易被读反 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/[0.06] p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          先看懂这三条，否则数据会读反
        </div>
        <ul className="space-y-2 text-sm leading-relaxed text-dim">
          <li>
            <b className="text-ink">① FTD 不是当日新增，是累计余额。</b>{" "}
            SEC 原文：「Fails to deliver on a given day are a cumulative number of all
            fails outstanding until that day... <b className="text-ink">The figure is not
            a daily amount of fails</b>... may have little or no relationship to
            yesterday's aggregate fails. Thus... <b className="text-ink">the age of fails
            cannot be determined</b> by looking at these numbers.」
            <br />
            → 所以本页<b className="text-ink">不做日环比、不谈「激增」</b>，
            下方按结算日用柱状图而非折线 —— 相邻两日不构成连续趋势。
          </li>
          <li>
            <b className="text-ink">② FTD 不是裸卖空的证据。</b> SEC 原文：
            「fails-to-deliver can occur for a number of reasons on{" "}
            <b className="text-ink">both long and short sales</b>. Therefore,
            fails-to-deliver are <b className="text-ink">not necessarily the result of
            short selling, and are not evidence of abusive short selling or 'naked'
            short selling</b>.」
            <br />
            → 这恰恰是该数据最流行的用法。实测余额最大的标的是 GOOG / AMD / XOM
            这类高流动性大盘股，而非小盘股 —— 与「裸卖空打压」的叙事并不吻合。
          </li>
          <li>
            <b className="text-ink">③ 空头成交量 ≠ 空头持仓。</b> FINRA 原文：
            「short interest position data <b className="text-ink">does not—and is not
            intended to—equate to</b> the daily short sale volume data.」
            且该文件只含<b className="text-ink">场外</b>成交
            （not consolidated with exchange data）→
            拿它算「全市场做空占比」是错的。
          </li>
        </ul>
      </div>

      {/* 同步 */}
      <Card
        title="本地数据"
        sub={
          stats && stats.rows
            ? `${stats.rows.toLocaleString()} 条 · ${stats.symbols.toLocaleString()} 个标的 · ` +
              `${stats.days} 个结算日 · 覆盖 ${stats.earliest} ~ ${stats.latest}` +
              (stats.last_sync ? ` · 上次同步 ${stats.last_sync.replace("T", " ")}` : "")
            : "尚未导入"
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
                  最近 {n} 档（{n / 2} 个月）
                </option>
              ))}
            </select>
            <button
              onClick={startSync}
              disabled={sync?.running}
              className="rounded-lg border border-brand/40 bg-brand/10 px-3.5 py-1.5 font-mono
                         text-xs text-brand transition hover:bg-brand/20 disabled:opacity-40"
            >
              {sync?.running ? "导入中…" : "导入 FTD"}
            </button>
          </div>
        }
      >
        {sync?.running && (
          <div className="mb-2 font-mono text-[11px] text-dim">
            {sync.stage}
            {sync.rows > 0 && ` · 已入库 ${sync.rows.toLocaleString()} 条`}
          </div>
        )}
        <div className="space-y-1 text-[11px] leading-relaxed text-dim">
          <div>
            SEC 每月发<b className="text-ink">两个半月档</b>：上半月的月底发、
            下半月的次月 15 号左右发 —— 所以最新一两档常常还没有，属正常。
          </div>
          {stats && stats.tags.length > 0 && <div>已导入档：{stats.tags.join("、")}</div>}
        </div>
        {sync && sync.error_count > 0 && (
          <details className="mt-2 text-xs text-dim">
            <summary className="cursor-pointer">同步中有 {sync.error_count} 条提示</summary>
            <ul className="mt-1.5 space-y-0.5 font-mono text-[10px]">
              {sync.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </details>
        )}
      </Card>

      {/* ⚠️ FINRA 合规：把条款原文摆出来，判断交给用户 */}
      {finra && (
        <Card
          title={`FINRA 场外空头成交量 · ${finra.enabled ? "已开启" : "默认关闭"}`}
          sub="这条线的合规判断由你自己做 —— 我们只保证你看得到条款原文"
          right={
            <button
              onClick={() => setShowTerms((v) => !v)}
              className="shrink-0 rounded-lg border border-line bg-card2 px-3 py-1.5 font-mono
                         text-xs text-dim hover:text-ink"
            >
              {showTerms ? "收起条款" : "查看条款原文"}
            </button>
          }
        >
          <div className="text-xs leading-relaxed text-dim">
            {finra.enabled ? (
              <span>
                已通过 <span className="font-mono text-ink">{finra.env_var}=1</span> 开启。
              </span>
            ) : (
              <span>
                未开启。要用请设{" "}
                <span className="font-mono text-ink">{finra.env_var}=1</span> 并重启后端。
                <b className="text-ink">
                  {" "}
                  本分栏主源是 SEC FTD，不开这条也完全可用。
                </b>
              </span>
            )}
          </div>
          {showTerms && (
            <div className="mt-3 space-y-2 rounded-lg border border-line bg-card2 p-3.5 text-[11px] leading-relaxed">
              <div className="font-mono text-[10px] text-dim">
                FINRA Terms of Use · 最后修改 {finra.terms.last_modified} ·{" "}
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
                <b className="text-ink">Permitted Uses：</b>
                <span className="text-dim">「{finra.terms.permitted}」</span>
              </div>
              <div>
                <b className="text-ink">Restrictions (d)：</b>
                <span className="text-dim">「{finra.terms.restriction_d}」</span>
              </div>
              <div>
                <b className="text-ink">Restrictions (e)：</b>
                <span className="text-dim">「{finra.terms.restriction_e}」</span>
              </div>
              <div className="border-t border-line pt-2">
                <b className="text-brand">⚠️ 存在真实的模糊地带：</b>
                <span className="text-dim"> {finra.terms.ambiguity}</span>
              </div>
              <div>
                <b className="text-ink">本项目的处理：</b>
                <span className="text-dim"> {finra.terms.our_stance}</span>
              </div>
            </div>
          )}
        </Card>
      )}

      {/* 筛选 */}
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
            全部结算日
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
              placeholder="按代码筛选"
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
                清除
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
        <Card title="本地还没有数据" sub="导入一档约几秒">
          <div className="text-sm leading-relaxed text-dim">
            点右上角「导入 FTD」。SEC 每月发两个半月档，建议先导最近 4 档（两个月）。
          </div>
        </Card>
      )}

      {filterEmpty && !err && (
        <Card title="当前筛选没有命中" sub={`本地共 ${(stats?.rows ?? 0).toLocaleString()} 条`}>
          <div className="text-sm leading-relaxed text-dim">
            该代码在已导入的区间里没有交割失败余额记录 ——
            <b className="text-ink">这是正常情况</b>：余额为零的标的不会出现在文件里。
          </div>
        </Card>
      )}

      {!cacheEmpty && !filterEmpty && summary && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="记录数" value={(summary.counts.n ?? 0).toLocaleString()} />
            <Stat label="涉及标的" value={(summary.counts.syms ?? 0).toLocaleString()} />
            <Stat label="结算日" value={String(summary.counts.days ?? 0)} />
            <Stat
              label="区间"
              value={`${(summary.counts.lo ?? "").slice(5)} ~ ${(summary.counts.hi ?? "").slice(5)}`}
            />
          </div>

          <Card
            title="交割失败余额最大的标的"
            sub="按各结算日余额的均值排序（不是加总）· 条上标注名义金额"
          >
            {summary.by_symbol.length ? (
              <>
                <ReactECharts option={symbolOption} style={{ height: 400 }} notMerge />
                <div className="mt-2 text-[11px] leading-relaxed text-dim">
                  ⚠️ 用均值不用加总：FTD 是<b className="text-ink">某时点的累计余额</b>，
                  同一笔未交割会在连续多个结算日重复出现，把各日相加没有意义。
                </div>
              </>
            ) : (
              <div className="py-8 text-center text-sm text-dim">无数据</div>
            )}
          </Card>

          {days.length > 1 && (
            <Card title="各结算日的全市场未交割余额" sub="每根柱是一个独立时点，不构成趋势">
              <ReactECharts option={dateOption} style={{ height: 280 }} notMerge />
              <div className="mt-2 text-[11px] leading-relaxed text-dim">
                ⚠️ 刻意用柱不用折线：SEC 明说相邻两日的余额
                「may have little or no relationship」——
                折线会暗示一种并不存在的连续演进。
              </div>
            </Card>
          )}

          <Card title="明细" sub={`${fails.length} 条 · 按结算日与金额倒序`}>
            <div className="-mx-1 overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-xs">
                <thead className="text-dim">
                  <tr className="border-b border-line">
                    <Th>结算日</Th>
                    <Th>代码</Th>
                    <Th>名称</Th>
                    <Th>CUSIP</Th>
                    <Th>未交割余额</Th>
                    <Th>前收价</Th>
                    <Th>名义金额</Th>
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
                        {f.price != null ? `$${f.price.toFixed(2)}` : "无报价"}
                      </Td>
                      <Td className="text-ink">
                        {f.value != null ? money(f.value) : <span className="text-dim">无报价</span>}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      {/* 口径与边界 */}
      <div className="mb-5 rounded-2xl border border-brand/25 bg-brand/5 p-5">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-brand">
          口径与边界
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
            · <b className="text-ink">价格可能缺失</b>：SEC 说明价格字段在
            「不可得或低于一美分」时留空，这类记录本页标为「无报价」——
            不是价值为零。
          </li>
          <li>
            · <b className="text-ink">余额为零不会出现在文件里</b>：某标的查不到记录，
            意味着它当日没有未交割余额，不是数据缺失。
          </li>
          <li>
            · <b className="text-ink">发布有滞后</b>：上半月的档月底才发、
            下半月的次月 15 号左右发。SEC 亦声明「We cannot guarantee the accuracy of the data」。
          </li>
          <li>
            · 数据源 SEC（美国政府公开记录，不限商用）。FINRA 那条另有条款限制、默认关闭。
            本页只呈现已公开的数据，
            <b className="text-ink">不打「被做空」标签、不做评分、不构成任何投资建议</b>。
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
