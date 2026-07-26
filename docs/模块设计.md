# FloorZero 模块设计（对标 Unusual Whales · 全量）

> 2026-07-26 建 · 2026-07-27 定名 **FloorZero**（此前工作名 Vibe-Flow）。
> 定位：**开源版 Unusual Whales**——把 UW 每月 $29-99 的墙用开源代码打穿。
> 合规基石：**只分发代码，用户自部署自己跑**（数据落用户本地）→ 我们永远不是 OPRA redistributor。

---

## 一、对标对象实况（2026-07-26 实扒 api.unusualwhales.com/docs）

UW 公开 API：**35 个分类 · 197+ 端点**。分布：

| 分类 | 端点数 | 分类 | 端点数 |
|---|---|---|---|
| Stock | **37** | Predictions | 9 |
| WebSocket | 14 | Private Markets | 9 |
| Market | 12 | Institution | 7 |
| Gex/Greeks | **11** | Short | **7** |
| Option-Contract | 7 | Volatility | 6 |
| Alerts / Companies / ETFs / Intel / Option-Trade / Politician Portfolios | 各 5 | Congress / Crypto / Insiders / Seasonality / Unusual Trades | 各 4 |
| Earnings / Forex / Screener | 各 3 | Darkpool / Group Flow / Lit-Flow / Option Trades / Digital Currencies / POTUS | 各 2 |
| Commodities / Economy / News / Stock-Directory | 各 1 | | |

---

## 二、⭐ 核心判断：UW 的护城河不是数据独家

**实测（2026-07-26）UW 招牌功能的数据源，绝大部分免费公开：**

| UW 卖点 | 数据源 | 实测结果 |
|---|---|---|
| 期权流 / GEX / Greeks | **CBOE 官方延时** | ✅ 已在 global-stock-data 跑通 |
| 暗池 Darkpool | **FINRA ATS API** | ✅ 通（返回 symbol/周成交笔数/量） |
| 国会议员交易 | **众议院披露 ZIP** | ✅ 通（52KB 可直接下载） |
| 内部人 Insiders | **SEC EDGAR Form 4** | ✅ 已跑通（单日 547 份） |
| 机构持仓 Institution | **SEC EDGAR 13F** | ✅ 已跑通（单日 261 份） |
| 做空 Short | **FINRA Reg SHO + SEC FTD** | ✅ 已跑通（全市场 12,112 只） |
| 财报 / 基本面 | **SEC EDGAR XBRL** | ✅ 已跑通 |
| 预测市场 Predictions | **Polymarket + Kalshi** | ✅ Simon 已有 globalpercent |
| 宏观 Economy | **Treasury / CFTC** | ✅ 已跑通 |

**→ UW 真正的护城河是「整合 + 加工 + 界面 + 历史沉淀」，前三项正是开源最擅长打穿的。**

---

## 三、模块设计（按「数据源」重组，不照抄 UW 分类）

UW 的分类是按业务功能切的，会重复调同一数据源。我们按**数据源分层**，上面架**功能模块**——同一份数据喂多个功能，避免重复抓取（也天然对齐限流）。

```
┌─────────────────────────────────────────────────────────┐
│  L4  出口层  Web UI · MCP · REST · 告警                    │
├─────────────────────────────────────────────────────────┤
│  L3  功能模块  扫描器/GEX/暗池/国会/内部人/做空/异动…         │
├─────────────────────────────────────────────────────────┤
│  L2  加工层   计算 · 聚合 · 异动识别 · 历史沉淀              │
├─────────────────────────────────────────────────────────┤
│  L1  数据源   CBOE/FINRA/SEC/House/Treasury/CFTC/PM       │
└─────────────────────────────────────────────────────────┘
```

### L1 数据源层 `backend/sources/`

| 文件 | 源 | 合规级 | 供给 |
|---|---|---|---|
| `cboe.py` | CBOE 延时期权 | C（个人研究） | 期权链 · Greeks · IV · OI |
| `finra.py` | FINRA Reg SHO + ATS | B | 空头量 · 暗池 |
| `edgar.py` | SEC EDGAR | **S** | Form4 · 13F · 13D/G · XBRL · 全文检索 |
| `congress.py` | House Clerk + Senate eFD | **S⁻** ⚠️禁商用 | 议员交易披露（5 USC §13107(c)）|
| `macro.py` | Treasury + CFTC + Nasdaq | **S/C** | 收益率 · COT · 财报日历 |
| `quotes.py` | 多源行情 | C | 报价 · K线 |
| `predmkt.py` | Polymarket + Kalshi | — | 预测市场（复用 globalpercent） |

> ⭐ 直接复用 `global-stock-data` V2.0 的已验证代码，别重写。

### L2 加工层 `backend/modules/`

这是 **UW 真正收钱的地方**，也是我们的主战场：

