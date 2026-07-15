# V3 数据契约与阶段连接

## 1. 共享运行契约

- `schema_version`、`query_plan_version`、`scoring_version`：当前为 `3.0`。
- `run_id`：`RUN-YYYYMMDD-XXXXXXXXXX`，由日期、模式、定向范围和版本确定。
- `as_of`：北京时间 `YYYY-MM-DD`，必须与 `run_id` 日期一致。
- 所有原始、规范化、扩展、过滤、评分和状态文件都保留同一 `run_id`。

阶段顺序：

```text
query_plan
  -> community_normalized + tikhub_normalized
  -> paid_benchmarks
  -> expanded_candidates
  -> tiered_candidates
  -> OPP/SIG stable IDs
  -> A-level scoring
  -> validated_report
  -> state_observations
```

## 2. 付费对标

最小结构：

```json
{
  "id": "BENCH-A1B2C3D4",
  "product": "已有产品或服务",
  "source_market": "美国",
  "payer": "独立站商家",
  "price": "每月 49 美元",
  "payment_signals": [
    {"type": "subscription", "region": "美国", "url": "https://..."}
  ],
  "current_alternative": "人工客服",
  "product_gap": "价格高且不支持印尼语",
  "acquisition_channel": "Shopify 商家社区",
  "mvp_days": 21,
  "mvp_scope": "导入 FAQ 并生成一次回复",
  "evidence": []
}
```

`BENCH` ID 根据产品、来源市场、付款者和价格生成；相同对标重跑保持稳定。

## 3. 扩展输入与输出

`expand_ideas.py` 输入：

```json
{
  "run_id": "RUN-...",
  "as_of": "YYYY-MM-DD",
  "benchmarks": [],
  "dimensions": {
    "segments": [],
    "triggers": [],
    "forms": [],
    "regions": [],
    "channels": [],
    "offers": []
  }
}
```

六个维度都是可选非空数组；缺失时使用对标自身值。脚本按固定顺序做有限笛卡尔扩展、稳定去重，并生成 `CAND-XXXXXXXXXX`。

扩展候选必须携带：

- `benchmark_ids`
- `payer`、`buying_trigger`
- `current_alternative`、`current_spend`
- `payment_signals`、`demand_signals`
- `product_gap`、`acquisition_channel`、`delivery_model`
- `mvp_days`、`mvp_scope`
- `source_region`、`target_region`、`localization_gap`、`transfer_reason`
- `market_scope.country/region/language/primary_channel`

## 4. 过滤输出

`filter_ideas.py` 输出四个数组：

- `deep_candidates`：A 级，`record_kind=opportunity`
- `validated_ideas`：B 级，`record_kind=opportunity`
- `regional_signals`：R 级，`record_kind=signal`
- `rejected`：附 `rejection_reasons`
- `overflow`：超过 B 级 40 条或 R 级 80 条的合格候选；不丢弃，但不进入当日日报主卡片

每条保留 `hard_gates`。R 级还必须保留：

- `missing_proof`
- `promotion_triggers`
- `source_region`、`target_region`
- `localization_gap`、`transfer_reason`

## 5. 稳定 OPP/SIG 身份

格式：

- `OPP-YYYYMMDD-XXXXXX`
- `SIG-YYYYMMDD-XXXXXX`

指纹字段：

1. `target_user`
2. `context`
3. `problem_or_desire`
4. `wedge`
5. 可选 `market_scope.country`
6. 可选 `market_scope.region`
7. 可选 `market_scope.primary_channel`

标题、翻译、总分和证据增删不改变 ID。国家或主渠道真正不同时应生成不同 ID。旧记录没有 `market_scope` 时继续使用四字段身份，保持兼容。

必须先执行 `manage_state.py prepare` 分配 ID，再写报告和评分。不要手工编造 ID。

## 6. 状态与升级

当前视图：

- `state/opportunities.jsonl`
- `state/signals.jsonl`

追加历史：

- `state/opportunity-observations.jsonl`
- `state/signal-observations.jsonl`

同一 `run_id + kind + fingerprint` 重放不重复写事件；`occurrences` 按唯一日期计数。

R 级补齐本地直接付款和独立来源后，通过 `manage_state.py promote` 升级：

```text
SIG.promoted_to -> OPP ID
OPP.promoted_from -> SIG ID
```

升级在同一文件锁内写入两侧当前视图与观察事件。已升级 SIG 重放返回原 `OPP`。

## 7. 兼容与失败策略

- 阶段版本不一致时失败关闭，不静默混用。
- 缺六项硬门槛时写拒绝原因，不猜测补齐。
- 报告校验失败时不写机会状态。
- 状态文件损坏时明确报错，不自动覆盖。
- 运行数据永远写入 `RADAR_HOME`，不写入 Skill 目录。
