# 整理记忆：把本人的记忆理一遍，只提议、不直接改

每条提议会列在「记忆 → 记忆更新」里，本人点「用」才生效，所以每条都要有一句他一看就懂的理由。

## 要交出什么

当前文件夹的 `tidy.json`。没有要改的就留空，这是正常结果，不要为了凑数提议。

## 输入

- `记忆/`：经验库，一份一个文件（开头 name / description，正文按 `## 主题` 分节）；`记忆/目录.md` 是目录。
- `moments.json`：近 30 天会上留下的经历（定了 / 还没定 / 等条件再定 / 需求 / 要做），每条带 id、哪个会、什么时候。
- `versions.json`：项目和版本：状态、时间、说明、里面有哪些事（编号）、预计会遇到的事。
- `profile.md`：关于本人。`new-evidence.json`：本轮新的依据。
- `outcomes.json`：近 30 天本人对交付的评审。
- `unplaced.json`：还没归到版本的事（编号 n、标题、状态、已在的项目）。

## 共 5 步（每做完一步报「第几步/共几步 做了什么」，比如「2/5 看完经历，3 条过时」）

1. **经验库**：只提草稿（`"type": "memory"`，字段和 mode 见下面），本人点「用」才改。
   - 两份或两节说的是一件事：留一处，另一处 `remove`；要合在一起的先 `revise` 留下的那处。
   - 一节明显被本人后来的评审推翻了（看 `outcomes.json` 的原因和 `memory`：读了它照着做却被退回，原因正是它讲的事）：`revise` 或 `remove`。
   - 一节放错了份（「做事通用规矩」里其实只在发 PR 时才用得上）：`move` 挪到对应的那份，没有就同时写 `description` 新建。每件事都要守的（交付用中文、第一屏给结论）留在通用那份。
   - 目录描述写成了话题、或者没写全该打开它的动作：`describe` 改成「要……、……时打开」。AI 是拿自己要做的动作来比这句话的。
   - 文字规则里有检查守着的（交付或评审里写了加了哪个测试、哪个脚本会拦），只留一句并写明是哪个检查；检查已经完全守住的，删掉文字。
   - 拿不准就不提。经验库宁缺毋滥。
2. **经历**：较早的「定了 / 还没定 / 等条件再定」被后来的会推翻、或已经有了结论的，标为过时（`stale_moment`），`by` 填推翻它的那条新经历的 id（没有就不填）。
3. **没归类的事**：`unplaced.json` 里看得出属于哪个项目、哪个版本的，归过去（`place_item`）。拿不准不要硬归。项目名和版本名原样用 `versions.json` 里已有的（已有「示例 App」就不要写成「示例」）；看不出版本就只写项目。
4. **版本状态**：从评审、经历和事项状态看，状态已经变了的（事都做完了、已经上线），提议改状态（`version_status`）。
5. **关于你**：只核对 `new-evidence.json` 列出的本轮新依据。只是重复印证、换个说法或一次性任务的，跳过。具体事实确实改变了对本人的认识时才提，`evidence` 填 `moment:编号`、`outcome:编号` 或 `item:编号`。优先在原段补充或修正，全新主题才新增小节。最多两处。

## 输出格式

```json
{"changes": [
  {"type": "memory", "mode": "move", "name": "做事通用规矩", "old_text": "## 用 cw5-pr-notify 发 PR 并通知群\n……（整节逐字抄）", "to": "发 PR 经验", "description": "要建分支、提交代码、发 PR、通知 reviewer 时打开。", "why": "只在发 PR 时用得上，放在通用里每件事都要读"},
  {"type": "memory", "mode": "describe", "name": "查线上数据经验", "description": "要查线上数据、写 SQL、统计商家或订单时打开。", "why": "#30 写 SQL 时没想到读它"},
  {"type": "memory", "mode": "remove", "name": "做事通用规矩", "old_text": "（逐字抄）", "why": "和「发 PR 经验」里那节重复"},
  {"type": "stale_moment", "id": 12, "by": 30, "why": "11-20 周会改成先做年卡"},
  {"type": "place_item", "n": 45, "project": "示例 App", "version": "2.3", "why": "做的是 2.3 的会员页"},
  {"type": "version_status", "project": "示例 App", "version": "2.2", "status": "已完成", "why": "2.2 的事都做完了，11-18 已上线"},
  {"type": "profile", "old": "## 在做的事\n会员体系", "new": "## 在做的事\n会员体系改版（2.3），下一步做年卡", "why": "本次周会确认 2.3 开始做年卡", "evidence": "moment:30"}
]}
```

- 每条都要有 `why`：一句话，引用具体的事、会或评审。
- `memory` 的 mode：`append`（加节：name + body）、`create`（新建：name + description + body）、`revise`（改：name + old_text + body）、`remove`（删：name + old_text）、`describe`（改目录描述：name + description）、`move`（挪整节：name + old_text + to，to 还没有就加 description）。old_text 逐字抄，多抄几行让它只对上一处。
- `version_status` 的 `status` 只用：当前 / 下一版 / 规划中 / 已完成。
- `profile`：`old` 原样抄 `profile.md` 里要替换的那一段（新加一段就留空），`new` 写改好后的完整段落。
