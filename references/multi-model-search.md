# Grok 与宿主协作搜索

Grok 是可选的 X 搜索执行者。当前使用 AOR skill 的 AI 继续负责网页、评论、收费替代和反证；已有其他联网模型也可完成宿主任务。没有 Grok 的用户仍可使用 TikHub 或宿主可用的 X 搜索能力。

## 先配置 Grok 登录

需要使用 Grok 订阅搜索时，先在准备执行请求的机器安装 Hermes，并完成官方订阅 OAuth 登录：

```sh
hermes auth add xai-oauth
```

跟随 Hermes 提示，在官方授权页面用自己的 Grok 账户登录。可用性取决于账户的当前订阅与权限；登录成功不等于已确认 X 搜索、剩余额度或额外积分。参考 [Hermes OAuth 指南](https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth)。AOR 不办理订阅、不代为登录，也不需要用户把 token 粘贴到聊天或配置文件。

配置只保存认证文件路径和模型选项，凭据仍由 Hermes 保管。`auth_file` 应指向这台执行机器上的实际 Hermes 认证文件；有自定义 HOME/profile 或符号链接时核对真实配置。模型默认使用 `grok-4.6`，可显式配置。

将以下内容保存为数据目录中的 `search-config.json`，例如 `~/Documents/AI-Opportunity-Radar/search-config.json`。也可从发行包复制 [Grok 配置示例](../examples/search-config-grok.json)，按实际认证文件位置修改路径：

```json
{
  "schema_version": "1.0",
  "grok": {
    "enabled": true,
    "auth_file": "~/.hermes/auth.json",
    "model": "grok-4.6",
    "max_responses_requests": 6
  },
  "max_tasks": 24
}
```

`max_responses_requests` 是一轮研究最多发起的 Grok Responses 请求数，包含已发出但结果未知的请求；不是订阅剩余额度或服务端工具调用数。`max_tasks` 是计划任务上限，包含宿主与 X 任务。未传 `--config` 时使用 `DATA_HOME/search-config.json`；文件不存在即禁用 Grok，按正常回退路径继续。也可显式使用 [无 Grok 配置](../examples/search-config-host.json)。一轮计划固定配置摘要；更改模型或调用上限需创建后续研究，不能借修改配置重置本轮计数。

使用自定义 `--config FILE` 时，`search plan` 和每次 `search collect` 都须传入同一配置文件路径；配置路径也参与本轮摘要。`search status/import` 不需要该参数。下面的命令示例使用默认 `DATA_HOME/search-config.json`。

本版只读当前 access token：不刷新、不撤销、不轮换、不写回认证文件，也不读取 `XAI_API_KEY` 作为隐式回退。令牌失效时走其他搜索渠道；恢复 Grok 登录或令牌维护由 Hermes 用户自行完成。需要长期无人值守使用时，应另行验证凭据维护与额度策略。

使用同一订阅仍会消耗共享额度。AOR 不把 OAuth 标成免费，也不根据 API 的计价字段推断实际账单；订阅剩余量、额外积分和自动充值设置需在账户用量页面核对。

## 没有 Grok 时如何继续

| 情况 | 正常处理 |
|---|---|
| 未启用 Grok、未登录、认证文件不存在 | 使用有预算授权的 TikHub；否则生成宿主 X 搜索任务 |
| Grok 认证失效或权限拒绝 | 保留具体失败原因，交接其他渠道；不改账号或切换付费 API Key |
| TikHub 已配置且本轮明确传入预算 | 生成既有付费入口可执行的计划和参数，由唯一协调者执行 |
| TikHub 未配置或没有本轮预算 | 宿主或其已配置的联网模型搜索 X，保留实际执行回执 |
| 所有 X 搜索渠道均不可用 | 继续其他来源，报告 X 覆盖缺口，不写成没有需求 |
| 请求超时或断流、结果未知 | 不自动重发或自动重买；保留未知状态及已取得的其他结果 |

配置 TikHub 密钥不代表授权支出。付费回退沿用实时估价、`--max-cost-usd`、本轮累计账本和恢复规则，见 [费用与恢复](tikhub-integration.md)。宿主搜索任务是待执行交接，只有宿主实际调用工具并提交结果后才算执行；不把模型纯文本回答当成搜索。

原查询完整保留。查询超过 TikHub 当前 100 字符限制时，明确交给宿主按原查询搜索；需要改短时在后续研究显式提出，不静默截断查询。

## 并行执行与统一提交

以下 `RUN_ID`、`TASK_ID`、`DATA_HOME` 和文件路径需替换为实际值。`search` 不创建研究，先从 `research` 输出取得运行 ID：

```sh
aor search plan --home DATA_HOME --run-id RUN_ID
aor search status --home DATA_HOME --run-id RUN_ID
aor search collect --home DATA_HOME --run-id RUN_ID --task-id TASK_ID --output result.json
aor search import --home DATA_HOME --run-id RUN_ID --input result.json
```

