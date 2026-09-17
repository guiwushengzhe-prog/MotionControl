# Keep the PC's internet on Wi-Fi while the phone is tethered over USB.
#
# Windows picks the default route partly by link speed, and a USB tether
# reports a higher one than Wi-Fi -- measured on the development machine,
# 426 Mbps against 287.  So plugging in the cable to get a fast link to the
# phone silently moves the whole machine's internet onto the phone's mobile
# data.  Nothing warns you; the browser just keeps working.
#
# The fix is to pin the tether's interface metric high, so its default route
# always loses.  The direct link to the phone is unaffected: that traffic
# matches the on-link subnet route, which metrics do not gate.
#
# Windows writes this to the persistent store, keyed to the adapter, so it
# survives reboots and unplugging the cable.  It has to be redone only when a
# new tether adapter appears -- a different phone, or a different USB port
# that Windows enumerates as a new device.
#
# Needs administrator rights.  Run fix_usb_tether_metric.cmd, which asks for
# them, rather than this file directly.

$ErrorActionPreference = "Stop"
$LOSING_METRIC = 9999

$adapters = Get-NetAdapter | Where-Object {
    $_.Status -eq "Up" -and (
        $_.InterfaceDescription -like "*Remote NDIS*" -or
        $_.InterfaceDescription -like "*RNDIS*" -or
        $_.InterfaceDescription -like "*USB Ethernet*" -or
        $_.InterfaceDescription -like "*NCM*"
    )
}

if (-not $adapters) {
    Write-Host "没有找到 USB 网络共享的网卡。"
    Write-Host "先在手机上打开 USB 网络共享，再运行这个脚本。"
    exit 2
}

foreach ($adapter in $adapters) {
    Set-NetIPInterface -ifIndex $adapter.ifIndex -AddressFamily IPv4 -InterfaceMetric $LOSING_METRIC
    $now = Get-NetIPInterface -ifIndex $adapter.ifIndex -AddressFamily IPv4
    Write-Host ("已设置  {0}  ({1})  跃点 {2}" -f $adapter.Name, $adapter.InterfaceDescription, $now.InterfaceMetric)
}

# Report what the machine will actually do now, rather than assuming the
# setting was enough: when Wi-Fi has no internet at all, Windows keeps the
# tether regardless of metrics, and saying otherwise would be a lie.
$probe = New-Object System.Net.Sockets.Socket('InterNetwork', 'Dgram', 'Udp')
try {
    $probe.Connect('223.5.5.5', 53)
    $egress = $probe.LocalEndPoint.Address.ToString()
} catch {
    $egress = $null
} finally {
    $probe.Close()
}

Write-Host ""
if ($egress) {
    $onTether = $adapters | ForEach-Object {
        Get-NetIPAddress -ifIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    } | Where-Object { $_.IPAddress -eq $egress }

    if ($onTether) {
        Write-Host "注意：上网仍然走手机。"
        Write-Host "这说明这台电脑现在没有别的能上网的连接，跃点改不了这一点。"
        Write-Host "等 WiFi 恢复正常，它会自动切回去。"
    } else {
        Write-Host ("上网走 {0}，没有经过手机。" -f $egress)
    }
}
Write-Host "手机和电脑之间仍然走数据线，不受影响。"
