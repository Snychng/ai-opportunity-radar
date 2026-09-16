# 高信号查询模式

最后更新：2026-09-16。

## 1. 查询优先级

先按用户任务发现，再按当前研究缺口选证据。4.4 保留 6 方向、36 子赛道的原有任务种子，另增每方向五种入口的独立中英文种子：event/workflow/artifact/positive/capability_change，详见[任务优先发现](task-first-discovery.md)。收费查询用于后续商业核验，不要求每条初始发现都提付款或现有产品。常见证据目标：

1. 直接商业：付款、订单、收入、订阅、定价、合同、招聘、外包。
2. 购买摩擦：取消、退款、切换、太贵、套餐限制、缺少关键集成。
3. 替代行为：手工、表格、复制粘贴、自由职业者、内部自建、放弃任务。
4. 地区差异：语言、支付、主渠道、当地平台、法规与工作流。
5. 消费与创作行为：重复使用、完成作品、学习进展、真实分享、社区参与；有原文和场景时可以支持需求。
6. 泛讨论与愿望：只作发现线索，不能独立进入 OPP。

每条查询只负责一个意图。发现阶段组合“具体人群 + 任务/触发”，再按缺口补持续行为、收费、免费替代或反证；不要求所有用户同时谈 AI 或付款。避免 `AI startup ideas` 之类宽查询。

从已核验观察的 task、trigger、current_workaround、desired_outcome、artifact 和可选 query_terms 生成后续计划，每任务族至多三条、每轮至多八族；分别查实际操作/产物、现成替代和未采用原因。查询保留来源 OBS 和固定原文引用，不把生成的搜索词当成新增需求。task-followup-intents.json 是待执行 web 意图，可传给后续子运行；它不自动联网，也不自动追加预算。未知语言保持 unknown，已有竞品应继续查具体未满足环节和拒绝使用理由。

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

使用 `aor research --intent-plan-file FILE` 或 `aor plan --intent-plan-file FILE`。当显式意图或定向计划已包含 GitHub 请求时，`plan --include-recent-activity` 可增加对应旧 Issue 的近期活动查询；六方向默认计划不含 GitHub，单独加此开关不会创建 GitHub 请求，发布时间与活动时间分别保留。可运行格式见 [intent-plan-demo.json](../examples/intent-plan-demo.json)；示例为 `{is_demo: true, intent_plan: {...}}` 包装；按 README 先提取 intent_plan，再传给 CLI。intent_plan 顶层严格只接收 intents，单条意图也不接受额外 is_demo 字段。这里只演示离线编译，不执行网页搜索。

每项意图必填 `id/question/evidence_type/search_query/ranking_query/source/locale/candidate_gaps`；`locale` 含 country/language，candidate_gaps 可为空。最多 20 项意图，社区合并后最多 12 个请求。evidence_type 允许 official_pricing、product_update、product_review、hiring、outsourcing、payment、workflow_pain、alternative、regional_gap、counter_evidence、usage_behavior、creative_output、learning_progress、social_sharing；这些是寻找目标，不是已取得证据。

search_query 发送给指定来源，ranking_query 保留为研究排序上下文，不能假设远端搜索接口会执行它。HN/GitHub 必须提供英语 search_query 与 en locale；只有中文自由主题且无英语 scope 查询时返回 needs_host_queries。请宿主补出符合意图的英语查询，不能将中文主题与 manual 等英文词机械拼接。

显式意图替换本次检索计划，不追加默认付费来源；scope/focus 继续作为研究上下文。编译输出 community、tikhub、web_import 子计划，web_import.required_imports 是宿主人工待办。重复请求按 source/endpoint/method/params 指纹合并，保留所有 intent_refs、provenance 和 request_aliases；多个排序问题不会变成多次付费调用。真实付款和地区事实仍需核验原文。


## 6. 轮转、替补与查询质量

默认计划只读取历史覆盖快照中的实际 task×language 尝试次数；未采集任务优先轮转，明确缺口可以优先一次补查，随后继续探索其他任务，避免单主题占满所有批次。目录中的 existing_behavior_to_verify 与 spend_status 明确是待核验研究问题，不将“外包/订阅”等种子词统计为真实支出。

每个主查询可附 fallback_requests，使用现有白名单 search 构建器生成同查询/同语言的其他平台请求。无实时价格或预算不能容纳主请求时，仅从这些已登记替补中筛选；skipped_requests 记录原因，substitutions 记录替补关系，未实际请求不算覆盖。无法替补时明确留缺口，不猜端点或价格。

词面匹配是召回筛选：relevance_status 和 lexical_status 不能替代宿主对对象、任务与引用的核验。weak_match、跨文字系统未知、日期 uncertain、上游非空却未识别的响应各自保留，不能全部归成“无市场”。同查询只有无关材料时应修改任务锚点或换来源；无需通过提高预算重复获取相同噪声。
