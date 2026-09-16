# 研究引擎维护者架构

最后更新：2026-09-16。入口为 `scripts/radar.py`；核心包为 `src/aor/`。本文描述文件交接、职责与恢复边界，不将离线测试当作商业验证或实时覆盖证明。

## 依赖与职责

```text
宿主 Agent ── JSON 文件 ── scripts/radar.py（aor）
                              │
                    scripts/research.py
                              │
                   aor.workflow.research
                    │        │         │
                 sources  evidence  opportunity
                    │        │         │
                 community   └─ storage ┘
                    │                  │
                   net          reporting.report
                                       │
                             scripts/manage_state.py
```

`src/aor` 通过 `scripts/aor_bootstrap.py` 加入导入路径；迁移是渐进的，部分领域实现仍在 `scripts`，核心包目前也会导入这些兼容模块。不要把它描述成完全独立、无反向依赖的发布库。

| 模块/入口 | 职责及主要接口 | 依赖与边界 |
|---|---|---|
| `scripts/radar.py` | 固定命令分发、安装上下文与更新预检 | 子进程参数数组；无动态 shell |
| `workflow/research.py` | `start_research/resume_research/inspect_run/run_paid_batch/postmortem` | 运行锁、产物摘要、Agent 交接、提交恢复 |
| `sources/planning.py` | `validate_intent_plan/compile_intents/deduplicate_requests` | 编译意图到受控来源计划；不判断商业主张 |
| `sources/industries.py`、`industries.json` | catalog 2.0：六方向、36 子赛道、72 查询，按实际历史/缺口轮转 | 默认排除制造和农业；不按关键词猜用户行业 |
| `sources/discovery.py`、`coverage.py` | 实时估价的发现批次、行业覆盖与零结果诊断 | 已有同能力平台价格替补、同轮 journal；coverage 2.0 按角色、语义审阅与时效分计 |
| `sources/registry.py` | `source_catalog/diagnose_sources` | 能力目录、配置存在性；不访问网络 |
| `sources/importing.py` | `import_web_evidence` | 宿主网页原文和核验声明导入；不抓网页 |
| `community.py`、`net.py`、`text.py` | 社区采集、有界网络访问、文本处理 | 由 `scripts/community_query.py` 适配 CLI；免费并发 1–4，编排默认 3、底层兼容入口默认 1 |
| `evidence/quality.py` | `assess_quality/aggregate_status/mark_reposts` | 本地相关性、窗口、来源状态及转载标记 |
| `evidence/identity.py`、`retrieval.py` | 来源身份与 RRF 检索融合 | 不以多渠道标签增加独立来源 |
| `evidence/derivations.py` | `select_active_derivations`：原始执行 SHA 与版本化完整派生集合 | 不删除源日志；解析替代与来源撤回分开，未知来源保留复核 |
| `evidence/claims.py` | `validate_claims/build_evidence_packet` | 原文位置、修订、截止时间及受容量约束的 Agent 上下文 |
| `evidence/selection.py` | 行业、来源及角色平衡抽样、紧凑元数据、完整索引与 review_queue | 保留遗漏信息；不自动证实商业语义 |
| `storage/evidence_library.py` | `EvidenceLibrary` | JSONL 观察日志与可重建 SQLite/FTS 索引 |
| `opportunity/basis.py`、`selection.py` | 评分依据引用、候选研究优先级、实验背景 | 选择元数据不自动改变证据等级或分数 |
| `opportunity/exploration.py` | AI 增量描述、探索线索及历史 | 独立 LEAD 状态，不降低 A/B/R 门槛 |
| `scripts/expand_ideas.py`、`filter_ideas.py`、`score_candidates.py` | 扩展、家族归并、硬过滤、A 级计算 | 保留旧入口；语义与评分依据由 Agent 提供 |
| `request_identity.py`、`paid_execution.py` | 请求指纹、选择尝试、执行与结果复用 | 串行执行；TikHub 实时价格、白名单和账户预检由兼容脚本提供 |
| `storage/request_journal.py` | `RequestJournal` | SQLite 请求状态及同 run 跨批累计费用 |
| `reporting/public.py`、`public_contract.py` | 白名单网站导出及 public 1.0.0 校验 | 精确引用/发布复核、列表与详情分离；不暴露内部响应 |
| `reporting/report.py` | `build_report/validate_structured_report/render_report/commit_report` | 同一个结构化对象用于校验、展示和提交 |
| `scripts/manage_state.py` | 稳定 OPP/SIG、观察历史、事务恢复和升级 | 与证据库和付费 journal 各自负责不同持久状态 |
| `scripts/manage_validation.py` | 个人约束评估、实验日志 | 仅本地记录，不执行客户实验 |
| `scripts/evaluate_research.py` | 固定离线质量场景评估 | 使用注入的模拟传输，不表示在线覆盖 |

表中包内相对路径均相对 `src/aor/`。运行时使用 Python 标准库，包括 `sqlite3`、`fcntl`、`urllib`；没有新增模型服务依赖。可选凭证仅用于在线来源，不参与文档或证据存储。

## 运行状态与产物

```text
planned → awaiting_benchmarks → awaiting_assessment → committing → completed
                    ↑
             显式付费补证返回交接
```

history-context 在社区采集前建立；刷新后的 evidence-context/evidence-packet 包含本轮与相关历史证据。历史复用不默认跳过实时检索，也不写成当前来源健康。

状态不是必经的 UI 页面；输入齐全时同次调用可以连续推进。`inspect` 返回 `next_action`、`input_template`、`artifacts`、`stages` 和报告路径。各阶段产物以 canonical JSON SHA-256 登记，直接改落盘产物会在读取时拒绝；修订须作为 resume 输入提交。

