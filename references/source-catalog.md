# 数据源目录与覆盖契约

最后更新：2026-09-10。平台列表表示实现边界，不是实时可用性声明。

## aor sources 与人工 web 导入

```bash
AOR_OFFLINE=1 aor sources catalog --output /tmp/aor-sources.json
AOR_OFFLINE=1 aor sources diagnose --json
AOR_OFFLINE=1 aor sources import --input examples/web-import-demo.json \
  --run-id RUN-20260910-0123456789 --as-of 2026-09-10 --output /tmp/aor-web-demo.json
```

以上在仓库根运行；源码入口为 `python3 scripts/radar.py sources ...`，独立入口 `python3 scripts/source_query.py ...` 不进行更新预检。示例 ID 仅用于独立导入演示；接入真实研究时使用 research 返回的运行 ID。

`catalog` 返回 source/platform、provider、capabilities、cost、preferred_languages、regions、region_filter、default_enabled、manual_import_only。`diagnose` 只消费凭证存在性的布尔标记，返回 `configuration_status`、`network_checked=false`、`live_health=not_checked`，不是在线健康检查。

人工导入输入为 `{items: [...]}`，每批 1–100 条。必填 `source/url/title/original_text/supporting_quote/evidence_role/observed_at/verification`；可选 `published_at/language/country/original_url/original_publisher/intent_refs/industry_ids/candidate_gaps/is_demo`。verification 含 `verified_by/verified_at/method`，method 仅允许 `opened_page` 或 `authorized_browser`。quote 必须是 original_text 的原文子串；搜索摘要不能冒充打开过的正文。

导入器只检查宿主的核验声明，输出 `provider=host-verified-web`、`stage=web_evidence_import`、`input_sha256` 和 `evidence[]`，核验状态为 `host_attested`；它不会独立打开网页。定价只产生 pricing 信号，`payment_status=not_established`；即使 evidence_role 为 payment，也不能跳过后续付款主张核验。同批重复内容会拒绝并要求合并 intent_refs。

新证据可以 `resume RUN_ID --evidence FILE` 交给编排；输出不是 BENCH，也不能直接充当已验证候选。详细可核验契约见 [来源模块 README](../src/aor/sources/README.md)。

## 目录

1. 覆盖原则
2. 来源分组
3. 状态模型
4. 访问方式
5. 不可承诺的覆盖

## 1. 覆盖原则

把“能力可用”“本次发起搜索”“本次返回证据”和“完整覆盖平台”视为四个不同概念。

只有本次运行实际返回非空、相关且可核验条目的来源，才能写入“已覆盖”。不要根据诊断结果或曾经的登录状态推断本次成功。

每日采用核心源加滚动源，但检索顺序由商业信号强度决定：

- 尽力每日使用核心社区源。
- 在七天内滚动覆盖社交、产品评论、中文和商业信号。
- 每天保留全球英语和中国两个核心市场，再轮换一个地区。
- 不为满足地区配额而推荐低质量机会。
- 优先核验定价、购买、营收、订阅、招聘、外包、取消与切换；泛讨论只补充背景。

### 一期生效范围

一期采用 `existing_adapters_only`：TikHub 的 12 个已实现平台作为主采集层，Hacker News 与 GitHub 作为已有辅助层。其余来源不进入默认自动采集，但可在能力目录的人工导入边界内由宿主核验网页后导入。新增自动适配器仍须完成端点、参数、费用和授权边界验证，不能用人工导入冒充自动平台覆盖。

新默认研究以六方向任务分配来源，HN 仅为辅助，GitHub 不进入默认查询。Steam、Shopify App Store、Etsy 以人工核验导入补充玩家、商家和创作者证据，没有宣称新增自动抓取。每个方向都有网页核验任务；宿主未执行时如实保留缺口。实际覆盖以 `industry_coverage` 为准，平台目录完整不等于本轮研究完整。

## 2. 来源分组

### 核心社区

- Reddit
- Hacker News
- X
- YouTube
- GitHub Issues；Discussions 属于后续适配范围

### 滚动社交与创作者

- TikTok
- Instagram
- LinkedIn
- Product Hunt
- Indie Hackers
- Threads
- Pinterest

TikTok、Instagram、LinkedIn、Threads 和 X 优先通过 TikHub 第三方 API 获取搜索结果和公开互动数据。TikHub 可访问不等于平台完整覆盖；每次运行仍按实际非空结果记录状态。

### 产品反馈

- App Store
- Google Play
- Chrome Web Store
- G2
- Capterra
- Trustpilot

### 中文来源

- 小红书
- 即刻
- 知乎
- V2EX
- B站
- 抖音
- 脉脉
- 微信公众号及评论
- 电商评价

抖音、小红书、B站、知乎和微信搜索可使用 TikHub 作为第三方 API 补充层。即刻、脉脉和电商评价仍主要依赖公开索引或授权抽样。

### 当前实现路由

