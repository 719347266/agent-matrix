# 例行任务：会议、聊天、代做

工作目录：本项目根目录（`config.yaml` 所在目录）

前置脚本已经用代码检查过，结果里有三项：meetings（会议）、chats（聊天）、prework（代做）。**只处理 run_model 为 true 的那一项。**安静运行：不发开场白，不说"无变化"，不另发总结。

1. **会议**（meetings 里有 ready 或 prepared 的会）：严格按 `prompts/meeting_extract.md` 处理。
   - 逐字稿未就绪：`meeting_watch defer --meeting-id <ID>`。
   - needs_attention：`meeting_watch notify-missing --meeting-id <ID>`。
2. **聊天**（chats.run_model 为 true）：严格按 `prompts/chat_extract.md` 处理。
3. **代做**（prework 里有 ready 或待发送的项）：严格按 `prompts/prework_execute.md` 处理，结尾运行 `prework notify`。会议入表后同一轮接着检查代做。

所有消息只发给本人。不联系别人，不改源日程、源任务，不提交审批，不改代码和调度。真实出错、需要本人处理时，用一句中文简短报告。
