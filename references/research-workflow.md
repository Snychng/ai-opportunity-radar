# V3 研究工作流

最后更新：2026-09-10。

定向主题缺少可用英语查询时，`next_action` 会说明缺口；可以用 `resume RUN_ID --intent-plan-file FILE` 补充宿主查询并继续同一轮。已经执行过实际社区查询的运行应另建研究，不直接改写原检索历史。

## 1. 文件化主流程

```text
research → evidence-packet.json → Agent 核验并填写 benchmarks
         → resume --benchmarks → tiered.json 与 assessment 模板
         → Agent 评分依据、主张与行动判断 → resume --assessment
         → report.json 校验 → 本地状态提交 → completed
```

`research` 自动分配本轮唯一 ID，初始化目录、编译计划，先取得 history-context，再尝试免费来源并刷新证据包。JSON 返回 `status/next_action/input_template/artifacts`；`inspect RUN_ID --home DATA_HOME` 查看这些信息。新运行产物位于 `DATA_HOME/runs/RUN_ID/`，手动工具的 `raw/YYYY-MM-DD/` 仍兼容。

| 状态 | 宿主下一步 |
|---|---|
| `awaiting_benchmarks` | 读 evidence-packet 与必要原文，复制模板到独立输入文件，填写对标和维度；无合格对标填 `empty_reason` |
| `awaiting_assessment` | 读 tiered（含 overflow），按返回稳定 ID 填 A 级评分输入及 decision；用 assessment 文件恢复 |
| `committing` | 用同一运行、不带新输入恢复；等待原报告幂等提交完成 |
| `completed` | 读取 report.json、report.md、summary.md、receipt.json；修订另开带 parent-run-id 的研究 |

`resume RUN_ID --benchmarks FILE` 执行扩展、过滤和稳定 ID 准备；`resume RUN_ID --assessment FILE` 自动校验并提交本地状态，不只是保存草稿。decision 必填 `summary/largest_unknown/next_action/stop_condition`；没有主项目时 `primary_id=null`，无 A 级时 `scores=[]`。有 A 级时按当前模板用 `score_basis` 提供各维度 rationale 与 evidence_refs，不复制演示分数。可同时传 `--profile FILE` 评估个人约束。

通过 `resume --evidence FILE` 导入材料，可重复指定文件；跨轮证据保留来源运行，不能将旧费用混入新运行。产物受摘要保护，不直接编辑运行目录中的文件；将修订作为输入传回。已完成研究不接受新输入，使用 `research --parent-run-id RUN_ID` 建立后续研究。

### 离线执行

严格离线会话设置 `AOR_OFFLINE=1`，并使用 `research --offline` 或 `resume RUN_ID --offline`。环境变量避免 CLI 更新预检联网，`--offline` 也跳过本次更新预检并停止编排采集；以离线模式创建的运行会持久保留该约束，付费入口拒绝执行。`--no-collect` 仅用于已有材料处理，不是所有底层命令的网络沙箱。

