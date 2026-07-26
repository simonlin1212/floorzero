# Vibe-Flow — 开源版 Unusual Whales

> **一句话**：把 Unusual Whales 每月 $29-99 的墙，用开源代码打穿。
> 用户自己部署自己跑 → 数据落用户本地 → **我们永远不是 OPRA redistributor，不用交那 $1,500/月**。
>
> 2026-07-26 建。⚠️ **工作名 Vibe-Flow，最终名待 Simon 拍板**（延续 Vibe-* 品牌）。

## 动手前必读（顺序）

1. **`docs/模块设计.md`** —— 项目骨架：UW 197 端点实况 / 数据源分层 / 13 个加工模块 / 10 个前端分栏 / 合规铁律 / 已知硬骨头。**改架构前必读。**
2. 本文件的「合规铁律」节（下面）——**违反会产生真实法律与金钱后果**。
3. 上级项目 `../CLAUDE.md`（投资分析总纲）的 R5/R5.6 实时调研铁律仍然适用。

## 这是什么 / 不是什么

- **是**：一个自部署的市场数据分析平台，对标 UW 全量功能。产出是**代码**。
- **不是**：数据服务、SaaS、在线看板。⛔ **绝不托管数据给别人看**。
- **用户**：想自己做分析/做工具/喂给 AI 的开发者与进阶交易者，**不是**"打开就想看盘的散户"（那是券商 App 的活，免费且成熟，别去碰）。

## ⚠️ 合规铁律（写死，不可协商）

1. **只分发代码，绝不托管数据。** 这是整个项目成立的前提。用户 clone → 自己跑 → 数据落自己机器 = 用户 personal use，我们不是 redistributor。
2. ⛔ **绝不做在线 demo 站。** OPRA 规则原文：*"if you're showing OPRA data externally in an app, tool, or website, you're considered a redistributor"* → **$1,500/月**，且**免费/开源/非商业均不豁免**。推广只能靠截图、录屏、README。
3. **输出数据不输出结论。** 可以展示 GEX 数值、异动排名、空头占比；**不打「买入/卖出/低估/高估」标签、不给点位、不做主观评分、不预测涨跌**。
4. **各数据源标注合规级**（S/B/C + 条款原文）进 UI 与 README——既是差异化叙事，也是自我保护。
5. **key 用户自备**，绝不内置任何凭据；`.env` 进 `.gitignore`。
   ⚠️ **联系方式也算凭据**：SEC/国会站点要求 UA 带邮箱，
   **绝不能把作者邮箱硬编码进代码** —— 开源后每个用户的流量都会以作者身份发出，
   谁把上游打到封禁都算在他头上。走 `VF_CONTACT` 环境变量 + 未配置即 fail-fast
   （见 `backend/sources/contact.py`）。
6. 完整法律背景见项目0记忆 `project_vibe-astock-commercialization-legal`（五轮调研）与 `project_global-stock-data-v2`。

## 数据源合规级（取用前必看）

| 级 | 源 | 商用 | 再分发 |
|---|---|---|---|
| **S** | SEC EDGAR / Treasury / CFTC | ✅ | ✅ |
| **S⁻** | **国会两院财产申报** | ❌ **法律禁止商用** | ✅（公开记录） |
| **B** | FINRA（Reg SHO / ATS） | ❌ **条款限非商用** | ❌ |
| **C** | CBOE / Nasdaq / Yahoo / 东财等 | ❌需授权 | ❌ |

⭐ 做**展示型**功能时优先用 S 级源；C 级源（尤其 CBOE 期权）只能在用户本地跑。

⚠️ **S⁻ 的坑（2026-07-26 查法条原文更正，此前标错过）**：国会披露虽是政府公开记录，
但 **5 U.S.C. §13107(c)(1)(B)** 明文规定「为任何商业目的获取或使用这些报告均属违法」
（新闻媒体面向公众传播除外），§13107(c)(2) 罚款上限 $10,000，**两院均适用**。
→ 免费开源 + 用户自部署做个人研究 ✅；**任何收费产品/商业服务不得包含这条线** ❌。
这与 SEC EDGAR 不同（EDGAR 只限速率 10 请求/秒 + 要求声明 UA，不限商用），
**两者不可混为一谈**。

