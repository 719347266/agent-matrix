# 卸载 agent-matrix 的 Windows 计划任务（不删除数据和代码）。
$names = "AgentMatrix Listener", "AgentMatrix Tray", "AgentMatrix Recurring", "AgentMatrix Morning Brief", "AgentMatrix Evening Plan"
foreach ($n in $names) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Host "已移除：$n"
    }
}
