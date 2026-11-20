# 一键更新的真机检查：装一个旧版本，点「更新」，看它是不是自己变成了最新版并重新跑起来。
#   powershell -ExecutionPolicy Bypass -File windows\update-test.ps1 -From v0.4.0
param([Parameter(Mandatory = $true)][string]$From)
$ErrorActionPreference = "Stop"
function Stop-Xinxiang { Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*agent_matrix*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
$old = Join-Path $env:TEMP "old-setup.exe"
$rel = Invoke-RestMethod "https://api.github.com/repos/MILESMAKMAK/agent-matrix/releases/tags/$From" -Headers @{ "User-Agent" = "xinxiang-update-test" }
$asset = $rel.assets | Where-Object { $_.name -like "Xinxiang-Setup*.exe" } | Select-Object -First 1
if (-not $asset) { throw "release $From has no installer" }
Invoke-WebRequest $asset.browser_download_url -OutFile $old
$p = Start-Process $old -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/MERGETASKS="!desktopicon,!autostart"' -PassThru -Wait
if ($p.ExitCode -ne 0) { throw "old installer exit code $($p.ExitCode)" }
Stop-Xinxiang
$app = "$env:LOCALAPPDATA\Programs\Xinxiang"
$Data = Join-Path $env:TEMP ("xinxiang-update-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$env:XINXIANG_HOME = $Data
$env:XINXIANG_PORT = "47992"
$env:PYTHONUTF8 = "1"
$url = "http://127.0.0.1:47992/"
Start-Process -FilePath "$app\python\python.exe" -ArgumentList "-m", "agent_matrix.winclient", "--headless" -WorkingDirectory $app | Out-Null
function Get-State {
    $token = (Get-Content "$Data\state\app-token" -Raw).Trim()
    $raw = (Invoke-WebRequest "${url}api/state" -Headers @{ "X-Xinxiang-Token" = $token } -UseBasicParsing -TimeoutSec 60).RawContentStream.ToArray()
    return [System.Text.Encoding]::UTF8.GetString($raw) | ConvertFrom-Json
}
function Fail([string]$why) {
    Write-Host "FAILED: $why"
    try { Write-Host ("update status: " + ((Get-State).system.update | ConvertTo-Json -Compress)) } catch { Write-Host "state unreadable: $_" }
    Get-ChildItem $env:TEMP -Filter "Xinxiang-Setup*" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "downloaded: $($_.Name) $($_.Length) bytes" }
    Get-ChildItem $env:TEMP -Filter "Setup Log*" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "---- $($_.Name)"; Get-Content $_.FullName -Tail 30 }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -match "python|Setup|cmd|is-" } | ForEach-Object { Write-Host "proc: $($_.Name) :: $($_.CommandLine)" }
    foreach ($f in "$Data\logs\listen.log", "$Data\logs\client.log") { if (Test-Path $f) { Write-Host "---- $f"; Get-Content $f -Tail 40 -Encoding UTF8 } }
    Stop-Xinxiang; exit 1
}
try {
    $up = $false
    foreach ($i in 1..90) { Start-Sleep 1; try { if ((Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).Content) { $up = $true; break } } catch { } }
    if (-not $up) { Fail "the old version never came up" }
    $token = (Get-Content "$Data\state\app-token" -Raw).Trim()
    $headers = @{ "X-Xinxiang-Token" = $token }
    $made = Invoke-RestMethod "${url}api/item/new" -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes('{"title":"更新前建的事","who":"我"}')) -TimeoutSec 60
    $before = Invoke-RestMethod "${url}api/update" -Method Post -Headers $headers -ContentType "application/json" -Body '{"check":true}' -TimeoutSec 60
    Write-Host "installed $($before.current), newest $($before.latest), can=$($before.can) kind=$($before.kind)"
    if (-not $before.newer) { Fail "nothing newer than $($before.current) to update to" }
    if (-not $before.can) { Fail "it says it cannot update itself: $($before.why)" }
    $go = Invoke-RestMethod "${url}api/update" -Method Post -Headers $headers -ContentType "application/json" -Body '{"install":true}' -TimeoutSec 120
    if (-not $go.ok) { Fail "update did not start: $($go.reply)" }
    $done = $false
    foreach ($i in 1..36) {
        Start-Sleep 5
        try { $s = Get-State; if ($s.system.update.current -eq $before.latest -and $s.system.listener -eq "运行中") { $done = $true; break } } catch { }
    }
    if (-not $done) { Fail "after 3 minutes it is still not running $($before.latest)" }
    $s = Get-State
    if (-not ($s.items | Where-Object { $_.n -eq $made.n -and $_.title -eq "更新前建的事" })) { Fail "the item made before the update is gone" }
    Write-Host "OK: $($before.current) updated itself to $($s.system.update.current); it is running again and the data is intact."
} finally { Stop-Xinxiang }
