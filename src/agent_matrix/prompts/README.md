# 提示词

需要读懂内容的活交给 AI，其余全部由代码完成。每件活交给 AI 的说明，都按同一个顺序拼起来（`drive._open`）：

| 顺序 | 内容 | 出处 |
|---|---|---|
| 1 | 本人的规矩（必须遵守，冲突时以它为准） | `memory.rules_text` |
| 2 | 关于你：本人的岗位、产品、习惯 | `memory.profile_text` |
| 3 | 共同规矩：身份、只读、不替本人拍板、只交要的文件、宁缺毋滥、写法、报进度 | `_common.md` |
| 4 | 这件事所在的项目和版本（做事项时） | `versions.context_text` |
| 5 | 这一件的输入：事件简报（做事项时）或本次会议、聊天批次的编号 | `eventbrief.build`、`drive._prepare_*` |
| 6 | 这类活的说明 | 下表 |
| 7 | 交活方式：报进度、提交、卡住了问、叫停（外部 AI） | `drive.protocol_tail` |

## 每类活的说明

每份都按同一个骨架写：**要交出什么 → 输入 → 怎么判断 → 输出格式 → 交了之后**。共同规矩已经在第 3 段，这里只写这类活特有的；字段名和代码校验一一对应，改字段要同时改校验。

| 文件 | 活 | 交出 | 谁来校验 |
|---|---|---|---|
| `meeting_extract.md` | 从会议逐字稿提取待办、决定、需求、等条件的事 | `review.json` | `meeting_review` |
| `chat_extract.md` | 从代码筛过的聊天候选里提取待办 | `review.json` | `chat_review` |
| `triage.md` | 判断新进来的事：已办完 / 不用做 / AI 做 / 本人做 AI 备材料 / 本人做 | `triage.json` | `drive._submit_triage` |
| `worker.md` | 做一件事 | `交付.md` + `结果.json` | `itemwork` |
| `learn.md` | 从评审和反馈里学规矩 | `learned.json` | `drive._submit_learn` |
| `donecheck.md` | 看本人手上开着的事是不是已经办完（只认抄得出原话或给得出链接的） | `donecheck.json` | `donecheck` |
| `organize.md` | 把事归到项目和版本 | `organize.json` | `versions` |
| `tidy.md` | 每天整理记忆，只提议 | `tidy.json` | `tidy` |
| `profile_scan.md` | 第一次认识本人，写「关于你」草稿 | `画像.md` | `memory` |
| `profile_update.md` | 核对「关于你」有没有变化 | `profile-updates.json` | `memory` |
| `setup_wechat.md` | 接入微信的步骤（给驱动心响的 AI 看） | — | `setupcheck` |

## 写法

- 只写三样：做什么、照什么格式交、怎样才算完。流程交给代码，不写进提示词；提示词啰嗦，产出也会啰嗦。
- 例子用通用的（示例 App、王小明、张三），不写真实的公司、产品、同事。
- 想改某一份又不动程序：把它复制到数据目录的 `prompts/` 里改，那份优先。
