# A 级深度候选评分契约

最后更新：2026-09-10。

## 1. 评分不是过滤器

先执行六项硬门槛和 A/B/R 分层。只有 A 级候选进入本评分；B 级直接形成快速卡片，R 级保持 `SIG`。

不要用高总分补偿：没有付款者、没有付费对标、没有获客渠道或 MVP 超过 30 天。

## 2. 三条轨道

- `needle`：针尖型，强调明确任务、30 天 MVP、商业化和证据。
- `new_form`：老产品新形态，强调用户行为变化、需求和传播。
- `regional_gap`：已获得本地付款证据的区域机会，强调语言/渠道缺口、需求与商业化。只有迁移逻辑、没有本地付款时仍是 R 级，不能评分。

七项主评分均为 0–10，权重合计为 10，满分 100：

| JSON 字段 | `needle` | `new_form` | `regional_gap` |
|---|---:|---:|---:|
| `demand` | 2.5 | 2.0 | 2.0 |
| `new_form` | 0.5 | 2.5 | 0.5 |
| `distribution` | 1.5 | 1.5 | 1.5 |
| `regional_gap` | 0.5 | 0.5 | 2.5 |
| `monetization` | 1.5 | 1.0 | 1.5 |
| `mvp_feasibility` | 2.0 | 1.5 | 1.0 |
| `evidence` | 1.5 | 1.0 | 1.0 |

75 分以上为“推荐”，60–74.9 为“重点观察”，其余为“早期信号”。这些标签只代表 A 级候选内部优先级。

## 3. 辅助评分

四项 0–10，不进入总分：

- `first_revenue`：30 天内获得首笔真实收入
- `scale`：订阅、平台或网络效应潜力
- `personal_influence`：开源项目或代表作价值
- `confidence`：证据置信度

单一独立来源直接拒绝评分，不能通过降低 `confidence` 绕过 A 级门槛。来源数按有效原始内容和发布主体归并，采集渠道名称不代表独立来源。所有主评分和辅助评分必须是有限的 0–10 数字，不接受布尔值。

## 4. 输入示例

评分前必须先用 `manage_state.py prepare` 分配 OPP。以下是完整的离线契约样例：金额、域名、事实均为虚构，专用于测试结构，不能作为真实研究结果。把 JSON 保存为临时文件后可用 `score_candidates.py --input /tmp/a-candidate.json` 检查结构；实际生产记录必须换成已核验事实，仓库演示候选仍保留 `is_demo: true` 并拒绝 A 级。

```json
{
  "id": "OPP-20260910-A1B2C3",
  "evidence_tier": "A",
  "benchmark_ids": ["BENCH-1234ABCD"],
  "payer": "美国独立站商家",
  "buying_trigger": "大促前客服量翻倍",
  "current_alternative": "人工客服与现有服务",
  "product_gap": "每天仍需人工重复回复常见问题",
  "acquisition_channel": "商家社区",
  "mvp_days": 21,
  "mvp_scope": "导入 FAQ 并生成一次回复",
  "source_region": "美国",
  "target_region": "美国",
  "track": "needle",
  "target_user": "美国独立站商家",
  "context": "大促前 FAQ 激增",
  "problem_or_desire": "人工客服成本过高",
  "wedge": "自动生成一条 FAQ 回复",
  "payment_signals": [
    {
      "type": "purchase",
      "region": "美国",
      "payer": "美国独立站商家",
      "url": "https://vendor.example/receipt",
      "fact": "美国商家已支付49美元购买客服服务"
    }
  ],
  "evidence": [
    {
      "source": "vendor",
      "url": "https://vendor.example/receipt",
      "fact": "美国商家已支付49美元购买客服服务"
    },
    {
      "source": "merchant-forum",
      "url": "https://merchant-forum.example/problem",
      "fact": "另一位商家表示每天仍需两小时重复回复问题"
    }
  ],
  "scores": {
    "demand": 8,
    "new_form": 7,
    "distribution": 8,
    "regional_gap": 7,
    "monetization": 8,
    "mvp_feasibility": 9,
    "evidence": 7
  },
  "auxiliary_scores": {
    "first_revenue": 8,
    "scale": 7,
    "personal_influence": 5,
    "confidence": 7
  }
}
```

输入不得丢弃过滤器输出的 `hypotheses`、`candidate_verifications` 和原始证据。脚本重新运行分层检查，写入 `scoring_version=3.0`、实际权重和 `independent_source_count`。`confidence_capped=false` 仅为兼容旧输出字段；单来源已经在入口被拒绝。

## 5. 评分锚点

- 0–2：几乎不成立
- 3–4：弱
- 5–6：合理但关键问题未验证
- 7–8：多项直接证据支持
- 9–10：非常强且直接，谨慎使用

合规、平台政策、数据获取、内容审核、巨头复制和一人运营压力作为独立风险标签，不偷偷混入总分。

市场分数不合并个人技能、时间或预算适配分；个人行动建议通过 [个人适配与验证](personal-validation.md) 单独给出。
