import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Card, Emph, PageHead, Td, Th, esc } from "../components/Shell";

/* ── Types (aligned with the backend's modules/darkpool.py) ── */
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
  // ⚠️ `records` is a row count; `firms` is the number of distinct MPIDs. On the non-ATS side
  //    every MPID is blank — 32 records name not one firm, and the two numbers must not be conflated.
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
  // null when the denominator is incomplete — **no approximation is substituted**
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
  // Hitting the row limit = the weekly series may be incomplete, and since the endpoint cannot sort, which weeks were cut is unknown
  truncated: boolean;
  weekdays: string[];
  locally_observed_days: string[];
  // The explanation when a whole week cannot be observed locally (a holiday and a missed scan being indistinguishable)
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
    // ⚠️ A failure here **must not be swallowed**: the disabled card requires `gated && status` and the main card `!gated`,
    //    so without status neither renders — the page goes blank with nothing to show what happened.
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
        // ⚠️ 409 = **this section is switched off** (a configuration state), not "there is no data".
        //    The two must look entirely different in the interface.
        // ⚠️ The sequence number is checked again **after** `await r.json()` — during parsing the user may
        //    have started a new query, and checking before the await does not stop a stale response overwriting the new state.
        const detail = (await r.json()).detail ?? "The FINRA source is switched off";
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
          `<b>Week beginning ${esc(ps[0]?.axisValue)}</b><br/>` +
          ps
            .map((p) => `${esc(p.seriesName)}: ${p.data.toLocaleString()} shares`)
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
          name: "ATS (dark pools)",
          type: "line",
          data: s.map((x) => x.ats_shares),
          showSymbol: false,
          lineStyle: { color: "#ff5a1f", width: 1.8 },
          itemStyle: { color: "#ff5a1f" },
        },
        {
          name: "Non-ATS off-exchange (internalisation)",
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
      <PageHead kicker="Darkpool" title="Off-exchange: ATS and internalisation">
        FINRA off-exchange transparency. This section is
        <b className="text-ink"> off by default</b> — a tier B source whose terms restrict it to non-commercial use and
        explicitly forbid using the site's data to build a database, which is exactly what this project does, downloading into SQLite.
      </PageHead>

      {/* ⭐ The most important point: a dark pool is not the same as off-exchange */}
      <div className="mb-5 rounded-2xl border border-brand/30 bg-brand/5 p-4 text-xs leading-relaxed text-dim">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-brand">
          The easiest thing to compute wrongly here, said first
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
          <b className="text-brand">Could not read this section's on/off state</b>
          <span className="text-dim"> — {statusErr}. What follows may be incomplete.</span>
        </div>
      )}

      {/* The fallback disabled card for when status cannot be fetched — do not let the page go blank */}
      {gated && !status && (
        <Card title="This section is currently switched off" sub="Set the environment variable FZ_ENABLE_FINRA=1 to enable it">
          <p className="text-xs leading-relaxed text-dim">{gated}</p>
        </Card>
      )}

      {/* Switched off */}
      {gated && status && (
        <Card
          title="This section is currently switched off"
          sub={`Set the environment variable ${status.env_var}=1 to enable it`}
          right={
            <button
              onClick={() => setShowTerms((v) => !v)}
              className="rounded-lg border border-line bg-card2 px-3 py-1.5 text-xs text-ink
                         hover:border-brand"
            >
              {showTerms ? "Hide the terms" : "Read the terms as written"}
            </button>
          }
        >
          <p className="mb-3 text-xs leading-relaxed text-dim">
            <Emph>{status.why_gated}</Emph>
          </p>
          <pre className="mb-3 overflow-x-auto rounded-lg border border-line bg-card2/60 p-3 font-mono text-[11px] text-ink">
            FZ_ENABLE_FINRA=1 python -m uvicorn app:app --host 127.0.0.1 --port 8920
          </pre>
          {showTerms && (
            <div className="space-y-2 rounded-lg border border-line bg-card2/40 p-3 text-[11px] leading-relaxed text-dim">
              <div>
                Terms from:{" "}
                <span className="font-mono text-ink">{status.terms.url}</span>{" "}
                ({status.terms.last_modified} version)
              </div>
              {(
                [
                  ["Permitted Uses", status.terms.permitted],
                  ["Restrictions (d)", status.terms.restriction_d],
                  ["Restrictions (e)", status.terms.restriction_e],
                ] as const
              ).map(([k, v]) => (
                <div key={k}>
                  <b className="text-ink">{k}:</b>
                  <span className="italic">“{v}”</span>
                </div>
              ))}
              <div>
                <b className="text-ink">There is a genuinely ambiguous area:</b>
                {status.terms.ambiguity}
              </div>
              <div>
                <b className="text-ink">How this project handles it:</b>
                {status.terms.our_stance}
              </div>
            </div>
          )}
        </Card>
      )}

      {!gated && (
        <Card
          title={data ? `${data.ticker} · week beginning ${data.week}` : "Off-exchange volume"}
          sub={
            data
              ? `${data.weeks.length} weeks available locally · newest ${data.weeks[data.weeks.length - 1]}`
              : "Enter a ticker to load"
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
                {loading ? "Loading…" : "Query"}
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
                  label="ATS (genuine dark pools)"
                  value={`${num(data.ats.shares)} shares`}
                  sub={`${data.ats.firms} firms / ${data.ats.records} records · ${num(data.ats.trades)} trades`}
                />
                <Stat
                  label="Non-ATS off-exchange (internalisation)"
                  value={`${num(data.otc.shares)} shares`}
                  sub={`${data.otc.records} records · ${data.otc.firms} can be named`}
                  warn
                />
                <Stat
                  label="ATS / non-ATS"
                  value={
                    data.ats_over_otc === null ? "—" : `${data.ats_over_otc.toFixed(2)}×`
                  }
                  sub="Below 1 = internalisation is larger"
                />
                <Stat
                  label="Off-exchange total"
                  value={`${num(data.off_exchange_shares)} shares`}
                  sub="⚠️ This is not called dark pool volume"
                />
              </div>

              {/* Reconciliation */}
              <div className="mb-4 space-y-1 text-[11px] leading-relaxed text-dim">
                {(
                  [
                    ["ATS", data.ats.reconcile],
                    ["Non-ATS off-exchange", data.otc.reconcile],
                  ] as const
                ).map(([k, rc]) => (
                  <div key={k}>
                    <b className={rc.matches === false ? "text-brand" : "text-ink"}>
                      {k} reconciliation:
                    </b>{" "}
                    <Emph>{rc.note}</Emph>
                  </div>
                ))}
                {data.truncated && (
                  <div className="text-brand">
                    ⚠️ This fetch hit the row limit,
                    <span className="text-dim">
                      {" "}
                      so the weekly series <b className="text-ink">may be incomplete</b> — the endpoint cannot sort,
                      and which weeks were cut is unknowable.
                    </span>
                  </div>
                )}
                {data.ats.null_share_records + data.otc.null_share_records > 0 && (
                  <div className="text-brand">
                    ⚠️ {data.ats.null_share_records + data.otc.null_share_records}{" "}
                    records have a null volume,
                    <span className="text-dim">
                      {" "}
                      and were <b className="text-ink">excluded</b> rather than counted as 0 — so the totals are correspondingly small,
                      and the off-exchange share is withheld.
                    </span>
                  </div>
                )}
                {data.ats.null_trade_records + data.otc.null_trade_records > 0 && (
                  <div className="text-dim">
                    ⚠️ {data.ats.null_trade_records + data.otc.null_trade_records}{" "}
                    records have a null trade count and were excluded (not counted as 0), so the trade totals are small.
                  </div>
                )}
                {data.calendar_note && (
                  <div className="text-dim">
                    <Emph>{data.calendar_note}</Emph>
                  </div>
                )}
                {Object.keys(data.unknown_types).length > 0 && (
                  <div className="text-brand">
                    ⚠️ Unrecognised record types appeared: {JSON.stringify(data.unknown_types)} —
                    <span className="text-dim">
                      {" "}
                      FINRA may have added a category, that volume has not been counted, and the parser needs updating.
                    </span>
                  </div>
                )}
              </div>

              {/* Share */}
              <div className="mb-4 rounded-xl border border-line bg-card2/40 px-3 py-2.5 text-xs leading-relaxed">
                {data.share ? (
                  <>
                    <div className="mb-1 flex flex-wrap gap-x-6 gap-y-1">
                      <span className="text-dim">
                        ATS share{" "}
                        <b className="font-mono text-ink">
                          {data.share.ats_pct.toFixed(2)}%
                        </b>
                      </span>
                      <span className="text-dim">
                        Non-ATS off-exchange share{" "}
                        <b className="font-mono text-ink">
                          {data.share.otc_pct.toFixed(2)}%
                        </b>
                      </span>
                      <span className="text-dim">
                        Off-exchange total{" "}
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
                    <b className="text-brand">The off-exchange share cannot be computed</b> —
                    the denominator is total volume over the same period, and locally that week
                    {data.missing_days.length > 0 && (
                      <>
                        {" "}
                        is missing {data.missing_days.length} trading days (
                        <span className="font-mono">{data.missing_days.join(", ")}</span>
                        )
                      </>
                    )}
                    . <Emph>{N?.share_needs_local}</Emph>
                    <div className="mt-1 text-[10px]">
                      Scan {data.ticker} in the Scanner section to accrue those days' quotes and it can be computed —
                      though this history <b className="text-ink">cannot be backfilled</b>,
                      and only accrues forward from installation.
                    </div>
                  </div>
                )}
              </div>

              {seriesOption && (
                <>
                  <div className="mb-1 text-xs text-dim">
                    Week by week — <b className="text-ink">the two lines are drawn apart</b>
                    , because they are two different kinds of trading
                  </div>
                  <ReactECharts option={seriesOption} style={{ height: 260 }} notMerge />
                </>
              )}

              <div className="mb-2 mt-4 flex items-center gap-2">
                {(
                  [
                    ["ats", `ATS dark pools (${data.ats.records} records)`],
                    ["otc", `Non-ATS off-exchange (${data.otc.records} records)`],
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
                  "Mean per trade" = volume ÷ trades, which is no more than a division
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-line text-dim">
                    <tr>
                      <Th>MPID</Th>
                      <Th>Firm</Th>
                      <Th>Tier</Th>
                      <Th>Volume</Th>
                      <Th>Trades</Th>
                      <Th>Mean per trade</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((v, i) => (
                      <tr key={`${v.mpid ?? "anon"}-${i}`} className="border-b border-line/50">
                        <Td className="font-mono">{v.mpid ?? "—"}</Td>
                        <Td className={v.name ? "" : "text-dim"}>
                          {v.name ?? "(this data does not disclose the firm)"}
                        </Td>
                        <Td className="text-dim">{v.tier ?? "—"}</Td>
                        <Td className="font-mono">{num(v.shares)}</Td>
                        <Td className="font-mono">{num(v.trades)}</Td>
                        <Td className="font-mono">
                          {v.avg_trade_size === null
                            ? "—"
                            : `${v.avg_trade_size.toFixed(0)} shares`}
                        </Td>
                      </tr>
                    ))}
                    {rows.length === 0 && (
                      <tr>
                        <td colSpan={6} className="py-8 text-center text-dim">
                          No records that week
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
        Sources: FINRA OTC Transparency (tier B, off by default) + Cboe delayed quotes (the denominator).
        This page presents values and makes no judgement or prediction.
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
