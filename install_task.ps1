# 安装校园网自动登录的开机自启（不需要管理员权限）
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1

param(
    # http = 纯 HTTP 模式(默认, 已实测通过, 无需额外组件)
    # browser = 浏览器模式(像人一样操作页面, 兼容性最好, 需要 playwright)
    [ValidateSet('browser', 'http')]
    [string]$Engine = 'http'
)

$ErrorActionPreference = 'Stop'

$AppDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'CampusNetAutoLogin'

if ($Engine -eq 'http') {
    $Script = Join-Path $AppDir 'campus_http.py'
    $Secret = Join-Path $AppDir 'secret.json'
} else {
    $Script = Join-Path $AppDir 'campus_login.py'
    $Secret = Join-Path $AppDir 'secret.bin'
}

if (-not (Test-Path $Script)) {
    throw "找不到 $Script"
}
if (-not (Test-Path $Secret)) {
    Write-Host "还没有保存账号密码, 请先执行:" -ForegroundColor Yellow
    Write-Host "    python `"$Script`" --set-password"
    exit 1
}

# 优先用随包自带的运行环境，其次才用系统 Python
$BundledPythonW = Join-Path $AppDir 'runtime\pythonw.exe'
if (Test-Path $BundledPythonW) {
    $PythonW = $BundledPythonW
} else {
    $RealPython = (& python -c "import sys; print(sys.executable)").Trim()
    $PythonW    = Join-Path (Split-Path $RealPython) 'pythonw.exe'
    if (-not (Test-Path $PythonW)) { $PythonW = $RealPython }
}

$UserSid    = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$StartTime  = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
$Arguments  = '"' + $Script + '" --login --quiet'

$Xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>登录后自动完成校园网认证, 掉线自动重连</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$UserSid</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <StartBoundary>$StartTime</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$UserSid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$PythonW</Command>
      <Arguments>$Arguments</Arguments>
      <WorkingDirectory>$AppDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$XmlPath = Join-Path $env:TEMP 'campus_net_task.xml'
[System.IO.File]::WriteAllText($XmlPath, $Xml, [System.Text.Encoding]::Unicode)

schtasks /Create /TN $TaskName /XML $XmlPath /F
$code = $LASTEXITCODE
Remove-Item $XmlPath -Force -ErrorAction SilentlyContinue
if ($code -ne 0) { throw "创建计划任务失败" }

Write-Host ""
Write-Host "已安装计划任务: $TaskName" -ForegroundColor Green
Write-Host "  - 登录 Windows 20 秒后首次执行, 之后每 1 分钟检查一次"
Write-Host "  - 电池供电时照常运行; 已在运行时不会重复启动"
Write-Host "  - 日志: $AppDir\logs\campus_login.log"
Write-Host "  - 立即试跑: schtasks /Run /TN $TaskName"
Write-Host "  - 查看状态: schtasks /Query /TN $TaskName /V /FO LIST"
