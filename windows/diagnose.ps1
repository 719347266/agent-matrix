# 按用户真实的样子在一台干净的 Windows 上把心响走一遍，查「打开是空的、连不上本机的心响」：
#   装旧版 → 从开始菜单打开、用一用 → 不关它，直接双击新安装包覆盖安装 → 再从开始菜单打开
#   → 接下来 3 分钟每 10 秒看一次：后台还是不是同一个进程、页面数据读不读得出来、读不出是什么错 → 截屏、留日志
#   powershell -ExecutionPolicy Bypass -File windows\diagnose.ps1 -From v0.4.26 -Setup dist\Xinxiang-Setup-x.y.z.exe -Out diag
param([string]$From = "", [Parameter(Mandatory = $true)][string]$Setup, [string]$Out = "diag", [int]$Watch = 180)
$ErrorActionPreference = "Continue"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
New-Item -ItemType Directory -Force $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$log = Join-Path $Out "diag.txt"
function Say([string]$text) { $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $text; Write-Host $line; Add-Content -Path $log -Value $line -Encoding UTF8 }
function Shot([string]$name) {
    try {
        $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
        $bmp.Save((Join-Path $Out "$name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose(); $bmp.Dispose()
    } catch { Say "screenshot $name failed: $_" }
}
$url = "http://127.0.0.1:47865/"
$data = "$env:LOCALAPPDATA\Xinxiang"
$start = Join-Path ([Environment]::GetFolderPath("Programs")) "心响.lnk"
function Listeners { @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*agent_matrix.listen*" } | ForEach-Object { $_.ProcessId }) }
function Token { try { (Get-Content "$data\state\app-token" -Raw -ErrorAction Stop).Trim() } catch { "" } }
function Probe {
    # what the page sees: the page itself, then its data with the token the page would carry
    $page = "down"
    try { $page = if ((Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5).Content -match "<title>") { "up" } else { "odd" } } catch { $page = "down: $($_.Exception.Message)" }
    $state = ""
    try {
        $r = Invoke-WebRequest "${url}api/state" -Headers @{ "X-Xinxiang-Token" = (Token) } -UseBasicParsing -TimeoutSec 60
        $j = [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json
        $state = "200 items=$(@($j.items).Count) version=$($j.system.update.current) broken=$(($j.broken | ConvertTo-Json -Compress))"
    } catch {
        $resp = $_.Exception.Response
        $body = ""
        if ($resp) { try { $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } catch { } }
        $state = "FAIL $([int]$(if ($resp) { $resp.StatusCode } else { 0 })) $($_.Exception.Message) $body"
    }
    return "page=$page state=$state listeners=$((Listeners) -join ',') token=$((Token).Substring(0, [Math]::Min(6, (Token).Length)))"
}
function Wait-Page([int]$seconds = 120) {
    foreach ($i in 1..$seconds) { Start-Sleep 1; try { if ((Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).Content -match "<title>") { return $true } } catch { } }
    return $false
}
function Dump {
    foreach ($f in Get-ChildItem "$data\logs" -Filter *.log -ErrorAction SilentlyContinue) {
        Say "---- $($f.Name) (last 120 lines)"; Get-Content $f.FullName -Tail 120 -Encoding UTF8 | ForEach-Object { Add-Content -Path $log -Value $_ -Encoding UTF8 }
        Copy-Item $f.FullName (Join-Path $Out $f.Name) -Force
    }
    Get-ChildItem $env:TEMP -Filter "Setup Log*" -ErrorAction SilentlyContinue | ForEach-Object { Copy-Item $_.FullName (Join-Path $Out $_.Name) -Force }
    if (Test-Path "$data\config.yaml") { Copy-Item "$data\config.yaml" (Join-Path $Out "config.yaml") -Force }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -match "python|msedge|Setup" } | ForEach-Object { Add-Content -Path $log -Value "proc $($_.ProcessId) $($_.Name) :: $($_.CommandLine)" -Encoding UTF8 }
}

if ($From) {
    Say "[1] 装旧版 $From，从开始菜单打开"
    $old = Join-Path $env:TEMP "old-setup.exe"
    $rel = Invoke-RestMethod "https://api.github.com/repos/MILESMAKMAK/agent-matrix/releases/tags/$From" -Headers @{ "User-Agent" = "xinxiang-diagnose" }
    $asset = $rel.assets | Where-Object { $_.name -like "Xinxiang-Setup*.exe" } | Select-Object -First 1
    Invoke-WebRequest $asset.browser_download_url -OutFile $old
    $p = Start-Process $old -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-' -PassThru -Wait
    Say "old installer exit $($p.ExitCode)"
    Start-Process $start
    Say "old page up: $(Wait-Page 120)"
    Start-Sleep 10
    Say (Probe)
    try {
        $made = Invoke-RestMethod "${url}api/item/new" -Method Post -Headers @{ "X-Xinxiang-Token" = (Token) } -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes('{"title":"旧版里建的事","who":"我"}')) -TimeoutSec 60
        Say "item made in the old version: #$($made.n)"
    } catch { Say "could not make an item in the old version: $_" }
    Shot "1-old-version"
}

Say "[2] 不关心响，直接双击新安装包覆盖安装（用户的做法）"
$p = Start-Process (Resolve-Path $Setup).Path -ArgumentList '/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-' -PassThru -Wait
Say "new installer exit $($p.ExitCode)"
Shot "2-after-install"
Say "[3] 从开始菜单打开"
Start-Process $start
Say "new page up: $(Wait-Page 120)"
$first = Listeners
Say "listeners right after opening: $($first -join ',')"
$t0 = Get-Date
$n = 0
while (((Get-Date) - $t0).TotalSeconds -lt $Watch) {
    Start-Sleep 10
    $n++
    Say (Probe)
    if ($n -in 1, 6, 12, 18) { Shot ("3-window-{0:D3}s" -f ($n * 10)) }
}
$last = Listeners
Say "listeners at the end: $($last -join ',') (at the start: $($first -join ','))"
if (($first -join ',') -ne ($last -join ',')) { Say "THE BACKGROUND PROGRAM WAS RESTARTED during the watch: it keeps dying" }
Dump
Say "done"
