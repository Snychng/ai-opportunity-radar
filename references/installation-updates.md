# 安装与更新

AOR 使用一个产品版本管理 CLI、根技能和本项目登记的子技能。当前版本为 `3.2.2`，目前登记一个根技能；稳定安装取得的版本以官方 Release 为准。运行需要 Python 3.10+ 与 macOS/Linux；安装和更新另需 Git，不引入 Python 运行时依赖。

## 安装稳定版本

先克隆仓库；已有项目目录时省略前两步：

```bash
git clone https://github.com/Snychng/ai-opportunity-radar.git
cd ai-opportunity-radar
python3 scripts/radar.py install
```

安装命令查询官方 [最新稳定 Release](https://github.com/Snychng/ai-opportunity-radar/releases/latest)，按对应的 `v主版本.次版本.修订版本` Git 标签下载独立版本。草稿和预发布不进入稳定渠道；没有稳定 Release 或无法确认发布信息时会报错，不回退到开发分支。

默认创建 `~/.local/bin/aor`，并将技能入口设为 `~/.local/share/aor/current/SKILL.md`。安装结果会返回实际命令与技能路径。命令目录尚未在 PATH 中时，可使用绝对路径，或为当前终端会话设置 PATH：

```bash
export PATH="$HOME/.local/bin:$PATH"
aor --version
aor skills --json
aor doctor --refresh
```

安装器不会修改 shell 配置。已有同名命令若不属于本次受管安装，将拒绝覆盖。宿主技能加载需要按其自身方式配置：优先引用 `current/SKILL.md`，或让宿主发现 `current` 目录；文件存在不代表宿主已经加载。

自定义位置时使用安装参数，并让宿主使用输出的路径：

```bash
python3 scripts/radar.py install \
  --home "$HOME/.local/share/aor-custom" \
  --bin-dir "$HOME/.local/bin/aor-custom"
"$HOME/.local/bin/aor-custom/aor" doctor --json
```

`--bin-dir` 是目录，命令文件仍名为 `aor`。安装目录与命令目录必须位于来源源码目录之外。安装记录会固定命令位置，重复安装不会隐式搬迁命令。

## 查看与诊断

| 命令 | 行为 |
|---|---|
| `aor` | 显示品牌导览、实际加载版本、技能与常用命令；保留技能异常提示，仅在缓存确认有新版本时提醒；不联网 |
| `aor --json` | 读取完整安装概览、提交、目录、技能和更新缓存状态；不联网 |
| `aor --version` | 输出当前产品版本 |
| `aor skills` | 按分组展示本项目技能名称、版本、说明和异常提示；不联网 |
| `aor skills --json` | 读取本项目清单登记的技能名称、路径、说明和入口状态 |
| `aor doctor --quiet` | 日常启动检查，仅在确认有新版本或本地致命错误时输出；其余情况保持安静 |
| `aor doctor --json` | 检查环境、清单、安装来源、技能入口及稳定更新；可复用有效缓存 |
| `aor doctor --refresh --json` | 跳过检查缓存，重新查询最新稳定 Release |
| `aor doctor --offline --json` | 仅检查本地与有效缓存，不联网、不写更新缓存 |
| `aor update --json` | 重新检查稳定 Release，满足条件时升级受管安装 |

普通 TTY 中，`aor` 首页包含五行圆角几何 AOR 青蓝字标、产品名、当前版本、技能和常用命令。`aor` 与 `aor skills` 的日常文本突出技能名称、版本及异常提示，技能列表补充说明，不默认展示 Git 提交 SHA 或绝对安装路径；完整信息可通过 `aor --json`、`aor skills --json` 或 `aor doctor` 查看。品牌导览只调整文本呈现，不改变 JSON 字段、更新提醒或诊断逻辑，也不增加运行时依赖。

| 输出环境 | 显示方式 |
|---|---|
| 普通 TTY | 使用分组布局；首页显示字标，内容随终端宽度自动换行 |
| 非 TTY 或 `TERM=dumb` | 使用紧凑文本，不输出 ANSI 转义序列或字标 |
| 普通 TTY 且 `NO_COLOR` 非空 | 禁用颜色，保留分组布局和编码支持的字标 |
| 输出编码不支持默认 UTF-8 符号 | 降级为可输出的文字 |

颜色按终端能力选择；无法确认背景色时使用终端自身的青蓝色盘，避免浅色背景上的亮色文字难以阅读。

`doctor --quiet` 与 `doctor --json` 不能同时使用；安静模式也支持 `--refresh` 或 `--offline`。`doctor --refresh` 与 `doctor --offline` 不能同时使用。诊断的 `health` 为 `ok`、`warning` 或 `error`；前两者退出码为 0，本地致命错误退出码为 1。网络状态未知或有可用更新通常属于 warning，应读取 `checks` 中的具体原因。

`updates.status` 的含义：

| 状态 | 含义 |
|---|---|
| `up_to_date` | 当前版本号与最新稳定版本号相同 |
| `update_available` | 最新稳定版本号高于当前版本 |
| `ahead` | 当前版本号高于最新稳定版本，例如本地开发快照 |
| `unknown` | 没有有效离线缓存、网络失败、没有稳定发布或响应校验失败，暂时不能确认 |

版本号一致不证明代码与发行标签具有相同 Git 提交。诊断另列本地 `commit`、`origin_verified` 和 `worktree_clean`，供核对来源与修改状态；不要把版本比较当作代码完整性证明。

## 调用技能时检查更新

Agent 开始使用技能时，按 `SKILL.md` 先运行 `doctor --quiet`。已是最新、当前版本领先或更新状态未知时，不输出例行版本提示，Agent 也不向用户复述“已是最新”“检查通过”等状态。有新版本时，提示真实版本号和更新命令；假设查到的新版本为 `3.2.3`，示例提示为“AOR 发现新版本 v3.2.3，可运行 `aor update` 更新。”同一次研究任务中，相同新版本只由 Agent 转述一次。

统一 CLI 和兼容业务脚本也会在启动时预检，同一进程只预检一次。提示写入标准错误，业务标准输出保持原格式；网络检查异常不改变正常业务结果或退出码。安静模式没有输出不代表一定已是最新；如需排查，使用完整 `doctor` 或 `doctor --json`。本地致命错误仍会显示并返回非零退出码。

成功结果缓存 24 小时，网络错误短暂缓存 5 分钟为 `unknown`；`--refresh` 可跳过缓存。缓存按安装路径、当前版本和提交区分，失效或损坏的缓存不用于声称已是最新版本。离线模式没有有效缓存时保持未知。

检查使用官方 GitHub HTTPS API，不添加 GitHub 令牌或 Cookie，并保留用户已有的网络代理配置。纯文档无法强制所有 Agent 执行预检；宿主需要实际运行命令。只支持聊天的宿主可读取研究方法，但必须由外部执行器运行工具。

## 显式升级

```bash
aor update
aor doctor --refresh --json
```

`update` 只适用于已登记的 AOR 受管安装，且只升级到更高的稳定版本。执行时重新核对当前指针、官方 Git 来源和工作区，准备独立候选目录，检查标签与清单版本、必要入口和离线启动，校验通过后原子切换 `current`。无法确认版本、来源不匹配或校验失败时保留现有生效版本。

受管业务命令持有共享运行锁，安装与升级使用独占锁；仍有命令运行时，升级会提示稍后重试，避免一条命令执行中切换版本。旧版本目录保留，不自动清理。锁覆盖每次命令执行，不覆盖 Agent 仅阅读文件或思考的整个会话。

更新成功后，让 Agent **重新读取返回的 `current/SKILL.md` 和本次用到的参考文件**。旧会话中已经读入的说明不会因磁盘更新自动变化；直接使用旧 `versions/` 路径也会继续加载旧版本。

不要在受管版本目录里存放研究数据、密钥或个人笔记。有本地修改、未跟踪文件或被忽略的非缓存文件时，升级会拒绝覆盖；Python 与常见工具缓存除外。保留的旧快照用于核对和恢复排查；当前没有 `rollback` 或 `uninstall` 命令。

## 源码使用与离线快照

开发时可直接执行 `python3 scripts/radar.py ...` 或仓库内的 `bin/aor ...`，无需安装。源码副本仍可诊断与研究，但 `update` 不会在源码仓库执行拉取、合并或覆盖。

如需从本地确定提交创建受管安装，可显式使用 `--source`：

```bash
python3 scripts/radar.py install --source /absolute/path/to/ai-opportunity-radar
```

此方式不查询远端；来源必须是完整、干净且 `origin` 指向官方仓库的 Git 根目录。安装器复制本地 HEAD 为独立快照，不将启动器绑定到开发目录。它只证明装入了所选本地提交，不证明该提交已发布；之后执行 `aor update` 仍使用稳定 Release 渠道。

## 目录与环境变量

下面以 `3.2.2` 展示目录结构，实际版本和提交前缀以安装结果为准：

```text
~/.local/bin/aor
~/.local/share/aor/
├── install.json
├── update.lock
├── current -> versions/v3.2.2-<提交前缀>/
└── versions/
    └── v3.2.2-<提交前缀>/
        ├── SKILL.md
        ├── agent-manifest.json
        ├── bin/aor
        ├── scripts/
        └── references/
~/.cache/aor/update-checks/
```

| 环境变量 | 用途 |
|---|---|
| `AI_OPPORTUNITY_RADAR_HOME` | 研究数据目录，默认 `~/Documents/AI-Opportunity-Radar`；独立于代码版本 |
| `AOR_INSTALL_HOME` | 安装命令未指定 `--home` 时的默认受管目录；已生成启动器使用登记的固定位置 |
| `AOR_CACHE_HOME` | 更新检查缓存目录，默认 `~/.cache/aor` |
| `AOR_OFFLINE=1` | 让诊断与业务预检只使用本地更新信息 |
| `AOR_NO_UPDATE_CHECK=1` | 跳过业务入口更新预检，适合可复现的测试运行；不关闭显式 doctor |
| `NO_COLOR` | 非空时禁用文本输出的颜色；普通 TTY 仍保留分组布局 |
| `TERM=dumb` | 使用紧凑文本，不输出 ANSI 转义序列或字标 |

`AOR_OFFLINE` 只约束更新检查，不会阻止 `community`、`paid` 等业务命令访问网络，也不会把显式 `install` 或 `update` 变成离线安装命令。本地安装应使用 `install --source`。研究数据的 `schema_version` 仍为 `3.0`，与产品版本 `3.2.2` 不同；本轮更新不迁移已有研究数据。

## 常见情况

| 情况 | 处理 |
|---|---|
| 找不到 `aor` | 使用安装输出的绝对命令路径，或将该目录加入当前会话 PATH |
| 源码或未受管副本无法 update | 在项目目录运行 `python3 scripts/radar.py install` 创建受管安装 |
| 更新状态 unknown | 查看原因；已有研究可继续，网络恢复后运行 `doctor --refresh` |
| 提示正在使用或更新 | 等当前 AOR 命令结束后重试，不删除锁文件 |
| 有本地修改 | 先将需要保留的内容保存到版本目录之外；不要用更新覆盖修改 |
| 来源或 current 指针异常 | 查看 `doctor --offline --json` 与安装记录，恢复正确来源和目录后再升级 |
| 更新后仍加载旧版本 | 使用安装输出的 current 入口，并让 Agent 重新读取技能及参考文件 |

版本变化见 [CHANGELOG](../CHANGELOG.md)，宿主接入见 [通用 Agent 集成](agent-integration.md)。
