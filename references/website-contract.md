# 网站数据契约与接入

产品版本 4.2.0；内部 `report_version=1.1`，公开 `contract_version=1.0.0`，分别维护。网站只读取公开导出，内部 `report.json` 含采集响应和本机路径，不应直接托管。

`coverage[].material_count/reviewed_count` 保持本轮新增材料口径；可选 `historical_material_count` 显示复用的历史材料，不将离线复核伪装成重新联网采集。全站当前材料复核总数读取 `quality.review_summary`。示例消费页见 [website-consumer](../examples/website-consumer/README.md)。

- [唯一字段定义：JSON Schema](../schemas/public-v1.schema.json)
- [由 Schema 生成的 TypeScript 类型](../types/public-v1.ts)
- [导出入口](../scripts/export_public.py)
- [运行时校验器](../src/aor/reporting/public_contract.py)

运行时校验器实现本 Schema 实际使用的关键字，不是任意 JSON Schema 的通用实现。新增 Schema 约束时同步验证器与回归；网站也可使用支持 Draft 2020-12 的校验器。

## 单次研究与全站目录

```bash
export AOR_OFFLINE=1
aor export /absolute/run/report.json --output /absolute/site/current
aor export --validate /absolute/site/current/public.v1.json
aor export --types /absolute/frontend/public-v1.ts
aor export --site-report /absolute/run-one/report.json \
  --site-report /absolute/run-two/report.json --output /absolute/site/current \
  --as-of 2026-10-15 --review-period-days 30 --due-soon-days 7
```

单次研究 `snapshot_scope=research_run`，只包含该次研究条目。全站目录 `snapshot_scope=site_catalog` 按研究日期、生成时间和运行 ID 稳定排序，以业务 ID 更新：新运行未提到的旧项保留；出现但被隔离的旧项撤回；显式 `withdrawn` 撤回。最新允许的行业范围决定站点范围。`source_runs` 与条目来源字段保留溯源。旧版 1.0 报告须通过新研究修订后才能加入。聚合命令不会自行启用定时任务、外部发布或持续付费。

| 文件 | `view` | 用途 |
|---|---|---|
| `public.v1.json` | `full` | 全量快照，用于入库与一致性校验 |
| `index.json` | `index` | 列表、行业导航，不含长引用 |
| `items/<稳定ID>.json` | `detail` | 单条详情及必要证据 |

三视图共用 Schema，以 `view` 判别。前端从 `industries` 渲染行业和子赛道，不将行业硬编码为不同页面。内容 `id` 用作收藏、URL 和更新的业务键，`revision_id` 表示修订；不要用标题、数组位置或当日排序作主键。修改线索时保留原 `lead_id`，晋级关联使用 `origin_lead_id`／`promoted_to`。

| 内容字段 | 含义 |
|---|---|
| `stage=hypothesis` | 仍待核验的产品假设 |
| `stage=observed_need` | 有绑定原文修订的用户行为审阅 |
| `stage=candidate`、`evidence_tier=A/B` | 达到对应研究门槛的正式候选 |
| `stage=regional_hypothesis`、`evidence_tier=R` | 地区迁移假设 |
| `ai_value.status` | AI 增量是推测、已有支持、被否定或未知 |
| `publication_status` | 展示或撤回，与商业验证分开 |
| `missing_evidence`、`next_action` | 还需要核验什么 |
| `coverage`、`quality` | 调查及审阅缺口，不宣称穷尽市场 |
| `quality.review_summary` | 可选的全量有效材料审阅汇总，含历史复用；`source_run_id` 指明统计来自哪轮研究 |
| `freshness` | 可选的复核期限及三类日期；缺失表示旧版本未提供，不默认视为新鲜 |

`market_validated` 保持 `false`。发布复核确认内容和引用可以按当前研究阶段公开，不代表用户付费或产品立项。

## 复核期限与刷新清单

`--as-of` 是本次网站快照和复核期限的计算日，不改变来源报告。省略时库函数使用最新报告日期；不能早于任何输入报告。`source_runs[].as_of` 仍是各轮研究原来的日期。全站未再次提及的旧项继续保留，以新的计算日标记是否过期。时间推进和复核状态改变会更新公开 `revision_id`，业务 `id` 不变，网站应按公开修订更新缓存。

`Item.freshness` 和列表项的同名字段保持一致：

