# 快速开始

安装 AOR，并完成一次可恢复研究或离线演示。

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

让宿主读取安装返回的 `current/SKILL.md`；默认入口为 `~/.local/share/aor/current/SKILL.md`。源码使用者可直接读取仓库根 [SKILL.md](../SKILL.md)，并将下文 `aor` 替换为 `python3 scripts/radar.py`。使用 `COMMAND --help` 核对参数。

数据目录由 `AI_OPPORTUNITY_RADAR_HOME` 指定，默认 `~/Documents/AI-Opportunity-Radar`；研究命令可用 `--home` 覆盖。`GITHUB_TOKEN` 可选，`TIKHUB_API_KEY` 仅供授权付费查询使用，凭证不要写入研究文件。

`doctor --quiet` 仅提示确认的新版本或本地致命错误，安静不等于版本已确认最新。`aor update` 显式升级受管安装，随后重新读取技能及本次参考文件。源码仓库不会被更新器拉取或覆盖。更多说明见 [安装与更新](installation-updates.md)。

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

`research --offline` 不执行研究采集，离线标记保存在该运行中；带 `--offline` 的调用同时跳过更新预检；整段离线工作流建议设置 `AOR_OFFLINE=1`，使 sources、library、report 等后续调用也不进行联网预检。`--no-collect` 用于只处理已有材料，不应当作全局网络开关。离线模式不会替宿主执行网页研究，也不支持付费补证。可选 `research --include-comments --include-recent-activity` 分别启用社区评论和旧帖近期活动查询；默认不启用，离线时也不采集。`--concurrency` 为免费检索并发数（1–4，默认 3），付费执行仍串行。详见 [研究工作流](research-workflow.md)。

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
