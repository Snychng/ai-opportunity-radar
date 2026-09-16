# 查询与来源模块契约

模块只使用标准库。兼容入口先 `import aor_bootstrap`；沿用 scripts 中的 V3 契约和固定端点构建器，不访问网络，不加载凭证文件。

## 宿主意图

`build_query_plan.build_plan(as_of: date, home: Path, focus: str | None = None, *, scope: dict | None = None, intent_plan: dict | None = None) -> dict`

CLI 新增 `--intent-plan-file FILE`。未提供时继续生成默认计划；提供后替换本次检索计划，不偷偷增加默认付费来源。scope/focus 仍作为研究上下文。run_id 包含结构化意图内容。

最小单源意图（每个字段必填，candidate_gaps 可空；最多 100 个意图，社区合并后最多 12 个请求）：

```json
{
  "intents": [{
    "id": "support-cost",
    "question": "商家为什么仍为订单客服投入人工？",
    "evidence_type": "workflow_pain",
    "search_query": "order support manual workflow",
    "ranking_query": "优先包含具体人工支出、使用中替代方案和缺失能力的叙述",
    "source": "hackernews",
    "locale": {"country": "JP", "language": "en"},
    "candidate_gaps": ["CAND-1:missing-spend"]
  }]
}
```

`evidence_type` 表示要找的证据，不是已经成立的证据。允许值：official_pricing、product_update、product_review、hiring、outsourcing、payment、workflow_pain、alternative、regional_gap、counter_evidence、usage_behavior、creative_output、learning_progress、social_sharing。

`aor.sources.planning.validate_intent_plan(plan: Any) -> dict` 验证并规范化契约。

`compile_intents(intent_plan: dict, *, as_of: str, run_id: str) -> dict` 返回 community、tikhub、web_import 三个子计划。前两者保留既有 provider/stage/window/scope/requests 契约，web_import.required_imports 只包含人工核验待办。

HN/GitHub 必须有英语 search_query 和 en locale；未翻译的中文主题不会变成“中文 + manual”。默认定向计划可用 scope.queries 中的英语查询，否则 community.requests 为空，plan_status 为 needs_host_queries，required_queries 记录缺失内容。宿主仅请求其他来源时空子计划为 not_requested。执行调用方需按这些状态跳过空计划；空计划不是联网成功。

每个实际请求增加：

- request_fingerprint：source、endpoint、method、params 的规范 JSON SHA-256。
- intent_refs：全部意图 ID。
- provenance：完整结构化意图；默认查询保存 intent_id/search_query/ranking_query/locale。
- request_aliases：合并前各请求 ID，保留首个 ID 作为执行 ID。

`deduplicate_requests(requests: list[dict]) -> list[dict]` 不修改输入，可重复调用；不同参数、方法、来源、端点保留独立请求。同请求的多个排序问题留在 provenance，不增加费用。付费执行器仍需对真实发出的请求再次去重。

## 来源目录与诊断

`source_catalog() -> list[dict]` 分离 source/platform、provider、capabilities、cost、preferred_languages、regions、region_filter、default_enabled、manual_import_only。语言与地区字段描述路由倾向或接口过滤能力，不证明本次证据来自当地。默认自动边界仍为 12 个 TikHub 平台及 HN/GitHub。

`diagnose_sources(configured: Mapping[str, bool] | None = None) -> dict` 仅接收配置存在的布尔标记；返回 configuration_status 和 live_health=not_checked。不会调用网络、读取凭证文件或把 key presence 说成 live health。

```sh
python3 scripts/source_query.py catalog
python3 scripts/source_query.py diagnose
```

统一 CLI 集成路由为 `aor sources catalog/diagnose/import`。此脚本不使用会访问更新服务器的 run_legacy 预检。

## 宿主网页证据导入

`import_web_evidence(payload: Any, *, run_id: str, as_of: str) -> dict`

输入 `{items: [...]}`，每批 1–100 条。必填：source、url、title、original_text、supporting_quote、evidence_role、observed_at、verification。可选：published_at、language、country、original_url、original_publisher、intent_refs、candidate_gaps、is_demo。

