# agent-matrix：会议与待办中心

> 许可：PolyForm Noncommercial 1.0.0，**禁止商业使用**，详见文末。

把**会议、聊天、飞书任务**里冒出来的事，自动收进一张飞书多维表格。AI 能做的先做掉，结果交给你验收。每天用一屏卡片告诉你：今天要做什么、等你验收什么、等你拍板什么。

设计原则：

- **能用代码做的不用模型。** 接收推送、过滤、同步、生成简报、处理回复，全是代码。模型只在三件事上被唤醒：读会议逐字稿、读筛过的聊天、代做文档。
- **AI 像员工一样接单。** 本机"AI 员工"默认最多同时做 10 件事，每件事有自己的文件夹和自己的 GPT 对话：先由代码备好背景（会议原话前后文、来源聊天、相关文档、做过的同类事项、你给过的链接），不够它自己去扒，扒够了再写；做完代码自检、建文档、发卡片。你后来补的要求追加进同一个对话，接着上次往下改。
- **宁缺毋滥。** 一条待办必须有原话里承接的字眼，负责人要么是说话的人，要么在原话里被点了名，日期必须写明。没有待办的会议不发任何消息。
- **你是最后一关。** AI 做的事只有你回"过"才算完成。每次写表都会读回核对，结果不确定时不会自动重试。

## 查看事项和 AI 原对话

- 事项详情里的「写备注」只留在本机；「备注并完成」同时记录完成人和时间。旧事项没有操作人记录时会如实显示。
- 左栏同时显示心响已领取的工作，以及本机 Cindy、Codex 和 Claude 有近期运行信号的会话。已有会话编号能对应事项时直接显示事项号；其余可点「关联事项」选择，关联保存在这台电脑，结束后可从详情返回原对话。没有运行信号不算在做；没有向心响登记的外部 AI 不会被当成领了任务。
- 「发给 AI」区分记录、排队、接手和等待补充。设置里可以管理 Slack、事项存放位置、本机 AI 和聊天整理频率。

## 心响：本机客户端

所有消息和操作都在本机的"心响"里，事项也存在本机（点了立刻生效），飞书多维表格在后台做备份；飞书消息只收早报和晚报。架构见 [心响架构.md](心响架构.md)。

- **打开方式**：Mac 菜单栏点"打开心响"，或者在聚焦搜索里搜"心响"；Windows 托盘双击。界面地址是 `http://127.0.0.1:47865/`，只对本机开放。
- **首页**：一条流程"新进来 → 在做 → 等你验收 → 完成"，下面是选中那一段的列表，每行一个主按钮。
- **点开一件事**：一条故事线，依次是从哪来、AI 对话和每一版交付、现在等你做什么。可以接着和 AI 说，AI 会在同一个对话里往下做。
- **"新进来"开关"AI 自己判断能不能做"**：打开后，AI 先看后续聊天：有完成证据的直接关掉，不用做的放下，能做的接手，要你拍板的先备材料。
- **停止与恢复**：新进来、正在做的事项都能点「不做」或「已完成」。本机驱动会停止当前运行及其命令；外部驱动下次调用会收到 `cancelled`，必须停止。点「恢复」回到新进来，历史对话和交付继续保留。
- **数据存放**：对话、交付版本、判断记录存在本机 `state/xinxiang.db`，消息存在 `state/inbox.db`，都不进 Git。
- **改回飞书通知**：在 `config.yaml` 里写 `notify_channel: feishu`。
- **谁来驱动，不限**：任何 AI 都能接入。用 `~/.agent-matrix/bin/xinxiang next --driver "名字"` 领活，在交回的文件夹里干活，再用 `xinxiang submit --job ID` 交活；完整说明运行 `xinxiang guide` 查看，或在心响系统页一键复制。本机 GPT 是内置的驱动，也可以在系统页关掉。

## 架构

