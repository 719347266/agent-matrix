# agent-matrix 的 Windows 安装脚本：Python 环境、依赖、开机启动（监听 + 托盘）和三个定时任务。
# 用法（在项目根目录）：powershell -ExecutionPolicy Bypass -File windows\install.ps1
# 卸载：powershell -ExecutionPolicy Bypass -File windows\uninstall.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$PyW = Join-Path $Root ".venv\Scripts\pythonw.exe"
& $Py -m pip install --upgrade pip | Out-Null
& $Py -m pip install -e ".[windows]"

if (-not (Get-Command lark-cli -ErrorAction SilentlyContinue)) {
    Write-Warning "没找到 lark-cli：先运行 npm install -g @larksuite/cli，再用 lark-cli auth login 登录飞书。"
}
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "已生成 config.yaml。按 README 的安装步骤运行 setup 后，系统才能正常工作。"
}

function Register-Job {
    param([string]$Name, [string]$Arguments, $Trigger, [switch]$KeepAlive)
    $action = New-ScheduledTaskAction -Execute $PyW -Argument $Arguments -WorkingDirectory $Root
    if ($KeepAlive) {
        # Always-on: no time limit, restarted a minute after any crash.
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    } else {
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    }
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings -Description "agent-matrix" -Force | Out-Null
    Write-Host "已注册：$Name"
}

$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$weekdays = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
Register-Job -Name "AgentMatrix Listener" -Arguments "-m agent_matrix.listen" -Trigger $logon -KeepAlive
Register-Job -Name "AgentMatrix Tray" -Arguments "-m agent_matrix.tray" -Trigger $logon -KeepAlive
Register-Job -Name "AgentMatrix Recurring" -Arguments "-m agent_matrix.scheduled recurring" -Trigger (New-ScheduledTaskTrigger -Daily -At "06:00")
Register-Job -Name "AgentMatrix Morning Brief" -Arguments "-m agent_matrix.scheduled morning" -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "08:30")
Register-Job -Name "AgentMatrix Evening Plan" -Arguments "-m agent_matrix.scheduled evening" -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "21:00")

Start-ScheduledTask -TaskName "AgentMatrix Listener"
Start-ScheduledTask -TaskName "AgentMatrix Tray"
Write-Host ""
Write-Host "完成。托盘图标在任务栏右下角（可能收在 ^ 里）。"
Write-Host "AI 例行任务：让你的 AI 助手每 2 分钟在项目目录运行 .venv\Scripts\python -m agent_matrix.precheck，"
Write-Host "退出码为 0 时，按 prompts\routine.md 处理。"