`plan.tasks` 提供任务 ID、渠道、查询、目的和日期窗；`status.tasks` 提供宿主回执模板路径。宿主将模板复制到独立文件，保留渠道对应的 `provider=host-x` 或 `host-web`，实际搜索后填写 `model`、公开回答、引用和执行记录；填写 `succeeded` 或 `empty` 并更换 `receipt_id` 后 import。没有实际执行时保持 pending；工具调用成功没有结果时才是 empty。原文受阻与搜索无结果分别记录。`--output` 可以指向独立目录；若写进当前 run 内，只能放在 `search/outputs/` 下，以保护运行状态、配置和认证文件。

无 Grok 时如已授权本轮付费上限，可在 collect 显式传 `--max-cost-usd AMOUNT`。返回的 `fallback.argv` 是供协调者执行的参数数组，带真实 paid-plan 路径、本轮 run ID 与稳定 batch ID；按数组调用，不拼接 shell。它沿用本轮累计预算，不是为每个任务重新授予一笔预算。执行后运行 `after_execution_argv`（即 `search status`），状态会根据已登记的 TikHub 执行产物自动生成回执，无需人工填报搜索成功。缺少密钥、未给预算或离线时返回宿主 X 搜索模板。详细恢复以本次返回的 next_action 为准。

1. 启动 `research`，在 `awaiting_benchmarks` 读取本轮计划、阅读包、覆盖与缺口。
2. 通过 `search plan` 生成本轮检索任务。Grok 负责 X，宿主负责其他网页及必要的 X 回退；已有其他联网模型可以领取宿主任务。
3. 宿主将 Grok `search collect` 与自己的网页搜索并行执行，各自产出独立结果文件。无法并行的宿主可顺序执行，记录真实执行方式。
4. 主协调者通过 `search import` 登记结果，查看 `search status` 的真实执行、候选和待核验情况。不要让多个 worker 同时调用同一运行的 `resume`。
5. 打开候选原帖，使用 `sources import` 导入已核验原文，再由协调者 `resume --evidence FILE`。语义审阅可复用 [审阅队列](review-queue.md)。
6. 合并观察后一次提交 `resume --observations-file FILE`。观察输入是快照；分别提交会替换本轮观察，不是自动追加。
7. 读取 `task-followup-plan.json`；由原话继续查询任务、替代与未采用原因。下一轮使用带父运行的研究，不修改已经执行过的意图历史。

每条查询只承担一个目的。结合 event/workflow/artifact/positive/capability_change 入口，保留持续使用、创作、收藏和学习行为；发现阶段不要求用户同时提付款或 AI。按实际缺口安排互补查询，不把同一组固定搜索词机械复制给全部模型。

## 候选不等于原文证据

搜索候选保留查询、执行者、模型、原帖 URL 和引用位置。零长度 URL 注解只表示返回了来源链接；非零位置也不证明已打开原帖。模型短摘录和摘要不得填进 `original_text` 假装网页正文。

403、登录限制或无法读取原文时保留待补证，不重复要求模型编造正文。只有实际打开并核验的原文才通过既有导入器进入证据库，固定 `evidence_id/revision_id` 后再生成 OBS。Grok 机器人的公开回复可以验证接口，但不能算真实用户需求。

X 帖按原生帖子 ID 合并不同 URL 和多个搜索执行者的发现；保留各自查询回执。原帖、回复、转载和不同原始用户分别处理。模型数量不增加独立用户或来源数量，旧证据身份不静默迁移。

核验后填写网页导入输入时，设置 `source=twitter`，将原帖 ID 填入 `source_object_id`，并明确 `object_kind=post` 或 `comment`/`reply`。候选中的 status 类型尚未区分原帖和回复，不能直接照抄。URL 和 original_url 必须指向这条内容自身；原文、引文及 verification 继续满足[网页导入契约](source-catalog.md)。没有提供原生身份的旧输入仍按既有 URL 方式处理。

## 用量、恢复与验收

Grok 搜索记录发起的请求数、实际工具调用、tokens、耗时和返回用量。客户端可限制发起次数和并发；请求中的 `max_tool_calls` 尚不是经验证的服务端费用硬上限，超时也不证明服务端停止消耗。

成功结果可复用，发送前保存执行标记；断流、超时和中断保留结果未知，普通恢复不自动重试。离线运行与 `AOR_OFFLINE=1` 禁止 Grok 联网；计划和已有结果处理仍可执行。

`search status` 从本轮 `evidence-context` 自动对齐候选和已经导入的固定原文修订；没有原文核验记录时核验数保持未知，旧的失效修订不计入当前核验。比较同一批跨行业任务的 host-only 与 host-plus-Grok，统计唯一候选、原文核验、直接用户任务、重复率、行业/语言缺口及用量。未知与未执行分别保留；链接数量、工具成功和多模型意见一致都不等于发现更多有效机会。

接口、实际参数和可复制示例以 `aor search --help` 及其各子命令为准。更新后确认 `aor skills --json` 指向新版本 `SKILL.md`；手动复制的 skill 需要同步，不能只更新仓库或固定指向旧 `versions/` 目录。
