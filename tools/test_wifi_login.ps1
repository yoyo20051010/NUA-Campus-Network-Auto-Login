<#
.SYNOPSIS
    无线（校园 WiFi）登录流程测试。

    流程：禁用有线网卡 → 等 WiFi 接管 → 用纯 HTTP 模式登录（走 Dr.COM 门户那套）
          → 无论成败都恢复有线网卡。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\test_wifi_login.ps1 -DryRun   # 演练
    powershell -ExecutionPolicy Bypass -File .\test_wifi_login.ps1           # 实际测试

.NOTES
    需要管理员权限（会弹 UAC）。
    禁用有线网卡期间本机会短暂断网，由 WiFi 接管；测试完成立即恢复有线。
    另外会拉起独立兜底进程：即使窗口被关掉，-HoldSeconds 秒后有线网卡也会自动恢复。
#>
param(
    [int]$WaitSeconds = 60,     # 最多等多久让 WiFi 接管
    [int]$HoldSeconds = 180,    # 兜底进程最多多久后强制恢复有线网卡
    [switch]$DryRun,
    # 内部参数
    [switch]$RestoreOnly,
    [string]$RestoreAdapter = '',
    [string]$RestoreLog = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8

$AppDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $AppDir 'campus_http.py'))) {
    # 脚本放在 tools/ 时，仓库根目录在上一层
    $AppDir = Split-Path -Parent $AppDir
}
$HttpScript = Join-Path $AppDir 'campus_http.py'
$TaskName   = 'CampusNetAutoLogin'

# --------------------------------------------------------------------------- #
# 兜底进程：到点无条件把有线网卡恢复
# --------------------------------------------------------------------------- #
if ($RestoreOnly) {
    Start-Sleep -Seconds $HoldSeconds
    $stamp = Get-Date -Format 'HH:mm:ss'
    try {
        Enable-NetAdapter -Name $RestoreAdapter -Confirm:$false
        Add-Content -LiteralPath $RestoreLog -Value "[$stamp] 兜底：已恢复有线网卡 $RestoreAdapter" -Encoding UTF8
    } catch {
        Add-Content -LiteralPath $RestoreLog -Value "[$stamp] 兜底：恢复失败 - $_" -Encoding UTF8
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

if (-not $DryRun -and -not (Test-Admin)) {
    Write-Host "需要管理员权限，正在弹出 UAC 授权窗口..." -ForegroundColor Yellow
    $argLine = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$PSCommandPath`" -WaitSeconds $WaitSeconds -HoldSeconds $HoldSeconds"
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argLine
    exit 0
}

$LogDir = Join-Path $AppDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("wifi_test_{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Start-Transcript -Path $LogFile -Force | Out-Null

function Get-PythonExe {
    $bundled = Join-Path $AppDir 'runtime\python.exe'
    if (Test-Path $bundled) { return $bundled }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Show-AuthState([string]$Label) {
    $py = Get-PythonExe
    if (-not $py) { Write-Host "$Label (找不到 python)" -ForegroundColor DarkGray; return }
    $line = & $py $HttpScript --check 2>&1 | Select-String -Pattern '状态' | Select-Object -Last 1
    if ($line) { Write-Host "$Label $line" } else { Write-Host "$Label (无输出)" -ForegroundColor DarkGray }
}

# --------------------------------------------------------------------------- #
# 找网卡：有线（要禁用的）和无线（要用它测试的）
# --------------------------------------------------------------------------- #
$virtual = 'Virtual|TAP|Hyper-V|VMware|VirtualBox|Loopback|Bluetooth|ZeroTier|WAN Miniport'
$wired = @(Get-NetAdapter | Where-Object {
    $_.PhysicalMediaType -eq '802.3' -and $_.InterfaceDescription -notmatch $virtual
}) | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
if (-not $wired) { $wired = @(Get-NetAdapter | Where-Object {
    $_.PhysicalMediaType -eq '802.3' -and $_.InterfaceDescription -notmatch $virtual
}) | Select-Object -First 1 }

$wifi = Get-NetAdapter | Where-Object { $_.PhysicalMediaType -eq 'Native 802.11' } | Select-Object -First 1

if (-not $wired) { throw "没有找到有线网卡" }
if (-not $wifi)  { throw "没有找到无线网卡" }

$wifiIp = (Get-NetIPAddress -InterfaceIndex $wifi.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "有线网卡（测试期间禁用）: $($wired.Name)  [$($wired.InterfaceDescription)]" -ForegroundColor Cyan
Write-Host "无线网卡（用它上网）    : $($wifi.Name)  [$($wifi.InterfaceDescription)]" -ForegroundColor Cyan
Write-Host "无线当前地址            : $(if ($wifiIp) { $wifiIp } else { '（未连接！）' })"
Write-Host ""

if (-not $wifiIp -and -not $DryRun) {
    Write-Host "无线网卡没有拿到地址，请先连上校园 WiFi 再运行。" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
}

if ($DryRun) {
    Write-Host "[演练模式] 即将执行: 禁用有线 → 等 WiFi 接管 → 纯 HTTP 登录 → 恢复有线" -ForegroundColor Yellow
    Show-AuthState "当前认证状态:"
    Stop-Transcript | Out-Null
    exit 0
}

# 纯 HTTP 测试期间先停掉计划任务，避免浏览器版抢先登录
Write-Step "临时停用计划任务（测完自动恢复）"
schtasks /Change /TN $TaskName /DISABLE 2>&1 | Out-Null

$guardLog = Join-Path $LogDir ("wifi_guard_{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
try {
    # 独立兜底：即使窗口被关，也会在 HoldSeconds 秒后恢复有线网卡
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
        '-RestoreOnly', '-HoldSeconds', $HoldSeconds,
        '-RestoreAdapter', "`"$($wired.Name)`"",
        '-RestoreLog', "`"$guardLog`""
    )

    Show-AuthState "禁用有线前:"

    Write-Step "[1/4] 禁用有线网卡 ..."
    Disable-NetAdapter -InputObject $wired -Confirm:$false
    Write-Step "      已禁用（此刻起走 WiFi）；兜底进程会在 $HoldSeconds 秒后强制恢复有线"

    Write-Step "[2/4] 等待 WiFi 接管 ..."
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://10.255.255.2/" -TimeoutSec 5 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if ($ready) {
        Write-Step "      门户已可通过 WiFi 访问"
    } else {
        Write-Step "      等待超时，仍然继续尝试登录"
    }

    Show-AuthState "      此时状态:"

    Write-Step "[3/4] 用纯 HTTP 模式登录（走无线那套 Dr.COM 流程）..."
    $py = Get-PythonExe
    if ($py) { & $py $HttpScript --login } else { Write-Host "找不到 python.exe" -ForegroundColor Red }

    Write-Step "[4/4] 查看登录结果 ..."
    Show-AuthState "      登录后状态:"
}
finally {
    Write-Step "恢复有线网卡 ..."
    try {
        $now = Get-NetAdapter -Name $wired.Name -ErrorAction SilentlyContinue
        if ($now -and $now.AdminStatus -ne 'Up') { Enable-NetAdapter -InputObject $now -Confirm:$false }
    } catch { }
    Write-Step "恢复计划任务 ..."
    schtasks /Change /TN $TaskName /ENABLE 2>&1 | Out-Null
}

Write-Host ""
Write-Host "完成。过程日志: $LogFile" -ForegroundColor Yellow
Write-Host "无线登录日志: $(Join-Path $LogDir 'campus_http.log')" -ForegroundColor Yellow
Stop-Transcript | Out-Null
