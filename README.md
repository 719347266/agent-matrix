# agent-matrix：会议与待办中心

> 许可：PolyForm Noncommercial 1.0.0，**禁止商业使用**，详见文末。

把**会议、聊天、飞书任务**里冒出来的事，自动收进一张飞书多维表格。AI 能做的先做掉，结果交给你验收。每天用一屏卡片告诉你：今天要做什么、等你验收什么、等你拍板什么。

设计原则：

- **能用代码做的不用模型。** 接收推送、过滤、同步、生成简报、处理回复，全是代码。模型只在三件事上被唤醒：读会议逐字稿、读筛过的聊天、代做文档。
- **宁缺毋滥。** 一条待办必须有原话里承接的字眼，负责人要么是说话的人，要么在原话里被点了名，日期必须写明。没有待办的会议不发任何消息。
- **你是最后一关。** AI 做的事只有你回"过"才算完成。每次写表都会读回核对，结果不确定时不会自动重试。

## 架构

```
飞书推送（实时）                  定时（兜底）              唤醒模型（只在有活时）
├ 私聊回复  ─┐                   ├ 08:30 早报 / 21:00 晚报   ├ 逐字稿 → 待办
├ 会议结束   ├→ listen（常驻）    ├ 06:00 周期事项           ├ 聊天候选 → 待办
├ 逐字稿生成 │   纯代码            ├ 每 2 分钟检查脚本 ───────→└ 代做（按模板，做完再交）
└ 任务变化  ─┘                   └ 聊天每 10 分钟读一次
                 ↓
        飞书多维表格（事项表）  →  卡片：简报 / 会前准备 / 做完了 / 周五复盘
                 ↑
        你回复："15 过""8 周五""18 AI备""展开 12"
```

| 模块 | 作用 |
|---|---|
| `listen.py` | 常驻监听：处理私聊回复、会议结束、逐字稿生成、任务变化；每分钟检查会前卡片、补发和周五复盘 |
| `reply.py` | 回复指令解析和写表，也负责飞书私聊轮询（兜底） |
| `brief_v1.py` | 早晚简报卡片：一屏读完，分"你要做 / 等你看 / 需要你定 / AI 在做" |
| `meeting_watch.py`、`meeting_review.py` | 会议队列；逐字稿提取结果的校验与入表 |
| `sources/chats.py`、`chat_review.py` | 聊天读取、代码过滤；定时批量提取 |
| `sources/feishu_tasks.py` | 飞书任务同步进事项表（只读飞书任务，不回写） |
| `prework.py` | AI 代做：按模板检查栏目、占位文字、篇幅，做完立即发卡片 |
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
| `12 完成` / `12 不做` | 完成 / 取消 |
| `12 我来` / `12 AI做` / `12 给张三` | 改执行方 |
| `18 AI备` | AI 先备材料，你验收后再开始 |
| `20 + 材料` | 补材料，卡住的事接着做 |
| `展开 12` | 看详情和原话 |

## 安装

需要 Python 3.11+、已登录的 `lark-cli`（npm 包 `@larksuite/cli`），以及一个能运行定时任务、按 `prompts/` 执行的 AI 宿主（作者用的是 Cindy，换成其他宿主也可以）。

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml config.yaml
.venv/bin/python -m agent_matrix setup          # 建多维表格，写回 config.yaml
.venv/bin/python -m agent_matrix.brief_migration setup-fields && .venv/bin/python -m agent_matrix.brief_migration apply
.venv/bin/python -m agent_matrix ping           # 给自己发一条测试消息
./launchd/install-listen.sh                     # 安装常驻监听（macOS）
```

实时推送要在飞书开放平台订阅这几种事件：`vc.meeting.participant_meeting_ended_v1`、`vc.note.generated_v1`、`task.task.update_user_access_v2`。未订阅时，`lark-cli event consume` 的报错里会直接给出订阅链接。没订阅也能用，只是退回成定时检查。

宿主需要配置的定时任务：

| 时间 | 方式 | 命令 / 说明 |
|---|---|---|
| 06:00 | 只跑脚本 | `python -m agent_matrix.scheduled recurring` |
| 工作日 08:30 / 21:00 | 只跑脚本 | `python -m agent_matrix.scheduled morning` / `evening` |
| 每 2 分钟 | 前置脚本 + 模型 | 前置脚本依次跑 `reply poll`，以及 `meeting_watch`、`prework`、`chat_review` 的 `check`。任一项 run_model 为 true 才唤醒模型，模型按 `prompts/routine.md` 执行 |

## 常用命令

```bash
.venv/bin/python -m agent_matrix brief --morning --preview     # 预览简报，不发送
.venv/bin/python -m agent_matrix.reply --text "15 过"           # 处理一条指令
.venv/bin/python -m agent_matrix.listen --status                # 监听连接状态
.venv/bin/python -m agent_matrix health                         # 授权与表格自检
.venv/bin/python -m unittest discover -s tests                  # 测试
```

## 隐私

`config.yaml`（账号与表格 ID）、`state/`（逐字稿、聊天候选、收据）、`logs/`、生成的文档和验收记录都已加入 `.gitignore`，不进版本库。消息只发给本人。聊天只在本地过滤，只有筛下来的少量句子才会交给模型。

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
