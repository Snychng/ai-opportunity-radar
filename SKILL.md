---
name: ai-opportunity-radar
description: 从全球付费产品、普通社区、产品评论、开发者社区与商业行为中批量发现 AI 创业机会。先建立真实付费对标，再扩展 100–200 个候选，用六项硬门槛筛出 20–40 个已验证快速点子、30–80 个区域迁移假设和 3–5 个深度机会。用于寻找大量可行且有市场需求的点子、不同地区的迁移创意、每日机会雷达、定向扫描、单机会深挖与历史回顾。
---

# AI Opportunity Radar

## 核心目标

寻找“小而明确、有现成付款行为、一人加 AI 可在 30 天做出 MVP”的机会。优先提供大量可行动点子，不把每条都写成长篇咨询报告。

遵守两句话：

1. 批量生成可以宽，进入正式机会必须严。
2. 先找谁正在为什么付钱，再判断 AI 能否用更简单的新形态替代一个昂贵动作。

三层正式输出：

- 3–5 个 `A` 级深度机会：本地直接付款证据、至少两个独立来源、六项硬门槛全部通过。
- 20–40 个 `A/B` 级快速点子：存在付费对标，并有目标用户投诉、替代、招聘或外包证据；也可包含未进入 Top 5 的 A 级候选。
- 30–80 个 `R` 级区域迁移创意：其他市场已有付费，本地语言、支付、渠道或工作流存在合理差异，但尚缺目标地区直接付款证据；必须用 `SIG`，不能写成已验证 `OPP`。

以上是目标区间，不是凑数指标。证据不足时可以少，但必须说明原因。

## 一期来源范围

- 内置公开来源：Hacker News、GitHub Issues。
- TikHub：TikTok、Instagram、LinkedIn、Threads、X、YouTube、Reddit、抖音、小红书、B站、知乎、微信搜一搜/公众号。
- Web 搜索只补充竞品官网、定价、付款证据、地区差异与反证，不冒充自动化完整覆盖。
- `platform_expansion_enabled` 必须为 `false`。未适配的新端点和新平台不得自动进入计划。

只有本次返回了非空、相关、可核验证据的来源才能标记为“已覆盖”。

## 运行路径

将本文件目录记为 `SKILL_DIR`，运行数据写到：

```bash
RADAR_HOME="${AI_OPPORTUNITY_RADAR_HOME:-$HOME/Documents/AI-Opportunity-Radar}"
```

不要把 Cookie、令牌、API Key、完整响应头或浏览器存储写入报告、状态或仓库。

## 模式

- “运行今天的雷达”或“给我很多有需求的点子”：每日雷达。
- 指定地区、语言、行业、用户或渠道：定向扫描，仍走完整漏斗。
- 指定 `OPP`：机会深挖。
- 指定 `SIG` 并要求验证：区域假设验证；证据达标后升级为 `OPP`。
- 询问 7/30/90 天变化：历史回顾。

## 按需参考

- 完整研究步骤：[research-workflow.md](references/research-workflow.md)
- 付费对标、硬门槛与 A/B/R 分层：[opportunity-policy.md](references/opportunity-policy.md)
- 阶段 JSON、ID 与重跑规则：[data-contracts.md](references/data-contracts.md)
- 高信号查询词与地区查询：[query-patterns.md](references/query-patterns.md)
- A 级候选深度评分：[scoring.md](references/scoring.md)
- 日报固定结构：[report-template.md](references/report-template.md)
- 来源覆盖：[source-catalog.md](references/source-catalog.md)
- TikHub 费用与执行：[tikhub-integration.md](references/tikhub-integration.md)
- 敏感领域和抓取边界：[safety-and-legality.md](references/safety-and-legality.md)

## 每日雷达

按顺序执行。报告校验前不得提交状态。

### 1. 初始化并生成计划

使用北京时间日期，创建 `$RADAR_HOME/raw/YYYY-MM-DD/`：

```bash
python3 "$SKILL_DIR/scripts/manage_state.py" init --home "$RADAR_HOME"

python3 "$SKILL_DIR/scripts/build_query_plan.py" \
  --date YYYY-MM-DD \
  --home "$RADAR_HOME" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/query-plan.json" \
  --export-community-plan "$RADAR_HOME/raw/YYYY-MM-DD/community-plan.json" \
  --export-tikhub-plan "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-search-plan.json"
```

定向扫描把用户范围写入 UTF-8 `focus.txt`，使用 `--focus-file`；不要把用户原文拼进 shell。所有阶段必须共享一个 `run_id`。

### 2. 来源预检、估价与证据采集

```bash
python3 "$SKILL_DIR/scripts/community_query.py" doctor --json
python3 "$SKILL_DIR/scripts/community_query.py" run \
  --plan "$RADAR_HOME/raw/YYYY-MM-DD/community-plan.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/community-normalized.json"

python3 "$SKILL_DIR/scripts/tikhub_query.py" estimate \
  --plan "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-search-plan.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-cost-estimate.json"
```

TikHub 执行前必须报告预计 USD、RMB、免费额度适用与不适用成本。API Key 只从 `TIKHUB_API_KEY` 读取；必须显式给 `--max-cost-usd`。缺密钥、实时价格、余额或预算时不得调用付费端点。

查询优先级：

1. 付款、营收、订阅、定价、招聘、外包。
2. 取消、切换、投诉、手工表格、复制粘贴和现有替代。
3. 本地语言、支付方式、主渠道和工作流差异。
4. 泛讨论只作线索，不能独立进入正式机会。

每条证据保留来源、直达 URL、简短原文与中文翻译、语言、发布时间、日期置信度、采集时间、访问方式、互动量和信号类型。无互动量写“未知”，不写 0。

