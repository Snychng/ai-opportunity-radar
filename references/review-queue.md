# 批量语义审阅任务

`review-queue` 把现有证据分成可以领取、提交和恢复的本地任务。它不搜索网页、不调用模型或付费 API，也不替审阅者判断语义。统一入口使用 `AOR_OFFLINE=1` 关闭更新预检；兼容入口为 `python3 scripts/review_queue.py`。

## 创建与领取

```bash
export AOR_OFFLINE=1
aor review-queue plan --home "$RADAR_HOME" --run-id "$RUN_ID" \
  --max-items 16 --max-chars 18000
aor review-queue claim --home "$RADAR_HOME" --run-id "$RUN_ID" \
  --worker industry-reader-a --industries ecommerce content_creation --lease-seconds 1800
```

默认校验读取该运行 manifest 登记的 `evidence-index.json`，次选 `research-followup.json`。索引 `items` 优先于 `review_queue`，因此已审条目也进入进度分母，但不会重新领取。可用 `plan --input FILE` 指定精确引用数组或含 `items/review_queue/evidence` 的对象。输入必须固定 `evidence_id/revision_id`；不能用父帖 URL 代替评论身份。没有 manifest 索引且没有 `--input` 时才使用研究日的完整库快照，返回的 `source.kind` 明示来源与统计范围。

队列位于 `DATA_HOME/review-queues/RUN_ID/`，不改写运行 manifest、原报告或采集观察日志。首次创建和没有有效租约时可再次 plan；已有任务及提交记录保留。每个批次先轮询行业、再分散来源；`max_items` 为 1–100，`max_chars` 为 2000–200000 个 JSON 字符。领取可指定 `--batch-id`，或用 `--industries` 限定自己的行业；同一精确对象不会被不同 Agent 同时领取。

领取结果包含 `lease_id`、`expires_at`、`packet_path` 和实际 `packet_chars`。阅读包提供 ID、修订、来源、链接、标题、原文、查询、行业及日期，避免展开采集元数据。极长原文标记 `text_truncated=true`，完整最小文本放在 `fulltext_path`。阅读完整文件后，提交该条的 `fulltext_sha256`；没有读到完整材料可以提交 `needs_fulltext`，不能把摘要当作已核验全文。租约按真实 UTC 时钟计算，与研究截止日分开。

## 提交判断

保存独立提交文件，保持同一次提交的 `submission_id` 和内容不变：

```json
{
  "lease_id": "从 claim 返回的租约 ID",
  "worker": "industry-reader-a",
  "submission_id": "reader-a-batch-01",
  "results": [
    {
      "evidence_id": "实际证据 ID",
      "revision_id": "实际原文修订",
      "status": "unknown",
      "rationale": "已读到的正文不足以判断目标用户与具体任务，需要补充上下文"
    }
  ]
}
```

```bash
aor review-queue submit --home "$RADAR_HOME" --run-id "$RUN_ID" --input "$SUBMISSION_FILE"
aor review-queue status --home "$RADAR_HOME" --run-id "$RUN_ID"
```

`relevant` 和 `unrelated` 必须附真实的 `rationale` 与 `reviewed_at`，可附目录允许的 `evidence_role`。审阅者固定为领取 `worker`，不能替别人署名；未来日期、错误修订、过期租约、重复或未领取任务均拒绝。程序通过 `library.register_reviews` 精确核验并登记，相关不意味着已付款、AI 增量成立或允许公开发布。脚本不能代替逐条阅读和语义判断。

`unknown`、`needs_fulltext`、`failed` 只保存原因，不写成库中已审结果。允许同租约分多次提交不同任务；未提交部分仍由原租约持有。已有库审阅会被同步，`selected=true`、进入阅读包或完成领取均不算已审。原文换版、撤回或被新的解析替代后，任务标为 `stale` 或 `excluded`，不会将旧判断套到新正文。

## 失败、延期与恢复

```bash
aor review-queue release --home "$RADAR_HOME" --run-id "$RUN_ID" \
  --worker industry-reader-a --lease-id "$LEASE_ID" --reason "本次未能完成，允许其他审阅者接手"
aor review-queue claim --home "$RADAR_HOME" --run-id "$RUN_ID" \
  --worker industry-reader-b --retry-deferred
```

释放或过期的任务进入 `failed`，下次普通 claim 可重领。`unknown/needs_fulltext` 默认不会自动循环重试；准备好补查条件后显式使用 `--retry-deferred`。过期 worker 不得提交或释放其他人的新租约。没有续期命令，预计耗时较长时在领取时调整 `--lease-seconds`，上限为 86400 秒。

提交先持锁保存恢复意图，再追加证据库审阅日志，最后记录回执。登记后中断时，后续 `status/claim/submit` 会重放同一意图；库按审阅内容幂等，不重复登记。同一 `submission_id` 搭配不同内容会拒绝。`status` 会同步库中审阅、恢复未完成提交并更新过期租约，因此它会写队列状态，而不是纯文件查看命令。

进度的 `counts` 分别为 `pending/leased/reviewed/unknown/needs_fulltext/failed/stale/excluded`，`unreviewed_count` 包含前述仍需处理的五类，排除已审和失效材料。分母是本队列来源输入，不一定等于整个证据库。语义审阅不自动生成检索质量标签、机会线索、发布复核或客户验证结论，这些仍走各自的输入契约。

Python 接口为 `ReviewQueue(home, run_id, library_root=None)`，提供 `plan`、`claim`、`submit`、`release`、`status`；方法参数与 CLI 同名，`claim(..., industries=[...])` 支持多 Agent 分行业领取。
