# 评分契约

## 1. 先选轨道，再评分

每个候选必须先填写 `track`：

- `needle`：针尖型机会，强调需求、30 天 MVP、商业化和证据。
- `new_form`：老产品新形态，强调形态变化、需求和传播。
- `regional_gap`：区域错配型机会，强调本地语言/渠道缺口、需求和商业化。

七项主评分均为 0 到 10。三条轨道分别使用以下权重，合计均为 10，因此满分都是 100：

| JSON 字段 | `needle` | `new_form` | `regional_gap` |
|---|---:|---:|---:|
| `demand` | 2.5 | 2.0 | 2.0 |
| `new_form` | 0.5 | 2.5 | 0.5 |
| `distribution` | 1.5 | 1.5 | 1.5 |
| `regional_gap` | 0.5 | 0.5 | 2.5 |
| `monetization` | 1.5 | 1.0 | 1.5 |
| `mvp_feasibility` | 2.0 | 1.5 | 1.0 |
| `evidence` | 1.5 | 1.0 | 1.0 |

不要因总分低就删除候选。75 分以上为“推荐”，60 到 74.9 为“重点观察”，其余保留为“早期信号”。跨轨道排名可以比较总分，但必须同时展示轨道，避免把不同类型的评分重点隐藏掉。

## 2. 辅助评分与证据约束

辅助评分不进入总分，分别填写 0 到 10：

- `first_revenue`：30 天内获得首笔真实收入
- `scale`：发展成订阅、平台或网络效应产品
- `personal_influence`：成为高质量开源项目或代表作
- `confidence`：证据置信度

单一独立来源的 `confidence` 不得高于 4。脚本会根据 `evidence` 自动计算独立来源数量并执行上限；来源多但都转载同一事件时，人工判断仍应按单来源处理。

## 3. 输入顺序和 JSON

评分前必须先运行 `manage_state.py prepare`，因此输入中已经有稳定机会 ID：

```json
{
  "id": "OPP-20260714-A1B2C3",
  "title": "机会名称",
  "track": "regional_gap",
  "target_user": "具体用户",
  "context": "触发场景",
  "problem_or_desire": "具体问题或欲望",
  "wedge": "最小产品切入口",
  "scores": {
    "demand": 8,
    "new_form": 7,
    "distribution": 7,
    "regional_gap": 9,
    "monetization": 7,
    "mvp_feasibility": 8,
    "evidence": 6
  },
  "auxiliary_scores": {
    "first_revenue": 7,
    "scale": 8,
    "personal_influence": 6,
    "confidence": 6
  },
  "risks": ["平台政策", "冷启动"],
  "evidence": [
    {"source": "github", "container": "owner/repo", "url": "https://..."},
    {"source": "hackernews", "container": "Hacker News", "url": "https://..."}
  ]
}
```

脚本写入 `scoring_version=2.0`、实际轨道权重、独立来源数和是否触发置信度上限。以后修改权重时不得静默覆盖旧观察事件中的分数。

## 4. 评分锚点

- 0 到 2：几乎没有相关证据或明显不成立
- 3 到 4：弱信号，适合观察
- 5 到 6：有合理迹象，但关键问题未验证
- 7 到 8：多项证据支持，值得主动验证
- 9 到 10：非常强且直接的证据；谨慎使用满分

把合规、平台政策、数据、内容审核、巨头复制和一人运营作为独立风险标签，不偷偷混入总分。
