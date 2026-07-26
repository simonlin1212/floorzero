import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Emph, PageHead, Td, Th } from "../components/Shell";

/* ── 类型（与后端 modules/stock.py 对齐）── */
type Notes = { timeline: string; no_score: string; missing: string };
type Lane = {
  key: string;
  title: string;
  lag_note: string;
  as_of: string | null;
  // ⚠️ 每块**自己的**滞后天数 —— 排序用它，不用我们写死的顺序
  lag_days: number | null;
  ok: boolean;
  // not_synced / not_enough / disabled / fetch_failed / no_data / no_mapping / bad_symbol
  reason: string | null;
  reason_label: string | null;
  detail: string | null;
  data: Record<string, unknown> | null;
};
type Stock = {
  ticker: string;
  lanes: Lane[];
  available: number;
  unavailable: number;
  lag_spread_days: { newest: number; oldest: number } | null;
  notes: Notes;
};

/** 每条线对应的分栏路由 —— 缺数据时给出"去哪儿补" */
const LANE_LINK: Record<string, { to: string; label: string }> = {
  quote: { to: "/gex", label: "GEX 伽马" },
  gex: { to: "/gex", label: "GEX 伽马" },
  flow: { to: "/flow", label: "期权流" },
  scanner: { to: "/scanner", label: "扫描器" },
  insider: { to: "/insiders", label: "内部人" },
  shorts: { to: "/shorts", label: "做空数据" },
  congress: { to: "/congress", label: "国会交易" },
  institution: { to: "/institutions", label: "机构持仓" },
  darkpool: { to: "/darkpool", label: "暗池" },
};

function num(n: unknown): string {
  if (typeof n !== "number" || !isFinite(n)) return "—";
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e9) return `${s}${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(0)}K`;
  return `${s}${a.toFixed(0)}`;
}

/** 滞后天数 → 颜色。越旧越灰，让"这块很旧"一眼可见。 */
function ageTone(d: number | null): string {
  if (d === null) return "text-dim";
  if (d <= 3) return "text-brand";
  if (d <= 20) return "text-ink";
  return "text-dim";
}

