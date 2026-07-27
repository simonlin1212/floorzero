import { NavLink } from "react-router-dom";

/** Section navigation. Adding a section changes only this. */
const NAV = [
  // The stock page brings the nine lanes together — first in the list, since most people enter here
  { to: "/stock", label: "Stock", tag: "nine lanes" },
  // Cboe = tier C: needs authorisation, runs locally only, never shown externally
  { to: "/flow", label: "Options flow", tag: "Cboe · local only" },
  { to: "/gex", label: "GEX", tag: "Cboe · local only" },
  { to: "/scanner", label: "Scanner", tag: "Cboe · local only" },
  // FINRA = tier B: the terms restrict it to non-commercial use, so it is off by default
  { to: "/darkpool", label: "Dark pools", tag: "FINRA · off" },
  // ⚠️ Congressional disclosures are government public record, but 5 U.S.C. §13107(c) forbids commercial use —
  //    a different tier from EDGAR's "commercial use allowed", and the tags must keep them apart
  { to: "/congress", label: "Congress", tag: "no commercial use" },
  { to: "/insiders", label: "Insiders", tag: "tier S · reusable" },
  { to: "/institutions", label: "Institutions", tag: "tier S · reusable" },
  // FTD is tier S; the FINRA lane is off by default, and the tag reflects only the default state
  { to: "/shorts", label: "Short data", tag: "tier S · FTD" },
  { to: "/market", label: "Macro", tag: "tier S · reusable" },
];

/**
 * The page shell: sidebar plus content area.
 *
 * The `tag` marks each source's compliance tier — putting it in the navigation is deliberate:
 * the user can see at any moment which lane is "government public record (freely redistributable)"
 * and which is "licence-bound, local only". That is this project's central story, and does not belong buried in the docs.
 */
export default function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen grid-bg">
      {/* Narrow-screen top navigation: the sidebar hides below lg, and without this, phone and tablet users
          land on the default route and can never reach another section (short of editing the URL by hand). */}
      <nav className="sticky top-0 z-10 flex items-center gap-1 border-b border-line
                      bg-bg/90 px-4 py-2.5 backdrop-blur lg:hidden">
        <span className="mr-2 font-mono text-[11px] uppercase tracking-[0.2em] text-brand">
          ■ FLOORZERO
        </span>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) =>
              `rounded-lg px-2.5 py-1.5 text-xs transition ${
                isActive ? "bg-brand/12 font-semibold text-brand" : "text-dim"
              }`
            }
          >
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="mx-auto flex max-w-[1320px] gap-6 px-6 py-8">
        <aside className="hidden w-[150px] shrink-0 lg:block">
          <div className="sticky top-8">
            <div className="mb-5 font-mono text-[11px] uppercase tracking-[0.2em] text-brand">
              ■ FLOORZERO
            </div>
            <nav className="flex flex-col gap-1">
              {NAV.map((n) => (
                <NavLink
                  key={n.to}
                  to={n.to}
                  className={({ isActive }) =>
                    `rounded-lg px-3 py-2 text-sm transition ${
                      isActive
                        ? "bg-brand/12 font-semibold text-brand"
                        : "text-dim hover:bg-card2 hover:text-ink"
                    }`
                  }
                >
                  <div>{n.label}</div>
                  <div className="mt-0.5 font-mono text-[10px] opacity-60">{n.tag}</div>
                </NavLink>
              ))}
            </nav>
            <div className="mt-6 border-t border-line pt-4 text-[10px] leading-relaxed text-dim">
              Self-hosted · your data stays on your own machine
            </div>
          </div>
        </aside>
        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}

/** A section card (shared by every page, so styling cannot drift page to page). */
export function Card({
  title,
  sub,
  right,
  children,
}: {
  title: string;
  sub?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-5 rounded-2xl border border-line bg-card p-5">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <div className="text-base font-bold">{title}</div>
          {sub && <div className="mt-0.5 text-xs text-dim">{sub}</div>}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

/** Page heading. */
export function PageHead({
  kicker,
  title,
  children,
}: {
  kicker: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <header className="mb-7">
      <div className="mb-2 font-mono text-[11px] uppercase tracking-[0.25em] text-brand">
        ■ {kicker}
      </div>
      <h1 className="text-3xl font-bold tracking-tight">{title}</h1>
      {children && (
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-dim">{children}</p>
      )}
    </header>
  );
}

/** A table header cell (shared by every page). */
export function Th({ children }: { children: React.ReactNode }) {
  return <th className="whitespace-nowrap px-2 py-2 font-normal">{children}</th>;
}

/** A table cell (shared by every page).
 *
 * ⚠️ It was extracted here because Congress and Insiders each had their own copy,
 * and adding `title` missed one of them — one component should not have two definitions. */
export function Td({
  children,
  className = "",
  title,
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <td className={`whitespace-nowrap px-2 py-1.5 ${className}`} title={title}>
      {children}
    </td>
  );
}

/**
 * Renders the `**emphasis**` inside the backend's notes.
 *
 * ⚠️ The backend's definitions are **prose written for people**, and they use `**` to mark "this bit matters"
 * ("they **can invert months apart**" and the like). Dropped straight into JSX as `{note}`,
 * what the user sees is a string of bare asterisks — as measured on both the Market and Shorts pages.
 *
 * It **deliberately pulls in no markdown library**: it recognises `**` alone, splits into an array and hands it to React,
 * never touching `dangerouslySetInnerHTML`. However the upstream text is written, injection is impossible.
 */
export function Emph({ children }: { children?: string }) {
  if (!children) return null;
  return (
    <>
      {children.split("**").map((seg, i) =>
        i % 2 ? (
          <b key={i} className="text-ink">
            {seg}
          </b>
        ) : (
          seg
        ),
      )}
    </>
  );
}

/**
 * Escapes upstream text destined for HTML.
 *
 * ⚠️ ECharts tooltip formatters return an **HTML string**,
 * while company names, insider names and asset names are all fields **typed freely by the filer**
 * (a ticker was measured containing `"""OMEX"""`).
 * Concatenated straight in, one filing containing `<img onerror=...>` would execute script in the local interface.
 * Every piece of upstream text going into a tooltip passes through here.
 */
export function esc(v: unknown): string {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string,
  );
}
