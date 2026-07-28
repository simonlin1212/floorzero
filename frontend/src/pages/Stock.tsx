import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Emph, PageHead, Td, Th } from "../components/Shell";

/* ── Types (aligned with the backend's modules/stock.py) ── */
type Notes = { timeline: string; no_score: string; missing: string };
type Lane = {
  key: string;
  title: string;
  lag_note: string;
  as_of: string | null;
  // ⚠️ Each block's **own** lag in days — the sort uses this, not an order we hardcoded
  lag_days: number | null;
  ok: boolean;
  // not_synced / not_enough / disabled / fetch_failed / no_data / no_mapping / bad_symbol
  reason: string | null;
  reason_label: string | null;
  // Only when this is true does it mean "this ticker genuinely has no such activity"; everything else means "we could not get it"
  means_absent: boolean | null;
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

/** The section each lane belongs to — so a missing block says where to go and fill it in */
const LANE_LINK: Record<string, { to: string; label: string }> = {
  quote: { to: "/gex", label: "GEX" },
  gex: { to: "/gex", label: "GEX" },
  flow: { to: "/flow", label: "Options flow" },
  scanner: { to: "/scanner", label: "Scanner" },
  insider: { to: "/insiders", label: "Insiders" },
  shorts: { to: "/shorts", label: "Short data" },
  congress: { to: "/congress", label: "Congress" },
  institution: { to: "/institutions", label: "Institutions" },
  darkpool: { to: "/darkpool", label: "Dark pools" },
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

/** Lag in days → colour. The older it is the greyer it goes, so "this block is stale" is visible at a glance. */
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
  // ⚠️ Submitting a new ticker invalidates in-flight requests **immediately**, without waiting for the effect —
  //    otherwise A returning before B's effect increments seq means A's data is accepted
  //    and shown until B comes back (the box says B while the cards are A).
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
      <PageHead kicker="Stock" title="One ticker, nine lanes">
        Nine sources converging on one ticker. ⚠️ They differ in
        <b className="text-ink"> freshness by two orders of magnitude</b> — so each block carries
        its own instant, ordered newest to oldest.
      </PageHead>

      {/* ⭐ The easiest thing to misread here */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          How much of the word "overview" is padding
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
        title={data ? data.ticker : "Stock"}
        sub={
          data
            ? `${data.available} lanes have data · ${data.unavailable} are empty` +
              (data.lag_spread_days
                ? ` · the newest is ${data.lag_spread_days.newest} days old, the oldest ${data.lag_spread_days.oldest}`
                : "")
            : "Enter a ticker to load"
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
              {loading ? "Loading…" : "Query"}
            </button>
          </div>
        }
      >
        {err && (
          <div className="rounded-lg border border-brand/40 bg-brand/8 px-3 py-2 text-xs text-brand">
            {err}
          </div>
        )}

        {/* ⚠️ The span in days is **always shown**, with no threshold — 10 days and 100 days are both worth knowing,
            and "only mention it beyond 30 days" leaves the reader assuming silence means "all fairly fresh". */}
        {data?.lag_spread_days && (
            <div
              className={`mb-4 rounded-lg px-3 py-2 text-xs ${
                data.lag_spread_days.oldest - data.lag_spread_days.newest > 30
                  ? "border border-brand/40 bg-brand/8"
                  : "border border-line bg-card2/40"
              }`}
            >
              <b className="text-brand">
                These blocks span {data.lag_spread_days.oldest - data.lag_spread_days.newest} days
              </b>
              <span className="text-dim">
                {" "}
                — the newest is from {data.lag_spread_days.newest} days ago and the oldest from{" "}
                {data.lag_spread_days.oldest} days ago.{" "}
                <b className="text-ink">They are not contemporaneous</b>,
                so read each one's instant before stringing them into a story.
              </span>
            </div>
          )}

        {/* Timeline overview */}
        {data && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-line text-dim">
                <tr>
                  <Th>Lane</Th>
                  <Th>Instant</Th>
                  <Th>Age</Th>
                  <Th>Inherent lag</Th>
                  <Th>State</Th>
                </tr>
              </thead>
              <tbody>
                {data.lanes.map((l) => (
                  <tr key={l.key} className="border-b border-line/50">
                    <Td className={l.ok ? "font-semibold" : "text-dim"}>{l.title}</Td>
                    <Td className="font-mono text-dim">{l.as_of ?? "—"}</Td>
                    <Td className={`font-mono ${ageTone(l.lag_days)}`}>
                      {l.lag_days === null ? "—" : `${l.lag_days} days`}
                    </Td>
                    <Td className="text-dim">{l.lag_note}</Td>
                    <Td>
                      {l.ok ? (
                        <span className="text-brand">has data</span>
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

      {/* Block by block */}
      {data?.lanes.map((l) => (
        <Card
          key={l.key}
          title={l.title}
          sub={
            l.ok
              ? `${l.as_of ?? "instant unknown"}${l.lag_days !== null ? ` · ${l.lag_days} days ago` : ""} · ${l.lag_note}`
              : l.reason_label ?? l.reason ?? "no data"
          }
          right={
            LANE_LINK[l.key] && (
              <Link
                to={LANE_LINK[l.key].to}
                className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs
                           text-dim transition hover:border-brand hover:text-ink"
              >
                Go to {LANE_LINK[l.key].label} →
              </Link>
            )
          }
        >
          {!l.ok ? (
            <div className="rounded-lg border border-line bg-card2/40 px-3 py-3 text-xs leading-relaxed text-dim">
              <Emph>{l.detail ?? ""}</Emph>
              {/* ⚠️ Only no_data means "this ticker has no such activity"; nothing else does */}
              {/* The backend gives means_absent directly — the frontend need not decide which reasons count as genuinely absent */}
              {l.means_absent === false && (
                <div className="mt-2 text-[10px]">
                  ⚠️ This lane is empty because
                  <b className="text-ink"> {l.reason_label}</b>,
                  which <b className="text-ink">does not mean</b>{" "}
                  this ticker has no such activity.
                </div>
              )}
            </div>
          ) : (
            <LaneBody lane={l} />
          )}
        </Card>
      ))}

      <p className="mb-6 text-[10px] leading-relaxed text-dim">
        Sources: Cboe (delayed, local only) · SEC EDGAR · House and Senate disclosures (commercial use forbidden) ·
        FINRA (off by default). This page sets the sources side by side, produces no cross-source score,
        and makes no recommendation or prediction.
      </p>
    </>
  );
}

/** A brief rendering of each block — a few numbers only; the detail lives in each section. */
function LaneBody({ lane }: { lane: Lane }) {
  const d = (lane.data ?? {}) as Record<string, any>;
  const rows: [string, string][] = [];

  if (lane.key === "quote") {
    rows.push(["Spot", `$${(d.spot ?? 0).toFixed?.(2) ?? "—"}`]);
    rows.push(["Contracts", num(d.contracts)]);
    rows.push(["Snapshot time", String(d.timestamp ?? "—")]);
  } else if (lane.key === "gex") {
    // ⚠️ Already in billions, so it must **not** go through `num()` — that formatter scales a
    //    raw number and appends B/M/K, and anything under a thousand comes out of it rounded to
    //    a whole number. −0.2007 rendered as "-0B": a real, non-zero figure shown as zero, which
    //    is the one thing this project is built not to do.
    rows.push(["Total GEX",
               typeof d.total_gex_bn === "number" && isFinite(d.total_gex_bn)
                 ? `${d.total_gex_bn.toFixed(2)}B` : "—"]);
    rows.push(["gamma flip", d.gamma_flip == null ? "—" : String(d.gamma_flip)]);
    rows.push(["call wall", String(d.call_wall ?? "—")]);
    rows.push(["put wall", String(d.put_wall ?? "—")]);
  } else if (lane.key === "flow") {
    const c = d.counts ?? {};
    const r = d.ratios ?? {};
    rows.push(["Contracts traded", num(c.traded_contracts)]);
    rows.push(["Unusual", num(c.unusual)]);
    rows.push([
      "P/C (volume / open interest)",
      `${r.by_volume?.pc?.toFixed?.(2) ?? "—"} / ${r.by_oi?.pc?.toFixed?.(2) ?? "—"}`,
    ]);
  } else if (lane.key === "scanner") {
    rows.push(["IV30", d.iv30 == null ? "—" : d.iv30.toFixed(1)]);
    rows.push([
      "IV Rank",
      d.iv_rank == null
        ? `not computable (${d.iv_reason === "insufficient_history" ? `${d.iv_days_needed} more trading days needed` : d.iv_reason})`
        : d.iv_rank.toFixed(1),
    ]);
    rows.push(["Local samples", `${d.iv_samples ?? 0} trading days`]);
  } else if (lane.key === "insider") {
    const c = d.counts ?? {};
    rows.push(["Open-market trades", `${num(c.om)} of ${num(c.n)} including compensation`]);
    rows.push(["Buys / sells", `${num(c.buys)} / ${num(c.sells)}`]);
    rows.push(["Bought / sold", `$${num(c.bv)} / $${num(c.sv)}`]);
  } else if (lane.key === "shorts") {
    const c = d.counts ?? {};
    rows.push(["Records", `${num(c.n)} · ${num(c.days)} settlement dates`]);
    rows.push(["Range", `${c.lo ?? "—"} to ${c.hi ?? "—"}`]);
  } else if (lane.key === "congress") {
    rows.push(["Disclosed trades", `${num(d.count)}`]);
  } else if (lane.key === "darkpool") {
    rows.push(["ATS (genuine dark pools)", `${num(d.ats?.shares)} shares`]);
    rows.push(["Non-ATS (internalisation)", `${num(d.otc?.shares)} shares`]);
    rows.push([
      "ATS / non-ATS",
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
