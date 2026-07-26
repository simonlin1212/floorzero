import { useCallback, useEffect, useRef, useState } from "react";
import { Card, Emph, PageHead, Td, Th } from "../components/Shell";

/* ── 类型（与后端 modules/scanner.py 对齐）── */
type Notes = { why_slow: string; iv_rank: string; universe: string; snapshot: string };
type Row = {
  symbol: string;
  session: string | null;
  price: number | null;
  change_pct: number | null;
  volume: number | null;
  iv30: number | null;
  iv30_change: number | null;
  security_type: string | null;
  iv_samples: number;
  // 攒不够历史时为 null —— **「算不出」不是「排名为 0」**
  iv_rank: number | null;
  iv_percentile: number | null;
  iv_ready: boolean;
  iv_days_needed: number;
  // ⚠️ 排名为空的**原因**：只有 insufficient_history 才是"再等几天就有了"
  iv_reason: string | null;
  volume_x_median: number | null;
  volume_samples: number;
  volume_x_reason: string | null;
};

/** 空值理由的人话（与后端 REASON_LABEL 同义，缺省回退到原始码）。 */
const REASON: Record<string, string> = {
  insufficient_history: "本机还没攒够历史",
  no_current_iv: "本次快照没有 IV30（上游没给）",
  no_current_volume: "本次快照没有成交量（上游没给）",
  flat_history: "历史区间为 0（最高=最低），位置无从谈起",
  zero_median: "历史成交量中位数为 0，倍数算不出",
};
type Batch = {
  id: number;
  started_at: string;
  finished_at: string | null;
  universe: number;
  scanned: number;
  stored: number;
  failed: number;
  session: string | null;
  note: string | null;
};
type Stats = {
  rows: number;
  symbols: number;
  sessions: number;
  earliest: string | null;
  latest: string | null;
  iv_ready_symbols: number;
};
type ScanResp = {
  rows: Row[];
  count: number;
  scanned?: number;
  session: string | null;
  truncated?: boolean;
  excluded?: {
    excluded_no_iv_rank: number;
    excluded_no_volume_x: number;
    iv_reasons?: Record<string, number>;
    volume_reasons?: Record<string, number>;
  };
  stats: Stats;
  batches: Batch[];
  sorts?: string[];
  notes: Notes;
  note?: string;
  thresholds?: { iv_lookback: number; iv_min_sample: number };
};
type ScanState = {
  running: boolean;
  stage: string;
  total: number;
  done: number;
  percent: number;
  stored: number;
  failed: number;
  errors: string[];
  error_count: number;
  dropped_no_session?: number;
  stats: Stats;
  started_at: string | null;
};

