# AI Opportunity Radar

当前产品版本：**4.0.0**。研究流程升级为可恢复引擎，数据与评分协议仍为 3.0；旧命令继续兼容。

面向通用 AI Agent 的创业机会研究与个人验证工具。先核对收费对标、需求行为和地区差异，再扩展候选、检查证据、安排个人验证。A/B/R 是研究分层：A 为深度候选，B 为**收费对标支持的候选**，R 为区域迁移假设；都不代表你的产品已经获得客户验证。

程序负责计划、采集、证据索引、过滤、稳定 ID、评分计算、报告校验和状态提交；宿主 Agent 负责原文核验、商业主张、反证、评分依据及行动判断。项目不内置模型服务，也不会自动联系客户、报价或收款。

## 环境与安装

- Python 3.10+，macOS 或 Linux；文件锁使用 `fcntl`，不支持原生 Windows。
- 运行时使用标准库；稳定安装与升级需要 Git 和 GitHub 网络连接。
- 开发检查使用 `pytest`、`ruff`。

```bash
git clone https://github.com/Snychng/ai-opportunity-radar.git
cd ai-opportunity-radar
python3 scripts/radar.py install
export PATH="$HOME/.local/bin:$PATH"
aor skills
aor doctor --quiet
```

让宿主读取安装返回的 `current/SKILL.md`；默认入口为 `~/.local/share/aor/current/SKILL.md`。源码使用者可直接读取仓库根 [SKILL.md](SKILL.md)，并将下文 `aor` 替换为 `python3 scripts/radar.py`。使用 `COMMAND --help` 核对参数。

数据目录由 `AI_OPPORTUNITY_RADAR_HOME` 指定，默认 `~/Documents/AI-Opportunity-Radar`；研究命令可用 `--home` 覆盖。`GITHUB_TOKEN` 可选，`TIKHUB_API_KEY` 仅供授权付费查询使用，凭证不要写入研究文件。

`doctor --quiet` 仅提示确认的新版本或本地致命错误，安静不等于版本已确认最新。`aor update` 显式升级受管安装，随后重新读取技能及本次参考文件。源码仓库不会被更新器拉取或覆盖。更多说明见 [安装与更新](references/installation-updates.md)。

## 首选研究流程

```text
research → evidence-packet → Agent 核验与 benchmarks
         → resume → tiered → Agent assessment
         → resume → report.json 校验 → 本地 commit → completed
```

```bash
aor research --date 2026-09-10 --home "$RADAR_HOME"
# 从返回 JSON 取得真实 RUN_ID 和 input_template，不自己编造运行 ID。
aor inspect "$RUN_ID" --home "$RADAR_HOME"
aor resume "$RUN_ID" --home "$RADAR_HOME" --benchmarks "$BENCHMARKS_FILE"
aor resume "$RUN_ID" --home "$RADAR_HOME" --assessment "$ASSESSMENT_FILE"
```

以上变量代表本次实际目录、返回值和已核验输入。首次启动会生成计划，先读取历史上下文，再按采集选项补充免费社区资料、刷新证据包并停在 `awaiting_benchmarks`。Agent 读取 `evidence-packet.json`，核对原文后填写对标及扩展维度。提交对标后程序扩展、过滤、准备 OPP/SIG，停在 `awaiting_assessment`；Agent 按模板提供 A 级 `score_basis` 评分依据、主张、最大未知项、下一步与停止条件，再恢复完成。

`resume --assessment` 会校验并提交本地研究状态，不是预览命令。最终产物在 `--home/runs/RUN_ID/`：`report.json` 是校验与提交对象，`report.md` 和 `summary.md` 用于展示，`receipt.json` 保存提交回执。报告保留 `run_ledger`（整轮费用和尝试状态）及 `evidence_inventory`，Markdown 从结构化输入重算并展示 claims 与 `score_basis`。空结果填写 `empty_reason`，不为完成流程凑造候选。

