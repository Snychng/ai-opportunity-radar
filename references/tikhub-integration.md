# TikHub 查询与费用控制

最后更新：2026-09-10。文中的外部价格均为历史演示，不表示当前价格。

## 目标

TikHub 用作社交媒体和中文内容平台的第三方 API 后端。它补充本项目内置的 Hacker News/GitHub 公开社区适配器与网页核验层，不替代其他证据层。

一期平台集合固定为本文件列出的 12 个搜索来源。价格目录出现新平台或新端点时只做记录，不自动扩展生产白名单与每日计划。

任何付费请求都必须遵循：

```text
生成查询计划 → 读取控制台实时价格 → 输出预计费用 → 校验费用上限 → 零费用账户预检 → 执行 → 记录预计实际消耗
```

不要把 API Key、Cookie、Authorization 头或完整响应头写入计划、报告、日志或 GitHub。

## 当前受控搜索端点

价格会变化，以下是仓库保留的 2026-07-14 历史演示样本，未在本次文档更新中重新核验。每次执行仍必须从 `https://user.tikhub.io/api/pricing?page=1&page_size=2000` 刷新。

| 来源 | 搜索端点 | 样本单价 USD |
|---|---|---:|
| TikTok | `/api/v1/tiktok/app/v3/fetch_video_search_result` | 0.001 |
| Instagram | `/api/v1/instagram/v2/general_search` | 0.002 |
| LinkedIn | `/api/v1/linkedin/web/search_posts` | 0.004 |
| Threads | `/api/v1/threads/web/search_recent` | 0.002 |
| X / Twitter | `/api/v1/twitter/web/fetch_search_timeline` | 0.001 |
| YouTube | `/api/v1/youtube/web/search_video` | 0.001 |
| 抖音 | `/api/v1/douyin/search/fetch_video_search_v2` | 0.010 |
| 小红书 | `/api/v1/xiaohongshu/app_v2/search_notes` | 0.010 |
| B站 | `/api/v1/bilibili/web/fetch_general_search` | 0.001 |
| 知乎 | `/api/v1/zhihu/web/fetch_article_search_v3` | 0.001 |
| 微信搜一搜 | `/api/v1/wechat_search/v2/fetch_search` | 0.010 |
| Reddit | `/api/v1/reddit/app/fetch_dynamic_search` | 0.001 |

只允许脚本内白名单端点。需要增加新端点时，先确认官方 OpenAPI 参数和控制台实时单价，再补测试和白名单。

## 生成计划

```bash
python3 "$SKILL_DIR/scripts/build_query_plan.py" \
  --date YYYY-MM-DD \
  --home "$RADAR_HOME" \
  --export-tikhub-plan "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-search-plan.json"
```

默认轻量计划采用每日核心源加三日滚动源：

- 核心源：Reddit、X、YouTube、小红书、知乎。
- 滚动组一：TikTok、Instagram、抖音。
- 滚动组二：LinkedIn、Threads、B站。
- 滚动组三：TikTok、Instagram、微信搜一搜。
- 当日地区查询使用轮换地区的本地语言表达，不只追加国家名。

这里导出的 `tikhub-search-plan.json` 是通用发现草稿，用于看覆盖范围和形成免费研究方向，不代表已经批准付费执行。免费证据还没有形成候选 ID 与明确缺口时，不得直接运行该计划。

## 从候选证据缺口生成可执行计划

完成初步 BENCH、扩展和过滤后，把值得补证的目标写成：

```json
{
  "gaps": [
    {
      "candidate_id": "SIG-20260716-ABC123",
      "missing_gate": "印度尼西亚本地直接付款证据",
      "target_region": "印度尼西亚",
      "expected_promotion": "r_to_b",
      "keyword": "layanan pelanggan AI berbayar usaha kecil",
      "sources": ["tiktok", "reddit"]
    }
  ]
}
```