export default function StockPage() {
  const [input, setInput] = useState("NVDA");
  const [ticker, setTicker] = useState("NVDA");
  const [data, setData] = useState<Stock | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  // ⚠️ 提交新代码时**立刻**作废在途请求，不等 effect 跑起来 ——
  //    否则 A 在 B 的 effect 递增 seq 之前返回，A 的数据会被接纳，
  //    并一直显示到 B 回来为止（输入框写着 B、卡片是 A）。
  const submit = useCallback((t: string) => {
    seq.current += 1;
    setTicker(t.trim() || "NVDA");
  }, []);

  const load = useCallback(async () => {
    const s = ++seq.current;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(ticker)}`);
      if (!r.ok) {
        const detail = (await r.json()).detail ?? `HTTP ${r.status}`;
        if (s !== seq.current) return;
        throw new Error(detail);
      }
      const d = (await r.json()) as Stock;
      if (s !== seq.current) return;
      setData(d);
    } catch (e) {
      if (s !== seq.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      if (s === seq.current) setLoading(false);
    }
  }, [ticker]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <PageHead kicker="Stock · 个股" title="一只票的九条线">
        把九个数据源在同一只票上汇合。⚠️ 它们的
        <b className="text-ink">新鲜度相差两个数量级</b> —— 所以每块都标了
        自己的时点，按从新到旧排。
      </PageHead>

      {/* ⭐ 这一栏最容易被误读的地方 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          先说清楚「全景」这两个字的水分
        </div>
        <p className="mb-1.5">
          <Emph>{data?.notes.timeline}</Emph>
        </p>
        <p className="mb-1.5">
          <Emph>{data?.notes.no_score}</Emph>
        </p>
        <p>
          <Emph>{data?.notes.missing}</Emph>
        </p>
      </div>

      <Card
        title={data ? data.ticker : "个股"}
        sub={
          data
            ? `${data.available} 条线有数据 · ${data.unavailable} 条空着` +
              (data.lag_spread_days
                ? ` · 最新的是 ${data.lag_spread_days.newest} 天前、最旧的是 ${data.lag_spread_days.oldest} 天前`
                : "")
            : "输入代码后加载"
        }
        right={
          <div className="flex items-center gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value.toUpperCase())}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit(input);
              }}
              className="w-24 rounded-lg border border-line bg-card2 px-2.5 py-1.5
                         text-xs uppercase text-ink"
            />
            <button
              onClick={() => submit(input)}
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
          <div className="rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {err}
          </div>
        )}

        {/* ⚠️ 横跨天数**总是显示**，不设阈值 —— 跨 10 天和跨 100 天都值得知道，
            而"只有超过 30 天才提"会让读者以为没提就是"都挺新的"。 */}
        {data?.lag_spread_days && (
            <div
              className={`mb-4 rounded-lg px-3 py-2 text-xs ${
                data.lag_spread_days.oldest - data.lag_spread_days.newest > 30
                  ? "border border-brand/40 bg-brand/8"
                  : "border border-line bg-card2/40"
              }`}
            >
              <b className="text-brand">
                这几块数据横跨 {data.lag_spread_days.oldest - data.lag_spread_days.newest} 天
              </b>
              <span className="text-dim">
                {" "}
                —— 最新的来自 {data.lag_spread_days.newest} 天前、最旧的来自{" "}
                {data.lag_spread_days.oldest} 天前。
                <b className="text-ink">它们不是同一时刻的事</b>，
                串成一个故事之前先看清各自的时点。
              </span>
            </div>
          )}

        {/* 时间轴总览 */}
        {data && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-line text-dim">
                <tr>
                  <Th>数据线</Th>
                  <Th>时点</Th>
                  <Th>距今</Th>
                  <Th>天然滞后</Th>
                  <Th>状态</Th>
                </tr>
              </thead>
              <tbody>
                {data.lanes.map((l) => (
                  <tr key={l.key} className="border-b border-line/50">
                    <Td className={l.ok ? "font-semibold" : "text-dim"}>{l.title}</Td>
                    <Td className="font-mono text-dim">{l.as_of ?? "—"}</Td>
                    <Td className={`font-mono ${ageTone(l.lag_days)}`}>
                      {l.lag_days === null ? "—" : `${l.lag_days} 天`}
                    </Td>
                    <Td className="text-dim">{l.lag_note}</Td>
                    <Td>
                      {l.ok ? (
                        <span className="text-brand">有数据</span>
                      ) : (
                        <span className="text-dim" title={l.detail ?? ""}>
                          {l.reason_label ?? l.reason}
                        </span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 逐块详情 */}
      {data?.lanes.map((l) => (
        <Card
          key={l.key}
          title={l.title}
          sub={
            l.ok
              ? `${l.as_of ?? "时点未知"}${l.lag_days !== null ? ` · ${l.lag_days} 天前` : ""} · ${l.lag_note}`
              : l.reason_label ?? l.reason ?? "无数据"
          }
          right={
            LANE_LINK[l.key] && (
              <Link
                to={LANE_LINK[l.key].to}
                className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs
                           text-dim transition hover:border-brand hover:text-ink"
              >
                去「{LANE_LINK[l.key].label}」→
              </Link>
            )
          }
        >
          {!l.ok ? (
            <div className="rounded-lg border border-line bg-card2/40 px-3 py-3 text-xs leading-relaxed text-dim">
              <Emph>{l.detail ?? ""}</Emph>
              {/* ⚠️ 只有 no_data 才是"这只票没有那类活动"，别的都不是 */}
              {l.reason !== "no_data" && (
                <div className="mt-2 text-[10px]">
                  ⚠️ 这一栏空着是因为
                  <b className="text-ink">{l.reason_label}</b>，
                  <b className="text-ink">不等于</b>
                  这只票没有这类活动。
                </div>
              )}
            </div>
          ) : (
            <LaneBody lane={l} />
          )}
        </Card>
      ))}

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        数据源：Cboe（延时，仅本地）· SEC EDGAR · 众议院/参议院披露（禁商用）·
        FINRA（默认关闭）。本页只把各源并排呈现，不做跨源综合评分、
        不做任何买卖建议与预测。
      </p>
    </>
  );
}

/** 各块的简要呈现 —— 只挑几个数，详情在各自分栏。 */
function LaneBody({ lane }: { lane: Lane }) {
  const d = (lane.data ?? {}) as Record<string, any>;
  const rows: [string, string][] = [];

  if (lane.key === "quote") {
    rows.push(["现价", `$${(d.spot ?? 0).toFixed?.(2) ?? "—"}`]);
    rows.push(["合约数", num(d.contracts)]);
    rows.push(["快照时刻", String(d.timestamp ?? "—")]);
  } else if (lane.key === "gex") {
    rows.push(["总 GEX", `${num(d.total_gex_bn)}B`]);
    rows.push(["gamma flip", d.gamma_flip == null ? "—" : String(d.gamma_flip)]);
    rows.push(["call wall", String(d.call_wall ?? "—")]);
    rows.push(["put wall", String(d.put_wall ?? "—")]);
  } else if (lane.key === "flow") {
    const c = d.counts ?? {};
    const r = d.ratios ?? {};
    rows.push(["有成交合约", num(c.traded_contracts)]);
    rows.push(["异动", num(c.unusual)]);
    rows.push([
      "P/C（成交量 / 持仓量）",
      `${r.by_volume?.pc?.toFixed?.(2) ?? "—"} / ${r.by_oi?.pc?.toFixed?.(2) ?? "—"}`,
    ]);
  } else if (lane.key === "scanner") {
    rows.push(["IV30", d.iv30 == null ? "—" : d.iv30.toFixed(1)]);
    rows.push([
      "IV Rank",
      d.iv_rank == null
        ? `算不出（${d.iv_reason === "insufficient_history" ? `还差 ${d.iv_days_needed} 个交易日` : d.iv_reason}）`
        : d.iv_rank.toFixed(1),
    ]);
    rows.push(["本地样本", `${d.iv_samples ?? 0} 个交易日`]);
  } else if (lane.key === "insider") {
    const c = d.counts ?? {};
    rows.push(["公开市场交易", `${num(c.om)} 笔（共 ${num(c.n)} 笔含薪酬类）`]);
    rows.push(["买 / 卖", `${num(c.buys)} / ${num(c.sells)} 笔`]);
    rows.push(["买额 / 卖额", `$${num(c.bv)} / $${num(c.sv)}`]);
  } else if (lane.key === "shorts") {
    const c = d.counts ?? {};
    rows.push(["记录", `${num(c.n)} 条 · ${num(c.days)} 个结算日`]);
    rows.push(["区间", `${c.lo ?? "—"} ~ ${c.hi ?? "—"}`]);
  } else if (lane.key === "congress") {
    rows.push(["申报交易", `${num(d.count)} 笔`]);
  } else if (lane.key === "darkpool") {
    rows.push(["ATS（真暗池）", `${num(d.ats?.shares)} 股`]);
    rows.push(["非 ATS（内部化）", `${num(d.otc?.shares)} 股`]);
    rows.push([
      "ATS / 非 ATS",
      d.ats_over_otc == null ? "—" : `${d.ats_over_otc.toFixed(2)}×`,
    ]);
  }

  return (
    <>
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
        {rows.map(([k, v]) => (
          <div key={k} className="rounded-xl border border-line bg-card2/60 px-3 py-2.5">
            <div className="text-[10px] uppercase tracking-wide text-dim">{k}</div>
            <div className="mt-0.5 break-words font-mono text-sm text-ink">{v}</div>
          </div>
        ))}
      </div>
      {lane.key === "flow" && d.limits?.no_direction && (
        <div className="mt-3 text-[10px] leading-relaxed text-dim">
          <Emph>{String(d.limits.no_direction)}</Emph>
        </div>
      )}
      {lane.key === "darkpool" && d.share_note && (
        <div className="mt-3 text-[10px] leading-relaxed text-dim">
          <Emph>{String(d.share_note)}</Emph>
        </div>
      )}
    </>
  );
}
