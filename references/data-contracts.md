# V3 数据契约与阶段连接

最后更新：2026-09-14。以下字段与当前脚本实现对应；证据真实性仍需研究者核验。

## 1. 共享运行契约

- `schema_version`、`query_plan_version`、`scoring_version`：当前为 `3.0`。
- `run_id`：`RUN-YYYYMMDD-XXXXXXXXXX`。独立 plan 按日期、模式、定向范围、版本及结构化意图确定；research 每次创建唯一运行并回填子计划，允许同日独立研究。
- `as_of`：北京时间 `YYYY-MM-DD`，必须与 `run_id` 日期一致。
- 同一轮原始、规范化、扩展、过滤、评分和状态文件保留同一 `run_id`。历史证据或研究结果跨轮复用时，显式声明 `reused_for_run_id`；执行费用只能属于本轮，不能通过复用标记合并旧费用。

阶段顺序：

```text
query_plan
  -> community_normalized
  -> paid_benchmarks
  -> expanded_candidates
  -> tiered_candidates
  -> evidence_gap_plan -> tikhub_gap_results -> tikhub_normalized
  -> re-run paid_benchmarks + expanded_candidates + tiered_candidates
  -> OPP/SIG stable IDs
  -> A-level scoring
  -> full_result_digest
  -> structured report.json（兼容链路仍可校验旧 Markdown）
  -> state_observations
```

`evidence_gap_plan` 必须由 `tikhub_query.py build-gaps` 生成。每个付费搜索请求携带 `candidate_id`、`missing_gate`、`target_region` 和 `expected_promotion`；通用发现草稿不能冒充已批准的补证计划。

## 2. 付费对标

下面是结构示例，域名和金额为虚构占位值。真实使用时替换为已核验的原文；演示数据必须标记 `is_demo: true`，不会进入 A 级。

```json
{
  "product": "现有客服服务",
  "source_market": "美国",
  "payer": "独立站商家",
  "price": "每月 49 美元",
  "job": "重复回复售前问题",
  "wedge": "自动生成 FAQ 回复",
  "payment_signals": [
    {
      "type": "purchase",
      "region": "美国",
      "payer": "独立站商家",
      "url": "https://vendor.example/receipt",
      "fact": "美国商家已支付49美元购买客服服务"
    }
  ],
  "current_alternative": "人工客服与现有服务",
  "product_gap": "每天仍要人工重复回复常见问题",
  "acquisition_channel": "商家社区",
  "mvp_days": 21,
  "mvp_scope": "导入 FAQ 并生成一次回复",
  "is_demo": true,
  "evidence": [
    {
      "source": "vendor",
      "url": "https://vendor.example/receipt",
      "fact": "美国商家已支付49美元购买客服服务",
      "is_demo": true
    }
  ]
}
```

`pricing`、`subscription`、`invoice`、`contract`、`preorder` 仅表示收费方式、报价或未确认交易；不能单独证明成交。确认已付款才使用 `purchase`、`paid_subscription`、`paid_invoice`、`paid_contract` 等直接交易类型。没有直接链接及支持文本的字符串标签不构成付款证据。

A 级的付款信号必须通过 `url` 或 `evidence_id` 引用候选 `evidence` 目录中有效、未撤回、非演示的证据。目标地区、付款者、交易类型和支持事实必须属于同一条记录；不能拼接本地定价页与国外购买记录，也不能用 `local: true` 代替地区证据。支持文本字段接受 `fact`、`supporting_fact`、`quote`、`text`、`supports` 或 `original_text`。

`BENCH` ID 优先保留显式 ID，否则根据产品、来源市场和付款者生成；价格属于可变化的观察，不参与业务身份。

## 3. 扩展输入与输出

`expand_ideas.py` 输入：

```json
{
  "run_id": "RUN-...",
  "as_of": "YYYY-MM-DD",
  "benchmarks": [],
  "dimensions": {
    "segments": [],
    "triggers": [],
    "forms": [],
    "regions": [],
    "channels": [],
    "offers": []
  }
}
```

六个维度都是可选非空数组；缺失时使用对标已有值，缺失事实保持未知。维度先去重，再按对标轮询扩展，生成 `CAND-XXXXXXXXXX` 和 `VAR-XXXXXXXXXX`。`--limit` 默认 200、最大 500；另有 5000 次组合扫描预算，达到任一上限时通过 `summary.truncated` 提示截断，并报告扫描次数及对标、人群和地区覆盖数。

