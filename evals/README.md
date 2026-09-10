# 采集质量离线基线

运行 `python3 scripts/evaluate_research.py --output /tmp/research-quality.json`。退出码 0 表示全部场景通过，1 表示行为回归。此入口不做版本联网预检，不读取凭证、不调用真实来源。fixtures 为合成案例，账号与域名均为虚构；基线包含固定场景结果、fixture SHA-256、评测/质量版本，不包含时钟和请求耗时，便于跨版本比较。

`research-quality.json` 覆盖 CJK 二元组、阿拉伯语、印地语、跨文字未知关联、无关热门内容、日期边界、同 URL 跟踪参数转载、同来源部分失败、旧 GitHub Issue 活动及归并后评论。相关自动测试还验证空宿主查询计划、评论默认关闭、评论请求失败和 TikHub 兼容计数。

社区执行与评论逻辑位于 `src/aor/community.py`，旧脚本保留计划构建、规范化适配器和 CLI。

## 公共接口与字段

- `aor.text.tokens(text: str) -> set[str]`：Unicode 词项及 CJK 二元组；`language(text, explicit=None) -> str` 保留明确语言标签，无法确定的语言返回 `unknown`。
- `aor.net.json_get(*, endpoint, params, headers, timeout) -> dict | list`：4 MiB 响应上限；`SameOriginRedirectHandler` 拒绝跨源和协议降级。调用者负责来源固定端点约束。兼容别名 `_SameOriginRedirectHandler` 可用，社区旧 `_default_transport` 仍可用。
- `aor.evidence.quality.assess_quality(item, *, query='', as_of, window=None) -> dict`：返回 `quality_version`、`local_relevance`、`matched_terms`、`relevance_status`、`relevance_reason`、`window_status`、`recent_evidence_eligible`。默认窗口为截止 `as_of` 的 30 个 UTC 自然日；自定义 window 使用 `range_from/range_to`。
- `community_query.build_community_plan(..., include_recent_activity=False)`：默认仅 created 窗口；显式开启后新增 GitHub updated 窗口。旧 Issue 保持原 `published_at`，另记 `updated_at/activity_window_status`。
- `community_query.execute_plan(plan, *, github_token='', timeout=20, max_items_per_request=20, transport=json_get, include_comments=False, max_comment_threads=4, max_comments_per_thread=10, concurrency=1)`：搜索并发上限 4、单请求超时 1–60 秒；最多 6 个评论主题、每主题最多 30 条。HN 固定 Algolia items 端点，GitHub 固定公开 Issue comments 端点并用 since 取窗口内活动；不跟随响应中的任意 API URL，不递归发请求、不自动分页。
- `normalize_tikhub_results.normalize_documents(documents, *, source_files=None, window=None)`：兼容旧 envelope，添加同一质量字段与 window，并把唯一 HTTP 结果的 request_ids/intent_refs/query_metadata 原样复制到帖子、详情、评论。

`relevant` 表示存在词面支持；`unrelated` 表示同文字体系无词面交集；`unknown` 表示查询/内容缺失、跨文字无法确认或评论需要父帖语境。未知保留供宿主核验。相关性不是语义模型，也不证明付款/独立买家；同语种同义词及同一文字体系的跨语言召回仍有限。

社区 `stats.valid_items` 只计归并后 **相关且窗口内** 的帖子，`valid_comments` 单计评论；`retained` 包括帖子和评论中的背景/未知。`requests` 每项记录 fetched（响应候选数量）、parsed（成功解析）、relevant（有词面关联）、retained（归并后保留）、valid_items（该请求收到的近期相关条目，跨请求归并前），另含 started_at/finished_at/duration_ms。请求 `ok` 表示收到近期相关结果，即使它因另一查询已保留而使 retained=0；总 valid_items 从最终独立帖子重新计算，不能直接累加请求的 valid_items。来源出现失败与其他状态混合，或 GitHub 声明 incomplete_results 时为 `partial`，请求原状态始终保留。

TikHub `stats.valid_items/valid_comments` 保持旧的可解析数量含义，新增 `recent_valid_items/recent_valid_comments` 表达严格近期相关计数；详情/评论无查询时关联为未知，不能因为日期明确就算近期有效。`window_status` 分 `in_window/out_of_window/unknown`，观察日期和 Issue 活动不能替代发布时间。已知同 URL 的跨源转载保留但标记 `duplicate_of`，不再计入近期独立证据；不靠短文本相似度猜测同源。

非英语主题缺少宿主查询时，显式 `plan_status=needs_host_queries` 且 `requests=[]` 可执行并返回 `status=needs_host_queries`、原 `required_queries`、来源 `skipped-policy`，不发请求、不报告健康。宿主只请求其他来源时，plan_status=not_requested 的空计划同样跳过并保留这个区别；未标明这两种状态的空计划仍非法。

所有字段仅描述采集质量，不改变商业 A/B/R。实时端点、真实网络限流、真实翻译/语义相关性不在本次离线验证证明范围。
