import { NavLink } from "react-router-dom";

/** 分栏导航。加新分栏时只改这里。 */
const NAV = [
  // 个股页把九条线汇合 —— 放最前面，多数人从这里进
  { to: "/stock", label: "个股", tag: "九条线汇合" },
  // CBOE = C 级：需授权，只能本地跑，绝不对外展示
  { to: "/flow", label: "期权流", tag: "CBOE·仅本地" },
  { to: "/gex", label: "GEX 伽马", tag: "CBOE·仅本地" },
  { to: "/scanner", label: "扫描器", tag: "CBOE·仅本地" },
  // FINRA = B 级：条款限非商业用途，默认关闭
  { to: "/darkpool", label: "暗池", tag: "FINRA·默认关" },
  // ⚠️ 国会披露虽是政府公开记录，但 5 U.S.C. §13107(c) 明文禁止商用 ——
  //    与 EDGAR 的"可商用"不是一个级别，标签必须区分开
  { to: "/congress", label: "国会交易", tag: "公开·禁商用" },
  { to: "/insiders", label: "内部人", tag: "S 级·可商用" },
  { to: "/institutions", label: "机构持仓", tag: "S 级·可商用" },
  // FTD 是 S 级；FINRA 那条默认关闭、标签只反映默认状态
  { to: "/shorts", label: "做空数据", tag: "S 级·FTD" },
  { to: "/market", label: "宏观", tag: "S 级·可商用" },
];

/**
 * 页面外壳：侧栏 + 内容区。
 *
 * `tag` 标的是数据源合规级 —— 放在导航上是刻意的：
 * 用户随时能看见哪条线是"政府公开记录（可自由再分发）"、
 * 哪条线是"受许可约束、只能本地跑"。这是本项目的核心叙事，不该藏进文档。
 */
export default function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen grid-bg">
      {/* 窄屏顶部导航：侧栏在 lg 以下隐藏，没有它的话手机/平板用户
          落在默认路由后就再也找不到别的分栏（只能手改 URL）。 */}
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
              本机自部署 · 数据留在你自己机器上
            </div>
          </div>
        </aside>
        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}

/** 分栏卡片（各页共用，避免每页各写一份而样式漂移）。 */
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

/** 页头。 */
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

/** 表头单元格（各页共用）。 */
export function Th({ children }: { children: React.ReactNode }) {
  return <th className="whitespace-nowrap px-2 py-2 font-normal">{children}</th>;
}

/** 表格单元格（各页共用）。
 *
 * ⚠️ 抽到这里是因为 Congress 与 Insiders 各写了一份，
 * 加 `title` 时又各漏各的 —— 同一个组件不该有两份定义。 */
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
 * 渲染后端 notes 里的 `**强调**`。
 *
 * ⚠️ 后端的口径说明是**写给人看的正文**，里头用 `**` 标了"这句是重点"
 * （"倒挂时点**可以差好几个月**"这种）。直接 `{note}` 塞进 JSX，
 * 用户看到的就是一串裸星号——实测 Market 与 Shorts 两页都这样。
 *
 * 这里**刻意不引 markdown 库**：只认 `**` 一种标记，切成数组交给 React 渲染，
 * 不碰 `dangerouslySetInnerHTML`。上游文本再怎么写都不可能注入。
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
 * 转义要插进 HTML 的上游文本。
 *
 * ⚠️ ECharts 的 tooltip formatter 返回的是 **HTML 字符串**，
 * 而公司名/内部人姓名/资产名都是**申报人自由填写**的字段
 * （实测 ticker 里出现过 `"""OMEX"""` 这类内容）。
 * 直接拼进去，一份含 `<img onerror=...>` 的申报就能在本地界面里执行脚本。
 * 所有插进 tooltip 的上游文本都要过这里。
 */
export function esc(v: unknown): string {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string,
  );
}
