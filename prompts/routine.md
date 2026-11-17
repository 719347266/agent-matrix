# 例行任务：会议、聊天、代做

工作目录：本项目根目录（`config.yaml` 所在目录）。一开始就 `cd` 进去。

- 直接在这一轮里做完，**不要转交、不要新开任务**，也不要去读和本例行任务无关的技能、记忆或说明。
- 这一轮的最后一条消息，每件事写一行：做到了哪一步，或者这一轮为什么没做。比如 prework 检查结果里 `in_progress_elsewhere` 列出的事，要写"#44 正由另一个会话处理，到 12:20"。不能只写开场白就结束。

前置脚本已经用代码检查过，结果里有三项：meetings（会议）、chats（聊天）、prework（代做）。**只处理 run_model 为 true 的那一项。**安静运行：不发开场白，不说"无变化"，不另发总结。

1. **会议**（meetings 里有 ready 或 prepared 的会）：严格按 `prompts/meeting_extract.md` 处理。
   - 逐字稿未就绪：`meeting_watch defer --meeting-id <ID>`。
   - needs_attention：`meeting_watch notify-missing --meeting-id <ID>`。
2. **聊天**（chats.run_model 为 true）：严格按 `prompts/chat_extract.md` 处理。
3. **代做**（prework 里有 ready 或待发送的项）：严格按 `prompts/prework_execute.md` 处理，结尾运行 `prework notify`。会议入表后同一轮接着检查代做。

所有消息只发给本人。不联系别人，不改源日程、源任务，不提交审批，不改代码和调度。真实出错、需要本人处理时，用一句中文简短报告。
