; 编译输入由 tools/build_windows_installer.py 指定。
#ifndef AppVersion
  #error 请指定 AppVersion
#endif
#ifndef PayloadDir
  #error 请指定 PayloadDir
#endif
#ifndef OutputDir
  #error 请指定 OutputDir
#endif
#define ProductId "{92377318-2397-4DC8-BDF9-421AF7B5FC18}"

[Setup]
AppId={{#ProductId}
AppName=MotionControl
AppVersion={#AppVersion}
AppPublisher=guiwushengzhe
AppPublisherURL=https://github.com/guiwushengzhe-prog/MotionControl
DefaultDirName={localappdata}\Programs\MotionControl
DefaultGroupName=MotionControl
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=MotionControl-Setup-{#AppVersion}
SetupIconFile={#PayloadDir}\程序文件\app\web\assets\motioncontrol.ico
UninstallDisplayIcon={app}\启动 MotionControl.exe
UninstallFilesDir={app}\程序文件
LicenseFile={#PayloadDir}\程序文件\LICENSE
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
LZMANumBlockThreads=2
DisableProgramGroupPage=yes
UsePreviousTasks=yes
AppMutex=Local\MotionControl.Desktop.Start,Local\MotionControl.Installed.Backend
CloseApplications=no
RestartApplications=no
UninstallDisplayName=MotionControl
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面图标"; GroupDescription: "快捷方式："

[Files]
Source: "{#PayloadDir}\启动 MotionControl.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\程序文件\*"; DestDir: "{app}\程序文件"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc,*.pyo,last-start.log*"

[Icons]
Name: "{app}\卸载 MotionControl"; Filename: "{uninstallexe}"
Name: "{userdesktop}\MotionControl"; Filename: "{app}\启动 MotionControl.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\MotionControl"; Filename: "{app}\启动 MotionControl.exe"; WorkingDir: "{app}"
Name: "{group}\MotionControl 故障诊断"; Filename: "{app}\启动 MotionControl.exe"; Parameters: "--diagnostics"; WorkingDir: "{app}"
Name: "{group}\卸载 MotionControl"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\程序文件\安装虚拟手柄驱动.exe"; Description: "安装虚拟手柄驱动（手柄输出需要）"; Verb: "runas"; Flags: postinstall shellexec skipifsilent unchecked
Filename: "{app}\启动 MotionControl.exe"; Description: "启动 MotionControl"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallDelete]
; 应用自更新会替换程序目录，旧安装清单无法覆盖更新后才出现的文件。
Type: filesandordirs; Name: "{app}\程序文件\app"
Type: filesandordirs; Name: "{app}\程序文件\app_next"
Type: filesandordirs; Name: "{app}\程序文件\app_previous"
Type: filesandordirs; Name: "{app}\程序文件\app_broken"
Type: files; Name: "{app}\程序文件\app.booting"
Type: files; Name: "{app}\程序文件\app_update_result.json"
Type: files; Name: "{app}\程序文件\last-start.log"
Type: files; Name: "{app}\程序文件\last-start.log.1"
Type: files; Name: "{app}\程序文件\last-start.log.2"
Type: files; Name: "{app}\程序文件\last-start.log.3"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PreviousDir: String;
  Contents: TFindRec;
begin
  Result := '';
  { 旧卸载程序的登记不可移动，否则会失去原安装的文件清单。 }
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#ProductId}_is1',
                         'InstallLocation', PreviousDir) then
  begin
    if not FileExists(AddBackslash(PreviousDir) + '程序文件\installer-payload.json') then
    begin
      Result := '请先从 Windows 的「已安装的应用」卸载旧版 MotionControl。个人设置会保留。';
      Exit;
    end;
    if CompareText(RemoveBackslash(PreviousDir), ExpandConstant('{app}')) <> 0 then
      Result := '请先卸载当前版本，再更换安装位置。';
    Exit;
  end;

  { 新装使用专用目录，不和便携程序、截图中的旧文件或其他应用混放。 }
  if FindFirst(ExpandConstant('{app}\*'), Contents) then
  try
    repeat
      if (Contents.Name <> '.') and (Contents.Name <> '..') then
      begin
        Result := '请选择一个空文件夹安装 MotionControl。';
        Exit;
      end;
    until not FindNext(Contents);
  finally
    FindClose(Contents);
  end;
end;
