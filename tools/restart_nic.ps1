<#
.SYNOPSIS
    禁用有线网卡一段时间再启用：用来强制校园网重新认证，或验证自动登录。

.EXAMPLE
    # 演练（不动网卡，不需要管理员）
    powershell -ExecutionPolicy Bypass -File .\restart_nic.ps1 -DryRun

    # 禁用 5 分钟后恢复，并用浏览器模式登录
    powershell -ExecutionPolicy Bypass -File .\restart_nic.ps1 -Seconds 300 -AutoLogin

    # 禁用 5 分钟后恢复，并用【纯 HTTP 模式】登录（用来验证不需要浏览器的方案）
    powershell -ExecutionPolicy Bypass -File .\restart_nic.ps1 -Seconds 300 -AutoLogin -Engine http

.NOTES
    需要管理员权限（会自动弹 UAC 提权）。
    只挑物理有线网卡（PhysicalMediaType = 802.3），自动排除 TAP / ZeroTier / Hyper-V / 蓝牙等虚拟网卡。

    安全兜底：禁用网卡的同时会拉起一个独立的隐藏进程，到点无条件把网卡恢复。
    所以即使这个窗口被关掉或脚本被中断，网卡也不会一直处于禁用状态。
#>
param(
    [int]$Seconds = 120,           # 网卡禁用时长（秒）
    [string]$AdapterName = '',     # 手动指定网卡名称，留空则自动识别
    [switch]$AutoLogin,            # 网卡恢复后执行一次登录
    [ValidateSet('browser', 'http')]
    [string]$Engine = 'browser',   # 登录引擎：浏览器 / 纯 HTTP
    [switch]$DryRun,               # 只演示，不动网卡
    # 以下是内部参数，供安全兜底进程使用，一般不用管
    [switch]$RestoreOnly,
    [string]$RestoreAdapter = '',
    [string]$RestoreLog = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8

$AppDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $AppDir 'campus_http.py'))) {
    # 脚本放在 tools/ 时，仓库根目录在上一层
    $AppDir = Split-Path -Parent $AppDir
}
$LoginScript = Join-Path $AppDir 'campus_login.py'
$HttpScript  = Join-Path $AppDir 'campus_http.py'
$TaskName    = 'CampusNetAutoLogin'

# --------------------------------------------------------------------------- #
# 兜底进程：只负责"到点把网卡恢复"，和主流程完全独立
# --------------------------------------------------------------------------- #
if ($RestoreOnly) {
    Start-Sleep -Seconds $Seconds
    $stamp = Get-Date -Format 'HH:mm:ss'
    try {
        Enable-NetAdapter -Name $RestoreAdapter -Confirm:$false
        Add-Content -LiteralPath $RestoreLog -Value "[$stamp] 兜底：已启用网卡 $RestoreAdapter" -Encoding UTF8
    } catch {
        Add-Content -LiteralPath $RestoreLog -Value "[$stamp] 兜底：启用网卡失败 - $_" -Encoding UTF8
    }
    exit 0
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Step([string]$Text) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Text)
}

