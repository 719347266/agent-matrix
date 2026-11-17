# 常驻监听（macOS）

`./install-listen.sh` 会按当前目录和本机的 `lark-cli`、`node` 路径，在 `~/Library/LaunchAgents/` 生成并加载 `com.agent-matrix.listen`。开机自动启动，进程退出会自动重启，日志写在 `../logs/listen.log`。

- 查看状态：`.venv/bin/python -m agent_matrix.listen --status`
- 卸载：`launchctl bootout gui/$UID/com.agent-matrix.listen && rm ~/Library/LaunchAgents/com.agent-matrix.listen.plist`

监听程序全是代码，不调用模型。电脑睡眠或关机期间，由宿主的定时检查兜底。