[README 离线示例](../README.md#可运行离线示例) 可直接完成交接、空结果报告校验与重放提交，所有输入均为 `is_demo: true`。没有采集不等于来源不可用，空演示不等于没有市场。

### 社区采集选项

`research --include-comments` 可选启用评论，`--include-recent-activity` 可选启用旧帖近期活动查询；默认均关闭；`--concurrency` 控制新研究免费检索并发数，允许 1–4，默认 3，只影响免费检索，付费执行仍串行。这些选项在创建研究时设置，恢复沿用运行中的采集配置。旧帖活动日期不能冒充新发帖日期。先取历史上下文再采集，既有资料不代表本轮在线覆盖，也不使程序默认跳过实时检索；本轮 source 状态不采纳历史复用载荷。离线运行保留本地流程，不执行这些可选网络查询。

### 兼容手动流程

`plan → community → BENCH → expand → filter → state prepare → score（仅 A）→ digest → report` 仍可使用。手动处理需给全部 A/B 分配 OPP、R 分配 SIG并回填（包含 overflow）。新编排已负责这些确定性步骤，不应再手动重复提交。个人评估与 planned 实验在准备稳定 ID 后可开展，无需等待日报。

## 2. 采集窗口与来源

- 30 个自然日：日常需求、投诉、切换、招聘和付款信号。
- 7 日：新出现或快速升温。
- 90 日：重复性与地区验证。
- 365 日：竞品、失败历史和“为什么是现在”。

所有窗口包含首尾日期。使用查询计划里的 `from/to`，不自行估算。

`build_query_plan.py` 生成共享 `run_id` 的社区与 TikHub 计划。精确定向用 `--scope-file` 声明国家、语言、人群、任务和本地查询；自由文本用 `--focus-file`，未指定的地区和语言保持未知，格式见 [Agent 集成](agent-integration.md)。社区只允许 Hacker News/GitHub；TikHub 只允许一期白名单。Web 用于打开竞品定价、付款证据、本地差异和反证，不绕过来源范围。

先检查同日及近 30 日原始文件和历史状态，再运行免费来源。免费证据尚未转成 BENCH、候选和缺口清单前，不执行广泛付费检索。

TikHub 必须先实时估价，再显式预算执行。每个付费请求必须关联候选 ID、缺失门槛和预期升级层级。搜索和评论分阶段估价；评论只深挖 1–5 个高价值帖子，不为凑数量批量抓取。付费发现最多占预算 20%。定向搜索补证每来源每批最多 3 请求是脚本硬限制；跨两个来源合计 4 请求合法。每批结束后由 Agent 评估是否新增 BENCH、合格候选或关键证据，无产出时停止该来源，不追加新批；执行器不能自动判断商业价值。

## 3. 证据规范化

```json
{
  "source": "reddit",
  "url": "https://...",
  "author": "u/example",
  "container": "r/example",
  "original_text": "short quote",
  "zh_translation": "简短翻译",
  "language": "en",
  "published_at": "2026-07-13",
  "date_confidence": "high",
  "observed_at": "2026-07-15T09:00:00+08:00",
  "engagement": {"comments": 42},
  "access_method": "native-platform",
  "signal_types": ["complaint", "workaround", "subscription"],
  "market": "美国",
  "notes": "与哪个 BENCH 或候选有关"
}
```

规范化器支持搜索发现、定向补证和评论深挖结果；同一轮搜索、详情与评论保留同一 `run_id`。评论保留父帖定位；未知的原文语言不从查询语言推断。

访问方式只用 `native-platform`、`third-party-api`、`search-index`、`authorized-browser-sample`、`manual-verification`。发布时间不确定就降低日期置信度；互动量未知就省略或写未知。

## 4. 建立 BENCH

每个付费对标核验：产品/服务、来源市场、付款者、价格或支出、付款信号、当前替代、缺口、获客渠道、30 天最小产品和直达证据。

优先证据顺序：

1. 可追溯的真实购买或已付订阅、发票、合同；标记 `purchase` 或相应 `paid_*` 类型。
2. 用户明确自述已经购买、付费续订、退款或取消，并能定位原文。
3. 招聘、外包与服务报价；分别说明支出意图和已成交事实。
4. 官方定价页、订阅方案、未付发票和合同条款；只能证明收费方式。
5. 互动、点赞、愿望和泛讨论；不能单独建立 BENCH。

付款主张必须有链接和支持文本。A 级还必须引用候选的有效 `evidence`，在同条记录中核对目标地区、付款者与已付款事实，不能跨记录拼接。研究者负责核验原文，脚本约束结构和引用关系。

## 5. 扩展与过滤

用 `expand_ideas.py` 做六轴有限扩展，先去重维度，再按对标轮询，最多扫描 5000 个组合。缺失事实保持未知；新用户、触发、形态、地区和渠道写入 `hypotheses`，通过 `candidate_verifications` 逐项补证。候选跨国家或主渠道时保留独立 `market_scope`；同一机会的报价和交付方案聚合为 `variants`。

用 `filter_ideas.py` 执行六项硬门槛。输出：

- A：硬门槛通过，本地直接付款 + 至少两个有效独立原始来源 + 候选假设已补证；进入深度评分。
- B：收费对标 + 投诉/替代/招聘/外包；进入快速点子，明确是否已有真实成交。
- R：来源市场收费对标 + 具体迁移理由，缺本地付款；进入区域 SIG，并说明本地差异。
- rejected：保留明确失败门槛，便于后续补证。

目标数量不足时停在真实数量，并在报告说明是哪种证据不足。仓库示例输出是离线流程演示，不能作为真实市场结论。

## 6. 评分、反证与报告

只为 A 级补齐三轨评分；评分器重新核验资格，不接受单来源降置信度后继续评分。拟进入 Top 5 前必须查：直接/间接竞品、免费替代、用户不购买原因、失败产品、平台内置能力、数据与合规依赖、当地竞品。

新编排自动准备稳定 ID、应用 Agent 的 A 级评分并回填 tiered；从同一结构化对象生成完整清单与 Markdown 展示。手动链路仍可使用 `build_result_digest.py`。内容保持以下分层：

- 深度机会：解释购买触发、证据、反证、MVP、首笔收入与最大风险。
- 收费对标支持的候选（B）：只写付款者、对标、需求、替代、缺口、渠道和 MVP。
- 区域创意：只写来源市场、目标地区、本地差异、最小产品、缺证和升级条件。
- 接近合格：展示最多 20 个失败门槛最少的候选和补证路径。
- 费用产出：展示请求、成本、证据利用率、单个合格结论成本和来源转化。

完整清单必须包含全部合格 A/B/R 和 overflow。结构校验通过前不写状态。

## 7. 状态与升级

从分层结果提取 A/B 或 R，分别用 `prepare --kind opportunity` / `--kind signal` 分配稳定 ID，再评分和写正式报告。实验 `record_id` 引用返回的 OPP/SIG；`prepare` 不提交研究观察。稳定身份基于任务，不基于标题；国家、地区和主渠道存在时加入身份，避免跨市场合并。

新流程以 `report.json` 校验后用同一 `run_id` 分别提交 OPP 与 SIG；Markdown 仅用于展示，旧格式仍可单独校验。同一运行、相同输入重放不新增事件；同一运行更改输入会报冲突。写前 journal 保证中断后可恢复；同链接纠错保留证据版本并使旧引用失效，新运行补证须绑定当前修订与事实。历史回顾只取截止日快照，不读取未来字段。

R/SIG 只有在目标地区出现直接付款且独立来源达标后，才通过 `manage_state.py promote` 升级。升级保留双向链接。

## 8. 深挖与趋势

深挖优先补“最可能推翻机会的证据”。用 [个人适配与验证](personal-validation.md) 检查自己的技能、可触达渠道、时间、预算及运营约束，选一个主验证项目。72 小时实验分别记录真实任务、交付样例、报价反馈、付款、投入费用和停止条件；实验不会自动改变证据等级。历史趋势看出现日期、重复观察、证据源、付款变化、地区和证据层级。没有新结果不等于需求下降；只有发现需求被满足、用户迁移、竞品覆盖或付费消失时才降级。