# --------------------------------------------------------------------------- #
# 提权（注意：所有参数都必须传过去）
# --------------------------------------------------------------------------- #
if (-not $DryRun -and -not (Test-Admin)) {
    Write-Host "需要管理员权限，正在弹出 UAC 授权窗口..." -ForegroundColor Yellow
    $argLine = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$PSCommandPath`" -Seconds $Seconds -Engine $Engine"
    if ($AdapterName) { $argLine += " -AdapterName `"$AdapterName`"" }
    if ($AutoLogin)   { $argLine += ' -AutoLogin' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argLine
    exit 0
}

# --------------------------------------------------------------------------- #
# 过程日志
# --------------------------------------------------------------------------- #
$LogDir = Join-Path $AppDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("restart_nic_{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Start-Transcript -Path $LogFile -Force | Out-Null

function Get-PythonExe {
    $bundled = Join-Path $AppDir 'runtime\python.exe'
    if (Test-Path $bundled) { return $bundled }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $found = Get-ChildItem "$env:LOCALAPPDATA\Python" -Recurse -Filter 'python.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Show-AuthState([string]$Label) {
    $py = Get-PythonExe
    if (-not $py -or -not (Test-Path $LoginScript)) {
        Write-Host "$Label (未能调用登录脚本)" -ForegroundColor DarkGray
        return
    }
    $line = & $py $LoginScript --check 2>&1 | Select-String -Pattern '状态' | Select-Object -Last 1
    if ($line) { Write-Host "$Label $line" } else { Write-Host "$Label (无输出)" -ForegroundColor DarkGray }
}

# --------------------------------------------------------------------------- #
# 选网卡
# --------------------------------------------------------------------------- #
if ($AdapterName) {
    $nic = Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue
    if (-not $nic) { throw "找不到网卡: $AdapterName" }
} else {
    $virtual = 'Virtual|TAP|Hyper-V|VMware|VirtualBox|Loopback|Bluetooth|ZeroTier|WAN Miniport'
    $candidates = @(Get-NetAdapter | Where-Object {
        $_.PhysicalMediaType -eq '802.3' -and $_.InterfaceDescription -notmatch $virtual
    })
    if ($candidates.Count -eq 0) { throw "没有找到物理有线网卡，请用 -AdapterName 手动指定" }
    $nic = $candidates | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
    if (-not $nic) { $nic = $candidates | Select-Object -First 1 }
}

Write-Host ""
Write-Host "目标网卡 : $($nic.Name)" -ForegroundColor Cyan
Write-Host "型号     : $($nic.InterfaceDescription)"
Write-Host "接口号   : $($nic.ifIndex)   当前状态: $($nic.Status)"
Write-Host "禁用时长 : $Seconds 秒 ($([math]::Round($Seconds / 60, 1)) 分钟)"
Write-Host "登录引擎 : $(if ($Engine -eq 'http') { '纯 HTTP（不开浏览器）' } else { '浏览器' })"

# 无线网卡如果连着，禁用有线之后它会接管流量，就测不到"有线那条路"了
$wifiUp = Get-NetAdapter | Where-Object {
    $_.PhysicalMediaType -eq 'Native 802.11' -and $_.Status -eq 'Up'
}
if ($wifiUp) {
    Write-Host ""
    Write-Host "⚠ 注意：无线网卡 $($wifiUp.Name) 当前是连接状态。" -ForegroundColor Yellow
    Write-Host "  禁用有线期间流量会走 WiFi，测到的就不是有线那条路了。" -ForegroundColor Yellow
    Write-Host "  想测有线请先断开 WiFi（或忽略这条，两条路用的是同一套 Dr.COM 逻辑）。" -ForegroundColor Yellow
}
Write-Host ""

if ($DryRun) {
    Write-Host "[演练模式] 即将执行: 禁用网卡 → 等待 $Seconds 秒 → 重新启用 → 等待链路恢复" -ForegroundColor Yellow
    Write-Host "[演练模式] 实际不会改动网卡，也不需要管理员权限" -ForegroundColor Yellow
    Write-Host ""
    Show-AuthState "当前认证状态:"
    Stop-Transcript | Out-Null
    exit 0
}

Show-AuthState "操作前认证状态:"

# 纯 HTTP 测试时，必须提前停用计划任务，否则浏览器版会抢先登录
$useHttpTest = ($AutoLogin -and $Engine -eq 'http')
if ($useHttpTest) {
    Write-Step "临时停用计划任务（测完自动恢复；否则浏览器版会抢先登录）"
    schtasks /Change /TN $TaskName /DISABLE 2>&1 | Out-Null
}

try {
    # 独立的兜底进程：即使本窗口被关闭，到点也会把网卡恢复
    $guardLog = Join-Path $LogDir ("restore_guard_{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $guardSeconds = $Seconds + 5
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
        '-RestoreOnly', '-Seconds', $guardSeconds,
        '-RestoreAdapter', "`"$($nic.Name)`"",
        '-RestoreLog', "`"$guardLog`""
    )

    Write-Host ""
    Write-Step "[1/4] 禁用网卡 ..."
    Disable-NetAdapter -InputObject $nic -Confirm:$false
    Write-Step "      已禁用，开始等待 $Seconds 秒"
    Write-Host "      提示：可以关掉这个窗口，网卡由独立的兜底进程保证恢复。" -ForegroundColor DarkGray
    Write-Host "            兜底记录：$guardLog" -ForegroundColor DarkGray

    $waited = 0
    while ($waited -lt $Seconds) {
        $chunk = [Math]::Min(30, $Seconds - $waited)
        Start-Sleep -Seconds $chunk
        $waited += $chunk
        if ($waited -lt $Seconds) {
            Write-Step ("      等待中…… 还剩 {0} 秒" -f ($Seconds - $waited))
        }
    }

    Write-Step "[2/4] 重新启用网卡 ..."
    Enable-NetAdapter -InputObject $nic -Confirm:$false

    Write-Step "[3/4] 等待链路和 IP 就绪 ..."
    $deadline = (Get-Date).AddSeconds(90)
    $ip = $null
    while ((Get-Date) -lt $deadline) {
        $adapter = Get-NetAdapter -Name $nic.Name -ErrorAction SilentlyContinue
        if ($adapter -and $adapter.Status -eq 'Up') {
            $addr = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1
            if ($addr) { $ip = $addr.IPAddress; break }
        }
        Start-Sleep -Seconds 2
    }
    if ($ip) {
        Write-Step "      链路已恢复，IP: $ip"
    } else {
        Write-Step "      等待超时，请检查网线是否插好 / 交换机端口是否正常"
    }

    Write-Step "[4/4] 检查认证状态 ..."
    Show-AuthState "      操作后认证状态:"

    if ($AutoLogin) {
        Write-Host ""
        if ($Engine -eq 'http') {
            Write-Step "用【纯 HTTP 模式】执行一次登录（验证不需要浏览器的方案）"
        } else {
            Write-Step "立即执行一次自动登录（会打开浏览器窗口便于观察）"
        }

        $py = Get-PythonExe
        if ($py) {
            if ($Engine -eq 'http') {
                & $py $HttpScript --login
            } else {
                & $py $LoginScript --login --show
            }
        } else {
            Write-Host "找不到 python.exe" -ForegroundColor Red
        }
    }
}
finally {
    if ($useHttpTest) {
        Write-Step "恢复计划任务（若纯 HTTP 失败，浏览器版会在 1 分钟内兜底恢复网络）"
        schtasks /Change /TN $TaskName /ENABLE 2>&1 | Out-Null
    }
    # 双保险：确保网卡是启用的
    try {
        $now = Get-NetAdapter -Name $nic.Name -ErrorAction SilentlyContinue
        if ($now -and $now.AdminStatus -ne 'Up') {
            Enable-NetAdapter -InputObject $now -Confirm:$false
            Write-Step "收尾：网卡已重新启用"
        }
    } catch { }
}

Write-Host ""
Write-Host "完成。" -ForegroundColor Yellow
Write-Step "过程日志: $LogFile"
Stop-Transcript | Out-Null