```json
{
  "items": [{
    "source": "web",
    "url": "https://example.com/pricing",
    "title": "示例定价页",
    "original_text": "Pro plan costs $29/month.",
    "supporting_quote": "Pro plan costs $29/month.",
    "evidence_role": "official_pricing",
    "observed_at": "2026-09-10T10:00:00+08:00",
    "verification": {
      "verified_by": "host-agent",
      "verified_at": "2026-09-10T10:00:00+08:00",
      "method": "opened_page"
    },
    "is_demo": true,
    "intent_refs": ["support-price"]
  }]
}
```

verification.method 只允许 opened_page/authorized_browser，不能把搜索摘要冒充已打开正文。supporting_quote 必须为 original_text 的原文子串。核验状态标为 host_attested，导入器不声称独立打开过网页。

输出保留 schema_version/run_id/as_of，provider=host-verified-web、stage=web_evidence_import、input_sha256、evidence[] 和 summary。证据 ID 从 original_url（缺失时 url）产生，content_sha256 单独表达内容修订。同批重复内容拒绝并要求合并 intent_refs。跨网页原始主体的独立性判定交给证据模块，不能按导入渠道增加独立来源。

定价页只写 signal_types=[pricing]，payment_status 始终为 not_established；即使 evidence_role=payment 也仍需证据模块核验付款主张，不会自动生成 paid_subscription 等已付款类型。演示标记原样保留。

```sh
python3 scripts/source_query.py import --input verified-pages.json --run-id RUN-20260910-0123456789 --as-of 2026-09-10 --output web-evidence.json
```

该操作只读写本地 JSON，不获取未知端点、不抓网页。常规网页归 source=web；已有平台的授权网页摘录可保留其 source，但 provider 仍为 host-verified-web。导入器不承诺 URL 规范化、来源独立性或真实付款，这些由证据层继续核验。


## 六方向计划与覆盖 2.0

`industries.json` 默认仅电商、游戏、创作、成人学习、生活、传统互联网 AI 改造；各 6 个子赛道，每个子赛道定义人群、job_to_be_done、existing_behavior_to_verify、spend_status、双语查询和人工来源。目录的支出描述是研究问题，不能计作付款证据。

`build_industry_discovery(..., history=None)` 保留原参数；可注入 `history={tasks:{(task_id, language):{attempts,last_attempt,review_gaps}}}`。未注入时只读 home/runs 最近 60 份 coverage 2.0 快照，排除当前 run；无实际请求不算历史完成。付费子计划增加 research_tasks、catalog_version 和 selection_history。每请求包含 task_id、subtrack_ids、research_priority 与未执行的 fallback_requests；后者只由已有 search 构建器生成同查询、同语言替补。

`prepare_discovery_plan` 在实时价格和累计预算内选择主请求或替补，返回 skipped_requests/substitutions；计划无 status 不计采集。执行结果及选中计划应连同这些原因进入报告覆盖输入。

`build_industry_coverage(plan,payloads,tiered,version='2.0')` 对象去重使用原生身份，payload 与 item 级 reused_for_run_id/run_ids 控制历史复用。coverage 包含逐行业和 tasks 维度的计划、实际请求、跳过、语义审阅、近期/历史/未知/不确定日期、用户行为、商业对标、替代与反证计数。保留 version='1.0' 仅供旧报告重算。`research_quality` 根据语义审阅与证据角色缺口判断，host_attested 官网不使方向自动完成。

透传覆盖所需字段：对象身份及 revision_id/content_hash、industry_ids/task_id/subtrack_ids、query_scope/provenance/query_metadata、evidence_role、published_at/published_at_interval、verification、relevance_review、relevance_status、retracted/is_demo、运行元数据。relevance_review 须绑定 evidence_id/revision_id（或当前 content_sha256），含 reviewer/reviewed_at 和 relevant/unrelated 状态。

来源角色描述不产生新 API 能力；appstore/googleplay/steam/shopify_app_store 等仍是宿主手动核验来源。默认辅助 HN 最多 3 请求，不能替代消费者来源，也不默认启用 GitHub。
