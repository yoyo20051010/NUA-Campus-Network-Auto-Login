# 卸载校园网自动登录的开机自启
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1

$ErrorActionPreference = 'Stop'
$TaskName = 'CampusNetAutoLogin'

# 计划任务现在跑的是常驻看门狗：光删任务不会停掉已经在跑的那个进程，
# 所以先把它结束掉，再删任务
schtasks /End /TN $TaskName 2>&1 | Out-Null
schtasks /Delete /TN $TaskName /F 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "已删除计划任务: $TaskName" -ForegroundColor Green
} else {
    Write-Host "没有找到计划任务: $TaskName"
}

# 兜底：万一上面没把常驻进程带走（比如任务已被手动禁用），按命令行精确匹配再清一次
$stale = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -and
        $_.CommandLine -match 'campus_(http|login)\.py"?\s+--watch'
    }
foreach ($proc in $stale) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}
