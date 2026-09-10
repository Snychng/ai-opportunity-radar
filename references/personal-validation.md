# 个人适配与验证

## 个人约束

复制 examples/founder-profile.json 到个人数据目录填写，不将私有能力、渠道和预算提交到仓库。null 表示未知。

- skills、languages：明确掌握的技能和支持的语言。
- reachable_channels：已确认实际可触达的渠道名称。
- weekly_hours：本周可用于验证的小时数。
- validation_budget_usd：验证预算美元上限。
- max_mvp_days：可接受的 MVP 工期。

候选使用现有 acquisition_channel 和 mvp_days，另补：

```json
{
  "validation_plan": {
    "required_skills": ["Python"],
    "required_languages": ["中文"],
    "hours": 5,
    "budget_usd": 20,
    "hypothesis": "商家愿意为一次准确回复付费",
    "success_criteria": "真实任务交付后接受报价",
    "stop_criteria": "无法取得真实任务"
  }
}
```

这些数值仅演示格式，不是预设的个人能力或成功门槛。

`python3 scripts/radar.py validation assess --profile PROFILE --input CANDIDATES --output RESULT`

CANDIDATES 支持对象、数组、candidates 包装或三层过滤结果（含 overflow）。按输入顺序选择一个主验证项目、最多两个备选；应先由研究证据与人工判断排序。

适配每项为 pass/unknown/conflict：有冲突为 park，有未知为 clarify，其余为 validate。技能/渠道做规范化的明确名称匹配，不作模糊能力推断。结果不生成个人总分，market_validated 不会因 A 级或适配通过而自动变为 true。

## 实验日志

examples/experiment.json 是 planned 示例，实际使用自己的稳定记录 ID：

评估与快速摘要可使用 CAND。记录实验前，从分层结果选择候选：A/B 用 `state prepare --kind opportunity`，R 用 `--kind signal`，将返回的 `id` 填入实验 `record_id`。`prepare` 不提交研究观察，计划实验无需先生成日报或运行真实采集。可直接执行的提取、准备、保存和读回示例见 [README](../README.md#为自己选择值得验证的项目)。

- experiment_id：EXP- 开头，后接字母、数字或连字符。
- record_id：OPP/SIG；run_id、as_of：本次观察运行与日期。
- status：planned/running/completed/stopped。
- hypothesis、offer、success_criteria、stop_criteria：假设、交付样例、预先定义的成功与停止条件。
- counts：contacted、interviewed、real_tasks、accepted_quotes、paid_trials。每项为非负整数，未统计可省略。
- cost_usd、minutes_spent：非负有限值，未知可省略。
- evidence：每条必须有 url 或 local_ref，以及 original_text 或 fact。local_ref 只保存引用，不读取或上传对应私有文件。
- completed/stopped 还需 outcome、decision（continue/pause/abandon）和 next_action；有客户行为必须保留有效证据。

`python3 scripts/radar.py validation record-experiment --home DATA_HOME --input EXPERIMENT_JSON`

`python3 scripts/radar.py validation experiments --home DATA_HOME --record-id OPP-20260910-ABCDEF --as-of 2026-09-10`

写入 state/experiment-events.jsonl，独立文件锁及原子替换。同 experiment_id + run_id 的同内容回放不重复，不同内容明确冲突；新观察使用新合法运行 ID。每次 counts 为观察快照，不把多次快照直接相加为人数。

实验不会自动联系客户、扣费、修改机会等级或批准立项。真实付款仍需按候选契约回填核验。
