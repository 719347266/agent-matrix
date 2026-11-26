# 试运行构建好的客户端：全新的数据目录，不开窗口，看它能不能自己建配置、启动、新建一件事。
#   powershell -ExecutionPolicy Bypass -File windows\smoke.ps1 [-App dist\Xinxiang]
param([string]$App = "dist\Xinxiang")
$ErrorActionPreference = "Stop"
$App = (Resolve-Path $App).Path
$Data = Join-Path $env:TEMP ("xinxiang-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$env:XINXIANG_HOME = $Data
$env:XINXIANG_PORT = "47991"
$url = "http://127.0.0.1:47991/"
$proc = Start-Process -FilePath "$App\python\python.exe" -ArgumentList "-m", "agent_matrix.winclient", "--headless" -WorkingDirectory $App -PassThru `
    -RedirectStandardOutput "$env:TEMP\xinxiang-smoke.out" -RedirectStandardError "$env:TEMP\xinxiang-smoke.err"
function Fail([string]$why) {
    Write-Host "FAILED: $why"
    foreach ($f in "$env:TEMP\xinxiang-smoke.out", "$env:TEMP\xinxiang-smoke.err", "$Data\logs\listen.log") { if (Test-Path $f) { Write-Host "---- $f"; Get-Content $f -Tail 60 -Encoding UTF8 } }
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*agent_matrix*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    exit 1
}
try {
    $page = $null
    foreach ($i in 1..90) {
        Start-Sleep -Seconds 1
        try { $page = (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).Content; if ($page) { break } } catch { }
    }
    if (-not $page) { Fail "the page never came up" }
    if ($page -notmatch "<title>") { Fail "not the 心响 page" }
    $token = (Get-Content "$Data\state\app-token" -Raw).Trim()
    $headers = @{ "X-Xinxiang-Token" = $token }
    $body = [System.Text.Encoding]::UTF8.GetBytes('{"title":"试一件事：检查安装包","who":"我"}')
    $made = Invoke-RestMethod "${url}api/item/new" -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 60
    if (-not $made.ok) { Fail "could not add an item" }
    $proj = Invoke-RestMethod "${url}api/projects" -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes('{"add":"安装检查"}')) -TimeoutSec 60
    Invoke-RestMethod "${url}api/item/project" -Method Post -Headers $headers -ContentType "application/json" -Body ('{"numbers":[' + $made.n + '],"project":' + $proj.id + '}') -TimeoutSec 60 | Out-Null
    $state = $null
    foreach ($i in 1..40) {   # the background program reports itself within a minute or two of starting
        $raw = (Invoke-WebRequest "${url}api/state" -Headers $headers -UseBasicParsing -TimeoutSec 60).RawContentStream.ToArray()
        $state = [System.Text.Encoding]::UTF8.GetString($raw) | ConvertFrom-Json
        if ($state.system.listener -eq "运行中") { break }
        Start-Sleep -Seconds 5
    }
    $item = $state.items | Where-Object { $_.n -eq $made.n }
    if (-not $item) { Fail "the new item is not on the page" }
    if ($item.title -ne "试一件事：检查安装包") { Fail "Chinese text was damaged: $($item.title)" }
    if ($item.project -ne $proj.id) { Fail "the item is not in its project" }
    if ($state.system.listener -ne "运行中") { Fail "the background program does not report as running: $($state.system.listener)" }
    if (-not $state.system.here.client) { Fail "the page does not know it runs in the client" }
    $cfg = Get-Content "$Data\config.yaml" -Raw -Encoding UTF8
    if ($cfg -notmatch "item_store: device") { Fail "first config is not the no-account one" }
    # nothing 心响 starts may flash a terminal: a console child started the usual way must have no window
    $quiet = & "$App\python\python.exe" -c "import agent_matrix, subprocess, sys; child='import ctypes;print(ctypes.windll.kernel32.GetConsoleWindow())'; a=subprocess.run([sys.executable,'-c',child],capture_output=True,text=True).stdout.strip(); b=subprocess.run([sys.executable,'-c',child],capture_output=True,text=True,creationflags=0x10).stdout.strip(); print(getattr(subprocess.Popen,'_xinxiang_quiet',False), a, b)"
    Write-Host "console windows: patched / window handle of a child started normally / with its own new console = $quiet"
    if ($quiet -notmatch "^True ") { Fail "console programs are not started hidden" }
    if ($quiet -notmatch "^True 0 ") { Fail "a console child started the usual way still has a window ($quiet)" }
    Write-Host "OK: page up, item #$($made.n) added and filed, Chinese intact, listener running. platform=$($state.system.here.platform) npm=$($state.system.here.npm)"
} finally {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*agent_matrix*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