```
飞书推送（实时）                  定时（兜底）              AI 员工（worker.py，只在有活时）
├ 私聊回复  ─┐                   ├ 08:30 早报 / 21:00 晚报   最多同时十件，一件一个 GPT 对话：
├ 会议结束   ├→ listen（常驻）    ├ 06:00 周期事项           ├ 逐字稿 → 待办
├ 逐字稿生成 │   纯代码 ──叫醒──→ ├ 聊天每 2 分钟读一次   ───→├ 聊天候选 → 待办
├ 卡片按钮   │                   └ 每 2 分钟看一次队列       └ 代做：找背景 → 写 → 自检 → 交付
└ 任务变化  ─┘
                 ↓
        飞书多维表格（事项表）  →  卡片：简报 / 会前准备 / 做完了 / 周五复盘（点「✓ 通过」卡片原地变成已完成）
                 ↑
        你回复："15 过""8 周五""18 AI备""44 先做""展开 12"
```

| 模块 | 作用 |
|---|---|
| `listen.py` | 常驻监听：处理私聊回复、会议结束、逐字稿生成、任务变化；每分钟检查会前卡片、补发和周五复盘 |
| `reply.py` | 回复指令解析和写表，也负责飞书私聊轮询（兜底） |
| `brief_v1.py` | 早晚简报卡片：一屏读完，分"你要做 / 等你看 / 需要你定 / AI 在做" |
| `meeting_watch.py`、`meeting_review.py` | 会议队列；逐字稿提取结果的校验与入表 |
| `sources/chats.py`、`chat_review.py` | 聊天读取、代码过滤；定时批量提取 |
| `sources/feishu_tasks.py` | 飞书任务同步进事项表（只读飞书任务，不回写） |
| `worker.py` | AI 员工：最多并发十件、每件一个 GPT 对话（`codex exec`）；自检交付、建文档、回写表格、发卡片 |
| `background.py` | 背景包：纯代码先拉会议原话、来源聊天、相关文档、同类事项、你给过的链接 |
| `prework.py` | 代做队列与交付校验：模板栏目、占位文字、篇幅；文档读回核对 |
| `cards.py` | 卡片原地更新：点了按钮，那一行变成"✓ 已完成 14:52" |
| `agents/meeting_prep.py`、`agents/weekly_review.py` | 会前准备卡、周五复盘卡（纯代码） |
| `agents/recurring.py` | 周期事项（RRULE），提前生成 |
| `workflow.py` | 事项表读写网关：写前重读，写后读回 |

## 事项模型

每件事都有：编号（自增，不复用）、执行方（我 / AI / 别人）、状态、进度（步骤式，15 字以内）、卡点、完成标准、交付物链接、验收结果、顺延历史。

状态共 7 种：待确认 → 未开始 → 进行中 → 卡住 → 待验收 → 完成 / 取消。

## 回复指令

| 你回复 | 效果 |
|---|---|
| `15 过` / `15 19 过` | 验收通过；AI 子项通过后，自动解锁你那一步 |
| `19 退 少了定价` | 退回修改，意见记入事项 |
| `8 周五` / `8 11-20` / `8 改到下周三` | 改日期，记录顺延 |
| `12 完成` / `12 不做` | 完成 / 取消；停止正在执行的 AI |
| `12 恢复` | 撤回误标，回到新进来，保留历史 |
| `12 我来` / `12 AI做` / `12 给张三` | 改执行方 |
| `18 AI备` | AI 先备材料，你验收后再开始 |
| `20 + 材料` | 补材料，卡住的事接着做（同一个对话里接着改） |
| `44 继续` | 卡住的事让 AI 按现有资料接着做 |
| `44 先做` | 插队，AI 员工下一件就做它 |
| `展开 12` | 看详情和原话 |

## 按自己的情况配置

每一项都是可选的，在首次打开的引导或「设置」里配，不用改文件：

| 配什么 | 可以选 |
|---|---|
| 办公工具 | 飞书（会议、聊天、飞书任务可以分别开关）、Slack，或者都不接，事项在心响里自己新建 |
| 别人找你的事怎么处理 | 全能模式（都接进来，AI 直接做）、全自动模式（你答应了才接）、手动模式（都不接） |
| 谁来做事 | Cindy、本机 GPT、本机 Claude，或者接入别的能运行命令的 AI |
| 事项存在哪 | 本机 + 飞书多维表格备份、本机 + Notion 同步、只存本机 |
| 通知 | 都在心响里；接了飞书的可以让早晚简报同时发到飞书 |

不接任何办公工具时，把 `config.yaml` 里的 `item_store` 设为 `device` 即可，不需要飞书账号。

## 安装

