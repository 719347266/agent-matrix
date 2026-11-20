; 心响 Windows 安装包（Inno Setup 6）。由 .github/workflows/windows.yml 构建：
;   iscc /DAppVersion=0.3.0 /DSource=..\dist\Xinxiang windows\installer.iss
; 装在当前用户下，不需要管理员权限。数据在 %LOCALAPPDATA%\Xinxiang，卸载时不删。
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Source
  #define Source "..\dist\Xinxiang"
#endif

[Setup]
AppId={{6B0B3C52-6D0E-4E0B-9C1D-7A3E5F2B8A41}
AppName=心响
AppVersion={#AppVersion}
AppPublisher=agent-matrix
DefaultDirName={localappdata}\Programs\Xinxiang
DefaultGroupName=心响
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Xinxiang-Setup-{#AppVersion}
SetupIconFile={#Source}\xinxiang.ico
UninstallDisplayIcon={app}\xinxiang.ico
UninstallDisplayName=心响
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面放一个图标"
Name: "autostart"; Description: "开机自动启动（只进托盘，不弹窗口）"

[InstallDelete]
; 覆盖安装前把旧版本的程序文件清干净（数据不在这里），否则旧版本的文件和版本记录会留下来
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\心响"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m agent_matrix.winclient"; WorkingDir: "{app}"; IconFilename: "{app}\xinxiang.ico"
Name: "{autodesktop}\心响"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m agent_matrix.winclient"; WorkingDir: "{app}"; IconFilename: "{app}\xinxiang.ico"; Tasks: desktopicon
Name: "{userstartup}\心响"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m agent_matrix.winclient --no-window"; WorkingDir: "{app}"; IconFilename: "{app}\xinxiang.ico"; Tasks: autostart

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m agent_matrix.winclient"; WorkingDir: "{app}"; Description: "现在打开心响"; Flags: nowait postinstall skipifsilent

[Code]
// 升级安装前先停掉正在运行的心响（只停装在这个目录里的那一份）
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Exec('powershell.exe', ExpandConstant('-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like ''{app}\python\*'' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"'), '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{app}\python\*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"""; Flags: runhidden; RunOnceId: "stop"
