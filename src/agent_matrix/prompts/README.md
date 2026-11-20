# 提示词

需要读懂内容的三件事交给模型来做，其余全部由代码完成。

| 文件 | 用途 |
|---|---|
| `routine.md` | 例行任务入口：根据前置检查结果，决定处理会议、聊天还是代做 |
| `meeting_extract.md` | 从会议逐字稿提取待办，宁缺毋滥，由 `meeting_review` 校验 |
| `chat_extract.md` | 从代码筛过的聊天候选里提取待办，由 `chat_review` 校验 |
| `prework_execute.md` | AI 代做：按模板做完再交，结论先行 |
