# V2 数据契约与阶段连接

## 1. 全局标识

- `schema_version`：当前固定为 `2.0`。
- `run_id`：`RUN-YYYYMMDD-XXXXXXXXXX`，由日期、运行模式、定向范围和计划版本确定。同一输入重跑得到同一 ID。
- 机会 ID：`OPP-YYYYMMDD-XXXXXX`；信号 ID：`SIG-YYYYMMDD-XXXXXX`。日期是首次观察日，末尾来自“目标用户 + 场景 + 问题或欲望 + 最小切入口”的稳定指纹。
- 标题、翻译和分数不得参与稳定 ID；四个身份字段变化时应视为新记录。

## 2. 阶段输入输出

| 阶段 | 输入 | 输出 | 必须保持的字段 |
|---|---|---|---|
| `query_plan` | 日期、偏好、可选定向范围 | 总计划、社区计划、TikHub 搜索计划 | `schema_version`、`run_id`、`as_of` |
| `community_discovery` | 社区计划 | `community_normalized` | `run_id`、来源状态、规范化 `evidence` |
| `search_discovery` | TikHub 搜索计划、实时价格、预算 | TikHub 搜索结果 | `run_id`、`stage`、请求状态 |
| `tikhub_normalized_search` | TikHub 搜索结果 | 规范化证据、`comment_candidates` | `run_id`、来源标识、直达 URL |
| `comment_deep_dive` | 1–5 个候选及 `selection_reason` | 详情与一级评论结果 | 与搜索阶段相同的 `run_id`、`selected_item_id` |
| `tikhub_normalized_comments` | 评论结果 | 规范化评论和详情状态 | `parent_item_id`、来源状态 |
| `candidates_with_ids` | 聚类候选 | 已解析稳定 ID 的候选 | 四个身份字段、`id`、`fingerprint` |
| `scored_candidates` | 已有 ID 的候选 | V2 轨道评分 | `id`、`track`、`scoring_version` |
| `validated_report` | 评分候选与证据 | Markdown 日报 | 报告 ID 与候选 ID 完全一致 |
| `state_observations` | 校验通过的候选、`run_id` | 当前视图 + 追加式观察事件 | `event_id`、`observed_on`、分数与证据快照 |

任何阶段缺少或改变 `run_id` 都应失败关闭。不要用文件名、数组位置或日报序号代替稳定标识。

## 3. 重跑与历史语义

- 同一 `run_id + kind + fingerprint` 只产生一个观察事件，重复提交返回 `replayed`。
- 同一天不同运行可以记录新的评分或证据快照，但 `occurrences` 等于唯一 `seen_dates` 数量，不会因同日重跑膨胀。
- `opportunities.jsonl` 与 `signals.jsonl` 是当前视图；`*-observations.jsonl` 是不可覆盖的观察历史。
- 更新由同一文件锁保护；当前视图和历史分别原子写入。报告结构校验通过前不得提交状态。

## 4. 最小候选契约

候选进入评分前必须具有：

```json
{
  "id": "OPP-20260714-A1B2C3",
  "track": "needle",
  "target_user": "具体用户",
  "context": "明确触发场景",
  "problem_or_desire": "具体问题或欲望",
  "wedge": "第一版只替代的动作",
  "evidence": [{"source": "...", "url": "https://..."}],
  "scores": {},
  "auxiliary_scores": {}
}
```

证据数组不得为空。低证据候选可以进入早期观察池，但不能通过抬高 `confidence` 掩盖来源不足。
