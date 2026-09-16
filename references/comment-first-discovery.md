# 评论优先的产品研究

4.3 引入产品评论 → 用户观察 OBS → 需求簇 NEED → LEAD → A/B；4.4 同时支持[任务优先发现](task-first-discovery.md)。OBS/NEED 不要求已知产品、收费对标、AI 方案或 30 天 MVP；后续商业候选保留既有门槛。程序核验原文绑定，宿主负责语义与产品关联，不自动证明身份、购买、退款或市场成立。

## 1. 按产品扩展查询

```sh
aor research --products-file products.json
```

products.json 示例（名称为虚构示例，实际研究替换为真实产品）：

```json
{
  "products": [{
    "name": "ExampleVideo",
    "aliases": ["示例视频"],
    "task": "制作商品短视频",
    "industry_ids": ["content_creation", "ecommerce"],
    "sources": ["xiaohongshu", "douyin", "twitter"]
  }]
}
```

每轮 1–10 个产品，每产品最多 2 个别名；三平台分别生成体验、持续使用、替代/退款查询，去重后最多 100 个意图。X 使用英文搜索角度，产品别名仍按输入保留；任务及品牌的地域歧义须由宿主核验。`--products-file` 仅用于新研究，不能同时传 `--intent-plan-file`。生成计划不代表已经采集。

付费发现仍通过 `resume RUN_ID --discover --max-cost-usd AMOUNT --max-discovery-requests COUNT` 执行；该上限是整轮累计授权，含后续评论，金额与请求数由本次用户授权决定。付费调用前实时估价和账户预检，无授权预算时继续免费社区与已授权网页访问。HN/GitHub 评论在新研究中默认开启，可 `--no-include-comments` 关闭。

## 2. 选帖与有界分页

先阅读搜索结果，核验确实讨论目标产品，涵盖持续使用、好评、差评、求替代与流失，不仅挑热门或负面帖子。从 comment_candidates 提取真实标识；不能以搜索摘要冒充评论原文。

```json
{
  "selections": [{
    "source": "xiaohongshu",
    "selected_item_id": "xiaohongshu:REPLACE_NOTE_ID",
    "identifiers": {"note_id": "REPLACE_NOTE_ID"},
    "content_type": "image_note",
    "parent_url": "https://www.xiaohongshu.com/explore/REPLACE_NOTE_ID",
    "selection_reason": "正文提到连续使用该产品，评论中有导出体验讨论",
    "product": "ExampleVideo",
    "industry_ids": ["content_creation"]
  }],
  "policy": {
    "max_pages": 3,
    "max_reply_pages": 2,
    "max_reply_threads": 5,
    "max_comments_per_post": 100,
    "max_requests": 150
  }
}
```

这是输入模板，必须替换帖子和产品，不能拿占位符联网。小红书 content_type 为 image_note/video_note；抖音 identifiers 用 aweme_id；X 用 tweet_id。selected_item_id 使用搜索归一化产物中的原始标识，保持同一父帖身份。

```sh
aor resume RUN_ID --comments-file comments.json --max-cost-usd AMOUNT --batch-id product-comments
# 相同输入中断恢复；若预算不足，只有用户明确提高累计授权后才增加 AMOUNT。
aor resume RUN_ID --comments-file comments.json --max-cost-usd AMOUNT --batch-id product-comments --resume-batch
```

每个集合选择 1–30 个帖子；小红书、抖音读取详情、一级评论以及有回复计数的线程，按返回游标继续；X 读取详情、相关回复和最新回复，沿 Bottom 游标继续。X 的折叠 Thread 游标、不可见或受限回复不保证展开。跨页与排序按原生评论 ID 去重；缺少 ID 时使用父帖、父回复与文本摘要，不能把这当作独立用户数。

| 容量项 | 默认 | 允许上限 | 含义 |
|---|---:|---:|---|
| max_pages | 3 | 20 | 每帖子、每排序的一级评论页数 |
| max_reply_pages | 2 | 10 | 每个子回复线程的页数 |
| max_reply_threads | 5 | 30 | 每帖最多选择的子回复线程 |
| max_comments_per_post | 100 | 2000 | 达到去重评论数后停止继续规划；按页停止，同批已安排页面可能超过目标 |
| max_requests | 150 | 1000 | 整个集合最多处理请求数，包含详情、分页和子回复 |

