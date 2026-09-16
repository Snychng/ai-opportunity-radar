# 面向普通用户的六方向研究

默认研究电商、游戏、创作、成人学习、生活，以及传统互联网产品的 AI 改造与迁移。运营能力体现在电商经营、内容分发、社区组织、用户留存与产品增长中。制造、农业及依赖陌生线下资源的行业不进入默认目录；这表示当前个人范围，不代表这些行业没有 AI 价值。

## 查询与覆盖

`src/aor/sources/industries.json` 定义 6 个方向、每方向 6 个子赛道，共 36 个子赛道和 72 条中英文任务查询。每个子赛道明确人群、任务、待核验的既有行为/支出、人工来源与证据目标；这些研究问题不是既定市场事实。每日为每个选定方向规划一条中文、一条英文付费发现查询和一个必须由宿主完成的网页核验任务。只读最近 60 份 coverage 2.0 快照，以实际 task×language 请求次数和缺口安排补查及轮转，规划数量不算完成历史。HN 只作为最多三个任务的辅助查询，默认不搜 GitHub。平台池仍受已实现适配器约束，轮换计划不代表这些来源已成功采集。

默认词关注用户实际任务，不要求帖子提及 AI。发现“现在怎么做、为什么不满意、是否持续使用或花钱”后，再判断 AI 能否带来增量。除提效外，也接受有原文支持的重复使用、复购、续订、创作产出、学习进展、任务完成、自然分享和社区参与。播放量、点赞量和泛讨论不能替代这些行为。

`DATA_HOME/config/preferences.json` 可选择范围，须合并已有配置：

```json
{
  "industries": ["ecommerce", "gaming", "content_creation", "learning", "personal_life", "internet_products"]
}
```

`DATA_HOME/config/industries.json` 可按同 ID 覆盖默认目录或新增方向，格式同内置 JSON。新增方向仍只能使用已支持的来源；扩展目录不需要增加一次性采集分支。定向 `research --focus-file FILE` 与显式 `--intent-plan-file FILE` 保留原路径，意图可携带 `industry_ids`。

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

`status` 为 `hypothesis` 或 `supported`；`disproven` 不纳入线索。supported 额外要求 evidence_refs 及 review.reviewer/reviewed_at/rationale；实际效果仍由原文、产品测试或客户实验核验，公开导出会复核引用。没有具体增量、只写“AI 赋能”或“加聊天框”不能通过。假设必须保持假设标签。

收费市场、付款者、当前替代、缺口、渠道和 30 天 MVP 仍是正式 A/B/R 的门槛。证据支持的具体用户需求暂时缺少渠道、定价或 MVP 结论时，保留为 `research_leads`，独立于正式候选数和单位合格结论成本。显式输入可为 `{ "benchmarks": [], "leads": [...] }`，无需编造 BENCH。每条 lead 必须包含 `title/target_user/problem_or_desire/wedge/industry_ids/ai_value/evidence`，建议填写 `missing_requirements/next_question`；evidence 必须有真实直达链接和原文事实。

线索首次分配 `LEAD` 身份后，修订输入必须继续携带 lead_id；revision_id 表示内容修订。按稳定 `LEAD` 身份写入 `state/research-leads.jsonl`；同一运行幂等，修改须新运行。下一次研究的 `research-lead-history.json` 提供截止研究日的最近观察。历史线索不增加本轮发现数量，也不自动升级为 OPP。

## 一次性预算发现

未给预算时不会自动执行 TikHub。用户明确同意发现目标及上限后：

```bash
aor resume "$RUN_ID" --discover --max-cost-usd 10 --max-discovery-requests 12 --batch-id discovery-01 --home "$RADAR_HOME"
```

金额只是命令示例，必须替换成用户授权额度内的美元上限，不能把人民币数直接当美元数。此命令实时估价，优先分散到各方向；无实时价格的端点记录跳过。全部批次共用该 run 的 journal 累计上限。替补只从该请求预先编译的同查询、同语言、同能力来源中选择，要求端点已登记且有实时价格；原跳过原因和替补 ID 留在 skipped_requests/substitutions，替补也计入同一预算与请求数上限。它不充值、不配置自动续费、不授权下一次研究支出。商业判断仍由宿主完成，初期无需先有 BENCH 才能购买发现材料；定向补证与评论深挖仍按原来的每来源/每帖上限执行。

每批评估查询相关性、无结果、平台错误、证据质量和新线索。参数错误先查官方文档修正；已保存响应解析失败只恢复解析，不能因此重新购买。`unknown` 仍需明确解决后才可重试。Reddit `time_range` 使用官方小写枚举，发现默认 `month`，见 [TikHub Reddit 搜索文档](https://docs.tikhub.io/369454687e0)。

## 阅读报告与继续研究

1. 先看 `industry-coverage.json`：逐方向及任务/语言分别展示 planned、attempted、skipped 和原因；材料、词面相关、打开页面、语义审阅、近期用户行为、收费对标、替代、反证分别计数。
2. 看 `evidence-packet.json`：按方向、来源及证据角色平衡抽样，不让先输入的平台挤掉其他材料。
3. 通过 `industry-packets.json` 阅读单方向材料；`evidence-index.json`/`research-followup.json` 的 review_queue 包含已入主包但未审阅、以及未入主包的条目。按 ID 与修订在完整 `evidence-context.json` 查原文。selected 不代表 reviewed，索引不代替原文核验。
4. 打开原始页面核验对标、用户行为和免费替代，经 `sources import` 导入并 `resume --evidence`；导入可写 `industry_ids`。
5. 在 assessment.evidence_reviews 记录精确 evidence_id/revision_id、status、reviewer、reviewed_at 和理由；形成正式候选或待验证线索后提交 assessment，交付内部报告与经白名单校验的 public 数据。completed 仅表示本轮交付完成，研究缺口保留在 research_quality。

相关性算法只是检索线索；只有宿主确实打开原文，才可写 `host_attested`。六方向都有材料不表示市场全面覆盖；未采集、无结果、检索失败、待核验、缺付款和个人不适配须分别说明。零正式候选时，`research_quality` 会保留覆盖缺口与遗漏数量，不推断“市场没有机会”。


## 子赛道地图与线索生命周期

| 方向 | 当前子赛道 |
|---|---|
| 电商 | 商品素材与上架、客服售后、店铺经营分析、投放与内容增长、跨境本地化、复购与私域运营 |
| 游戏 | 组队协调、攻略与练习、UGC 创作、社区运营、游戏发现、游戏理解与辅助体验 |
| 创作 | 视频、音频、摄影、图文研究、多平台运营、客户协作 |
| 成人学习 | 语言、职业技能、复习、阅读研究、讲师反馈、学习习惯 |
| 生活 | 照片回忆、家庭物品、衣橱穿搭、旅行、日常计划、兴趣创意 |
| 互联网迁移 | 收藏检索、推荐、社群运营、资讯订阅、服务匹配、非技术团队协作 |

任务目录提供研究广度，不要求每个方向产出固定数量，更不声称已覆盖全部市场。observed_need 需要与当前修订绑定的用户行为语义审阅；官网、价格和产品存在只能帮助提出假设。生命周期命令见 [研究工作流](research-workflow.md#7-状态与升级)。