过滤结果以 `expansion_summary` 保留上游扩展摘要，并将截断写入 `warnings`。`truncated=true` 是达到上限后的保守提示，包含刚好穷尽的可能；不证明一定还有遗漏。完整清单展示这些提示，不能把“展示全部已生成候选”描述为“穷尽所有组合”。费用指标附带 `expansion_truncated`（缺少上游信息时为 null）、`contains_demo_data` 和 `demo_family_count`；继承顶层演示标记并检查变体内证据，混合数据中的演示家族逐行标记，演示数据不作为市场验证。

扩展候选必须携带：

- `benchmark_ids`
- `payer`、`buying_trigger`
- `current_alternative`、`current_spend`
- `payment_signals`、`demand_signals`
- `product_gap`、`acquisition_channel`、`delivery_model`
- `mvp_days`、`mvp_scope`
- `source_region`、`target_region`、`localization_gap`、`transfer_reason`
- `market_scope.country/region/language/primary_channel`

扩展改变的用户、触发、形态、地区和渠道字段记入 `hypotheses`；缺失的缺口、渠道和 MVP 字段也记为未知假设。A 级前必须逐项提供匹配候选当前值的 `candidate_verifications`，并引用有效证据。例如：

```json
{
  "hypotheses": {
    "target_user": {"value": "小型商家", "basis": "待验证的新细分人群"}
  },
  "candidate_verifications": {
    "target_user": {
      "value": "小型商家",
      "evidence": [{"url": "https://merchant.example/interview", "fact": "受访者经营一家小型商店"}]
    }
  }
}
```

此片段需并入完整候选，且引用原文也必须位于该候选的 `evidence` 中。不能仅删除假设标记来绕过验证。

## 4. 过滤输出

`filter_ideas.py` 按业务身份归并为 `opportunity_family`，保留报价与交付 `variants`，选取单个实际合格变体作为代表，不拼凑不同变体的门槛。输出以下集合：

- `deep_candidates`：A 级，`record_kind=opportunity`
- `validated_ideas`：B 级收费对标支持的候选，`record_kind=opportunity`；字段名保持兼容，不表示客户已验证
- `regional_signals`：R 级，`record_kind=signal`
- `research_leads`：尚缺正式门槛的可追溯探索线索，使用稳定 LEAD，与正式 A/B/R 数量分别计数
- `rejected`：附 `rejection_reasons`，例如无明确 AI 增量或无可用证据
- `overflow`：超过 B 级 40 条或 R 级 80 条的合格候选；不丢弃，但不进入当日日报主卡片

过滤结果同时原样保留 `benchmarks`，并在 `summary.benchmark_count` 记录数量，供费用产出和完整展示使用。

每条保留 `hard_gates`、`variants` 与 `unverified_hypotheses`；`summary.raw` 是原始变体数，`summary.families` 是归并后的机会数。缺失、`unknown`、待验证等占位事实不会通过硬门槛。A 级另需至少两个有支持文本的独立原始来源；同一 URL、转载原文或同一发布主体的不同采集标签不能增加来源数。R 级还必须保留：

- `missing_proof`
- `promotion_triggers`
- `source_region`、`target_region`
- `localization_gap`、`transfer_reason`

## 5. 完整结论清单

`build_result_digest.py` 读取 tiered JSON。快速摘要可以直接使用 `CAND`；正式研究先为全部 A/B 准备 OPP、R 准备 SIG（含 overflow），将返回记录和 A 级评分按 ID 回填分层数组，再生成完整清单与日报。可重复接收：

- `--execution`：TikHub 搜索、评论或重试执行结果
- `--evidence`：包含 `evidence` 数组的规范化结果
- `--research`：包含 `ranked_candidates` 或 `clusters` 的研究结果

输出全部 A、全部 B、全部 R、全部 overflow、建议日报展示数量、完整清单额外数量、按失败门槛数量排序的接近合格项，以及请求、费用、证据利用率、单个合格结论成本和来源产出。同一路径和相同内容的执行结果副本均去重，避免费用重复计算。额外报告 `qualified_family_count`、`validated_opportunity_family_count`、`regional_hypothesis_family_count` 与 `cost_per_validated_family_usd`；R 级是迁移假设，不能计为已验证市场。

