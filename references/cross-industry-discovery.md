# 面向普通用户的六方向研究

默认研究电商、游戏、创作、成人学习、生活，以及传统互联网产品的 AI 改造与迁移。运营能力体现在电商经营、内容分发、社区组织、用户留存与产品增长中。制造、农业及依赖陌生线下资源的行业不进入默认目录；这表示当前个人范围，不代表这些行业没有 AI 价值。

## 查询与覆盖

`src/aor/sources/industries.json` 定义方向、目标用户、需求模型、中英文任务查询及适配来源。每日为每个选定方向规划一条中文、一条英文付费发现查询和一个必须由宿主完成的网页核验任务。HN 只作为最多三个任务的辅助查询，默认不搜 GitHub。平台池仍受已实现适配器约束，轮换计划不代表这些来源已成功采集。

默认词关注用户实际任务，不要求帖子提及 AI。发现“现在怎么做、为什么不满意、是否持续使用或花钱”后，再判断 AI 能否带来增量。除提效外，也接受有原文支持的重复使用、复购、续订、创作产出、学习进展、任务完成、自然分享和社区参与。播放量、点赞量和泛讨论不能替代这些行为。

`DATA_HOME/config/preferences.json` 可选择范围，须合并已有配置：

```json
{
  "industries": ["ecommerce", "gaming", "content_creation", "learning", "personal_life", "internet_products"]
}
```

`DATA_HOME/config/industries.json` 可按同 ID 覆盖默认目录或新增方向，格式同内置 JSON。新增方向仍只能使用已支持的来源；扩展目录不需要增加一次性采集分支。定向 `--focus` 与显式 `--intents` 保留原路径，意图可携带 `industry_ids`。

## AI 增量与探索线索

新 `research` 候选和显式线索需要 `ai_value`，例如：

```json
{
  "status": "hypothesis",
  "baseline": "创作者逐段听录音，手动标记重复语句后交剪辑师",
  "capability": "识别语义重复并关联原始音频时间位置",
  "user_benefit": "先审阅可撤销的删改建议，减少反复听录音",
  "incremental_advantage": "处理语义重复而非仅靠音量阈值切除静音"
}
```

`status` 为 `hypothesis` 或 `supported`；`disproven` 不纳入线索。脚本只检查对比描述是否完整，实际效果由原文、产品测试或客户实验核验。没有具体增量、只写“AI 赋能”或“加聊天框”不能通过。假设必须保持假设标签。

收费市场、付款者、当前替代、缺口、渠道和 30 天 MVP 仍是正式 A/B/R 的门槛。证据支持的具体用户需求暂时缺少渠道、定价或 MVP 结论时，保留为 `research_leads`，独立于正式候选数和单位合格结论成本。显式输入可为 `{ "benchmarks": [], "leads": [...] }`，无需编造 BENCH。每条 lead 至少包含 `title/target_user/problem_or_desire/wedge/industry_ids/ai_value/evidence`，建议填写 `missing_requirements/next_question`；evidence 必须有真实直达链接和原文事实。

线索按稳定 `LEAD` 身份写入 `state/research-leads.jsonl`；同一运行幂等，修改须新运行。下一次研究的 `research-lead-history.json` 提供截止研究日的最近观察。历史线索不增加本轮发现数量，也不自动升级为 OPP。

## 一次性预算发现

未给预算时不会自动执行 TikHub。用户明确同意发现目标及上限后：

```bash
aor resume "$RUN_ID" --discover --max-cost-usd 10 --max-discovery-requests 12 --batch-id discovery-01 --home "$RADAR_HOME"
```

金额只是命令示例，必须替换成用户授权额度内的美元上限，不能把人民币数直接当美元数。此命令实时估价，优先分散到各方向；无实时价格的端点记录跳过。全部批次共用该 run 的 journal 累计上限。它不充值、不配置自动续费、不授权下一次研究支出。商业判断仍由宿主完成，初期无需先有 BENCH 才能购买发现材料；定向补证与评论深挖仍按原来的每来源/每帖上限执行。

每批评估查询相关性、无结果、平台错误、证据质量和新线索。参数错误先查官方文档修正；已保存响应解析失败只恢复解析，不能因此重新购买。`unknown` 仍需明确解决后才可重试。Reddit `time_range` 使用官方小写枚举，发现默认 `month`，见 [TikHub Reddit 搜索文档](https://docs.tikhub.io/369454687e0)。

## 阅读报告与继续研究

1. 先看 `industry-coverage.json`：每个方向的请求、失败、材料、相关性、人工核验、正式候选和线索分别计数。
2. 看 `evidence-packet.json`：按方向、来源及证据角色平衡抽样，不让先输入的平台挤掉其他材料。
3. 包被截断时，用 `evidence-index.json` 定位遗漏条目，在完整 `evidence-context.json` 查原文。索引不代替原文核验。
4. 打开原始页面核验对标、用户行为和免费替代，经 `sources import` 导入并 `resume --evidence`；导入可写 `industry_ids`。
5. 形成正式候选或待验证线索，提交 assessment，交付 `report.md/summary.md`。

相关性算法只是检索线索；只有宿主确实打开原文，才可写 `host_attested`。六方向都有材料不表示市场全面覆盖；未采集、无结果、检索失败、待核验、缺付款和个人不适配须分别说明。零正式候选时，`research_quality` 会保留覆盖缺口与遗漏数量，不推断“市场没有机会”。