| 来源族 | 首选实现 | 当前边界 |
|---|---|---|
| TikTok、Instagram、LinkedIn、Threads、X、YouTube、Reddit | TikHub 受控搜索；高价值帖子再查详情与一级评论 | 需要 TikHub API Key、实时价格和显式预算；不承诺全量覆盖 |
| 抖音、小红书、B站、知乎、微信搜一搜/公众号 | TikHub 受控搜索；高价值内容再查详情与一级评论 | 小红书按图文/视频路由；微信从搜一搜 URL 切到公众号接口；知乎当前评论深挖只接回答 |
| Hacker News、GitHub | 本项目内置 `community_query.py` 公开只读适配器 | 固定官方公开端点；GitHub Token 可选；结果直接规范化，不保存完整响应 |
| Product Hunt、Indie Hackers、V2EX、即刻、脉脉 | 原生 Web 搜索 + 打开正文核验，必要时授权浏览器抽样 | 没有稳定统一 API；失败时标记状态，不宣称完整扫描 |
| App Store、Google Play、Chrome Web Store | 商店公开页面/搜索索引；候选产品少量评论抽样 | 不批量抓取完整评论集；需要单独适配器后才能稳定自动化 |
| G2、Capterra、Trustpilot | 搜索索引与产品页抽样；有可用 CLI/API 时再切换 | 登录墙、反爬和地区差异较大；当前不得承诺完整数据集 |
| 搜索趋势、招聘、外包、融资、定价、广告 | Web 搜索、官网、公开 ATS/榜单和商业数据库的逐项适配 | 属于商业信号层，不由 TikHub 单独覆盖 |
| Discord、私有 Telegram/微信群、封闭论坛 | 用户授权导出或人工提供材料 | 默认不抓私有内容，不绕过访问控制 |

TikHub 价格目录还包含 Telegram、微博、快手、Lemon8、微信视频号、今日头条、西瓜视频等平台，但在确认搜索语义、参数、价格和测试前不自动加入每日计划。目录里“存在端点”不等于已经完成可靠适配。

一期期间即使这些端点完成初步确认，也只记录为候选适配器，不修改生产计划；平台扩展开关保持关闭。

### 商业信号

- 搜索趋势
- 应用榜单
- 招聘与外包需求
- 众筹
- 融资和收购
- 竞品定价
- 社交广告和达人推广
- 新模型和平台能力

商业信号是建立 `BENCH` 的主入口。社交热度只能说明关注度；没有产品、付款者与可核验收费或付款证据时不得建立正式对标。定价与未付合同不证明成交；A 级要求同条原始证据支持真实付款。

### 地区轮换

- 东南亚：印尼、越南、泰国、菲律宾、马来西亚、新加坡
- 非洲：南非、尼日利亚、肯尼亚、加纳及法语非洲
- 南亚：印度、巴基斯坦、孟加拉国、斯里兰卡
- 中东：海湾国家、土耳其、埃及
- 拉美：巴西、墨西哥、阿根廷、哥伦比亚、智利

地区轮换用于生成 R/SIG 迁移池，不要求每条都做深度分析。有来源市场对标和明确迁移理由、但缺本地付款的候选保留 R；A 必须补齐本地直接付款、独立来源和候选验证。B 表示收费对标与需求信号达到研究门槛，不等同于已证明本地成交，具体分层见 [机会政策](opportunity-policy.md)。

## 3. 状态模型

为每个尝试过的来源记录以下状态之一：

| 状态 | 含义 |
|---|---|
| `ok` | 请求取得有效条目；仍需结合相关性与原文核验，不能直接写“已覆盖” |
| `no-results` | 请求成功但没有有效条目；是否相关另看质量字段 |
| `partial` | 同一来源存在部分失败或不完整结果，不能聚合成全成功 |
| `auth-required` | 缺少登录或凭证 |
| `rate-limited` | 遇到限流 |
| `blocked` | 被平台或网络阻断 |
| `skipped-policy` | 因安全、隐私或平台政策主动跳过 |
| `error` | 其他明确错误 |

社区结果还保留逐请求状态及 `relevance_status/window_status/recent_evidence_eligible`。缺宿主英语查询的空计划为 `needs_host_queries`，未请求的来源子计划为 `not_requested`；都不是成功采集。旧帖最近活动使用独立 `updated_at/activity_window_status`，不改写 published_at。

新编排记录来源状态；兼容手动链路可使用 `manage_state.py source-health` 写入状态。只保存简短错误摘要；脚本会遮盖常见凭证形态，但仍不要主动把凭证传入 `--detail`。

## 4. 访问方式

在每条证据旁标注：

| 报告标签 | 定义 |
|---|---|
| 原生平台 | 由平台公开页面或公开接口返回 |
| 第三方 API | 由第三方数据服务返回 |
| 搜索索引 | 通过搜索引擎发现并核验 |
| 授权浏览器抽样 | 使用用户登录状态少量人工核验 |
| 人工核验 | 人工打开并确认，不代表可自动化 |

不同访问方式取得同一链接不算独立来源；转载通过 `original_url` 指向原文，发布主体通过 `original_publisher`、`original_author` 或 `publisher_id` 标识。A 级只计有支持文本、有效且非演示的原始来源。

把研究访问方式与未来产品数据来源分开描述。第三方 API 或浏览器可访问，不自动意味着能长期、批量、商业化使用。

## 5. 不可承诺的覆盖

不要宣称完整扫描任何具有登录墙、反爬、动态加载或私有社区属性的平台。

以下来源通常只能被描述为搜索引擎发现、浏览器抽样、需要适配器或未覆盖：

- X 和缺少认证的社交源
- 小红书、即刻、知乎、B站、抖音、脉脉和微信评论
- App Store、Google Play、Chrome Web Store 的完整评论
- Product Hunt、Indie Hackers、G2、Capterra 的完整数据集
- Discord 和其他封闭社区
- 电商平台完整评论
- 各国本地封闭论坛

每次运行都重新诊断，不把本文件当成实时可用性清单。

## 4.3 三平台评论能力

来源目录声明小红书/抖音的 comment_pagination、comment_replies，以及 X 的 comment_pagination、latest_comments。它们是适配器能力，不是实时健康证明；实际预算内的分页流程见[评论优先发现](comment-first-discovery.md)。X 仅处理返回的对话节点及 Bottom 游标，不承诺展开全部 Thread 游标、折叠或受限回复。