新编排将完整清单放入结构化报告并渲染；Markdown 只负责展示。手动 digest 与旧日报仍兼容，但不能取代 report.json 的新提交契约。validated_* 指标指研究资格，不是客户已验证数量。

## 6. 稳定 OPP/SIG 身份

格式：

- `OPP-YYYYMMDD-XXXXXX`
- `SIG-YYYYMMDD-XXXXXX`

指纹字段：

1. `target_user`
2. `context`
3. `problem_or_desire`
4. `wedge`
5. 可选 `market_scope.country`
6. 可选 `market_scope.region`
7. 可选 `market_scope.primary_channel`

标题、翻译、总分和证据增删不改变 ID。国家或主渠道真正不同时应生成不同 ID。旧记录没有 `market_scope` 时继续使用四字段身份，保持兼容。

正式日报、A 级评分及实验引用前，执行 `manage_state.py prepare` 分配 ID；快速摘要不强制准备。A/B 使用 `--kind opportunity`，R 使用 `--kind signal`。实验的 `record_id` 必须引用返回的 `id`，不能填写 `candidate_id`。不要手工编造 ID。

`prepare` 不提交研究观察；准备好稳定 ID 就可以记录 `planned` 实验，不要求先跑真实采集、生成日报或 `record-batch`。个人约束与实验输入见 [个人验证](personal-validation.md)；完整编排示例见 [快速开始](quick-start.md#可运行离线示例)。

## 7. 状态与升级

当前视图：

- `state/opportunities.jsonl`
- `state/signals.jsonl`

追加历史：

- `state/opportunity-observations.jsonl`
- `state/signal-observations.jsonl`

同一 `run_id + kind + fingerprint` 且输入相同的重放不重复写事件；同一运行修改输入会明确报冲突，应使用新的运行保存修订。`occurrences` 按唯一日期计数。

写入通过文件锁、写前 journal 与原子替换覆盖当前视图和观察历史；中断后下一次访问先恢复事务。`history --date YYYY-MM-DD` 返回截止日内最近快照（该参数对应内部 `as_of`），不混入未来字段；不允许倒写日期污染过去快照。

同一链接的纠错保存到 `evidence_history`，保留 `evidence_id`、递增 `version`、`revision_id` 和 `supersedes`。撤回或修订使旧付款信号、需求信号及验证引用失效，并重新检查层级。新运行重新引用修订证据时，必须提供当前 `evidence_revision_id` 和与该版本一致的支持事实；只改版本号不能复活旧主张。

R 级补齐本地直接付款和独立来源后，通过 `manage_state.py promote` 升级：

```text
SIG.promoted_to -> OPP ID
OPP.promoted_from -> SIG ID
```

升级在同一可恢复事务内写入两侧当前视图与观察事件，重新核验 A 级门槛及业务身份。已升级 SIG 的相同运行、相同输入重放返回原 `OPP`。

## 8. 兼容与失败策略

- 阶段版本不一致时失败关闭，不静默混用。
- 缺正式硬门槛时不猜测补齐；启用探索线索保留且存在具体用户任务、原文和 AI 增量假设时可进入 research_leads，不能直接进入 A/B/R。
- 报告校验失败时不写机会状态。
- 新报告使用结构化字段与计数校验；旧 Markdown 报告仍按原格式检查完整清单链接及费用产出。
- 状态文件损坏时明确报错，不自动覆盖。
- 持久研究数据统一写入 `RADAR_HOME`；示例临时输出可指定其他位置，不写入项目或技能目录。
- 通用 CLI、定向范围文件见 [Agent 集成](agent-integration.md)；个人约束和实验日志见 [个人验证](personal-validation.md)。

## 9. 编排交接契约

运行清单另有 `workflow_version=1.0`，包含 status、stages、artifacts、evidence_artifacts、execution_artifacts 与 offline。artifact 以路径和 canonical JSON SHA-256 登记；直接修改会导致摘要不符。

- `benchmarks` 输入沿用本文件对标与扩展结构；使用 research 返回模板。允许 `{benchmarks: [], leads: [...]}`；仅在 benchmarks 与 leads 都为空时要求非空 `empty_reason`。
- `assessment` 包含 `scores` 数组、`claims` 数组及 `decision`，可提供 `evidence_reviews` 和按内容 ID 索引的 `publication_reviews`。每个 A 候选含 overflow 都按稳定 id 提交评分；没有 A 时 scores 可空。编排接收 track、scores、auxiliary_scores、score_basis、validation_plan；评分依据使用 score_basis。
- 输入未带 run_id/as_of 时由本轮补齐；带入其他运行的 benchmarks/assessment 被拒绝。历史材料通过 `--evidence` 导入，不能将未来资料放进过去的研究。
- `resume --assessment` 完成结构化校验与本地 commit；已提交运行拒绝新输入。`research --parent-run-id` 记录后续关系。

## 10. 来源意图与主张

intent_plan 分离 question、search_query、ranking_query；编译后按请求指纹归并并保存 intent_refs/provenance，详情见 [查询模式](query-patterns.md)。`sources import` 接收宿主已核验网页的原文子串与核验声明，不将价格页转换成直接付款，详情见 [来源目录](source-catalog.md)。

证据库使用 evidence_id、revision_id、content_hash、观察日期与 raw_ref；重复来源标签不增加独立来源。新主张引用使用 evidence_refs 内的 evidence_id/revision_id/quote，可选 field；旧 state 付款信号的 evidence_revision_id 属于另一层兼容契约，不要混用字段名。引用定位成功不等于语义成立，详见 [证据库与评估](evidence-library.md)。

## 11. 报告与付费回执

新内部结构化报告使用 `report_version=1.1`；1.0 只保留历史审计兼容，公开发布需重新校验升级。候选与报告顶层运行元数据一致，`market_validated=false`。decision 必填四项非空文本；计数含 overflow；有效报告的 commit 回执保存 report_sha256 与 records_sha256。报告还保留 evidence_inventory（证据统计输入）和 run_ledger（整轮费用与尝试状态）；metrics、source_yield 和 Markdown 清单从这些结构化材料重算，不依赖旧 digest_markdown。完整字段见 [报告契约](report-template.md)。

付费结果区分本次调用 results/summary、预算保护 execution_budget 和跨批累计 run_ledger。`execution_budget.scope=run` 只在共用持久 journal 时成立；旧无 journal 调用为 batch。unknown 不能自动重买，详细参数与恢复语义见 [TikHub](tikhub-integration.md)。


## 12. 网站公开契约与探索状态

发行版本 4.1.0、研究 schema 3.0、内部 report 1.1、行业 catalog/coverage 2.0 与公开 contract 1.0.0 分别版本化，不能混用。网站消费经过白名单、引用和发布校验的公开数据，不直接读取 execution_results、原始评论、缓存链接和本地路径。字段定义、示例与兼容策略见 [网站契约](website-contract.md)。

LEAD 必填 title、target_user、problem_or_desire、wedge、industry_ids、ai_value 和可用 evidence；行业必须属于本轮允许范围。第一次分配 lead_id 后，后续修订必须继续传该 ID，文案与证据变化产生 revision_id，不用新标题替换业务身份。research_status 包含 needs_verification、observed_need、needs_review、archived、disproven、promoted；升级保留 promoted_to，且目标必须是已通过门槛并已入库的 OPP/SIG。

`ai_value.status=hypothesis` 表示待实验的增量假设。`supported` 额外需要原文引用及 reviewer/reviewed_at/rationale，公开导出仍会核验这些引用。四项重复套话不能代替 baseline/capability/user_benefit/incremental_advantage 的具体对比。

coverage 2.0 分开统计 material、词面 related、页面 verified、语义 reviewed、近期用户行为、官方收费对标、替代与反证。`relevance_review` 需 status、reviewer、reviewed_at，并绑定 evidence_id+revision_id，或匹配当前 content_hash 的 content_sha256；过期修订、未来或无效审阅不能算完成。`industry-coverage.tasks` 保留任务/语言的实际尝试与缺口，未执行计划、无实时价格和历史复用不计为本轮成功采集。

`evidence-index` 与 `research-followup.review_queue` 保存未审阅材料；selected 仅表示进入阅读包。industry-packets 提供逐方向有界包。`completed` 是文件交接结束，不能等同于市场覆盖完整或客户需求已验证。
