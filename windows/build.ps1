# 构建心响的 Windows 客户端：自带 Python，不需要用户装任何东西。
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1 [-Version 0.3.0]
# 产物：dist\Xinxiang\（可直接运行的文件夹）、dist\Xinxiang-Setup-<版本>.exe（装了 Inno Setup 时）
param([string]$Version = "", [string]$PythonVersion = "3.12.8")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $Version) { $Version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value }
$Out = Join-Path $Root "dist\Xinxiang"
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force "$Out\python" | Out-Null

# 1. Python 本体（官方的免安装版）。去掉 ._pth，让它像普通 Python 一样认环境变量（客户端靠 PYTHONUTF8=1 统一用 UTF-8）。
$zip = Join-Path $env:TEMP "python-embed.zip"
Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $zip
Expand-Archive $zip "$Out\python" -Force
Get-ChildItem "$Out\python" -Filter "python*._pth" | Remove-Item

# 2. 心响和它的依赖，装进这份 Python 的 site-packages（用同一个小版本的 Python 来装，二进制包才对得上）
$site = "$Out\python\Lib\site-packages"
New-Item -ItemType Directory -Force $site | Out-Null
python -m pip install --disable-pip-version-check --no-warn-script-location --target $site ".[windows]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 3. 这份 Python 真的能跑心响吗
& "$Out\python\python.exe" -c "import sys, agent_matrix.winclient, agent_matrix.listen, agent_matrix.tray, pydantic, yaml; print('bundled python ok', sys.version.split()[0], sys.flags.utf8_mode)"
if ($LASTEXITCODE -ne 0) { throw "bundled python cannot import agent_matrix" }

# 4. 图标（用包里的 Pillow 做）
& "$Out\python\python.exe" -c "from PIL import Image; Image.open(r'macapp\AppIcon.png').save(r'$Out\xinxiang.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE -ne 0) { throw "icon failed" }
Copy-Item LICENSE.md "$Out\LICENSE.md"

# 5. 安装包
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "/DAppVersion=$Version" "/DSource=$Out" "windows\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "installer failed" }
} else { Write-Warning "没装 Inno Setup，只生成了 dist\Xinxiang 文件夹。" }
Write-Host "完成：$Out"
