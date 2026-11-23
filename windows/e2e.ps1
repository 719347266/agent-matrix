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

Write-Host "[3b] 看面板不会把后台弄停"
$before = @((Procs "*agent_matrix.listen*") | ForEach-Object { $_.ProcessId })
foreach ($i in 1..5) { $null = Api "api/state"; Start-Sleep 1 }
$after = @((Procs "*agent_matrix.listen*") | ForEach-Object { $_.ProcessId })
Check "连看五次面板，后台还是同一个、没被重启" ($before.Count -ge 1 -and ($before -join ",") -eq ($after -join ",")) "before=$($before -join ',') after=$($after -join ',')"

Write-Host "[3c] 记忆增强（混元 Hy Memory）随安装包带上了"
$hy = (& "$app\python\python.exe" -c "import hy_memory, chromadb; print('hy ok')" 2>&1 | Out-String)
Check "安装包里带着混元记忆 SDK，能加载" ($hy -match "hy ok") ($hy.Substring(0, [Math]::Min(300, $hy.Length)))
$mb = (Api "api/state").settings.memory_boost
Check "设置里能看到记忆增强，默认关着" ($mb -and $mb.installed -eq $true -and $mb.enabled -eq $false) "installed=$($mb.installed) enabled=$($mb.enabled)"

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

Write-Host "[4b] 换一个 AI 来驱动：报到、领活、交活"
$xin = "$env:USERPROFILE\.agent-matrix\bin\xinxiang.cmd"
Check "给 AI 领活的命令装好了" (Test-Path $xin)
$hello = (& cmd /c "`"$xin`" hello --name Antigravity --app Antigravity --model `"Gemini 3 Pro`"" 2>&1 | Out-String)
Check "AI 报到成功" ($hello -match '"you": "Antigravity"') $hello
$state = Api "api/state"
Check "心响记下了是谁在做事" ($state.settings.engine -eq "driver:Antigravity" -and @($state.settings.drivers | Where-Object { $_.name -eq "Antigravity" -and $_.model -eq "Gemini 3 Pro" }).Count -eq 1) "engine=$($state.settings.engine)"
$c = Api "api/item/new" @{ title = "整理一份周会议程草稿"; who = "AI"; note = "三项议题，每项一句话" }
$raw = (& cmd /c "`"$xin`" next --driver Antigravity --wait 90" 2>&1 | Out-String)
$job = $null; try { $job = $raw | ConvertFrom-Json } catch { }
Check "AI 领到了这件事，说明书是完整的" ($job -and $job.kind -eq "item" -and $job.n -eq $c.n -and $job.instructions -match "交付.md" -and $job.instructions.Length -gt 1500) ($raw.Substring(0, [Math]::Min(300, $raw.Length)))
if ($job -and $job.folder) {
  Check "工作台里有任务单和背景" ((Test-Path "$($job.folder)\任务单.md") -and (Test-Path "$($job.folder)\背景.md"))
  $doc = "# 周会议程草稿`n`n- 周会定三项议题，总时长 30 分钟。`n- 先对齐上周进度，再定本周重点，最后留出风险讨论。`n- 会前不需要额外准备材料。`n`n## 议程`n`n| 顺序 | 议题 | 一句话 |`n|---|---|---|`n| 1 | 上周进度 | 每人说清完成了什么 |`n| 2 | 本周重点 | 定下最重要的三件事 |`n| 3 | 风险与求助 | 提出卡住的事 |`n`n## 需要决策`n`n本周最重要的三件事由谁拍板，会上定。`n`n## 背景数据`n`n任务单只要求三项议题，没有其他数据。`n`n## 用到的资料`n`n- 任务单.md`n`n## 我做的假设`n`n- 周会总时长按 30 分钟安排。`n"
  [IO.File]::WriteAllText("$($job.folder)\交付.md", $doc, (New-Object Text.UTF8Encoding $false))
  $res = '{"outcome": "done", "template": "会议材料", "summary": "拟好三项议题的周会议程", "confirmation": "", "sources": ["任务单.md"], "note": "按任务单拟了三项议题"}'
  [IO.File]::WriteAllText("$($job.folder)\结果.json", $res, (New-Object Text.UTF8Encoding $false))
  $sub = (& cmd /c "`"$xin`" submit --job $($job.job)" 2>&1 | Out-String)
  Check "AI 交活成功（没接飞书也能交）" ($sub -match '"status": "done"') $sub
  $state = Api "api/state"; $ic = $state.items | Where-Object { $_.n -eq $c.n }
  Check "这件事变成等你验收，记在这个 AI 名下" ($ic.status -eq "待验收" -and @($ic.sessions | Where-Object { $_.driver -eq "Antigravity" }).Count -ge 1) "status=$($ic.status)"
  $page = $null; try { $page = Invoke-WebRequest -UseBasicParsing -Uri $ic.url -TimeoutSec 20 } catch { }
  Check "交付能在这台电脑上打开" ($page -and $page.StatusCode -eq 200 -and $page.Content -match "周会议程草稿") "$($ic.url)"
}
$d = Api "api/item/new" @{ title = "改一下首页的按钮文案"; who = "AI"; note = "三处按钮" }
$h = Api "api/item/ai" @{ n = $d.n; to = "handoff" }
Check "这一件单独交给另一个 AI，拿到给它的话" ($h.ok -and $h.message -match "--n $($d.n)" -and $h.message -match "--helper") "$($h.reply)"
$idle = (& cmd /c "`"$xin`" next --driver Antigravity" 2>&1 | Out-String)
$other = $null; try { $other = $idle | ConvertFrom-Json } catch { }
Check "原来的 AI 不会领走这一件（空下来可以去整理记忆）" ($other -and ($other.idle -or ($other.n -ne $d.n -and $other.kind -ne "item"))) ($idle.Substring(0, [Math]::Min(200, $idle.Length)))
if ($other -and $other.job) { & cmd /c "`"$xin`" release --job $($other.job)" | Out-Null }
$hello2 = (& cmd /c "`"$xin`" hello --name Cursor --app Cursor --model gpt-5 --helper" 2>&1 | Out-String)
$raw2 = (& cmd /c "`"$xin`" next --driver Cursor --n $($d.n)" 2>&1 | Out-String)
$job2 = $null; try { $job2 = $raw2 | ConvertFrom-Json } catch { }
Check "收到那段话的 AI 领到了这一件" ($hello2 -match '"you": "Cursor"' -and $job2 -and $job2.n -eq $d.n) ($raw2.Substring(0, [Math]::Min(300, $raw2.Length)))
$state = Api "api/state"
Check "其余的事还归原来的 AI，这一件记在新 AI 名下" ($state.settings.engine -eq "driver:Antigravity" -and ($state.items | Where-Object { $_.n -eq $d.n }).ai_for -eq "driver:Cursor") "engine=$($state.settings.engine)"
if ($job2) { & cmd /c "`"$xin`" release --job $($job2.job)" | Out-Null }
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
