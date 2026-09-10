# 研究引擎改造约定

本轮保持 Python 3.10+、标准库运行时和已有命令可用。新增业务模块放在 src/aor；scripts 中的兼容脚本使用 import aor_bootstrap 加载。现有 schema_version 3.0 不整体迁移，新增字段向后兼容。

## 共享接口

- 阶段元数据继续使用 schema_version、run_id、as_of；低层既有 API 保持默认行为兼容。
- 原始证据保留现有 id/source/url/original_text/published_at/observed_at 等字段，新的质量、引用与主张字段作为补充。
- 确定性代码负责请求、预算、规范化、分层、保存和报告；宿主 Agent 通过文件提供研究计划、BENCH 与商业主张，无需新模型 API。
- 网络读取与用户授权沿用当前边界。开发与验收默认离线，不访问私有客户数据，不调用付费来源，不发布版本。
- 新研究编排负责 run manifest 和显式阶段进度；请求日志与状态历史分别保存。已有 state prepare/record-batch 接口兼容。
- 每个工作流阶段保存输入路径/摘要、产物路径和 next_action；可用既有产物恢复。
- 研究质量与机会评分分离；热度不改变 A/B/R 的付款和独立来源门槛。

## 并行所有权

1. 查询与信源：build_query_plan.py、src/aor/sources、query 相关新模块、对应 tests。
2. 采集质量：community_query.py、normalize_tikhub_results.py、src/aor/evidence/quality.py、公共网络/语言模块及对应 tests/evals；含免费社区评论深挖。
3. 付费恢复：tikhub_query.py、src/aor/storage/request_journal.py 及对应 tests。
4. 证据知识库：contracts.py、expand_ideas.py、score_candidates.py、src/aor/evidence 中除 quality.py 外模块、src/aor/storage 中除 request_journal.py 外模块、opportunity 模块、对应 tests。
5. 集成主任务：radar.py、aor_status.py、manage_state.py、build_result_digest.py、validate_report.py、workflow/reporting 模块、整体文档、发行集成和验收。

模块实现完提交独立 commit，返回公共函数签名、产物格式和验证结果。分支不修改 main，不推送或发布。跨模块需求先报告给集成任务，由集成任务统一衔接。
