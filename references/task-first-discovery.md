# 任务优先的机会发现

4.4 从具体任务开始保留需求，再研究商业方案。用户观察不要求已知产品、付款、AI 增量或 MVP；LEAD、BENCH、A/B/R 仍遵循各自的证据门槛。原话可定位只证明引用正确，宿主分类不自动证明身份、商业语义或市场成立。

## 1. 五种发现入口

默认目录保留六方向、36 子赛道，并为每方向增加五类独立任务种子。历史 2.0 计划按原目录验证，新计划使用目录 3.0。

| 入口 | 要找的事实 | 示例问题 |
|---|---|---|
| event | 发生某件事后，用户必须完成的任务 | 第一次参加市集，需要准备什么？ |
| workflow | 实际操作、绕行办法、转交与反复确认 | 每个定制订单的细节怎样交给制作人员？ |
| artifact | 用户为完成任务制作的东西 | 表格、清单、项目日志、手写本包含哪些内容？ |
| positive | 收藏、纪念、分享、创作、完成和掌握技能 | 为什么持续记录作品，哪些记录之后会复用？ |
| capability_change | 新能力出现后，用户实际采用或拒绝的理由 | 自动转写之后，仍要校对哪些人物、决定和事实？ |

种子是待调查问题，不是已验证需求。默认主付费发现请求上限和授权规则不因扩充入口而提高；手动网页任务由宿主打开原始来源并导入。

## 2. 独立保存观察

先导入原文，在 evidence-context 中取得真实 evidence_id 和 revision_id，再复制本轮 observations-template.json 到自己的输入文件。以下是形状示例，占位符不能作为真实证据提交：

```json
{
  "schema_version": "3.0",
  "run_id": "替换为本轮运行 ID",
  "as_of": "替换为本轮日期",
  "observations": [{
    "target_user": "自行整理家庭食谱的人",
    "task": "把家庭手稿整理成纪念书",
    "need": "保留手迹并完成可印制的家庭版本",
    "trigger": "准备家庭纪念礼物",
    "current_workaround": "扫描后逐页手工排版",
    "desired_outcome": "家人能阅读和保存",
    "artifact": "手写食谱与排版文件",
    "constraints": ["保留原始手迹"],
    "industry_ids": ["personal_life"],
    "feedback_type": "positive_behavior",
    "sentiment": "positive",
    "behavior_type": "commemorating",
    "language": "zh",
    "query_terms": ["家庭食谱", "手写食谱排版"],
    "evidence_refs": [{"evidence_id": "替换真实 ID", "revision_id": "替换真实修订", "quote": "逐字摘录原话"}],
    "solution_hypotheses": [{"delivery_form": "human_assisted_service", "statement": "协助转写、确认疑字并交付排版文件", "status": "hypothesis"}]
  }]
}
```

```sh
aor resume RUN_ID --observations-file observations.json --home DATA_HOME
```

未提交 benchmarks 时仍处于 awaiting_benchmarks。输入是本次观察快照，替换输入须保留本轮还要使用的观察；不会自动追加文件外的旧项。原有 benchmarks.observations 仍兼容，流程合并两个入口并校验同一引用的冲突分类。已完成的运行保持不可变，修订另建子运行。

每条必填 target_user/task/need（各 1–500 字符）、industry_ids（1–10 项）和 evidence_refs（1–20 项）；每轮最多 2000 条。product 可省略，products 最多 20 项；trigger/current_workaround/desired_outcome/artifact 是可选的 1–500 字符文本，constraints 最多 10 项、每项 200 字符，query_terms 最多 10 项、每项 100 字符。未知用户身份应在 target_user 中明确未知，不凭平台或产品猜职业和身份。

language 仅为 en/zh/unknown，默认 unknown，描述宿主观察与查询关键词的语言，与证据原文 evidence.language 分开。声明和实际查询文本不一致时生成计划保留 unknown，不机械翻译、不推断作者语言。behavior_type 为 problem_solving/collecting/commemorating/sharing/creating/completing/mastery/unknown。feedback_type 增加 positive_behavior；usage/purchase_claim/refund_claim/recommendation_request/positive_behavior 的非演示观察可以进入直接需求簇，推广、官方回复、疑似刷评和 unknown 不进入。

## 3. 原话驱动下一次检索

每次刷新产生 task-followup-plan.json；有新查询时还产生可直接使用的 task-followup-intents.json。按任务族归并后，每轮最多 8 族、每族至多 3 条查询：实际操作与产物、现成替代、未采用原因。查询保留 source_observation_ids 与固定证据引用；任务、约束和原文提供锚点，产品和方案不增加任务数。

```sh
aor research --parent-run-id RUN_ID --intent-plan-file PATH_TO_TASK_FOLLOWUP_INTENTS --home DATA_HOME
```

这是 source=web 的待执行人工核验计划，automatic_fetch=false，不自动搜索、购买、联系用户或宣称取得新证据。宿主读取计划后实际打开来源，再导入新运行。稳定 query_key 可用于识别重复查询；当前主流程没有依据跨运行执行回执自动抑制所有已查查询的承诺。没有合格观察或新查询时不生成空意图文件。

## 4. 需求事实与交付假设分层

user_discovery 2.0 中，TASK 身份由规范化 target_user/task/constraints 构成；NEED 再加入 need。产品只作为上下文，同一任务提到不同产品不拆成新需求。不同表达是否指同一任务，仍由宿主规范化；程序不宣称完成自动语义识别。OBS 由 NEED 与固定引用组成，同一原话的产品上下文可以合并，冲突标签拒绝提交。

solution_hypotheses 最多 10 项，每项只包含 delivery_form/statement/status；status 必须为 hypothesis。形态包括 one_off_delivery、human_assisted_service、plugin、studio_tool、subscription_software、other。一次性交付或人工辅助服务可以先作为假设保留，进入正式 AI 机会候选时再说明具体 AI 增量。方案变体不改变任务或需求身份，不能用多种收费方式膨胀需求数。

报告分别展示原文事实、产品上下文和解决方案假设。公开网站只取得白名单计数和固定枚举统计，不取得任务、产物、用户原话或方案正文。已保存的 user_discovery 1.0 继续按旧产品身份规则校验，不用 2.0 身份重算旧报告。

## 5. 主包和补读

主 evidence-packet 在既定条数、字符预算内兼顾任务族、行业、来源和证据角色；未知任务族保留未知，不从词面编造。industry-packets 保留各方向阅读入口。review-packets.json 为主包以外的可读修订生成补读批次，research-followup.json 的 reading_batches 指出下一批、遗漏引用与原因。

补读和主包都是摘录，全文按 evidence_id/revision_id 到 evidence-context.json 读取。超预算、未来、撤回或其他不符合选取条件的材料会保留未分配原因。生成批次、入包、打开全文均不自动增加 semantic reviewed 计数；正式审阅仍须提供绑定当前修订的 reviewer、reviewed_at、status、rationale。覆盖 2.1 保留 scope_defined/scope_gaps/task_gaps；范围未定义或任务仍未执行时不能仅因行业清单为空宣布覆盖完整。