const SORT_LABEL: Record<string, string> = {
  iv_rank: "IV Rank",
  iv_percentile: "IV 百分位",
  iv30: "IV30",
  volume: "成交量",
  volume_x: "量/中位数",
  change_pct: "涨跌幅",
  symbol: "代码",
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

export default function Scanner() {
  const [sort, setSort] = useState("iv30");
  const [minIvRank, setMinIvRank] = useState("");
  const [minVolume, setMinVolume] = useState("");
  const [minVolumeX, setMinVolumeX] = useState("");
  const [minPrice, setMinPrice] = useState("");
  const [symbols, setSymbols] = useState(
    "AAPL,MSFT,NVDA,TSLA,AMD,GOOG,META,AMZN,SPY,QQQ",
  );

  const [data, setData] = useState<ScanResp | null>(null);
  const [scan, setScan] = useState<ScanState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const seq = useRef(0);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    const s = ++seq.current;
    setLoading(true);
    setErr(null);
    try {
      const q = new URLSearchParams({ sort, limit: "200" });
      if (minIvRank) q.set("min_iv_rank", minIvRank);
      if (minVolume) q.set("min_volume", minVolume);
      if (minVolumeX) q.set("min_volume_x", minVolumeX);
      if (minPrice) q.set("min_price", minPrice);
      const r = await fetch(`/api/scanner?${q}`);
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      const d = (await r.json()) as ScanResp;
      if (s !== seq.current) return;
      setData(d);
    } catch (e) {
      if (s !== seq.current) return;
      setErr(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      if (s === seq.current) setLoading(false);
    }
  }, [sort, minIvRank, minVolume, minVolumeX, minPrice]);

  useEffect(() => {
    void load();
  }, [load]);

  // ⚠️ 轮询也要序号 + 防重叠：请求 A 慢、B 后发先至返回 running=false 并停表，
  //    随后 A 的旧响应把状态写回 running=true —— 页面会永远显示"扫描中"，
  //    而定时器已经停了，再也不会自己纠正。
  const pollSeq = useRef(0);
  const inFlight = useRef(false);
  const poll = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      if (inFlight.current) return;        // 上一次还没回来，跳过这一拍
      inFlight.current = true;
      const n = ++pollSeq.current;
      try {
        const r = await fetch("/api/scanner/scan");
        if (!r.ok) return;
        const s = (await r.json()) as ScanState;
        if (n !== pollSeq.current) return;  // 过期响应，丢弃
        setScan(s);
        if (!s.running && pollRef.current) {
          window.clearInterval(pollRef.current);
          pollRef.current = null;
          void load();
        }
      } catch {
        /* 轮询失败不打断页面 */
      } finally {
        inFlight.current = false;
      }
    }, 2000);
  }, [load]);

  useEffect(() => {
    fetch("/api/scanner/scan")
      .then((r) => (r.ok ? r.json() : null))
      .then((s: ScanState | null) => {
        if (!s) return;
        setScan(s);
        if (s.running) poll();
      })
      .catch(() => {});
    return () => {
      // 清定时器后必须置空 ref，否则重新进页面 poll() 会直接 return
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [poll]);

  // ⚠️ 取消也要看结果：接口 500 或断网时静默失败，
  //    用户以为已经取消了，后台其实还在跑 26 分钟。
  const cancelScan = useCallback(async () => {
    try {
      const r = await fetch("/api/scanner/scan/cancel", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      setScan((await r.json()) as ScanState);
    } catch (e) {
      setErr(
        `取消失败：${e instanceof Error ? e.message : String(e)} —— ` +
          "后台扫描仍在继续。",
      );
    }
  }, []);

  const startScan = useCallback(
    async (all: boolean) => {
      try {
        const q = all ? "" : `?symbols=${encodeURIComponent(symbols)}`;
        const r = await fetch(`/api/scanner/scan${q}`, { method: "POST" });
        if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
        setScan((await r.json()) as ScanState);
        poll();
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    },
    [symbols, poll],
  );

  const st = data?.stats ?? scan?.stats;
  const N = data?.notes;
  // 用实测速率外推剩余时间，不用固定值猜
  const eta = (() => {
    if (!scan?.running || !scan.started_at || scan.done < 5) return null;
    const elapsed = (Date.now() - new Date(scan.started_at).getTime()) / 1000;
    const per = elapsed / scan.done;
    const left = (scan.total - scan.done) * per;
    return left > 90 ? `约 ${Math.round(left / 60)} 分钟` : `约 ${Math.round(left)} 秒`;
  })();

  return (
    <>
      <PageHead kicker="Scanner · 扫描器" title="全市场筛选">
        CBOE 官方延时行情。<b className="text-ink">只在你自己机器上跑</b> ——
        C 级源，任何对外展示都会触发 OPRA redistributor 认定。
      </PageHead>

      {/* ⭐ 两条能力边界摆在最前 */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          先说清楚这一栏的两个限制
        </div>
        <p className="mb-1.5">
          <Emph>{N?.why_slow}</Emph>
        </p>
        <p>
          <Emph>{N?.iv_rank}</Emph>
        </p>
      </div>

      {/* 扫描控制 */}
      <Card
        title="扫描"
        sub={
          st
            ? `本地已攒 ${num(st.rows)} 行 / ${num(st.symbols)} 只 / ${st.sessions} 个交易时段` +
              (st.earliest ? ` （${st.earliest} ~ ${st.latest}）` : "")
            : "还没扫过"
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value.toUpperCase())}
            placeholder="AAPL,MSFT,NVDA…"
            className="min-w-0 flex-1 rounded-lg border border-line bg-card2 px-2.5 py-1.5
                       text-xs uppercase text-ink placeholder:text-dim"
          />
          <button
            onClick={() => void startScan(false)}
            disabled={scan?.running || !symbols.trim()}
            className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                       transition hover:border-brand disabled:opacity-40"
          >
            扫这几只
          </button>
          <button
            onClick={() => void startScan(true)}
            disabled={scan?.running}
            title="6,049 只 × 自律限流 4 次/秒 ≈ 26 分钟"
            className="rounded-lg border border-brand/50 bg-brand/10 px-3 py-1.5 text-xs
                       text-brand transition hover:bg-brand/20 disabled:opacity-40"
          >
            扫全市场（约 26 分钟）
          </button>
          {scan?.running && (
            <button
              onClick={() => void cancelScan()}
              className="rounded-lg border border-line px-3 py-1.5 text-xs text-dim
                         hover:border-brand hover:text-ink"
            >
              取消
            </button>
          )}
        </div>

        {scan?.running && (
          <div className="mb-3">
            <div className="mb-1 flex items-center justify-between text-xs">
              <span className="text-ink">
                {scan.stage} · {scan.done}/{scan.total}
                {eta && <span className="text-dim"> · 剩余 {eta}</span>}
              </span>
              <span className="font-mono text-dim">{scan.percent}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded bg-card2">
              <div
                className="h-full bg-brand transition-all"
                style={{ width: `${scan.percent}%` }}
              />
            </div>
          </div>
        )}

        {/* ⚠️ 失败数必须显示：一轮里若有几百只取不到，
            结果表看上去只是"少了些票"，不说就永远发现不了。 */}
        {scan && !scan.running && scan.error_count > 0 && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            {/* ⚠️ `error_count` 是**消息条数**，缺 session 的几十只会被聚成一条。
                真正受影响的标的数要用 failed 报，否则 20 只被丢弃却显示"1 项异常"。 */}
            <b className="text-brand">
              上一轮 {scan.failed} 只未能入库（{scan.error_count} 类原因）
              {(scan.dropped_no_session ?? 0) > 0 &&
                `，其中 ${scan.dropped_no_session} 只有行情但上游没给交易时段`}
            </b>
            <span className="text-dim">
              {" "}
              —— 它们不会出现在结果里，但那是<b className="text-ink">取不到</b>，
              不是"这些票没有数据"。
            </span>
            <ul className="mt-1 space-y-0.5 font-mono text-[10px] text-dim">
              {scan.errors.slice(0, 4).map((e, i) => (
                <li key={i}>· {e}</li>
              ))}
            </ul>
          </div>
        )}

        {/* IV Rank 就绪度 —— 这一栏"现在有多可用"的直接答案 */}
        {st && (
          <div className="rounded-lg border border-line bg-card2/40 px-3 py-2.5 text-xs leading-relaxed text-dim">
            IV Rank 已攒够历史的标的：
            <b className="font-mono text-ink"> {num(st.iv_ready_symbols)}</b> /{" "}
            {num(st.symbols)}
            {st.iv_ready_symbols === 0 && (
              <span>
                {" "}
                —— 一只都还没到{data?.thresholds?.iv_min_sample ?? 60} 个交易日。
                在此之前 IV Rank 一栏显示<b className="text-ink">空值</b>，
                这是<b className="text-ink">还没攒够</b>，不是"排名很低"。
                每个交易日打开跑一次就会攒起来。
              </span>
            )}
          </div>
        )}

        {data?.batches && data.batches.length > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead className="border-b border-line text-dim">
                <tr>
                  <Th>开始</Th>
                  <Th>全集</Th>
                  <Th>扫到</Th>
                  <Th>入库</Th>
                  <Th>失败</Th>
                  <Th>时段</Th>
                  <Th>备注</Th>
                </tr>
              </thead>
              <tbody>
                {data.batches.map((b) => (
                  <tr key={b.id} className="border-b border-line/50">
                    <Td className="font-mono text-dim">{b.started_at?.slice(0, 16)}</Td>
                    <Td className="font-mono">{num(b.universe)}</Td>
                    <Td className="font-mono">{num(b.scanned)}</Td>
                    <Td className="font-mono">{num(b.stored)}</Td>
                    <Td className={`font-mono ${b.failed ? "text-brand" : "text-dim"}`}>
                      {b.failed}
                    </Td>
                    <Td className="font-mono text-dim">{b.session ?? "—"}</Td>
                    <Td className="text-dim">{b.note || (b.finished_at ? "" : "进行中")}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 结果 */}
      <Card
        title="筛选结果"
        sub={
          data?.session
            ? `交易时段 ${data.session} · ${data.count} 只符合（共扫到 ${num(data.scanned ?? 0)} 只）`
            : "还没有数据"
        }
        right={
          <div className="flex items-center gap-2">
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value)}
              className="rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-ink"
            >
              {(data?.sorts ?? Object.keys(SORT_LABEL)).map((s) => (
                <option key={s} value={s}>
                  按 {SORT_LABEL[s] ?? s}
                </option>
              ))}
            </select>
            <button
              onClick={() => void load()}
              disabled={loading}
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         transition hover:border-brand disabled:opacity-40"
            >
              {loading ? "加载中…" : "刷新"}
            </button>
          </div>
        }
      >
        {err && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {err}
          </div>
        )}

        <div className="mb-3 flex flex-wrap items-center gap-2">
          {(
            [
              [minPrice, setMinPrice, "最低价", "10"],
              [minVolume, setMinVolume, "最低成交量", "1000000"],
              [minVolumeX, setMinVolumeX, "量 ≥ N× 中位数", "2"],
              [minIvRank, setMinIvRank, "IV Rank ≥", "80"],
            ] as const
          ).map(([v, set, label, ph]) => (
            <label key={label} className="flex items-center gap-1.5 text-xs text-dim">
              {label}
              <input
                value={v}
                onChange={(e) => set(e.target.value)}
                placeholder={ph}
                className="w-24 rounded-lg border border-line bg-card2 px-2 py-1
                           text-xs text-ink placeholder:text-dim/50"
              />
            </label>
          ))}
        </div>

        {/* ⚠️ 「因为算不出而被排除」和「不满足条件」必须分开说 */}
        {data?.excluded &&
          (data.excluded.excluded_no_iv_rank > 0 ||
            data.excluded.excluded_no_volume_x > 0) && (
            <div className="mb-3 rounded-lg border border-line bg-card2/40 px-3 py-2 text-[11px] leading-relaxed text-dim">
              另有
              {/* ⚠️ 不写死"还没攒够历史" —— 真实原因可能是当前 IV 缺失、
                  历史区间为 0、中位数为 0，写死会和下面的明细自相矛盾。 */}
              {data.excluded.excluded_no_iv_rank > 0 && (
                <>
                  {" "}
                  <b className="text-ink">{data.excluded.excluded_no_iv_rank}</b> 只因
                  <b className="text-ink"> IV Rank 算不出</b>而被该条件滤掉
                </>
              )}
              {data.excluded.excluded_no_volume_x > 0 && (
                <>
                  {data.excluded.excluded_no_iv_rank > 0 && "、"}
                  <b className="text-ink">{data.excluded.excluded_no_volume_x}</b> 只因
                  <b className="text-ink">量比算不出</b>而被该条件滤掉
                </>
              )}
              。它们是<b className="text-ink">算不出</b>，不是"不满足条件"。
              {(data.excluded.iv_reasons || data.excluded.volume_reasons) && (
                <span>
                  {" "}
                  具体原因：
                  {/* ⚠️ 两组理由要**相加**，不能用对象展开 ——
                      同名键（insufficient_history）会被后者覆盖，10+30 变成 30。 */}
                  {Object.entries(
                    [data.excluded.iv_reasons, data.excluded.volume_reasons].reduce<
                      Record<string, number>
                    >((acc, src) => {
                      for (const [k, v] of Object.entries(src ?? {}))
                        acc[k] = (acc[k] ?? 0) + v;
                      return acc;
                    }, {}),
                  )
                    .map(([k, v]) => `${REASON[k] ?? k} ${v} 只`)
                    .join("、")}
                  。
                </span>
              )}
            </div>
          )}

        {data?.note && (
          <div className="rounded-lg border border-line bg-card2/40 px-3 py-3 text-xs leading-relaxed text-dim">
            <Emph>{data.note}</Emph>
          </div>
        )}

        {data && data.rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-line text-dim">
                <tr>
                  <Th>代码</Th>
                  <Th>类型</Th>
                  <Th>现价</Th>
                  <Th>涨跌</Th>
                  <Th>成交量</Th>
                  <Th>量/中位数</Th>
                  <Th>IV30</Th>
                  <Th>IV Rank</Th>
                  <Th>IV 百分位</Th>
                  <Th>样本</Th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={r.symbol} className="border-b border-line/50">
                    <Td className="font-mono font-semibold">{r.symbol}</Td>
                    <Td className="text-dim">{r.security_type ?? "—"}</Td>
                    <Td className="font-mono">
                      {r.price === null ? "—" : `$${r.price.toFixed(2)}`}
                    </Td>
                    <Td
                      className={`font-mono ${
                        (r.change_pct ?? 0) < 0 ? "text-[#5b9cf7]" : "text-brand"
                      }`}
                    >
                      {r.change_pct === null ? "—" : `${r.change_pct.toFixed(2)}%`}
                    </Td>
                    <Td className="font-mono">{num(r.volume)}</Td>
                    <Td
                      className="font-mono"
                      title={
                        r.volume_x_median === null
                          ? REASON[r.volume_x_reason ?? ""] ?? "算不出"
                          : `${r.volume_samples} 个样本`
                      }
                    >
                      {r.volume_x_median === null ? (
                        <span className="text-dim">
                          {r.volume_x_reason === "zero_median" ? "中位数 0" : "—"}
                        </span>
                      ) : (
                        `${r.volume_x_median.toFixed(2)}×`
                      )}
                    </Td>
                    <Td className="font-mono">
                      {r.iv30 === null ? "—" : r.iv30.toFixed(1)}
                    </Td>
                    <Td className="font-mono">
                      {r.iv_rank === null ? (
                        <span
                          className="text-dim"
                          title={
                            (REASON[r.iv_reason ?? ""] ?? "算不出") +
                            " —— 这是算不出，不是排名低"
                          }
                        >
                          {r.iv_reason === "insufficient_history"
                            ? `还差 ${r.iv_days_needed} 天`
                            : r.iv_reason === "no_current_iv"
                              ? "无 IV30"
                              : r.iv_reason === "flat_history"
                                ? "区间为 0"
                                : "—"}
                        </span>
                      ) : (
                        r.iv_rank.toFixed(1)
                      )}
                    </Td>
                    <Td className="font-mono">
                      {r.iv_percentile === null ? (
                        <span className="text-dim">—</span>
                      ) : (
                        r.iv_percentile.toFixed(1)
                      )}
                    </Td>
                    <Td className="font-mono text-dim">{r.iv_samples}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.truncated && (
              <div className="mt-2 text-[10px] text-dim">
                共 {data.count} 只符合，只显示前 200 —— 收紧条件可以看到全部。
              </div>
            )}
          </div>
        )}

        <div className="mt-4 text-[10px] leading-relaxed text-dim">
          <b className="text-ink">IV Rank 与 IV 百分位是两个口径</b>，本页两个都给：
          IV Rank =（当前 − 最低）/（最高 − 最低），只看两个端点，一年里有一天暴涨
          就会把它永久压扁；IV 百分位 = 有多少天低于当前，看的是整个分布。
          <br />
          <Emph>{N?.snapshot}</Emph> <Emph>{N?.universe}</Emph>
        </div>
      </Card>

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        数据源：Cboe Global Markets 延时行情。⛔ 仅供在本机做个人研究，
        对外展示会被认定为 OPRA redistributor（$1,500/月）。
        本页只呈现数值与排名，不做任何买卖建议与预测。
      </p>
    </>
  );
}
