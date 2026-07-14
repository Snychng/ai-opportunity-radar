# 研究工作流

## 目录

1. 研究原则
2. 每日采集
3. 证据规范化
4. 聚类与反证
5. 状态提交
6. 深挖与历史回顾

## 1. 研究原则

遵循“先证据、后机会、再产品形态”的顺序。不要先生成产品列表再为它寻找引用。

把一次运行拆成两个研究面：

- 消费者：恋爱陪伴、成人、游戏、虚拟角色、社交娱乐、身份表达、创作玩法和大需求的新形态。
- 小微企业：明确工作触发点、手工替代、招聘与外包、区域化缺口、开发者工具和平台撮合。

把 30 个自然日作为每日主窗口，从其中派生 7 天新信号。每周或深挖时再做 90 天验证；使用 12 个月信息解释竞争历史和“为什么是现在”。窗口为含首尾的自然日区间，30 日窗口起点是 `as_of - 29 days`；报告直接引用计划中的 `range_from` 和 `range_to`。

## 2. 每日采集

### 2.1 导出两类独立计划

使用 `build_query_plan.py` 同时导出：

```text
RUN_DIR/community-plan.json
RUN_DIR/tikhub-search-plan.json
```

两个计划与总计划共享 `run_id`。社区计划只允许 Hacker News 和 GitHub；TikHub 计划只允许一期白名单平台。不要跨计划拼接运行 ID。

### 2.2 运行社区证据引擎

社区检索逻辑内置在本 Skill 中，不直接调用其他 Skill。先做离线能力检查，再执行固定端点、固定参数范围的只读查询：

```bash
RUN_DIR="$RADAR_HOME/raw/YYYY-MM-DD"
python3 "$SKILL_DIR/scripts/community_query.py" doctor --json
python3 "$SKILL_DIR/scripts/community_query.py" run \
  --plan "$RUN_DIR/community-plan.json" \
  --output "$RUN_DIR/community-normalized.json"
```

GitHub Token 可选，只从 `GITHUB_TOKEN` 或 `GH_TOKEN` 读取；缺少时使用公开限额。结果文件只保留规范化证据和来源状态，不持久化完整响应或令牌。

### 2.3 使用 Web 补充

一期仅串行补充：

- 当日轮换地区的本地语言讨论
- 竞品官网、价格、融资和发布记录
- 候选机会的反证与失败案例

先搜索，再打开高价值结果。只有读取正文后，才能把文本当作直接证据。搜索摘要只能作为发现线索。

一期不把 Product Hunt、Indie Hackers、应用商店、V2EX、即刻、脉脉或其他尚未适配平台加入每日采集。Web 只承担核验和反证，不绕过一期范围。

### 2.4 使用 TikHub 补充社交与中文平台

先生成并独立导出 TikHub 查询计划，然后读取控制台实时价格：

```bash
python3 "$SKILL_DIR/scripts/tikhub_query.py" estimate \
  --plan "$RUN_DIR/tikhub-search-plan.json" \
  --output "$RUN_DIR/tikhub-cost-estimate.json"
```

每次执行前都报告费用。API Key 只从 `TIKHUB_API_KEY` 环境变量读取，执行命令必须设置 `--max-cost-usd`。执行器会先通过零费用账户端点核验账户状态、免费额度与最坏情况下所需付费余额，余额不足时不发起付费数据请求。每日采用“核心源 + 三日滚动源”，三日集合并后覆盖 TikTok、Instagram、LinkedIn、Threads、X、YouTube、Reddit、抖音、小红书、B站、知乎和微信搜一搜。

以上 12 个平台是一期固定集合。生成计划时必须包含 `scope.id=phase_1_existing_platforms` 和 `platform_expansion_enabled=false`；实时价格目录中的其他平台不得自动加入。

搜索和评论分两阶段计费：第一阶段只做关键词搜索并用 `normalize_tikhub_results.py --selection-output` 生成可深挖候选；第二阶段从候选中选 1–5 个、补充 `selection_reason`，用 `tikhub_query.py build-comments` 生成“详情 + 一级评论”计划并重新估价。微信必须从搜索结果的 `url` 接入公众号接口；小红书按图文/视频类型路由；知乎只接受回答 ID。评论执行结果要再次规范化。12 个当前搜索来源都已配置评论深挖白名单；如果任一端点从实时价格目录消失，整份评论计划必须失败关闭。不要为了补齐数量批量抓取无关评论，也不要隐式翻页。

详细端点、价格口径与安全规则见 [tikhub-integration.md](tikhub-integration.md)。

### 2.5 使用浏览器抽样

仅在用户明确要求深挖、且公开搜索无法核实时使用授权浏览器。限制为少量页面和字段核验；不要批量遍历私有内容。

## 3. 证据规范化

将每条证据整理成以下结构：

```json
{
  "source": "reddit",
  "url": "https://...",
  "author": "u/example",
  "container": "r/example",
  "original_text": "short quote",
  "zh_translation": "简短中文翻译",
  "language": "en",
  "published_at": "2026-07-13",
  "date_confidence": "high",
  "observed_at": "2026-07-14T09:00:00+08:00",
  "engagement": {"comments": 42},
  "access_method": "native-platform",
  "signal_types": ["pain", "workaround", "spending"],
  "notes": "为什么与候选有关"
}
```

使用以下访问方式：

- `native-platform`
- `third-party-api`
- `search-index`
- `authorized-browser-sample`
- `manual-verification`

发布时间不确定时保留原始值并降低 `date_confidence`。互动量缺失时省略字段或写“未知”，不要填 0。

## 4. 聚类与反证

按用户任务聚类，而不是按关键词聚类。至少比较：

- 目标用户是否相同
- 触发场景是否相同
- 损失或欲望是否相同
- 当前替代方案是否相同
- 最小切入口是否相同

对每个拟进入 Top 5 的候选主动搜索：

- 已有直接和间接竞品
- 用户为何不购买已有产品
- 免费替代品
- 失败或停止维护的类似项目
- 平台和数据依赖
- 巨头近期新增能力
- 目标用户是否只想免费使用

“没有搜到”只表示证据不足，不表示不存在。

## 5. 状态提交

在评分和写 Markdown 前先用 `manage_state.py prepare` 解析稳定 ID；Markdown 校验通过后，再用共享 `run_id` 批量提交当前视图和追加式观察历史。保存每个机会用于指纹的四个字段：

```json
{
  "title": "机会标题",
  "target_user": "具体用户",
  "context": "触发场景",
  "problem_or_desire": "问题或欲望",
  "wedge": "最小切入口"
}
```

轻微改写或翻译标题不得改变稳定 ID。切入口真正变化时应创建新机会。

同一 `run_id` 重放不新增观察事件；同日不同运行可以记录新的分数与证据快照，但 `occurrences` 只按唯一 `seen_dates` 计数。历史文件只追加，当前视图可更新；写入由文件锁串行化并原子替换。

## 6. 深挖与历史回顾

深挖时扩展证据与反证，不只是扩写产品方案。历史回顾时使用：

- `first_seen`
- `last_seen`
- `seen_dates`
- `occurrences`
- 独立来源数量
- 新地区、新人群、新付费和技术变化

没有新增证据不等于降温。只有观察到讨论减少、需求被满足、竞品覆盖或用户行为转移时，才标记降温。