`expected_promotion` 只允许 `rejected_to_b`、`rejected_to_r`、`r_to_b`、`b_to_a` 或 `confirm_rejection`。每个缺口只选 1–3 个最可能给出答案的来源，单次最多 10 个缺口。定向搜索补证每来源每批最多 3 请求，由计划生成器与执行前校验硬性限制；两个来源各 2 请求、合计 4 请求合法。达到限制后不能直接切批继续：先执行并由 Agent 评估产出。

```bash
python3 "$SKILL_DIR/scripts/tikhub_query.py" build-gaps \
  --date YYYY-MM-DD \
  --run-id RUN-YYYYMMDD-XXXXXXXXXX \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/evidence-gaps.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-gap-plan.json"
```

生成的每个请求都携带候选 ID、缺失门槛、目标地区和预期升级结果。估价与执行必须使用 `tikhub-gap-plan.json`，不能把“再找更多点子”写成缺口。

评论不在搜索阶段批量获取。先筛选 1–5 个高价值帖子，再生成单独的评论深挖计划并重新估价。

## 评论深挖计划

12 个搜索来源都已配置“帖子详情 + 一级评论”白名单。以下为仓库保留的 2026-07-14 单帖首页历史演示价，未在本次更新中重新核验；执行前仍以实时目录为准。

| 来源 | 详情 + 一级评论 USD | 关键标识 |
|---|---:|---|
| TikTok | 0.002 | `aweme_id` |
| Instagram | 0.004 | `code_or_url` |
| LinkedIn | 0.008 | `post_id` |
| Threads | 0.004 | 数字 `post_id` |
| X / Twitter | 0.002 | `tweet_id` |
| YouTube | 0.002 | `video_id` |
| 抖音 | 0.002 | `aweme_id` |
| 小红书 | 0.020 | `note_id` 或 `share_text`，并指定图文/视频 |
| B站 | 0.002 | `bv_id` |
| 知乎回答 | 0.002 | `answer_id`；当前不把文章 ID 套入回答评论接口 |
| 微信文章 | 0.020 | `https://mp.weixin.qq.com/` 文章 URL |
| Reddit | 0.002 | `post_id`，自动规范为 `t3_` 前缀 |

搜索执行后先规范化并导出可深挖候选：

```bash
python3 "$SKILL_DIR/scripts/normalize_tikhub_results.py" \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-gap-results.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-normalized-search.json" \
  --selection-output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-comment-candidates.json"
```

候选会携带平台所需标识和小红书/知乎/微信的 `content_type`，但不会自动决定研究价值。从中选 1–5 个并补充 `selection_reason`，例如：

```json
[
  {
    "source": "threads",
    "selected_item_id": "threads-12345",
    "selection_reason": "多人重复描述同一人工流程并询问替代工具",
    "identifiers": {"post_id": "12345"}
  }
]
```

生成独立计划：

```bash
python3 "$SKILL_DIR/scripts/tikhub_query.py" build-comments \
  --date YYYY-MM-DD \
  --parent-search-run-id RUN-YYYYMMDD-XXXXXXXXXX \
  --input "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-comment-selections.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-comment-plan.json"

python3 "$SKILL_DIR/scripts/tikhub_query.py" estimate \
  --plan "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-comment-plan.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-comment-cost-estimate.json"
```

每个选中帖子固定两次首页请求。任何评论翻页都必须另建计划、重新读取价格并单独通过预算；脚本不会隐式自动翻页。

规范化脚本接受 `search_discovery`、`evidence_gap_verification` 和 `comment_deep_dive` 三类结果，定向补证可以沿标准流程继续处理。执行评论计划后再次运行规范化脚本。输出评论必须保留 `parent_item_id`，这样证据可以追溯到搜索阶段选中的帖子。微信搜索结果使用 `identifiers.url` 传给公众号详情和评论接口；不得再使用旧的 `article_url` 字段。

## 运行前估价

估价不会调用付费数据端点：

```bash
python3 "$SKILL_DIR/scripts/tikhub_query.py" estimate \
  --plan "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-gap-plan.json" \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-cost-estimate.json"
```

估价必须返回：

