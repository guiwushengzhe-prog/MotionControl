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

[Setup]
AppId={{92377318-2397-4DC8-BDF9-421AF7B5FC18}
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
SetupIconFile={#PayloadDir}\app\web\assets\motioncontrol.ico
UninstallDisplayIcon={app}\MotionControl.exe
LicenseFile={#PayloadDir}\LICENSE
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
LZMANumBlockThreads=2
DisableProgramGroupPage=yes
UsePreviousTasks=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayName=MotionControl
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面图标"; GroupDescription: "快捷方式："

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc,*.pyo,last-start.log*"

[Icons]
Name: "{userdesktop}\MotionControl"; Filename: "{app}\MotionControl.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\MotionControl"; Filename: "{app}\MotionControl.exe"; WorkingDir: "{app}"
Name: "{group}\MotionControl 故障诊断"; Filename: "{app}\MotionControl.exe"; Parameters: "--diagnostics"; WorkingDir: "{app}"
Name: "{group}\卸载 MotionControl"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\安装虚拟手柄驱动.exe"; Description: "安装虚拟手柄驱动（手柄输出需要）"; Verb: "runas"; Flags: postinstall shellexec skipifsilent unchecked
Filename: "{app}\MotionControl.exe"; Description: "打开 MotionControl"; Flags: postinstall nowait skipifsilent runasoriginaluser