```text
DATA_HOME/
├── runs/RUN_ID/
│   ├── run.json                         运行清单、阶段、摘要和交接信息
│   ├── plan.json / *-plan.json           查询计划
│   ├── evidence-*.json                   导入证据
│   ├── history-context.json             采集前历史证据上下文
│   ├── evidence-context.json            截止日内完整检索上下文
│   ├── evidence-packet.json              交给 Agent 的有界原文包
│   ├── evidence-index.json               全部上下文条目与是否入包
│   ├── industry-coverage.json            逐方向/任务/语言实际覆盖、角色与时效缺口
│   ├── industry-packets.json             各方向独立阅读容量
│   ├── research-followup.json            待审阅队列与任务缺口
│   ├── research-lead-history.json        截止研究日的最新探索线索
│   ├── awaiting_*-template.json          当前交接模板
│   ├── benchmarks.json / expanded.json / tiered.json
│   ├── assessment.json                  Agent 判断与评分输入
│   ├── paid-journal.sqlite3              使用付费入口后建立
│   ├── report.json / report.md / summary.md
│   ├── public/                          白名单公开数据集、列表与详情
│   └── receipt.json                     提交回执与摘要
├── evidence-library/
│   ├── evidence.jsonl                   追加式观察事实源
│   └── evidence.sqlite3                 可重建检索索引
├── state/                              OPP/SIG 当前视图、历史及实验事件
├── raw/YYYY-MM-DD/                      兼容手动采集路径
└── reports/daily/                       兼容手写日报路径
```

新研究运行 ID 每次唯一；同日重复启动不会覆盖旧运行。候选 ID 仍按业务身份稳定解析。父运行通过 `--parent-run-id` 关联，不意味着自动复制全部旧判断。

`committing` 已固定报告，恢复时不接收新输入；用原运行 `resume` 重放。OPP/SIG 分别提交，不能承诺两种记录跨文件一次原子成功；底层事务和报告摘要支持中断后幂等补齐。`completed` 仅表示本轮文件提交与导出结束，research_quality 仍可显示覆盖不足，不能解释为市场研究或客户验证完成。`completed` 后研究输入保持不可变，补证另开研究运行；不带新输入 resume 可从已校验报告重新渲染丢失的 Markdown 展示。

## 三类日志不得混用

- 运行清单：记录编排阶段及产物，不代替证据原文。
- 证据库：保留来源身份、正文修订、观察日期及历史复用；SQLite 索引可以重建，JSONL 不能当可丢缓存。
- 付费 journal：发送前登记尝试并保留累计原价成本，成功结果复用，未完成尝试恢复为 `outcome_unknown`；新批不重置总预算。

状态模块另有写前事务日志，处理 OPP/SIG 当前视图和历史写入。不要用删除任一种 journal 的方式消除冲突或重置费用。

## 验证边界与维护入口

新内部 report_version=1.1，旧 1.0 按历史规则审计；公开契约独立使用 1.0.0。`report.json` 的分层、稳定 ID、计数、A 级计算、决策字段及引用由程序校验；`market_validated` 必须为 `false`。报告展示主张及评分依据，`score_basis` 保留 rationale 与 evidence_refs。引文定位成功只说明引用存在于对应原文修订，语义判断仍由宿主负责。report.json 保留 evidence_inventory 与整轮 run_ledger，统计校验及 Markdown 渲染均从结构化输入重算；旧 digest_markdown 不是事实来源。Markdown 是展示，不是新提交协议。

`--offline` 停止编排采集；严格离线会话设置 `AOR_OFFLINE=1`，覆盖入口更新预检。付费估价和执行属于显式联网能力。`sources diagnose` 的配置存在性、社区请求 `ok`、离线 eval 的 `passed` 都不能单独证明真实证据或平台完整覆盖。

日常维护先运行 [离线示例](quick-start.md#可运行离线示例)，再根据改动检查相关测试、`aor eval`、内部链接及 CLI 参数。契约细节见 [数据契约](data-contracts.md)、[来源模块说明](../src/aor/sources/README.md)、[证据库](evidence-library.md)、[付费恢复](tikhub-integration.md)与[报告契约](report-template.md)。


## 修复与兼容边界

对象身份 2.0 将帖子与评论拆开，以 native identity 关联采集副本和库快照；新语义审阅绑定对应修订。旧库只通过 `library migrate-identities --destination` 派生迁移，源 JSONL 不覆盖。`resume --reparse` 复用父运行已存响应创建离线子运行，不重购、不断言新来源健康，父运行费用独立保留。

内部 report、公开 contract、目录/覆盖、LEAD 和发行包各自版本化。发行 4.1.0 不表示服务器任务或已安装 skill 自动更新；真实安装和发布需独立检查。网站只读取 [公开契约](website-contract.md)，不绑定内部 execution_results、raw_refs 或原始评论结构。

## 评论优先发现（4.3）

`sources/products.py` 编译均衡的产品搜索意图；`sources/comments.py` 根据已保存响应推进分页/子回复并推导覆盖计数；`workflow.research.run_comment_collection` 持久化状态和逐轮计划，独立队列锁防并发推进，每轮仍经 `run_paid_batch` 的实时价格、账户和共享预算预检。`opportunity/needs.py` 核验用户观察原文并归组需求簇；report 构建/验证共用统计函数，私有观察按 run_id 幂等提交，public 只导出统计。解释语义和确认产品关联仍由宿主负责。
