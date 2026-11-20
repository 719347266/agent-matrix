# 按用户的方式把心响走一遍：装好 → 从开始菜单打开 → 用一用 → 后台被关掉能自己回来 → 卸载。每一步截屏。
#   powershell -ExecutionPolicy Bypass -File windows\e2e.ps1 -Setup dist\Xinxiang-Setup-x.y.z.exe -Shots shots
param([Parameter(Mandatory = $true)][string]$Setup, [string]$Shots = "shots")
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
New-Item -ItemType Directory -Force $Shots | Out-Null
$Shots = (Resolve-Path $Shots).Path
$script:passed = 0; $script:failed = @()
function Shot([string]$name) {
    try {
        $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
        $bmp.Save((Join-Path $Shots "$name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose(); $bmp.Dispose()
    } catch { Write-Host "  (screenshot $name failed: $_)" }
}
function Check([string]$what, [bool]$ok, [string]$note = "") {
    if ($ok) { $script:passed++; Write-Host "  PASS $what" } else { $script:failed += $what; Write-Host "  FAIL $what $note" }
}
function Stop-Xinxiang { Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*agent_matrix*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
function Procs([string]$like) { @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $like }) }
$url = "http://127.0.0.1:47865/"
$app = "$env:LOCALAPPDATA\Programs\Xinxiang"
$data = "$env:LOCALAPPDATA\Xinxiang"
function Api([string]$path, $body = $null) {
    $h = @{ "X-Xinxiang-Token" = (Get-Content "$data\state\app-token" -Raw).Trim() }
    if ($null -eq $body) {
        $raw = $null
        foreach ($try in 1..4) {   # right after a start the first read can be refused or slow: say why, try again
            try { $raw = (Invoke-WebRequest "$url$path" -Headers $h -UseBasicParsing -TimeoutSec 120).RawContentStream.ToArray(); break }
            catch { Write-Host "  (read $path, try ${try}: $($_.Exception.Message))"; Start-Sleep 5; $h = @{ "X-Xinxiang-Token" = (Get-Content "$data\state\app-token" -Raw).Trim() } }
        }
        if ($null -eq $raw) { throw "cannot read $path" }
    }
    else {
        $json = if ($body -is [string]) { $body } else { $body | ConvertTo-Json -Compress -Depth 6 }
        try { $raw = (Invoke-WebRequest "$url$path" -Method Post -Headers $h -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -UseBasicParsing -TimeoutSec 200).RawContentStream.ToArray() }
        catch { return @{ ok = $false; reply = "$_" } }
    }
    return [System.Text.Encoding]::UTF8.GetString($raw) | ConvertFrom-Json
}
function Wait-Page([int]$seconds = 90) {
    foreach ($i in 1..$seconds) { Start-Sleep 1; try { if ((Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).Content -match "<title>") { return $true } } catch { } }
    return $false
}

Write-Host "[1] 安装"
$p = Start-Process (Resolve-Path $Setup).Path -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-' -PassThru -Wait
Check "安装包静默安装成功" ($p.ExitCode -eq 0) "exit $($p.ExitCode)"
Stop-Xinxiang
$start = Join-Path ([Environment]::GetFolderPath("Programs")) "心响.lnk"
$desk = Join-Path ([Environment]::GetFolderPath("Desktop")) "心响.lnk"
$boot = Join-Path ([Environment]::GetFolderPath("Startup")) "心响.lnk"
Check "开始菜单里有心响" (Test-Path $start)
Check "桌面上有心响" (Test-Path $desk)
Check "开机自启已登记" (Test-Path $boot)
$plain = Join-Path $env:TEMP "xinxiang-check.lnk"   # the scripting object cannot open a shortcut with a Chinese name
Copy-Item -LiteralPath $start -Destination $plain -Force
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($plain)
Check "快捷方式指向装好的程序" (Test-Path $lnk.TargetPath) $lnk.TargetPath
Check "快捷方式的图标文件在" (Test-Path (($lnk.IconLocation -split ",")[0])) $lnk.IconLocation
Shot "01-installed-desktop"

Write-Host "[2] 从开始菜单打开（全新电脑，什么都没配）"
Start-Process $start
Check "页面起来了" (Wait-Page 120)
Start-Sleep 12
Shot "02-first-open"
$state = Api "api/state"
Check "第一次打开会进新手引导" ($state.settings.onboarded -eq $false)
Check "后台程序在运行" ($state.system.listener -eq "运行中") $state.system.listener
Check "知道自己是安装包版" ($state.system.here.client -eq $true)
Check "配置文件是不用账号的那种" ((Get-Content "$data\config.yaml" -Raw -Encoding UTF8) -match "item_store: device")
$icon = Invoke-WebRequest "${url}icon.png" -UseBasicParsing -TimeoutSec 20
Check "图标能取到" ($icon.StatusCode -eq 200 -and $icon.RawContentLength -gt 2000) "$($icon.StatusCode) $($icon.RawContentLength)"
Check "有心响的窗口" (@(Get-Process | Where-Object { $_.MainWindowTitle -like "*心响*" }).Count -ge 1) ((Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object { $_.ProcessName + ":" + $_.MainWindowTitle }) -join " | ")
Check "托盘程序在" ((Procs "*agent_matrix.winclient*").Count -ge 1)
Check "没有弹出来的终端窗口" (@(Get-Process | Where-Object { $_.MainWindowTitle -and ($_.ProcessName -match "^(cmd|powershell|pwsh|conhost|WindowsTerminal|OpenConsole)$") -and $_.Id -ne $PID -and $_.MainWindowTitle -notmatch "e2e|Administrator|runner|GitHub|hosted-compute" }).Count -eq 0) ((Get-Process | Where-Object { $_.MainWindowTitle -and $_.ProcessName -match "cmd|powershell|pwsh|Terminal" } | ForEach-Object { $_.ProcessName + ":" + $_.MainWindowTitle }) -join " | ")

Write-Host "[3] 走新手引导里要填的"
Check "填称呼" ((Api "api/settings" @{ owner_name = "王小明"; owner_aliases = @("小王") }).ok)
Check "选接收模式" ((Api "api/settings" @{ intake_mode = "manual" }).ok)
Check "记下想接飞书" ((Api "api/settings" @{ want_feishu = $true }).ok)
Check "引导走完" ((Api "api/settings" @{ onboarded = $true }).ok)
$state = Api "api/state"
Check "称呼存下了" ($state.settings.me.name -eq "王小明")
Check "有一段给 AI 的话" ($state.settings.setup_prompt -match "xinxiang-setup")
$shim = "$env:USERPROFILE\.agent-matrix\bin\xinxiang-setup.cmd"
Check "给 AI 用的命令装好了" (Test-Path $shim)
$doctor = (& cmd /c "`"$shim`" doctor" 2>&1 | Out-String)
Check "检查命令能说出缺什么" ($doctor -match "要处理" -and $doctor -match "飞书") ($doctor.Substring(0, [Math]::Min(300, $doctor.Length)))
Check "检查命令的中文没乱码" ($doctor -match "心响现在的配置情况")
$set = (& cmd /c "`"$shim`" set owner_aliases=小王,明哥" 2>&1 | Out-String)
Check "AI 能替用户改设置" ($set -match '"ok": true') $set
Check "改的设置生效了" (((Api "api/state").settings.me.aliases -join ",") -eq "小王,明哥")

Write-Host "[4] 用一用"
$a = Api "api/item/new" @{ title = "整理十二月活动排期"; who = "我"; due = "明天" }
$b = Api "api/item/new" @{ title = "写季度复盘"; who = "我" }
Check "新建两件事" ($a.ok -and $b.ok -and $b.n -eq ($a.n + 1))
$proj = Api "api/projects" @{ add = "运营复盘" }
Check "新建项目并归进去" ($proj.ok -and (Api "api/item/project" @{ numbers = @($b.n); project = $proj.id }).ok)
Check "加一版交付" ((Api "api/item/deliverable" @{ n = $b.n; op = "add"; url = "https://example.com/docx/A"; summary = "初稿" }).ok)
Check "改交付链接" ((Api "api/item/deliverable" @{ n = $b.n; op = "edit"; v = 1; url = "https://example.com/docx/MINE"; summary = "我自己建的文档" }).ok)
Check "把两件事串起来" ((Api "api/item/link" @{ earlier = $a.n; later = $b.n; on = $true }).ok)
Check "加一条规矩" ((Api "api/rules" @{ add = "对外文案先给我两版" }).ok)
Check "反馈一个问题" ((Api "api/bugs" @{ add = "这是安装检查时记的一条" }).ok)
$rep = Api "api/bugs" @{ report = $true }
Check "问题能汇总成一份" ($rep.report -match "心响问题反馈（1 条）" -and $rep.issue_url -like "https://github.com/*")
$cmd = Api "api/commands" @{ text = "$($a.n) 完成"; request_id = [guid]::NewGuid().ToString("N") }
Check "点「完成」被接收" ($cmd.ok -eq $true) "$($cmd.reply)"
$done = $false
foreach ($i in 1..20) { Start-Sleep 3; $state = Api "api/state"; if (($state.items | Where-Object { $_.n -eq $a.n }).status -eq "完成") { $done = $true; break } }
Check "那件事变成已完成" $done (($state.items | Where-Object { $_.n -eq $a.n }).status)
$ib = $state.items | Where-Object { $_.n -eq $b.n }
Check "页面数据对得上：项目、交付、串联" ($ib.project -eq $proj.id -and $ib.url -eq "https://example.com/docx/MINE" -and ($ib.before -contains $a.n)) ("project=$($ib.project) url=$($ib.url) before=$($ib.before)")
Check "中文标题没乱码" ($ib.title -eq "写季度复盘")
Check "规矩在" (@($state.memory.rules | Where-Object { $_.rule -eq "对外文案先给我两版" }).Count -eq 1)
$up = Api "api/update" @{ check = $true }
# the releases page limits how often one address may ask, and this machine shares its address with many others:
# knowing its own version and either the newest one or a plain "could not reach it" is what must work
Check "查新版本：知道自己的版本，查到最新版或说明连不上" ($up.current -and ($up.latest -or $up.error)) "current=$($up.current) latest=$($up.latest) err=$($up.error)"
if (-not $up.latest) { Write-Host "  (没查到最新版：$($up.error))" }
Shot "03-after-use"

Write-Host "[5] 后台程序被关掉，自己回来"
Procs "*agent_matrix.listen*" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 3
$back = $false
foreach ($i in 1..60) { Start-Sleep 2; try { if ((Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $back = $true; break } } catch { } }
Check "一分多钟内自己恢复" $back
Check "恢复后数据还在" (@((Api "api/state").items | Where-Object { $_.title -eq "写季度复盘" }).Count -eq 1)

Write-Host "[6] 全部关掉，再按开机自启的方式起来（不弹窗口）"
Stop-Xinxiang
Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*心响*" } | ForEach-Object { $_.CloseMainWindow() | Out-Null }
Start-Sleep 3
Start-Process $boot
Check "开机自启的方式能起来" (Wait-Page 120)
Start-Sleep 8
Check "引导不会再弹（已经走过）" ((Api "api/state").settings.onboarded -eq $true)
Shot "04-after-autostart"
Start-Process $start          # the owner opens it again from the Start menu: only a window, the background is already there
Start-Sleep 14
Shot "05-main-window"
Check "再点一次只开窗口，不会起第二套后台" ((Procs "*agent_matrix.listen*").Count -eq 1) "$((Procs '*agent_matrix.listen*').Count)"

Write-Host "[7] 卸载"
Stop-Xinxiang
$un = Get-ChildItem $app -Filter "unins*.exe" | Select-Object -First 1
$p = Start-Process $un.FullName -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -PassThru -Wait
Start-Sleep 5
Check "卸载成功" ($p.ExitCode -eq 0)
Check "程序删干净了" (-not (Test-Path "$app\python\pythonw.exe"))
Check "快捷方式删掉了" (-not (Test-Path $start) -and -not (Test-Path $boot))
Check "数据留着" (Test-Path "$data\state\xinxiang.db")
Shot "06-after-uninstall"

Write-Host ""
Write-Host "通过 $script:passed 项，没过 $($script:failed.Count) 项"
if ($script:failed.Count) { $script:failed | ForEach-Object { Write-Host "  没过：$_" }; foreach ($f in "$data\logs\listen.log", "$data\logs\client.log") { if (Test-Path $f) { Write-Host "---- $f"; Get-Content $f -Tail 40 -Encoding UTF8 } }; exit 1 }
