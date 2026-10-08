param(
    [ValidateSet('Check', 'Ensure', 'Install')][string]$Mode = 'Check',
    [Parameter(Mandatory)][string]$Program,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$ResultPath = '',
    [string]$LifetimeJob = ''
)
$ErrorActionPreference = 'Stop'
if ($LifetimeJob) {
    # 提权助手由 Windows 创建，需主动加入主后台的作业；加入后不保留句柄。
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class MotionControlLifetime {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool member);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    public static void Bind(string name) {
        IntPtr job = OpenJobObject(0x5, false, name); // 加入和查询权限。
        if (job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            bool member;
            if (!IsProcessInJob(GetCurrentProcess(), job, out member) ||
                (!member && !AssignProcessToJobObject(job, GetCurrentProcess())))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        } finally { CloseHandle(job); }
    }
}
'@
    [MotionControlLifetime]::Bind($LifetimeJob)
}
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$Program = [System.IO.Path]::GetFullPath($Program)
if (-not (Test-Path -LiteralPath $Program -PathType Leaf)) { throw '软件运行程序不存在' }
$digest = [System.Security.Cryptography.SHA256]::Create()
$hash = [BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($Program.ToLowerInvariant()))).Replace('-', '').Substring(0, 16)
$prefix = "MotionControl-$hash-$Port"

function Get-ConnectionPermission {
    $profiles = @((Get-NetConnectionProfile).NetworkCategory | ForEach-Object {
        if ([string]$_ -eq 'DomainAuthenticated') { 'Domain' } else { [string]$_ }
    } | Sort-Object -Unique)
    if (-not $profiles.Count) { $profiles = @('Private', 'Public') }
    $needed = @($profiles | ForEach-Object { "TCP:$_"; "UDP:$_" })
    # 先按本程序筛选，避免逐条查询整台电脑的数千条规则。
    $programFilters = @(Get-NetFirewallApplicationFilter -PolicyStore ActiveStore -Program $Program -ErrorAction SilentlyContinue)
    if (-not $programFilters.Count) { return @{state = 'missing'; message = '局域网连接权限未配置'} }
    $rules = @($programFilters |
        Get-NetFirewallRule | Where-Object { [string]$_.Direction -eq 'Inbound' -and [string]$_.Enabled -eq 'True' -and [string]$_.Action -eq 'Allow' })
    foreach ($rule in $rules) {
        $address = $rule | Get-NetFirewallAddressFilter
        if ('Any' -notin $address.RemoteAddress -and 'LocalSubnet' -notin $address.RemoteAddress) { continue }
        $filter = $rule | Get-NetFirewallPortFilter
        if ('Any' -notin $filter.LocalPort -and [string]$Port -notin $filter.LocalPort) { continue }
        foreach ($profile in $profiles) {
            if ([string]$rule.Profile -ne 'Any' -and $profile -notin ([string]$rule.Profile -split ',\s*')) { continue }
            if ([string]$filter.Protocol -in @('TCP', '6', 'Any', '256')) { $needed = @($needed | Where-Object { $_ -ne "TCP:$profile" }) }
            if ([string]$filter.Protocol -in @('UDP', '17', 'Any', '256')) { $needed = @($needed | Where-Object { $_ -ne "UDP:$profile" }) }
        }
        if (-not $needed.Count) { break }
    }
    if ($needed.Count) { return @{state = 'missing'; message = '局域网连接权限未配置'} }
    return @{state = 'ready'; message = ''}
}

try {
    $result = Get-ConnectionPermission
    if ($Mode -ne 'Check' -and $result.state -ne 'ready') {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $admin = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $admin -and $Mode -eq 'Ensure') {
            $temporaryResult = [IO.Path]::GetTempFileName()
            try {
                # 单次系统授权，仅提升这个配置助手；主程序仍保持原有权限。
                $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Mode Install -Program "{1}" -Port {2} -ResultPath "{3}" -LifetimeJob "{4}"' -f $PSCommandPath, $Program, $Port, $temporaryResult, $LifetimeJob
                $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru -Wait
                if ($child.ExitCode -ne 0) { throw '管理员配置未完成' }
                $result = Get-Content -LiteralPath $temporaryResult -Raw -Encoding UTF8 | ConvertFrom-Json
            } catch {
                $result = @{state = 'missing'; message = '未获得连接权限授权，可点此重试'}
            } finally { Remove-Item -LiteralPath $temporaryResult -Force -ErrorAction SilentlyContinue }
        } else {
            if (-not $admin) { throw '需要管理员权限配置连接规则' }
            foreach ($protocol in @('TCP', 'UDP')) {
                $ruleName = "$prefix-$protocol"
                $existing = Get-NetFirewallRule -PolicyStore PersistentStore -Name $ruleName -ErrorAction SilentlyContinue
                if ($existing) { Remove-NetFirewallRule -PolicyStore PersistentStore -Name $ruleName }
                New-NetFirewallRule -Name $ruleName -DisplayName "MotionControl 手机连接 $protocol $Port" -Group 'MotionControl' -Direction Inbound -Action Allow -Enabled True -Profile Any -Program $Program -Protocol $protocol -LocalPort $Port -RemoteAddress LocalSubnet -EdgeTraversalPolicy Block | Out-Null
            }
            $result = Get-ConnectionPermission
        }
    }
} catch { $result = @{state = 'error'; message = '连接权限配置未完成，可重试'; detail = $_.Exception.Message} }
$json = $result | ConvertTo-Json -Compress
if ($ResultPath) { [IO.File]::WriteAllText($ResultPath, $json, [Text.UTF8Encoding]::new($false)) }
else { [Console]::WriteLine($json) }
