# AOR 项目工作约定

## 入口与职责

- AOR 是由宿主 AI 判断、Python 执行确定性步骤的研究 Skill。先读 [SKILL.md](SKILL.md)，架构见 [研究引擎](references/engine-architecture.md)。
- 统一入口：`python3 scripts/radar.py COMMAND ...`；Python 3.10+，macOS/Linux，运行时使用标准库。
- 数据、证据与机会门槛见 [数据契约](references/data-contracts.md)；研究交接见 [研究工作流](references/research-workflow.md)。
- 检索执行者与原始来源分开；模型摘要、候选链接、核验原文和商业判断分别保存。多个模型找到同帖不增加独立来源。

## 修改与维护

- 在所属模块实现行为；兼容脚本保留已有 CLI 合约。核心包与 scripts 仍有兼容依赖，不假定两者完全隔离。
- 当前行为变更同步对应 references 文档、SKILL 入口和必要的 manifest；用户登录、可选能力、回退与未配置路径必须可理解、可执行。
- 重要决策保存在 [.agents/notes](.agents/notes/README.md)，按主题检索并更新，不重建一套重复架构文档。
- 不直接修改受摘要保护的运行产物；证据和观察经正式输入提交。同一研究由一个协调者提交，搜索 worker 输出独立回执。
- 已发布的证据身份、报告与研究运行保持历史可读；身份迁移写新目标，不静默重算旧引用。
- OAuth 只读接入不得刷新、轮换或写回共享凭据；付费入口须保持预算预检、累计账本及未知结果不重买。测试不得使用真实凭据或自动联系客户。
- 产品版本与内部数据、公开网站契约分别维护。改仓库或本地测试通过不代表已发布、安装、部署或市场验证。

## 验证

从仓库根目录运行，开发依赖安装在隔离环境：`python3 -m pip install -r requirements-dev.txt`。

- 定向回归：`AOR_OFFLINE=1 python3 -m unittest discover -s tests -p 'test_相关模块.py' -v`。
- CI 检查：`AOR_OFFLINE=1 python3 -m unittest discover -s tests -v`、`ruff check scripts src tests`、`AOR_OFFLINE=1 python3 scripts/evaluate_research.py`。
- CLI 变更通过真实 `scripts/radar.py` 入口验证。网络只在外部传输边界替换；测试主体使用实际实现。
- 发行包由固定 Git 提交构建：`python3 scripts/build_release.py --ref REF --output-dir OUTPUT`。HEAD 不包含工作区未提交改动；不能用旧提交的包证明新改动可安装。
- 对配置、证据、预算和中断恢复验证拒绝路径；最后检查文档链接及 `git diff --check`。真实账户、在线额度、长期刷新和终端交付未验证时明确说明。
