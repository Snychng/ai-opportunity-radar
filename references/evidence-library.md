# 本地证据库、主张与离线评估

最后更新：2026-09-14。统一入口为 `aor library` 和 `aor eval`；源码兼容入口为 `scripts/evidence_library.py` 与 `scripts/evaluate_research.py`。这些领域命令处理本地资料；通过统一 CLI 运行时设置 `AOR_OFFLINE=1`，避免更新预检联网。

## 证据库接口

程序内 `search(..., include_retracted=True)` 可读取已撤回的最新修订，默认检索仍排除撤回记录。研究分层使用该完整状态，避免旧对标摘要重新恢复撤回证据的效力。正式状态保留带 `library_evidence_id` 的源库 `revision_id`，另用 `state_revision_id` 和 `version` 记录本地状态版本，评分引用不随落库改写。

`library` 的全局参数 `--root`（必填）和 `--output` 必须放在子命令前。示例承接 [快速开始](quick-start.md#可运行离线示例) 的临时目录、运行 ID 与网页导入结果：

```bash
AOR_OFFLINE=1 aor library --root "$AOR_DEMO_HOME/evidence-library" ingest \
  --input "$AOR_DEMO_HOME/web-evidence.json" --as-of 2026-09-10 --run-id "$AOR_DEMO_RUN_ID"
AOR_OFFLINE=1 aor library --root "$AOR_DEMO_HOME/evidence-library" search \
  --as-of 2026-09-10 --run-id "$AOR_DEMO_RUN_ID" --query "support" --query "pricing" --limit 10
AOR_OFFLINE=1 aor library --root "$AOR_DEMO_HOME/evidence-library" \
  --output "$AOR_DEMO_HOME/context.json" context \
  --as-of 2026-09-10 --run-id "$AOR_DEMO_RUN_ID" --limit 10 --max-chars 12000
AOR_OFFLINE=1 aor library --root "$AOR_DEMO_HOME/evidence-library" delta \
  --since 2026-09-09 --as-of 2026-09-10 --run-id "$AOR_DEMO_RUN_ID"
AOR_OFFLINE=1 aor library --root "$AOR_DEMO_HOME/evidence-library" rebuild
```

| 子命令 | 输入与用途 |
|---|---|
| `ingest` | `--input` 接收证据对象、数组、JSONL 或带 `evidence` 的阶段对象；`--as-of` 必填，`--run-id` 可选 |
| `search` | `--as-of` 必填；可重复 `--query`，多查询以 RRF 融合，`--limit` 默认 20 |
| `context` | search 参数加 `--claims FILE`、`--experiments FILE`、`--max-chars`；生成供宿主阅读的证据包 |
| `delta` | `--since` 与 `--as-of` 必填，查看期间变化；可带 `--run-id` |
| `rebuild` | 从库内 `evidence.jsonl` 重建 `evidence.sqlite3` |
| `migrate-identities` | `--destination` 指向不同且尚不存在的目录；重建帖子/评论独立身份，保留原库和历史引用映射 |

输入的运行元数据须与 ingest 参数一致。历史复用使用 search/context，不通过改写旧运行日期伪造新采集。截止日快照避免未来原文或未来观察泄漏；同源重复采集标签会归并，不增加独立来源。保留 `raw_ref`、修订 ID 和内容摘要，索引损坏可重建，观察日志仍是事实来源。

## 主张引用与 Agent 交接

`research` 自动将证据入库，输出 `evidence-context.json` 和较小的 `evidence-packet.json`。采集前先保存 history-context；后续保留完整截止日上下文，主包按 30 条、18000 字符上限组织；industry-packets 提供逐方向有界包。原文窗保留 field/start/end，重复采集元数据压缩为单个追溯指针和引用数，完整历史保存在库和 context 中。对标中的事实摘要不能覆盖已导入原文修订。包中截断信息必须保留；需要更多原文时回到引用文件，不能把包内没有某条资料当成原始资料不存在。

`assessment.claims` 是主张数组，每条有非空 `id`、`statement`，可补 `kind/payer/market/date`。`verification_status` 允许 `supports/partial/conflicts/unverified`；没有引用只能是 `unverified`。`evidence_refs` 每项需实际 `evidence_id`、对应 `revision_id`、原文 `quote`；`field` 可指定 `original_text`、`text` 或 `comments.0.text` 等评论原文字段。内部旧证据兼容读取时不能猜造缺失修订；新的公开引用必须能解析到确切的源库 ID 与修订。

程序定位 quote，返回 `field/start/end` 与 `reference_validation`；`semantic_validation=not_performed` 明确保留。引用存在、日期有效并不说明它支持付款、人群或市场主张。演示材料始终保留 `is_demo`，不能变成真实商业结论。`context --claims` 读取主张数组，`--experiments` 读取实验数组，不是整个 assessment 对象。

机会层可保留 `score_basis`、`axis_priorities`、`selection_strategy` 与 `experiment_results`，用引用解释选择依据。选择和实验背景不会自动改变 A/B/R；旧评分输入缺依据时可以保留缺失说明，不会由程序填入语义判断。具体评分入口见 [评分契约](scoring.md)。

## 离线评估接口

```bash
AOR_OFFLINE=1 aor eval --output /tmp/aor-eval.json
# 使用符合相同 fixture 契约的本地固定材料：
AOR_OFFLINE=1 aor eval --fixtures evals/research-quality.json --output /tmp/aor-eval-explicit.json
```

默认 fixture 为 `evals/research-quality.json`。输出包含 `eval_version`、`quality_version`、`fixture_version`、`fixture_sha256`、`network_called`、`passed`、`cases_passed/cases_total` 与逐项 `cases`；全部通过退出 0，否则退出 1。

当前检查固定材料的相关性、时间窗口、部分失败、转载、去重、评论父链和旧帖活动日期。`network_called=false` 表示该评估不进行真实网络采集；通过不等于真实平台可用、检索穷尽、在线召回率改善或客户验证完成。


## 对象身份、语义审阅与历史修复

`evidence_object_identity` 使用平台、对象类型和原生 ID；同帖评论不再因 parent_post URL 被合并成帖子修订。普通网页无原生对象身份时才回退到规范 URL。引用优先按 evidence_id/revision_id 解析，URL 回退必须唯一；有多个对象时明确报歧义，不静默选择最后一条。

词面筛选输出 relevance_basis=lexical_screening 与 lexical_status，不自动得出语义判断。宿主提交的 relevance_review 包含 relevant/unrelated 状态、非空 reviewer、有效且不晚于研究日的 reviewed_at，并绑定当前 evidence_id+revision_id（或 content_sha256 匹配 content_hash）。角色仍须对应原文：official_pricing 只说明收费方案，不能填成已付款用户行为。assess_quality 的提示不替代公开导出的精确修订校验。

`evidence-index.items[].selected` 仅表示在阅读包中；review_queue 包含全部仍待审阅条目，包括已入包者。先按 research-followup.industry_tasks 找需求、收费对标、替代、反证缺口，再按 ID/修订读取原文并提交 assessment.evidence_reviews；不能仅修改 selected 或 host_attested 来消除覆盖缺口。

旧库纠错使用派生迁移：

```bash
AOR_OFFLINE=1 aor library --root "$RADAR_HOME/evidence-library" migrate-identities \
  --destination "$RADAR_HOME/evidence-library-v2"
```

目标目录必须尚不存在；迁移不覆盖源库、源报告或受摘要保护的原始记录，输出可审阅的新库和旧引用映射。映射冲突须显式处理，不能按父帖 URL 自动改写主张。已保存的采集响应还可通过 `resume PARENT_RUN_ID --reparse` 生成离线子运行重新解析；两者都不发起新付费请求。参考 [研究工作流](research-workflow.md#9-已付费响应的离线重解析)。


规范化结果额外保留 `derive_sets`：每份原始执行响应的 SHA-256、parser_version、完整/部分解析状态、派生对象和正文摘要；记录以 derivation_refs 关联。较新解析器处理同一响应后，完整派生集合中消失的旧对象或旧正文标记 `derivation_status=superseded`，保留原始日志与修订，但不进入当前阅读包、覆盖或公开支撑。这表示解析产物被替代，不表示发布者撤回原文。部分解析、同版本冲突及无法确认的旧输入保留 needs_review；其他独立响应仍支持的材料保留有效。阅读包在 omitted.evidence_superseded 单独计数。

有效派生集合登记在库内独立的追加式 `derivations.jsonl`，包含获知日和运行来源，重复登记幂等。后续运行按截止日加载登记，重建和身份迁移保留它；新运行无需重复提供原响应，也不会恢复已被替代的解析产物。默认库搜索排除 superseded，审计程序可用 `search(..., include_superseded=True)` 查看。旧记录只有无法核验的响应路径时保留 needs_review，不能因路径改变就认定有另一个独立来源。精确历史修订仍能通过 resolve 查阅。

绑定原文修订的语义复核单独追加到 `evidence-reviews.jsonl`，后续 search/resolve 可按截止日复用。复核不计为新采集，不更改原文或 run_ids/观察日期；同正文复采沿用有效判断，正文变化后需重新复核。后来的否定判断覆盖旧相关判断，重复提交旧复核不会回滚它。重建与身份迁移保留复核日志。