需要 Python 3.11+、已登录的 `lark-cli`（npm 包 `@larksuite/cli`）、已登录的 GPT 命令行 `codex`（AI 员工用它干活；找不到时在 `config.yaml` 写 `worker_codex: <路径>`），以及一个能跑定时脚本的宿主（launchd、Windows 计划任务或任何 AI 宿主都行）。

AI 员工启动 GPT 时不加载你的插件、MCP 和钩子（`--ignore-user-config`），只沿用你 codex 配置里的服务商设置；工作文件夹在纯英文路径下（项目路径含中文时放在 `~/.agent-matrix/`）。本机 AI 可以在心响「系统」页切换成 Claude（用本机的 `claude` 命令行，同样不加载你的插件、钩子、MCP 和 CLAUDE.md，只能写工作文件夹，可以联网）；对应可调项 `worker_claude`（命令路径，默认从 PATH 找）、`worker_claude_model`（如 opus、sonnet，默认用账号默认模型）。GPT 的可调项：`worker_model`（默认 gpt-6-astra）、`worker_effort`（默认 high）、`extract_effort`（默认 medium）、`worker_timeout_minutes`（默认 30）、`worker_concurrency`（默认 10）、`worker_enabled`。

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml config.yaml
.venv/bin/python -m agent_matrix setup          # 建多维表格，写回 config.yaml
.venv/bin/python -m agent_matrix.brief_migration setup-fields && .venv/bin/python -m agent_matrix.brief_migration apply
.venv/bin/python -m agent_matrix ping           # 给自己发一条测试消息
./launchd/install-listen.sh                     # 安装常驻监听（macOS）
```

实时推送要在飞书开放平台订阅这几种事件：`vc.meeting.participant_meeting_ended_v1`、`vc.note.generated_v1`、`task.task.update_user_access_v2`；卡片按钮要在"事件与回调 → 回调配置"里开启 `card.action.trigger`。未订阅时，`lark-cli event consume` 的报错里会直接给出订阅链接。没订阅也能用，只是退回成定时检查。

**事项记在哪**：心响「系统 → 事项记在哪」可选飞书多维表格、Notion 或只存本机（`item_store: local / notion / device`）。选 Notion 或本机时完全不需要飞书：飞书会议、群聊、任务这几个来源随之关闭，Slack 照常。Notion 要一个 Internal integration 的 Secret 和一个共享给它的页面，心响在页面下建「心响事项」数据库并单向同步，在 Notion 里改的内容会被下一次同步覆盖。

**Slack（可选）**：在 `config.yaml` 写 `slack_token: xoxp-...`（或设环境变量 `SLACK_USER_TOKEN`），常驻监听每分钟用搜索拉一次新的 Slack 消息，按和飞书聊天一样的规则过滤，到整理时间一起提取，来源链接直接跳回那条 Slack 消息。用户 token 需要这些权限：`search:read`（一次搜出所有新消息；没有它就退回逐个会话读，只读前 25 个）`channels:history` `groups:history` `im:history` `mpim:history` `channels:read` `groups:read` `im:read` `mpim:read` `users:read`。

宿主需要配置的定时任务：

| 时间 | 方式 | 命令 / 说明 |
|---|---|---|
| 06:00 | 只跑脚本 | `python -m agent_matrix.scheduled recurring` |
| 工作日 08:30 / 21:00 | 只跑脚本 | `python -m agent_matrix.scheduled morning` / `evening` |
| 每 2 分钟（可选） | 只跑脚本 | `python -m agent_matrix.precheck`：处理回复、确保 AI 员工在跑。常驻监听已经每 2 分钟叫醒 AI 员工，这一项只是兜底；它永远不需要宿主唤醒模型 |

## 常用命令

```bash
.venv/bin/python -m agent_matrix brief --morning --preview     # 预览简报，不发送
.venv/bin/python -m agent_matrix.reply --text "15 过"           # 处理一条指令
.venv/bin/python -m agent_matrix.listen --status                # 监听连接状态
.venv/bin/python -m agent_matrix.worker status                  # AI 员工正在做哪件、做到哪、排队顺序
.venv/bin/python -m agent_matrix health                         # 授权与表格自检
.venv/bin/python -m unittest discover -s tests                  # 测试
tools/e2e/run.sh                                                # 端到端演练：复制线上数据到临时目录，模拟推送跑全部链路，不碰线上
node tests/test_client.cjs && node tests/test_client_live.cjs    # 页面交互与断线重试
```

## 隐私

`config.yaml`（账号与表格 ID）、`state/`（逐字稿、聊天候选、收据）、`logs/`、生成的文档和验收记录都已加入 `.gitignore`，不进版本库。消息只发给本人。聊天只在本地过滤，只有筛下来的少量句子才会交给模型。AI 员工的工作文件夹（任务单、背景、交付草稿）在本机 `state/work/` 或 `~/.agent-matrix/`，同样不进版本库。

## Windows

需要 Windows 10/11、Python 3.11+、Node.js，以及已登录的 `lark-cli`（`npm install -g @larksuite/cli`，再运行 `lark-cli auth login`）。

```powershell
git clone https://github.com/MILESMAKMAK/agent-matrix.git
cd agent-matrix
powershell -ExecutionPolicy Bypass -File windows\install.ps1
```

安装脚本会建好 `.venv` 并装上依赖（含托盘所需的 `pystray`、`Pillow`），然后在"任务计划程序"里注册：

| 任务 | 时间 | 作用 |
|---|---|---|
| AgentMatrix Listener | 登录时启动，崩溃后 1 分钟自动重启 | 实时监听（同 macOS 的 launchd） |
| AgentMatrix Tray | 登录时启动 | 任务栏右下角的托盘监控 |
| AgentMatrix Recurring | 每天 06:00 | 周期事项 |
| AgentMatrix Morning Brief | 工作日 08:30 | 早报 |
| AgentMatrix Evening Plan | 工作日 21:00 | 明日计划 |

托盘图标颜色：蓝 = 有事需要你，绿 = 一切正常，橙 = 要注意，红 = 出错了。点开能看"需要你 / 正在进行 / 今天发生了什么"，可以打开完整页面和飞书看板；每发一条消息、每出一次错都会弹系统通知。

首次使用还要按上面"安装"一节，运行一次 `setup`、`brief_migration setup-fields`、`brief_migration apply`。卸载计划任务：`powershell -ExecutionPolicy Bypass -File windows\uninstall.ps1`（不删数据）。

**AI 员工（任何系统都一样）**：常驻监听每 2 分钟会叫醒它，有活就做，没活就退出，不需要另配 AI 宿主。如果想再加一道兜底，可以让任意定时器在项目目录运行 `python -m agent_matrix.precheck`（退出码 2 = 正常，1 = AI 员工没能启动）。

## 本地监控（macOS 菜单栏）

```bash
./macapp/build.sh        # 编译安装到 ~/Applications/心响.app，开机自动启动
```

- 菜单栏图标：✓ 正常，⚠ 有地方要注意，✕ 出错了。
- 点开能看到：今天发给你的每条消息和时间、各环节最后一次运行时间、排队数量、飞书推送连接状态。按钮可以打开监控页面、打开看板、立即检查、发测试消息、重启监听。
- 每发出一条飞书消息、每出一次错，都会弹一个 macOS 通知。
- 数据来自 `python -m agent_matrix.monitor --json`，发送和报错记录在本地的 `state/activity.db`，只读、不联网、不调用模型。

## 许可：禁止商业使用

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE.md)。

- ✅ 允许：个人学习、研究、自用、修改，以及学校、公益组织、政府机构等非商业组织使用；转发或分发时须附上许可证全文和版权声明。
- ❌ 禁止：任何商业用途，例如拿去卖、集成进收费产品或服务、在公司里为营利目的使用。
- 需要商用授权，请通过 GitHub 联系作者。

以上是中文摘要，具体以 [LICENSE.md](LICENSE.md) 英文原文为准。

## 设计文档

- `SPEC.md`：最初的整体方案
- `BRIEF-SPEC.md`：简报、状态、执行方、回复指令的规范
- `FIX-待办与报告质量.md`：解决硬凑待办、报告做不完、写成小作文的修复单

心响里的操作会立即显示接收和处理进度，完成后直接使用飞书读回结果更新页面。交付文件在后台查找；连续点击、刷新、断线重试共用同一操作记录。失败时点「核对并重试」，先检查上次是否已保存。
