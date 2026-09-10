# 本地证据库、主张与离线评估

最后更新：2026-09-10。统一入口为 `aor library` 和 `aor eval`；源码兼容入口为 `scripts/evidence_library.py` 与 `scripts/evaluate_research.py`。这些领域命令处理本地资料；通过统一 CLI 运行时设置 `AOR_OFFLINE=1`，避免更新预检联网。

## 证据库接口

`library` 的全局参数 `--root`（必填）和 `--output` 必须放在子命令前。示例承接 [README](../README.md#可运行离线示例) 的临时目录、运行 ID 与网页导入结果：

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

输入的运行元数据须与 ingest 参数一致。历史复用使用 search/context，不通过改写旧运行日期伪造新采集。截止日快照避免未来原文或未来观察泄漏；同源重复采集标签会归并，不增加独立来源。保留 `raw_ref`、修订 ID 和内容摘要，索引损坏可重建，观察日志仍是事实来源。

## 主张引用与 Agent 交接

`research` 自动将证据入库，输出 `evidence-context.json` 和较小的 `evidence-packet.json`。采集前先保存 history-context；后续先取最多 100 条检索结果，再补入本轮材料、显式主张/评分引用及对标原文，按 30 条、18000 字符上限组织交接包。对标中的事实摘要不能覆盖同页已导入原文修订。包中截断信息必须保留；需要更多原文时回到引用文件，不能把包内没有某条资料当成原始资料不存在。

`assessment.claims` 是主张数组，每条有非空 `id`、`statement`，可补 `kind/payer/market/date`。`verification_status` 允许 `supports/partial/conflicts/unverified`；没有引用只能是 `unverified`。`evidence_refs` 每项需实际 `evidence_id`、对应 `revision_id`、原文 `quote`；`field` 可指定 `original_text`、`text` 或 `comments.0.text` 等评论原文字段。旧证据未带修订时引用也省略修订，不能猜造值。

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
