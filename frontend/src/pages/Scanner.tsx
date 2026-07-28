import { useCallback, useEffect, useRef, useState } from "react";
import { Card, Emph, PageHead, Td, Th } from "../components/Shell";

/* ── Types (aligned with the backend's modules/scanner.py) ── */
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
  // null when not enough history has accrued — **"not computable" is not "a rank of 0"**
  iv_rank: number | null;
  iv_percentile: number | null;
  iv_ready: boolean;
  iv_days_needed: number;
  // ⚠️ **Why** a ranking is null: only insufficient_history means "a few more days and it will be there"
  iv_reason: string | null;
  volume_x_median: number | null;
  volume_samples: number;
  volume_x_reason: string | null;
};

/** Null reasons in plain words (the same wording as the backend's REASON_LABEL, falling back to the raw code). */
const REASON: Record<string, string> = {
  insufficient_history: "not enough history accrued on this machine yet",
  no_current_iv: "this snapshot carries no IV30 (upstream did not provide one)",
  no_current_volume: "this snapshot carries no volume (upstream did not provide one)",
  flat_history: "the history has a zero range (high = low), so a position within it is undefined",
  zero_median: "the median historical volume is 0, so no multiple can be computed",
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
  iv_percentile: "IV percentile",
  iv30: "IV30",
  volume: "Volume",
  volume_x: "Volume / median",
  change_pct: "Change",
  symbol: "Ticker",
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

  // ⚠️ Polling needs a sequence number and overlap guard too: request A is slow, B overtakes it, returns running=false and stops the timer,
  //    then A's stale response writes running=true back — the page shows "scanning" forever
  //    with the timer already stopped, and never corrects itself.
  const pollSeq = useRef(0);
  const inFlight = useRef(false);
  const poll = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      if (inFlight.current) return;        // the last one has not returned; skip this tick
      inFlight.current = true;
      const n = ++pollSeq.current;
      try {
        const r = await fetch("/api/scanner/scan");
        if (!r.ok) return;
        const s = (await r.json()) as ScanState;
        if (n !== pollSeq.current) return;  // a stale response; discard
        setScan(s);
        if (!s.running && pollRef.current) {
          window.clearInterval(pollRef.current);
          pollRef.current = null;
          void load();
        }
      } catch {
        /* a failed poll should not interrupt the page */
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
      // The ref has to be nulled after clearing the timer, or poll() returns immediately on re-entering the page
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [poll]);

  // ⚠️ Cancelling has to check its result too: a 500 or a dropped connection fails silently,
  //    and the user believes they cancelled while the job runs on in the background for 26 minutes.
  const cancelScan = useCallback(async () => {
    try {
      const r = await fetch("/api/scanner/scan/cancel", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
      setScan((await r.json()) as ScanState);
    } catch (e) {
      setErr(
        `Cancelling failed: ${e instanceof Error ? e.message : String(e)} — ` +
          "the background scan is still running.",
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
  // Extrapolate the time remaining from the rate actually measured, rather than guessing a fixed value
  const eta = (() => {
    if (!scan?.running || !scan.started_at || scan.done < 5) return null;
    const elapsed = (Date.now() - new Date(scan.started_at).getTime()) / 1000;
    const per = elapsed / scan.done;
    const left = (scan.total - scan.done) * per;
    return left > 90 ? `about ${Math.round(left / 60)} min` : `about ${Math.round(left)} s`;
  })();

  return (
    <>
      <PageHead kicker="Scanner" title="Market-wide screening">
        Cboe's official delayed quotes. <b className="text-ink">It runs on your own machine only</b> —
        a tier C source, and showing it externally in any form triggers OPRA redistributor status.
      </PageHead>

      {/* ⭐ Two limits, stated up front */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          Two limits of this section, said plainly first
        </div>
        <p className="mb-1.5">
          <Emph>{N?.why_slow}</Emph>
        </p>
        <p>
          <Emph>{N?.iv_rank}</Emph>
        </p>
      </div>

      {/* Scan controls */}
      <Card
        title="Scan"
        sub={
          st
            ? `${num(st.rows)} rows / ${num(st.symbols)} symbols / ${st.sessions} trading sessions accrued locally` +
              (st.earliest ? ` (${st.earliest} to ${st.latest})` : "")
            : "Nothing scanned yet"
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
            Scan these
          </button>
          <button
            onClick={() => void startScan(true)}
            disabled={scan?.running}
            title="6,049 symbols at a self-imposed 4 requests/second ≈ 26 minutes"
            className="rounded-lg border border-brand/50 bg-brand/10 px-3 py-1.5 text-xs
                       text-brand transition hover:bg-brand/20 disabled:opacity-40"
          >
            Scan the whole market (about 26 min)
          </button>
          {scan?.running && (
            <button
              onClick={() => void cancelScan()}
              className="rounded-lg border border-line px-3 py-1.5 text-xs text-dim
                         hover:border-brand hover:text-ink"
            >
              Cancel
            </button>
          )}
        </div>

        {scan?.running && (
          <div className="mb-3">
            <div className="mb-1 flex items-center justify-between text-xs">
              <span className="text-ink">
                {scan.stage} · {scan.done}/{scan.total}
                {eta && <span className="text-dim"> · {eta} remaining</span>}
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

        {/* ⚠️ The failure count has to be shown: if several hundred could not be fetched in a round,
            the results table merely looks like "a few symbols missing", and unsaid it is never noticed. */}
        {scan && !scan.running && scan.error_count > 0 && (
          <div className="mb-3 rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs">
            {/* ⚠️ `error_count` counts **messages**, and dozens missing a session collapse into one.
                The symbols actually affected must be reported through failed, or 20 dropped shows as "1 issue". */}
            <b className="text-brand">
              {scan.failed} symbols did not reach the store last round ({scan.error_count} distinct reasons)
              {(scan.dropped_no_session ?? 0) > 0 &&
                `, of which ${scan.dropped_no_session} had quotes but no trading session from upstream`}
            </b>
            <span className="text-dim">
              {" "}
              — they do not appear in the results, but that is a <b className="text-ink">failed fetch</b>,
              not "these symbols have no data".
            </span>
            <ul className="mt-1 space-y-0.5 font-mono text-[10px] text-dim">
              {scan.errors.slice(0, 4).map((e, i) => (
                <li key={i}>· {e}</li>
              ))}
            </ul>
          </div>
        )}

        {/* IV Rank readiness — the direct answer to "how usable is this section right now" */}
        {st && (
          <div className="rounded-lg border border-line bg-card2/40 px-3 py-2.5 text-xs leading-relaxed text-dim">
            Symbols with enough history for IV Rank:
            <b className="font-mono text-ink"> {num(st.iv_ready_symbols)}</b> /{" "}
            {num(st.symbols)}
            {st.iv_ready_symbols === 0 && (
              <span>
                {" "}
                — not one has reached {data?.thresholds?.iv_min_sample ?? 60} trading days.
                Until then the IV Rank column shows a <b className="text-ink">null</b>,
                which means <b className="text-ink">not enough accrued yet</b>, not "a low rank".
                Open and run it once each trading day and it accrues.
              </span>
            )}
          </div>
        )}

        {data?.batches && data.batches.length > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead className="border-b border-line text-dim">
                <tr>
                  <Th>Started</Th>
                  <Th>Universe</Th>
                  <Th>Scanned</Th>
                  <Th>Stored</Th>
                  <Th>Failed</Th>
                  <Th>Session</Th>
                  <Th>Note</Th>
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
                    <Td className="text-dim">{b.note || (b.finished_at ? "" : "running")}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Results */}
      <Card
        title="Screening results"
        sub={
          data?.session
            ? `Trading session ${data.session} · ${data.count} matching (${num(data.scanned ?? 0)} scanned)`
            : "No data yet"
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
                  By {SORT_LABEL[s] ?? s}
                </option>
              ))}
            </select>
            <button
              onClick={() => void load()}
              disabled={loading}
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         transition hover:border-brand disabled:opacity-40"
            >
              {loading ? "Loading…" : "Refresh"}
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
              [minPrice, setMinPrice, "Min price", "10"],
              [minVolume, setMinVolume, "Min volume", "1000000"],
              [minVolumeX, setMinVolumeX, "Volume ≥ N× median", "2"],
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

        {/* ⚠️ "Excluded because it could not be computed" and "failed the condition" must be said apart */}
        {data?.excluded &&
          (data.excluded.excluded_no_iv_rank > 0 ||
            data.excluded.excluded_no_volume_x > 0) && (
            <div className="mb-3 rounded-lg border border-line bg-card2/40 px-3 py-2 text-[11px] leading-relaxed text-dim">
              A further
              {/* ⚠️ Do not hardcode "not enough history" — the real reason may be a missing current IV,
                  a zero-range history or a zero median, and hardcoding contradicts the breakdown below. */}
              {data.excluded.excluded_no_iv_rank > 0 && (
                <>
                  {" "}
                  <b className="text-ink">{data.excluded.excluded_no_iv_rank}</b> filtered out by that condition because
                  <b className="text-ink"> IV Rank could not be computed</b>
                </>
              )}
              {data.excluded.excluded_no_volume_x > 0 && (
                <>
                  {data.excluded.excluded_no_iv_rank > 0 && ", "}
                  <b className="text-ink">{data.excluded.excluded_no_volume_x}</b> filtered out because
                  <b className="text-ink"> the volume multiple could not be computed</b>
                </>
              )}
              . They are <b className="text-ink">not computable</b>, not "failing the condition".
              {(data.excluded.iv_reasons || data.excluded.volume_reasons) && (
                <span>
                  {" "}
                  Reasons:
                  {/* ⚠️ The two sets of reasons must be **added**, never merged by object spread —
                      a shared key (insufficient_history) would be overwritten and 10+30 become 30. */}
                  {Object.entries(
                    [data.excluded.iv_reasons, data.excluded.volume_reasons].reduce<
                      Record<string, number>
                    >((acc, src) => {
                      for (const [k, v] of Object.entries(src ?? {}))
                        acc[k] = (acc[k] ?? 0) + v;
                      return acc;
                    }, {}),
                  )
                    .map(([k, v]) => `${REASON[k] ?? k}: ${v}`)
                    .join("; ")}
                  .
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
                  <Th>Ticker</Th>
                  <Th>Type</Th>
                  <Th>Price</Th>
                  <Th>Change</Th>
                  <Th>Volume</Th>
                  <Th>Volume / median</Th>
                  <Th>IV30</Th>
                  {/* ⚠️ Named for what it is. The history it ranks against is what this install
                      has accrued, not 252 sessions of market history — Cboe serves only the
                      present and it cannot be backfilled. Called plain "IV Rank" it reads as the
                      standard measure, and a fresh install would appear to be reporting one. */}
                  <Th>IV Rank (local)</Th>
                  <Th>IV percentile</Th>
                  <Th>Samples</Th>
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
                        (r.change_pct ?? 0) < 0 ? "text-[#2563eb]" : "text-brand"
                      }`}
                    >
                      {r.change_pct === null ? "—" : `${r.change_pct.toFixed(2)}%`}
                    </Td>
                    <Td className="font-mono">{num(r.volume)}</Td>
                    <Td
                      className="font-mono"
                      title={
                        r.volume_x_median === null
                          ? REASON[r.volume_x_reason ?? ""] ?? "not computable"
                          : `${r.volume_samples} samples`
                      }
                    >
                      {r.volume_x_median === null ? (
                        <span className="text-dim">
                          {r.volume_x_reason === "zero_median" ? "median 0" : "—"}
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
                            (REASON[r.iv_reason ?? ""] ?? "not computable") +
                            " — this is not computable, not a low rank"
                          }
                        >
                          {r.iv_reason === "insufficient_history"
                            ? `${r.iv_days_needed} more days`
                            : r.iv_reason === "no_current_iv"
                              ? "no IV30"
                              : r.iv_reason === "flat_history"
                                ? "zero range"
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
                {data.count} match; only the first 200 are shown — tighten the conditions to see them all.
              </div>
            )}
          </div>
        )}

        <div className="mt-4 text-[10px] leading-relaxed text-dim">
          <b className="text-ink">IV Rank and IV percentile are two different measures</b>, and this page gives both:
          IV Rank = (current − low) / (high − low), which reads only the two extremes, so one spike in a year
          flattens it permanently; IV percentile = how many days sat below the current value, which reads the whole distribution.
          <br />
          <Emph>{N?.snapshot}</Emph> <Emph>{N?.universe}</Emph>
        </div>
      </Card>

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        Source: Cboe Global Markets delayed quotes. ⛔ For personal research on your own machine only;
        showing it externally makes you an OPRA redistributor ($1,500/month).
        This page presents values and rankings, and makes no recommendation or prediction.
      </p>
    </>
  );
}