| 模块 | 做什么 | 对标 UW |
|---|---|---|
| `greeks.py` | **GEX 伽马敞口**：Σ(gamma×OI×100×spot²×0.01)，按 strike/expiry 聚合；Vanna/Charm | Gex/Greeks (11) |
| `flow.py` | **异动识别**：vol/OI>1、sweep 检测、大单分级、P/C 比、净 delta 敞口 | Option-Trade + Flow (12) |
| `scanner.py` | **全市场扫描**：跨标的批量筛选（UW 头号卖点） | Screener + Hottest Chains (3) |
| `darkpool.py` | ATS 成交聚合、暗池占比、异常放量 | Darkpool + Lit-Flow (4) |
| `insider.py` | Form 4 解析、买卖分级、板块流向 | Insiders (4) |
| ✅ `institution.py` | 13F 解析、持仓变动、机构画像 | Institution (7) |
| `congress.py` | 议员交易解析、延迟披露检测、政客组合 | Congress + Politician (9) |
| `shorts.py` | 空头量比、FTD、趋势 | Short (7) |
| `vol.py` | IV Rank/百分位、期限结构、偏度、方差风险溢价 | Volatility (6) |
| `seasonality.py` | 月度/年度季节性统计 | Seasonality (4) |
| `fundamental.py` | 三表、财报日历、盈利历史 | Companies + Stock 财务 (12) |
| `tide.py` | 市场潮汐、板块净流入、OI 变化 | Market (12) |
| `history.py` | **本地历史沉淀**（装上即开始积累） | UW 的 Data Shop |

### L3 出口层

| 出口 | 说明 |
|---|---|
| **Web UI** | React 19 + Vite + Tailwind + ECharts（复用 Vibe-Trading 视觉） |
| ⭐ **MCP server** | 让任何人的 Claude/GPT 直接问——**UW 的 MCP 要先付费，我们免费** |
| **REST API** | FastAPI，本地 |
| **告警** | 本地规则引擎（对标 UW Alerts 5 端点） |

---

## 四、前端分栏设计（对标 UW 网站导航）

复用 Vibe-Trading 的侧栏范式，10 个主分栏：

| # | 分栏 | 核心内容 | 数据源 |
|---|---|---|---|
| 1 | **Flow 期权流** | 实时异动、sweep、大单、按标的/板块过滤 | CBOE |
| 2 | ✅ **GEX 伽马** | GEX levels、按 strike/expiry 分布、spot GEX | CBOE 自算 |
| 3 | **Scanner 扫描器** | 全市场筛选（IV Rank/OI变化/异动比） | CBOE + 行情 |
| 4 | **Darkpool 暗池** | ATS 成交、暗池占比、异常 | FINRA |
| 5 | ✅ **Congress 国会** ⚠️禁商用 | 议员交易、延迟披露、组合 | House/Senate |
| 6 | ✅ **Insiders 内部人** | Form 4 流、买卖分级、集群买入 | EDGAR |
| 7 | ✅ **Institutions 机构** | 13F 持仓、季度环比、机构画像 | EDGAR |
| 8 | ✅ **Shorts 做空** | SEC FTD（主）· FINRA 场外空头量（可选·默认关） | SEC + FINRA |
| 9 | **Market 市场** | 潮汐、板块、财报日历、宏观 | 多源 |
| 10 | **Stock 个股页** | 单只全景（期权链/GEX/财务/申报/做空） | 全部 |

---

## 五、⚠️ 合规铁律（写死，不可协商）

1. **只分发代码，绝不托管数据。** 用户自己 clone、自己跑、数据落自己机器 → 用户是 personal use，我们不是 redistributor。
2. ⛔ **绝不做在线 demo 站。** 一旦我们托管一个能看期权数据的站点 → 立即成为 OPRA redistributor（**$1,500/月**）。推广只能靠截图/录屏/README。
3. **输出数据不输出结论。** 展示 GEX 数值、异动排名；**不打「买入/卖出」标签、不给点位、不做主观评分**。
4. **各源合规级标注进 UI 与文档**（S/B/C，条款原文）——这是我们的差异化叙事，也是自我保护。
5. **API key 用户自备**（若用到需 key 的源），绝不内置。

---

## 六、⚠️ 三个已知硬骨头（诚实记录）

1. **历史数据是硬伤。** UW 跑了多年攒下历史；用户自部署是**第一天零历史**。
   → 缓解：`history.py` 装上即积累；EDGAR/FINRA 本身有历史可回补；期权链历史确实补不回来。
2. **实时流做不了。** UW 有 14 个 WebSocket 端点走实时 tape，那需要 OPRA 实时 feed（付费）。
   → 我们只做**延时**（CBOE 免费延时够用于研究，不够用于抢单）。
3. **工作量。** 197 端点全量对标不现实一步到位——**按分栏逐个交付**，每个分栏是一个可独立验收的里程碑。

## 七、不做的部分

| UW 有 | 我们不做 | 原因 |
|---|---|---|
| Crypto (4) + Digital Currencies (2) | ❌ | Simon 红线：不碰加密 |
| Private Markets (9) | ❌ | 数据源需付费/难得 |
| Forex (3) / Commodities (1) | 暂缓 | 非核心受众 |
| WebSocket 实时 (14) | ❌ | 需 OPRA 实时 feed（付费） |
| POTUS (2) | 暂缓 | 边缘且涉政（红线） |

**净剩可做 ≈ 165 端点 / 30 个分类。**

---

## 八、技术栈（沿用已验证的）

- **后端**：Python + FastAPI（照搬 VibeResearch 开源版的模块化范式）
- **前端**：React 19 + Vite 6 + TS + Tailwind 3.4 + ECharts 6 + zustand + react-router 7
- **视觉**：Vibe-Trading 同款（暗色 + 朱橙 `#F35D2B`）
- **存储**：SQLite（本地历史沉淀，零配置）
- **AI 出口**：MCP（stdlib JSON-RPC，照搬 `mcp_server.py` 范式）
- **依赖原则**：能少则少，`requests` 优先（延续 global-stock-data 的"零鉴权、开箱即用"）
