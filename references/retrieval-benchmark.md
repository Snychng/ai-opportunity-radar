# 真实检索质量基准

`aor eval` 的合成用例检查程序行为；`aor retrieval-eval` 统计真实缓存材料的语义标注，两者不能互相替代。评估不联网、不付费，不把 Agent 标注结果当作自动分类器的准确率。

```sh
aor retrieval-eval --evidence evidence-context.json --labels retrieval-labels.json \
  --as-of 2026-09-14 --output retrieval-benchmark.json
```

标注格式：

```json
{
  "schema_version": "retrieval-labels-1",
  "labels": [{
    "evidence_id": "EVID-...",
    "revision_id": "EVID-...:...",
    "query_match": "direct",
    "actor": "user",
    "signal": "task",
    "quote": "必须是该修订正文中的逐字片段",
    "rationale": "结合完整可用原文解释标注，指出正文缺失等限制",
    "reviewer": "审阅人或 Agent 标识",
    "reviewed_at": "2026-09-14"
  }]
}
```

三个维度分别判断：

- `query_match`：`direct / adjacent / unrelated / unknown`，相对保存的原始 query，不是相对宽泛行业。没有原始 query 时使用 unknown，不用事后新查询证明检索命中。
- `actor`：`user / supplier / editorial / unknown`。用户自己描述任务是 user；推销服务、产品或课程是 supplier；整理教程和行业资讯是 editorial。身份不足保留 unknown。
- `signal`：`task / payment / alternative / promotion / discussion / unknown`。payment 需正文明确本人付款或实际成交，标价和购买链接不算；alternative 需本人描述正在使用、替换或放弃的办法。

“直接用户信号”必须同时是 user、direct 和 task/payment/alternative。此计数既不是独立付款人数，也不是机会数。输出分别列出材料总数、已标注数、缺失数、未知数，以及按行业、来源和原始查询的统计；多行业分组可能重叠。供应商广告有供给研究价值，但不会增加直接用户需求计数。

每条标注绑定确切修订、该对象自己的原文引句和审阅日期；评论须使用自己的对象 ID，不能借父帖日期。正文变化后必须重标。相同对象多修订、冲突正文或统计元数据、重复标注及未来材料拒绝。日期比较与研究质量口径一致，按 UTC 自然日；近期用户信号另列，观察时间不代替发布日期。可选 `--estimated-cost-usd` 仅表示调用方给定的本材料集估算费用，不能代替账单或混入其他运行支出。

首次基准用于发现检索噪声、缺失正文和来源偏差。调整检索后应采集另一组查询或后续时间窗作独立验收；不要拿同一份标注同时训练和宣称泛化准确率。行业相关性仍由 evidence review 队列记录，不等同于这里的精确查询相关性。