| 字段 | 含义 |
|---|---|
| `as_of` | 本次计算日期 |
| `source_observed_at` | 全部必要引用中最早的有效来源观察日；对应真实采集或保存原文的观察，导出日期不代替观察 |
| `semantic_reviewed_at` | 全部必要引用中最早的、绑定当前证据修订的语义复核日 |
| `publication_checked_at` | 该条内容最近一次发布批准日；不用于延长原始证据的核验期限 |
| `last_verified_at` | 上述来源观察日与语义复核日两者较早值；任一必要引用缺日期则为 `null` |
| `review_due_at` | `last_verified_at + review_period_days`；无法确定则为 `null` |
| `status` | `fresh` 尚未进入提醒期；`due` 已进入提醒期且未超过到期日；`overdue` 已过到期日；`unknown` 缺少完整核验日期 |

`freshness_policy` 保存计算参数：默认周期 30 天，提前 7 天提醒。周期范围 1–3650 天，提醒天数须非负且小于周期。Evidence 的可选 `source_observed_at`、`semantic_reviewed_at` 提供日期依据。网站可用 `freshness.as_of - last_verified_at` 显示“距上次完整核验 N 天”；缺日期显示“核验日期不完整”。同版本的旧公开数据没有这些可选字段仍可读取。

过期只表示需要再次核验，**不等于来源撤回，不自动删除条目，也不恢复已归档切口**。后续运行仅重新批准发布，不会把旧资料改为新鲜；只重新采集但未做语义复核，也不能延长完整核验期限。后续报告可以单独提供同一证据修订的新观察或新语义复核，不必重复提交历史条目。

导出结果的私有 `refresh_tasks` 数组给出 `item_id`、执行状态 `not_started`、原因、固定 `evidence_id/revision_id`、公开原始 URL、步骤和预期产物。它供宿主逐条重新采集、写入 `evidence_reviews`，必要时在新研究运行修订结论并重新审批。生成清单不会访问网络或自动付费；清单仅通过 CLI 返回值或调用方的私有文件保存，不写入公开目录。归档与已撤回项不自动加入此清单。

## 审阅与版本

`assessment.evidence_reviews` 先对 `evidence_id`、`revision_id` 填写相关性、角色、审阅者、时间和理由，报告写入绑定修订的 `relevance_review`。`assessment.publication_reviews` 按 LEAD/OPP/SIG ID 提交发布复核：`status=approved`、`reviewer`、`reviewed_at`、`rationale`、`content_sha256`。哈希由 `review_content_hash()` 生成。A 级先完成评分再复核最终内容；宿主可用纯函数 `_apply_assessment(tiered, assessment, context)` 取得最终内容后填写复核。引用或内容改变后重新审阅。

AI 增量标为 `supported` 必须附可解析的原文引用；官网不代替用户行为。身份歧义、原文错配、越界行业和无效引用先修正，不手改状态、计数或版本绕过。导出使用白名单并过滤联系方式、临时链接及内部字段；网站必须用 `textContent` 或框架默认转义渲染纯文本，不将它作为 HTML 插入。

`aor leads list --date YYYY-MM-DD` 与 `transition` 读取截止日全库的当前对象状态，与公开导出使用同一引用判别：正文修订变化、撤回、解析替代、身份歧义或待复核均会提示 `reference_status=needs_review`。读取不会改写历史记录，也不会把 `archived`／`disproven` 自动恢复成活跃线索。引用失效时不能只改变生命周期状态重新发布；须先在新研究运行修订并核验引用。

兼容扩展通过目录和新增字段完成。生产端严格校验；消费端 `--consumer` 容忍同主版本的未知字段，但仍检查已知类型、范围与引用。字段改名、收紧必填或改变语义应发布新主版本并迁移，不能承诺任意未来变更无需适配。

## 原子发布与恢复

`--output` 是受管当前目录。程序在相邻隐藏版本目录中生成并验证完整快照，再原子切换目录链接；失败保留前一代。首次用新路径，非空普通目录拒绝覆盖。

**只托管 `--output` 指向的目录内容，不托管父目录。** 相邻隐藏目录保存本地恢复版本，可能含已撤回旧详情，不属于当前站点。部署工具不跟随链接时，复制链接指向的完整目录到新部署版本，再切换站点。接数据库的网站还须消费撤回事件，不能只新增。

内部隔离原因仅保存在 CLI 返回值或运行 `publication.json`，不写进公开目录。契约、类型、三视图、跨运行聚合、撤回与中断均有离线回归；业务审阅仍需核验真实原文和竞品。