`research --offline` 不执行研究采集，离线标记保存在该运行中；带 `--offline` 的调用同时跳过更新预检；整段离线工作流建议设置 `AOR_OFFLINE=1`，使 sources、library、report 等后续调用也不进行联网预检。`--no-collect` 用于只处理已有材料，不应当作全局网络开关。离线模式不会替宿主执行网页研究，也不支持付费补证。可选 `research --include-comments --include-recent-activity` 分别启用社区评论和旧帖近期活动查询；默认不启用，离线时也不采集。`--concurrency` 为免费检索并发数（1–4，默认 3），付费执行仍串行。详见 [研究工作流](references/research-workflow.md)。

## 可运行离线示例

在仓库根运行。所有示例材料明确含 `is_demo: true`，域名与业务内容均为虚构；该路径演示空结果交接、报告校验和幂等提交，不证明商业需求或在线来源覆盖。

```bash
export AOR_OFFLINE=1
AOR_DEMO_HOME="$(mktemp -d "${TMPDIR:-/tmp}/aor-demo.XXXXXX")"
# intent_plan 严格只接收 intents；演示包装中的 is_demo 不作为 API 字段。
python3 -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["intent_plan"],open(sys.argv[2],"w"),ensure_ascii=False)' \
  examples/intent-plan-demo.json "$AOR_DEMO_HOME/intent-plan.json"
python3 scripts/radar.py research --offline --date 2026-09-10 \
  --home "$AOR_DEMO_HOME" --intent-plan-file "$AOR_DEMO_HOME/intent-plan.json" \
  > "$AOR_DEMO_HOME/start.json"
AOR_DEMO_RUN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$AOR_DEMO_HOME/start.json")"

python3 scripts/radar.py sources import --input examples/web-import-demo.json \
  --run-id "$AOR_DEMO_RUN_ID" --as-of 2026-09-10 \
  --output "$AOR_DEMO_HOME/web-evidence.json"
python3 scripts/radar.py resume "$AOR_DEMO_RUN_ID" --offline \
  --home "$AOR_DEMO_HOME" --evidence "$AOR_DEMO_HOME/web-evidence.json"
# 阅读返回的 evidence-packet；示例选择“没有真实合格对标”。
python3 scripts/radar.py resume "$AOR_DEMO_RUN_ID" --offline \
  --home "$AOR_DEMO_HOME" --benchmarks examples/research-empty-benchmarks-demo.json
python3 scripts/radar.py resume "$AOR_DEMO_RUN_ID" --offline \
  --home "$AOR_DEMO_HOME" --assessment examples/research-assessment-demo.json
python3 scripts/radar.py inspect "$AOR_DEMO_RUN_ID" --home "$AOR_DEMO_HOME"
python3 scripts/radar.py report "$AOR_DEMO_HOME/runs/$AOR_DEMO_RUN_ID/report.json" --json
# 同一份有效报告可以重放提交；不会新增重复观察。
python3 scripts/radar.py report "$AOR_DEMO_HOME/runs/$AOR_DEMO_RUN_ID/report.json" \
  --commit --home "$AOR_DEMO_HOME"
```

保留输出目录便于检查，结束离线会话时可 `unset AOR_OFFLINE`。如需演示非空候选扩展，使用已有演示输入：

```bash
AOR_OFFLINE=1 python3 scripts/radar.py expand \
  --input examples/benchmarks-and-dimensions.json --output "$AOR_DEMO_HOME/expanded.json"
AOR_OFFLINE=1 python3 scripts/radar.py filter \
  --input "$AOR_DEMO_HOME/expanded.json" --output "$AOR_DEMO_HOME/tiered.json"
```

演示候选不能进入真实 A 级；报价变体保留在同一机会家族中。新编排输入应使用本轮模板，不能直接把旧示例 `run_id` 当成本轮 ID。

## 来源与查询意图

```bash
AOR_OFFLINE=1 aor sources catalog
AOR_OFFLINE=1 aor sources diagnose
```

`catalog` 区分来源、提供商、能力、费用、语言地区倾向及人工导入边界；`diagnose` 只检查配置存在性，`live_health=not_checked` 不代表接口可用。自动适配范围是 HN、GitHub Issues 和受控的 12 个 TikHub 平台。其他站点可由宿主核验网页后通过 `sources import` 导入，不能写成自动采集已覆盖。

