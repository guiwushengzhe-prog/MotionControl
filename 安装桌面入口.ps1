param(
    [string]$ProgramRoot = $PSScriptRoot,
    [string]$Destination = (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Apps\MotionControl'),
    [string]$UserDataRoot = '',
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
$modernShell = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'PowerShell\7\pwsh.exe'
$shellPath = if (Test-Path -LiteralPath $modernShell) { $modernShell } else { Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe' }
if (-not $UserDataRoot) {
    $pythonPath = Join-Path $ProgramRoot '.venv-multicore\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = 'python' }
    Push-Location $ProgramRoot
    try {
        $locations = @(& $pythonPath -X utf8 -m motioncontrol.user_paths --locate 2>&1)
        if ($LASTEXITCODE -ne 0) { throw ('找不到原设置目录：' + ($locations -join "`n")) }
        $UserDataRoot = [string]$locations[-1]
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath $UserDataRoot -PathType Container)) { throw '原设置目录不存在，未修改入口。' }
$identity = ''
$marker = Join-Path $UserDataRoot '.motioncontrol-data.json'
if (Test-Path -LiteralPath $marker) { $identity = (Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).id }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not [IO.File]::Exists($compiler)) { $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not [IO.File]::Exists($compiler)) { throw '找不到 Windows 自带的程序编译器，未修改桌面入口。' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$executable = Join-Path $Destination 'MotionControl.exe'
$temporary = Join-Path $Destination ('MotionControl-' + [guid]::NewGuid().ToString('N') + '.exe')
$installRecord = Join-Path $Destination ('installation-' + [guid]::NewGuid().ToString('N') + '.json')
$settings = @{schema=1;program_root=$ProgramRoot;shell=$shellPath;data_root=$UserDataRoot;data_id=$identity} | ConvertTo-Json -Compress
[IO.File]::WriteAllText($installRecord,$settings,[Text.UTF8Encoding]::new($false))
try {
    & $compiler /nologo /target:winexe /optimize+ /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll `
        "/resource:$installRecord,MotionControl.Installation" `
        "/win32icon:$(Join-Path $PSScriptRoot 'web\assets\motioncontrol.ico')" "/out:$temporary" `
        (Join-Path $PSScriptRoot 'tools\windows\MotionControl.cs')
    if ($LASTEXITCODE -ne 0) { throw '桌面入口编译失败，原入口未替换。' }
    Move-Item -LiteralPath $temporary -Destination $executable -Force
    Move-Item -LiteralPath $installRecord -Destination (Join-Path $Destination 'installation.json') -Force
} finally {
    if ([IO.File]::Exists($temporary)) { Remove-Item -LiteralPath $temporary }
    if ([IO.File]::Exists($installRecord)) { Remove-Item -LiteralPath $installRecord }
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