- 请求次数。
- 原价、预计美元和人民币费用。
- 最大尝试次数下的最坏费用。
- 按来源费用明细。
- 免费额度适用端点对应的费用。
- 免费额度不适用端点对应的费用。

人民币金额默认使用可配置的估算汇率 `7.2`，不是实时外汇报价。需要其他口径时传 `--usd-to-cny`。

## 执行

API Key 只能通过环境变量提供，不允许作为命令行参数：

```bash
export TIKHUB_API_KEY='由用户在本机安全设置，不写入文件'

python3 "$SKILL_DIR/scripts/tikhub_query.py" run \
  --plan "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-gap-plan.json" \
  --max-cost-usd 0.10 \
  --journal "$RADAR_HOME/runs/$RUN_ID/paid-journal.sqlite3" --batch-id gap-1 \
  --output "$RADAR_HOME/raw/YYYY-MM-DD/tikhub-gap-results.json"
```

中国大陆默认使用官方 `https://api.tikhub.dev`；其他地区可显式传 `--api-base https://api.tikhub.io`。

`--max-cost-usd` 是硬性上限，按“实时目录原价 × 最大尝试次数”的未舍入金额校验；账户折扣不会降低硬预算保护值。`run` 不接受离线价格文件。预算通过后，脚本先确认 `/api/v1/tikhub/user/get_user_info` 在实时目录中的价格仍为 `$0`，再读取账户状态、付费余额和免费额度；只保留这些非敏感摘要，不保存邮箱、API Key 名称或其他账户资料。脚本按端点资格计算免费额度可覆盖部分和最坏情况下所需付费余额，余额不足时会在任何数据请求之前失败关闭。

没有密钥、账户不可用、零费用预检端点涨价、余额不足、实时价格缺失、端点不在白名单或预计费用超过上限时，不发起任何付费数据请求。

付费发现最多占本轮预算的 20%，其余预算只用于上述定向补证。每批结束后，Agent 必须判断是否新增 BENCH、合格结论或关键门槛证据；无产出则停止该来源，不追加新批。执行器只检查请求数、预算和响应状态，不能自动判断商业价值。“每来源 3 请求”的硬限制针对定向搜索补证；评论计划仍采用前述 1–5 帖、每帖 2 请求的独立限制。

## 费用报告口径

每次向用户返回：

```text
TikHub 预计：N 次，$X（约 ¥Y）
免费额度适用成本：$A
免费额度不适用成本：$B
执行后估计尝试成本：$C
实际账单：以 TikHub 使用日志为准
```

`allow_free_credit` 只表示端点资格，不表示账户当前免费额度一定足够。`run` 会通过零费用账户端点核验余额，并在结果中记录最坏情况下需要的付费余额；失败请求是否扣费由 TikHub 账单决定，因此“执行后估计尝试成本”不能写成已经核对的实际扣款。需要精确对账时，再读取用户授权的 TikHub 使用日志。完整结论汇总只接受本轮 `run_id` 的执行费用；复制执行文件或重复传入相同内容不会重复计费。跨轮证据可声明 `reused_for_run_id` 复用，但旧执行费用不得混入。

## 安全与故障处理

- 不使用用户平台 Cookie 参数；只使用 TikHub Bearer API Key。
- 不允许任意 API 域名；跨域重定向和 HTTPS 降级重定向会被拒绝，避免 Bearer 密钥外泄或重定向型 SSRF。
- `run` 强制刷新公开实时价格目录；离线价格文件只允许用于估价和测试。
- `run` 在付费数据请求前调用零费用账户端点；若该端点缺失或价格不再为零，会直接停止。
- 搜索阶段的翻页、数量和筛选参数固定为受测首页值，防止手工篡改导致无效但可能计费的请求。
- 单计划最多 100 个请求，关键词最长 100 字符，单响应最大 8 MiB，整批持久化结果最大 32 MiB；达到批次上限后停止后续付费调用。
- 第三方响应中的 Authorization、Cookie、Token、API Key、Secret 和 Password 字段会被删除。
- 第三方 HTTP 错误正文不会进入报告；错误以 `auth_error`、`rate_limited`、`timeout` 等结构化状态记录。
- HTTP 200 但响应 `code` 非 0/200 或 `success=false` 时按业务错误处理，不计为成功。
- 单一来源错误会记录在该请求下，其他来源继续执行；但批次结果超过安全上限时会停止继续花费。
- 默认每个请求只尝试一次；提高到 2 或 3 次后，可确定失败且允许重试的限流/5xx 才在额度内退避。超时、网络错误、无效响应或响应过大可能已经计费，记为 `outcome_unknown`，不自动重试。401/403 等不可重试错误直接保留失败。
- 估价与结果保存计划哈希、价格目录哈希、抓价时间和价格来源，便于后续对账。

