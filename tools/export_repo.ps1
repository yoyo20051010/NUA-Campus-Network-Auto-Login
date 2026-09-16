<#
    导出一份"可以直接传到 GitHub 的干净副本"。

    用**白名单**复制文件，所以密码、日志、自带运行环境、打包产物都不会被带出去。

        powershell -ExecutionPolicy Bypass -File .\tools\export_repo.ps1
        powershell -ExecutionPolicy Bypass -File .\tools\export_repo.ps1 -Destination "D:\somewhere"
#>
param([string]$Destination = '')

$ErrorActionPreference = 'Stop'

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $AppDir 'campus_http.py'))) {
    $AppDir = Split-Path -Parent $AppDir      # 脚本在 tools/ 里
}

if (-not $Destination) {
    $Destination = Join-Path (Split-Path -Parent $AppDir) 'NUA-Campus-Network-Auto-Login'
}

if (Test-Path -LiteralPath $Destination) {
    # 只清理我们自己的导出目录（校验路径确实在预期位置）
    $destFull = [System.IO.Path]::GetFullPath($Destination)
    if ($destFull -notmatch 'NUA-Campus-Network-Auto-Login$') {
        throw "导出目录名不符，已中止：$destFull"
    }
    # 保留 .git（否则会把 git 仓库、远程配置和推送记录一起删掉）
    Get-ChildItem -LiteralPath $Destination -Force |
        Where-Object { $_.Name -ne '.git' } |
        Remove-Item -Recurse -Force
} else {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}

# 白名单：只会复制这里列出的东西
$files = @(
    'README.md', 'LICENSE', '.gitignore', '.gitattributes',
    'AAA一键安装.bat', '卸载.bat', 'setup.ps1',
    'install_task.ps1', 'uninstall_task.ps1',
    'campus_http.py', 'campus_login.py', 'config.json', 'requirements.txt',
    'AAA使用说明.txt', '常见问题.txt'
)
$dirs = @('docs', 'openwrt', 'tools', 'macos', '.github')

foreach ($f in $files) {
    $src = Join-Path $AppDir $f
    if (-not (Test-Path -LiteralPath $src)) { throw "缺少文件: $f" }
    Copy-Item -LiteralPath $src -Destination $Destination
}

foreach ($d in $dirs) {
    $src = Join-Path $AppDir $d
    if (-not (Test-Path -LiteralPath $src)) { throw "缺少目录: $d" }
    Copy-Item -LiteralPath $src -Destination $Destination -Recurse
}

# 清掉可能被带进来的缓存
Get-ChildItem -LiteralPath $Destination -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

# 安全检查：确认没有凭据类文件混进去
$bad = Get-ChildItem -LiteralPath $Destination -Recurse -File |
    Where-Object { $_.Name -in @('secret.json', 'secret.bin') -or $_.Extension -eq '.zip' }
if ($bad) {
    throw ("导出目录里出现了不该有的文件: " + ($bad.Name -join ', '))
}

Write-Host "已导出干净副本: $Destination" -ForegroundColor Green
Write-Host "文件数: $((Get-ChildItem -LiteralPath $Destination -Recurse -File).Count)" -ForegroundColor Cyan
Write-Host ""
Get-ChildItem -LiteralPath $Destination -Recurse -File |
    ForEach-Object { "  " + $_.FullName.Substring($Destination.Length + 1) }
