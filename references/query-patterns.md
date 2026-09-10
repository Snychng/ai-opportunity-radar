# 高信号查询模式

最后更新：2026-09-10。

## 1. 查询优先级

按证据价值排序：

1. 直接商业：付款、订单、收入、订阅、定价、合同、招聘、外包。
2. 购买摩擦：取消、退款、切换、太贵、套餐限制、缺少关键集成。
3. 替代行为：手工、表格、复制粘贴、自由职业者、内部自建、放弃任务。
4. 地区差异：语言、支付、主渠道、当地平台、法规与工作流。
5. 泛讨论与愿望：只作发现线索，不能独立进入 OPP。

每条查询只负责一个意图。组合“具体人群 + 任务/触发 + 商业行为”，避免 `AI startup ideas` 之类宽查询。

## 2. 付费对标查询

英语：

```text
[category] pricing subscription annual plan
[product] revenue customers paid users
[task] hiring freelancer agency outsource
[product] cancelled refund switched alternative
[workflow] costs per month manual labor
site:reddit.com [product] paying too much
```

中文：

```text
[品类] 价格 订阅 套餐 付费
[产品] 营收 客户 续费 订单
[任务] 招聘 外包 服务商 报价
[产品] 取消订阅 退款 换工具 替代
[流程] 每月成本 人工处理
```

产品官网定价是弱付费证明；收入、客户数、采购、真实购买、续费和招聘是强证明。对关键数字必须打开原始页面核验。

## 3. 需求与替代查询

英语：

```text
"still doing this manually" / "takes hours every week"
"I use a spreadsheet" / "copy and paste"
"too expensive" / "looking for an alternative"
"missing integration" / "doesn't support"
"cancelled" / "switched from" / "hired someone"
"would pay" / "budget approved"
```

中文：

```text
只能手工处理 / 每周花几个小时
一直用表格凑合 / 复制粘贴
太贵了 / 有什么替代 / 换了工具
不支持中文 / 不支持国内平台
取消订阅 / 找外包 / 招人处理
愿意付费 / 已经买了 / 每月预算
```

“愿意付费”弱于真实购买；与现有支出、替代行为或招聘同时出现时才提升权重。

## 4. 六轴扩展查询

对每个 `BENCH` 分别搜索：

- 细分人群：`[product] for [narrow segment] complaints`
- 购买触发：`[event] need [task] urgently`
- AI 新形态：`[workflow] agent voice camera embedded automation`
- 地区与语言：`[product/task] [country] [local language] pricing`
- 渠道嵌入：`[task] WhatsApp/WeChat/Shopify/Chrome/Excel`
- 价格与交付：`cheaper pay per use managed service alternative`

一次查询不要塞入全部六轴。先验证对标和需求，再验证变体。

## 5. 地区与本地语言

区域创意不是深度分析主线，但需要大量、清晰、可追踪的迁移假设。每日英语和中文固定覆盖，再轮换：

- 东南亚：印尼语、越南语、泰语、菲律宾语、马来语
- 南亚：印地语、乌尔都语、孟加拉语、英语
- 非洲：英语、法语、葡萄牙语、斯瓦希里语、阿拉伯语
- 中东：阿拉伯语、土耳其语、英语
- 拉美：西班牙语、葡萄牙语

本地查询至少加入一个实际行为：当地支付、主要聊天工具、行业平台、外包方式或价格表达。使用自然本地表达并回译检查，不机械翻译英语关键词。

只查到来源市场付费、未查到目标市场直接付款时，输出 R/SIG，并明确缺失证据。

## 6. 反证查询

对拟进入 A 级的候选至少做两类：

```text
[category] failed startup shut down discontinued
[competitor] complaints refund pricing alternatives
[task] free workaround open source
[feature] built into [major platform]
[target region] local competitor local language pricing
```

中文对应：失败项目、停止维护、退款、免费方案、开源替代、平台内置、本地竞品和本地价格。

“没有搜到”只表示当前证据不足，不能证明市场空白。

## 7. 结构化 intent_plan

使用 `aor research --intent-plan-file FILE` 或 `aor plan --intent-plan-file FILE`。默认计划可用 `plan --include-recent-activity` 增加旧 GitHub Issue 的近期活动查询，发布时间与活动时间分别保留。可运行格式见 [intent-plan-demo.json](../examples/intent-plan-demo.json)；示例为 `{is_demo: true, intent_plan: {...}}` 包装；按 README 先提取 intent_plan，再传给 CLI。intent_plan 顶层严格只接收 intents，单条意图也不接受额外 is_demo 字段。这里只演示离线编译，不执行网页搜索。

每项意图必填 `id/question/evidence_type/search_query/ranking_query/source/locale/candidate_gaps`；`locale` 含 country/language，candidate_gaps 可为空。最多 20 项意图，社区合并后最多 12 个请求。evidence_type 允许 official_pricing、product_update、product_review、hiring、outsourcing、payment、workflow_pain、alternative、regional_gap、counter_evidence；这些是寻找目标，不是已取得证据。

search_query 发送给指定来源，ranking_query 保留为研究排序上下文，不能假设远端搜索接口会执行它。HN/GitHub 必须提供英语 search_query 与 en locale；只有中文自由主题且无英语 scope 查询时返回 needs_host_queries。请宿主补出符合意图的英语查询，不能将中文主题与 manual 等英文词机械拼接。

显式意图替换本次检索计划，不追加默认付费来源；scope/focus 继续作为研究上下文。编译输出 community、tikhub、web_import 子计划，web_import.required_imports 是宿主人工待办。重复请求按 source/endpoint/method/params 指纹合并，保留所有 intent_refs、provenance 和 request_aliases；多个排序问题不会变成多次付费调用。真实付款和地区事实仍需核验原文。
