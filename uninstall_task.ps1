# 卸载校园网自动登录的开机自启
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1

$ErrorActionPreference = 'Stop'
$TaskName = 'CampusNetAutoLogin'

schtasks /Delete /TN $TaskName /F 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "已删除计划任务: $TaskName" -ForegroundColor Green
} else {
    Write-Host "没有找到计划任务: $TaskName"
}
