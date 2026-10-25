param(
    [string]$ProgramRoot = $PSScriptRoot,
    [string]$Destination = (Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'MotionControl\Launcher'),
    [string]$DesktopDirectory = [Environment]::GetFolderPath('DesktopDirectory'),
    [string]$ProgramsDirectory = [Environment]::GetFolderPath('Programs'),
    [switch]$DoNotRegister
)

$ErrorActionPreference = 'Stop'
$ProgramRoot = [IO.Path]::GetFullPath($ProgramRoot)
if (-not [IO.File]::Exists((Join-Path $ProgramRoot 'server.py')) -or
        -not [IO.File]::Exists((Join-Path $ProgramRoot 'START.ps1'))) {
    throw '所选程序文件夹不完整，未修改桌面入口。'
}
$shellPath = (Get-Command pwsh -ErrorAction Stop).Source
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not [IO.File]::Exists($compiler)) { $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not [IO.File]::Exists($compiler)) { throw '找不到 Windows 自带的程序编译器，未修改桌面入口。' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$executable = Join-Path $Destination 'MotionControl.exe'
$temporary = Join-Path $Destination ('MotionControl-' + [guid]::NewGuid().ToString('N') + '.exe')
try {
    & $compiler /nologo /target:winexe /optimize+ /reference:System.Windows.Forms.dll `
        "/win32icon:$(Join-Path $ProgramRoot 'web\assets\motioncontrol.ico')" "/out:$temporary" `
        (Join-Path $ProgramRoot 'tools\windows\MotionControl.cs')
    if ($LASTEXITCODE -ne 0) { throw '桌面入口编译失败，原入口未替换。' }
    Move-Item -LiteralPath $temporary -Destination $executable -Force
} finally {
    if ([IO.File]::Exists($temporary)) { Remove-Item -LiteralPath $temporary }
}
if (-not $DoNotRegister) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\MotionControl')
    try {
        $key.SetValue('ProgramRoot', $ProgramRoot)
        $key.SetValue('PowerShellPath', $shellPath)
    } finally { $key.Dispose() }
}
$shell = New-Object -ComObject WScript.Shell
foreach ($directory in @($DesktopDirectory, $ProgramsDirectory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $shortcut = $shell.CreateShortcut((Join-Path $directory 'MotionControl.lnk'))
    $shortcut.TargetPath = $executable
    $shortcut.WorkingDirectory = $Destination
    $shortcut.IconLocation = "$executable,0"
    $shortcut.Description = 'MotionControl · 体感控制'
    $shortcut.Save()
}
Write-Host "已创建桌面与开始菜单入口：$executable"
Write-Host '在开始菜单搜索 MotionControl，右键选择“固定到任务栏”。迁移程序后原入口会帮助重新定位。'