⚠️ **B 级（FINRA）的实况（2026-07-26 实读条款原文）**：Terms of Use 限
「**ONLY for your own non-commercial personal or professional use**」，
且限制 (d) 明文禁止「**develop or create a database of data using the FINRA Website**」——
**这直接冲击本项目「下载→落 SQLite」的架构**。另有模糊之处：条款范围写的是
"the FINRA.**ORG** site"，而数据文件在 `cdn.finra.org`；FINRA 还有一套需注册接受的
API Terms of Service。→ **做法：FINRA 源一律默认关闭**（`VF_ENABLE_FINRA=1` 才启用），
UI 上把条款原文与模糊之处原样摆出来，**judgment 交给用户，我们不替他解释**。
任何分栏都不得把 FINRA 作为唯一数据源。

## 架构速览

```
backend/sources/   L1 数据源（cboe/finra/edgar/congress/macro/quotes/predmkt）
backend/modules/   L2 加工层（greeks/flow/scanner/darkpool/insider/institution/
                              congress/shorts/vol/seasonality/fundamental/tide/history）
backend/           app.py(FastAPI) · mcp_server.py · tools.py
frontend/src/pages 10 个分栏（Flow/GEX/Scanner/Darkpool/Congress/Insiders/
                              Institutions/Shorts/Market/Stock）
docs/              模块设计.md（唯一 source of truth）
research/          调研与竞品拆解
```

## 技术栈（沿用已验证的，别另起）

- **后端**：Python + FastAPI，模块化范式**照搬** `../用AI做投研/VibeResearch-开源版/backend/`（`tools.py` 唯一工具定义处 → chat/MCP/REST 三出口自动继承）
- **前端**：React 19 + Vite 6 + TS + Tailwind 3.4 + ECharts 6 + zustand + react-router 7
- **视觉**：Vibe-Trading 同款暗色 + 朱橙 `#F35D2B`
- **存储**：SQLite（本地历史沉淀，零配置）
- **依赖**：能少则少，`requests` 优先（延续 global-stock-data「零鉴权开箱即用」）

## ⭐ 代码复用（别重写轮子）

- **数据源层直接复用 `global-stock-data` V2.0 的已验证代码**：仓库在 `../../6、自媒体-独立开发者转型/开源项目/global-stock-data/SKILL.md`，CBOE/FINRA/EDGAR/Treasury/CFTC 全部跑通且经 Codex 三轮审计。
- **预测市场复用 `globalpercent`**（Polymarket + Kalshi 已封装）。
- **后端架构模式复用 VibeResearch 开源版**；**UI 视觉复用 Vibe-Trading-Simon**。

## ⚠️ 三个已知硬骨头（别假装不存在）

1. **历史数据**：用户自部署第一天零历史。`history.py` 装上即积累；EDGAR/FINRA 可回补；**期权链历史补不回来**——这是 UW 的真护城河，要在 README 里对用户诚实。
2. **实时流做不了**：UW 有 14 个 WebSocket 端点走实时 tape，需 OPRA 付费 feed。我们只做**延时**（研究够用，抢单不够）。
3. **工作量**：197 端点不可能一步到位 → **按分栏逐个交付**，每个分栏是可独立验收的里程碑。

## 开发流程（照上级项目铁律）

1. **改前先比对线上版本**（若已开源）——本地副本常落后于仓库。
2. 改 → **抽出代码实跑回归 + 边界**（不是看一遍）。
3. ⛔ **push 前必跑 `codex review`**（2026-07-24 踩过：推完才审 → 追发补丁）。
4. 逐条核实并修 → **复审至 "No actionable regressions"**。
5. push + tag + Release → 更新记忆与配置。

## 开源发布规范（照全局 CLAUDE.md）

- README 版式：语言切换行 → `<h1 align="center">` → 副标 → 徽章 → 导航 → 正文 → CHANGELOG → 免责 → 赞赏(BMC 二维码) → License(含署名行)
- 章节标题**不带 emoji**；主档语言=**英文**（全球向）+ `README_zh.md`
- 联系方式只三样：X `@linsizhen` · TikTok `@simonlin0423` · Email `simonlin0423@gmail.com`
- commit message 用英文（2026-07-25 起）