## 请求 journal、累计预算与恢复

严格离线研究不能运行付费补证；`estimate` 的实时价格读取和 `run` 的账户预检也是网络访问。下面假定真实补证计划已经建立，用户已授权该预算；变量须来自同一实际运行。

```bash
aor paid run --plan "$PAID_PLAN" --max-cost-usd 0.10 \
  --journal "$PAID_JOURNAL" --batch-id gap-1 --output "$PAID_RESULT"
aor paid run --plan "$PAID_PLAN" --max-cost-usd 0.10 \
  --journal "$PAID_JOURNAL" --batch-id gap-1 --resume --output "$RESUME_RESULT"
```

- `--journal` 绑定 run_id/as_of；同一研究的所有补证批共用该文件。未指定 journal 的旧调用保持单批兼容，但没有跨进程恢复和跨批累计保护，不能宣称可恢复。
- 同一 `--batch-id` 绑定同一计划摘要；再次执行必须 `--resume`。计划改变要用新 batch-id，不能覆盖旧批；新批仍共用 journal，预算不重置。
- 请求指纹来自 source、endpoint、method、params，成功请求跨批复用。状态依次为 planned、started，再到 succeeded、failed 或 outcome_unknown；发送前登记尝试和原价成本。
- 硬预算校验为“历史累计原价尝试成本 + 本次允许新增尝试的最坏原价成本”。折扣、免费额度和新批都不会抹去历史尝试费用。`--max-attempts` 是每个请求指纹累计最大尝试数（1–3），不是每次恢复新增次数。
- 恢复时未完成的 started 变成 outcome_unknown。超时或断网无法证明供应方未执行或未计费；普通 resume 不重买，也不把未知当成功。
- 只有已获得明确重试授权，才在同批恢复参数中增加 `--resolve-unknown REQUEST_ID`；确定失败的跨调用重试使用 `--retry-failed REQUEST_ID`。二者可重复，仍须有剩余次数和累计预算。resolve-unknown 是允许再次尝试，不是将旧账单改为未扣费。
- `results/summary` 的尝试与费用表示本次调用，复用项标记 reused；`run_ledger` 是同 run 累计。保留各次结果文件，汇总本次调用费用，不把累计 ledger 反复相加。恢复也可能读取实时价格和账户，不能理解成完全离线回放。

### 从 research 编排发起明确补证

```bash
aor resume "$RUN_ID" --home "$RADAR_HOME" --paid-plan "$PAID_PLAN" \
  --max-cost-usd 0.10 --batch-id gap-1
# 恢复付费批次的参数是 --resume-batch：
aor resume "$RUN_ID" --home "$RADAR_HOME" --paid-plan "$PAID_PLAN" \
  --max-cost-usd 0.10 --batch-id gap-1 --resume-batch
```

编排固定使用 `runs/RUN_ID/paid-journal.sqlite3`，保存每次执行与规范化结果，再返回 awaiting_benchmarks，要求 Agent 用新增事实修订对标和主张。它拒绝通用 search_discovery 草稿、离线运行和已开始提交的运行。编排支持 `--max-attempts`、可重复 `--resolve-unknown` 和 `--retry-failed`，语义同上；恢复批次使用 `--resume-batch`。其余参数以 `--help` 为准。