结构化 `intent_plan` 将“想回答的问题”“发送给来源的检索词”“本地排序问题”分开。使用 `research --intent-plan-file FILE` 或 `plan --intent-plan-file FILE`；HN/GitHub 使用英语检索词，缺失时返回待宿主补词，不拼接中文主题冒充有效检索。字段与人工 web 导入示例见 [来源目录](references/source-catalog.md)、[查询模式](references/query-patterns.md)。

## 付费补证与恢复

先免费研究、明确候选缺口，再 `paid build-gaps → paid estimate → paid run → normalize`。估价会读取实时价格，执行需要显式预算与账户预检；不属于离线路径。计划本身不构成购买授权。

```bash
aor paid run --plan "$PAID_PLAN" --max-cost-usd 0.10 \
  --journal "$PAID_JOURNAL" --batch-id gap-1 --output "$PAID_RESULT"
# 恢复同一计划、批次和 journal；使用新的结果文件保留每次调用。
aor paid run --plan "$PAID_PLAN" --max-cost-usd 0.10 \
  --journal "$PAID_JOURNAL" --batch-id gap-1 --resume --output "$RESUME_RESULT"
```

同 run 的所有补证批共用 journal，`--max-cost-usd` 是累计原价尝试费用上限，不是每批重新充值的额度。新计划使用新 `--batch-id`，已成功请求复用；`outcome_unknown` 可能已经计费，恢复不会自动重买。请求级重试授权与编排的 `resume --paid-plan` 入口见 [TikHub 与费用控制](references/tikhub-integration.md)。

## 本地证据库与离线评估

`library` 提供 `ingest/search/context/delta/rebuild`；JSONL 保留原始观察和修订，SQLite 是可重建索引。`--root`、`--output` 放在子命令之前：

```bash
AOR_OFFLINE=1 aor library --root "$AOR_DEMO_HOME/evidence-library" \
  --output "$AOR_DEMO_HOME/context.json" context --as-of 2026-09-10 \
  --run-id "$AOR_DEMO_RUN_ID" --query "support" --limit 10
AOR_OFFLINE=1 aor eval --output "$AOR_DEMO_HOME/eval.json"
```

`context` 供 Agent 阅读，原文引用还需对应修订与截止日期；检索相关性不证明主张成立。`eval` 使用离线固定材料报告版本、输入摘要和逐项结果，不代表真实平台健康或在线召回率。详见 [证据库与评估](references/evidence-library.md)。

## 兼容入口与个人验证

既有 `plan/community/paid/normalize/expand/filter/score/state/digest/report/validation` 和独立 `scripts/*.py` 入口继续使用。手动链路仍可 `filter → state prepare → score（仅 A）→ digest`；不要把整个 tiered 包装传成单个候选。

`aor report old-report.md --json` 继续校验旧 Markdown 格式。新报告使用 `aor report report.json --json`，显式提交用 `--commit --home DATA_HOME`；不要再把新生成的 Markdown 当状态提交依据。

结合 [个人约束示例](examples/founder-profile.json) 与 [个人验证指南](references/personal-validation.md) 安排一个主验证项目。`validation assess` 的 `validate/clarify/park` 是个人执行判断；`planned` 实验只代表计划，不等于真实任务、接受报价或付款。

## 架构与维护

`src/aor/` 按来源、证据、机会、工作流、报告和存储分工；`scripts/` 保留 CLI 适配及兼容实现。入口、依赖方向、落盘结构与恢复约束见 [维护者架构](references/engine-architecture.md)。

- [数据契约](references/data-contracts.md)：阶段数据、稳定身份和主张引用。
- [机会政策](references/opportunity-policy.md)与[评分](references/scoring.md)：A/B/R 门槛及评分依据。
- [报告契约](references/report-template.md)：结构化报告与旧格式兼容。
- [宿主接入](references/agent-integration.md)与[安全边界](references/safety-and-legality.md)。

```bash
AOR_OFFLINE=1 python3 -m pytest
ruff check scripts src tests
```

离线检查通过只证明对应程序行为。真实证据、客户实验、在线访问和生产交付应分别核验。