每轮最多执行 100 个请求，更多待执行请求保存在队列。所有页面继续使用同轮 paid-journal 的累计预算、实时端点白名单和价格校验；输入中不能加入令牌或私有请求头。首批计划超容量拒绝执行，不静默砍掉选帖。失败/未知页面不自动重买。修改帖子、产品或 policy 必须使用新 batch-id，不能改已有状态文件。

`inspect RUN_ID` 返回 `comment-collection-*.json` 产物；其中 pages 包括帖子、产品、页码、排序、父评论、游标、供方 has_more/total、去重增量和 stop_reason。常见停止原因包括 provider_end、page_limit、comment_limit、repeated_cursor、pagination_unknown、empty_or_unrecognized_page、upstream_error、outcome_unknown；deferred_request_count 表示因请求容量未继续的请求。completed 仅表示本集合队列已结束，始终 `exhaustive=false`，不代表抓到了全部评论。报告的评论覆盖从实际响应重新计算，不用计划或历史资料填充本轮产出。

## 3. 原文、观察与需求簇

对原始评论做宿主语义审阅后，可先用 `resume RUN_ID --observations-file FILE` 保存观察快照并生成后续查询，保持 awaiting_benchmarks；也可通过 benchmarks 输入的 observations 提交。原有 benchmarks/leads 仍兼容；需要完成报告时继续 benchmarks/assessment 流程。

```json
{
  "observations": [{
    "product": "ExampleVideo",
    "target_user": "制作商品视频的个人卖家",
    "task": "导出竖屏视频",
    "need": "在手机上快速导出字幕完整的视频",
    "industry_ids": ["ecommerce", "content_creation"],
    "feedback_type": "usage",
    "sentiment": "negative",
    "evidence_refs": [{
      "evidence_id": "从 evidence-context 复制真实 ID",
      "revision_id": "复制同一对象的确切修订 ID",
      "quote": "从该修订逐字摘录的原话"
    }]
  }]
}
```

2.0 每条观察需要 target_user、task、need、industry_ids 和 1–20 个固定修订引用，product 可省略，products 可保留多个产品上下文，单轮最多 2000 条；quote 必须在可见日期内的该修订原文中准确定位。当前撤回、失效或修订变化须重新复核，不能用旧原话维持当前结论。新增 trigger/current_workaround/desired_outcome/artifact/constraints 与正向行为字段，详见[完整任务契约](task-first-discovery.md)。

feedback_type 允许 usage、purchase_claim、refund_claim、recommendation_request、positive_behavior、promotion、official_response、suspected_spam、unknown；sentiment 为 positive/negative/mixed/neutral/unknown。不能凭关键词自动将帖子标为真实付费用户。前五类的非演示观察可进入需求簇，其他材料仍保留在观察层。

2.0 以规范化的人群、任务、明确约束组成 TASK，再加需求形成 NEED，产品只作为上下文；已保存的 1.0 数据仍按原产品身份规则核验，不重算旧报告。不同表达是否是同一需求由宿主规范化，不做无依据的自动语义归并。同一原文可以支持多个不同需求；观察数不等于评论数或人数。OBS 由需求及固定引用生成，重复输入幂等，冲突分类要求合并。需求簇固定 needs_verification、market_validated=false、independent_user_count=null。solution_hypotheses 只能为 hypothesis，不增加需求数或已验证商业事实。

报告保留原话、证据 ID/修订和归组，完整私有结构写入 `state/user-discovery/RUN_ID.json`。公开 `extensions.user_discovery` 只导出统计，不导出未发布复核的评论、用户或需求正文。等收费对标、AI 增量和验证路径明确后，再进入 LEAD/A/B。

## 验收边界

发布测试覆盖合成的小红书、抖音、X 响应、分页/子回复、去重、未知响应、预算暂停、恢复不重买、原文绑定和观察独立交付。供应商字段依据公开接口文档适配；正式使用时仍需按实际授权预算抽样核验三平台在线响应、原文/父帖关系和解析率。代码与离线测试通过不构成实时覆盖或评论真实性证明。