### 3. 建立付费对标

先把已核验的付费产品整理为 `benchmarks.json`。每个对标至少包含：

- `product`、`source_market`、`payer`
- `price` 或 `current_spend`
- 非空 `payment_signals`
- `current_alternative`、`product_gap`
- `acquisition_channel`
- `mvp_days`、`mvp_scope`
- 直接证据 URL

只看到“有人喜欢”“帖子很热”不算付费对标。定价页只能证明产品收费；收入、订单、订阅、采购、招聘或外包证据更强。

### 4. 六轴批量扩展

围绕每个对标准备 `dimensions`：细分人群、购买触发、AI 新形态、地区与语言、渠道嵌入、价格与交付。运行：

```bash
python3 "$SKILL_DIR/scripts/expand_ideas.py" \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/benchmarks-and-dimensions.json" \
  --limit 200 \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/expanded-candidates.json"
```

目标生成 100–200 个原始候选。不同国家或主渠道必须保留独立 `market_scope`，不能合并成一个泛化点子。

### 5. 六项硬过滤和 A/B/R 分层

```bash
python3 "$SKILL_DIR/scripts/filter_ideas.py" \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/expanded-candidates.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tiered-candidates.json"
```

正式机会必须同时满足：

1. 已有付费市场。
2. 付款者明确。
3. 当前替代方案明确。
4. 产品缺口具体。
5. 获客渠道明确。
6. 30 天内能完成单任务 MVP。

缺一项就进入拒绝池，不用综合分补偿。`R` 级不能进入深度评分；先保存为 `SIG`。

### 6. 只对 A 级候选深度评分

为 A 级候选补齐七项主评分与四项辅助评分，再先分配稳定 ID、后评分：

```bash
python3 "$SKILL_DIR/scripts/manage_state.py" prepare \
  --home "$RADAR_HOME" --kind opportunity --date YYYY-MM-DD \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/deep-candidates.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/deep-candidates-with-ids.json"

python3 "$SKILL_DIR/scripts/score_candidates.py" \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/deep-candidates-with-ids.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/scored-deep-candidates.json"
```

总分只用于 A 级候选内部排序。选 3–5 个深写；其余 A 级可加入快速点子，但保留 `A` 证据标签。与 B 级合并后最多输出 40 个，其余保留在运行数据中。

分别用 `prepare --kind opportunity` 给 B 级快速点子分配 `OPP`，用 `prepare --kind signal` 给 R 级分配 `SIG`。

### 7. 写三层日报并校验

按 [report-template.md](references/report-template.md) 写入：

```text
$RADAR_HOME/reports/daily/YYYY-MM-DD.md
```

```bash
python3 "$SKILL_DIR/scripts/validate_report.py" \
  "$RADAR_HOME/reports/daily/YYYY-MM-DD.md"
```

修复所有 `ERROR`。验证器检查结构和证据层级，不证明市场规模、法律合规或引用真实性。

### 8. 校验后提交状态

按同一 `run_id` 分别提交 OPP 与 SIG：

```bash
python3 "$SKILL_DIR/scripts/manage_state.py" record-batch \
  --home "$RADAR_HOME" --kind opportunity --date YYYY-MM-DD \
  --run-id RUN-YYYYMMDD-XXXXXXXXXX --input opportunities.json

python3 "$SKILL_DIR/scripts/manage_state.py" record-batch \
  --home "$RADAR_HOME" --kind signal --date YYYY-MM-DD \
  --run-id RUN-YYYYMMDD-XXXXXXXXXX --input regional-signals.json
```

同一 `run_id` 重放返回 `replayed`。稳定身份使用“用户 + 场景 + 需求 + 切入口 + 可选国家/地区/主渠道”；标题翻译不影响 ID。

### 9. 返回用户

提供日报绝对路径，简要列出：Top 机会、快速点子数量、区域创意数量、被拒绝数量、来源缺口和最大风险。不要在聊天里重复整份日报。

## SIG 升级为 OPP

区域假设补齐目标地区直接付款证据与至少两个独立来源后，准备升级后的机会 JSON，再执行：

```bash
python3 "$SKILL_DIR/scripts/manage_state.py" promote \
  --home "$RADAR_HOME" \
  --signal-id SIG-YYYYMMDD-XXXXXX \
  --date YYYY-MM-DD \
  --run-id RUN-YYYYMMDD-XXXXXXXXXX \
  --input promoted-opportunity.json
```

状态会在 `SIG.promoted_to` 与 `OPP.promoted_from` 两端保留链接。没有本地付款证据时不得升级。

## 深挖与历史回顾

深挖 `OPP` 时增加独立证据、反证、竞品差评、购买触发、获客渠道、30 天 MVP、首笔收入路径和 72 小时实验。深挖 `SIG` 时优先验证本地付款者、渠道和现有替代，不扩写宏大市场故事。

历史回顾比较 `first_seen`、`last_seen`、`occurrences`、证据源、分数和 A/B/R 变化。没有新增证据不等于需求下降。

## 硬边界

- 仅允许恋爱约会与情感陪伴、成人内容、游戏虚拟角色与社交娱乐三类敏感方向；严格排除未成年人、非自愿内容、真实人物色情仿冒、隐私窃取和违法交易。
- 排除医疗诊断治疗、金融投资建议、法律意见和儿童敏感产品。
- 只推荐主要通过 Web、App、插件、消息机器人、付费内容、开发者工具或轻平台交付的产品。
- 不推荐需要自建支付、物流、银行、硬件或大规模线下基础设施的方案。
- 不绕过验证码、访问控制、付费墙或平台保护。
- 不把偶尔能访问的研究数据描述成产品可稳定获取的数据。
- 不把无结果解释为没有竞品、没有需求或市场空白。
