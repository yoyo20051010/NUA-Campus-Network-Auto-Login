<#
    打包成可以直接发给同学的压缩包

      powershell -ExecutionPolicy Bypass -File .\make_release.ps1            # 完整版（含浏览器组件，约 50MB）
      powershell -ExecutionPolicy Bypass -File .\make_release.ps1 -Lite      # 精简版（纯 HTTP，约 11MB）
      powershell -ExecutionPolicy Bypass -File .\make_release.ps1 -Both      # 两个都生成

    两个版本都自带 Python 运行环境，同学不需要安装任何东西。
#>
param(
    [switch]$Lite,
    [switch]$Both
)

$ErrorActionPreference = 'Stop'

$AppDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $AppDir 'campus_http.py'))) {
    # 脚本放在 tools/ 时，仓库根目录在上一层
    $AppDir = Split-Path -Parent $AppDir
}
$Version = '1.2'

function Build-Package {
    param([bool]$IsLite)

    $suffix  = if ($IsLite) { '标准版' } else { '含浏览器组件' }
    $foldTag = if ($IsLite) { 'lite' } else { 'full' }
    $Stage   = Join-Path $env:TEMP "campus-net-release-$foldTag"
    $PkgName = "校园网自动登录_v${Version}_$suffix"
    $Pkg     = Join-Path $Stage $PkgName
    $Zip     = Join-Path $AppDir "$PkgName.zip"

    if (Test-Path -LiteralPath $Stage) {
        $stageFull = [System.IO.Path]::GetFullPath($Stage)
        $tempFull  = [System.IO.Path]::GetFullPath($env:TEMP)
        if (-not $stageFull.StartsWith($tempFull, [StringComparison]::OrdinalIgnoreCase)) {
            throw "暂存路径异常，已中止：$stageFull"
        }
        Remove-Item -LiteralPath $Stage -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Pkg -Force | Out-Null

    # 同学需要的文件（不含账号密码、日志、浏览器缓存、测试/调试组件）
    $files = @(
        'AAA一键安装.bat', '卸载.bat', 'setup.ps1',
        'campus_login.py', 'campus_http.py', 'config.json', 'requirements.txt',
        'install_task.ps1', 'uninstall_task.ps1',
        'AAA使用说明.txt', '常见问题.txt'
    )
    foreach ($f in $files) {
        $src = Join-Path $AppDir $f
        if (-not (Test-Path $src)) { throw "缺少文件: $f" }
        Copy-Item -LiteralPath $src -Destination $Pkg
    }
    Copy-Item -LiteralPath (Join-Path $AppDir 'openwrt') -Destination $Pkg -Recurse

    # 自带运行环境
    $RuntimeSrc = Join-Path $AppDir 'runtime'
    if (-not (Test-Path $RuntimeSrc)) { throw "缺少 runtime 目录（自带运行环境）" }
    $RuntimeDst = Join-Path $Pkg 'runtime'
    Copy-Item -LiteralPath $RuntimeSrc -Destination $RuntimeDst -Recurse

    $stageFull = [System.IO.Path]::GetFullPath($Stage)
    $rtFull    = [System.IO.Path]::GetFullPath($RuntimeDst)
    if (-not $rtFull.StartsWith($stageFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "路径校验失败，已中止：$rtFull"
    }

    if ($IsLite) {
        # 标准版：去掉 playwright，只保留 Python 本体（纯 HTTP 模式开箱可用，也是默认模式）
        $sp = Join-Path $rtFull 'Lib\site-packages'
        Get-ChildItem -LiteralPath $sp -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notlike 'pip*' } |
            Remove-Item -Recurse -Force
    } else {
        Get-ChildItem -LiteralPath $rtFull -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force
    }

    # 校验运行环境可用
    Write-Host "[$suffix] 校验运行环境 ..." -ForegroundColor Cyan
    $stagedPy = Join-Path $RuntimeDst 'python.exe'
    $pyProbe = if ($IsLite) { "import ssl, urllib.request; print('OK')" }
               else { "import playwright, ssl, urllib.request; print('OK')" }
    $probe = & $stagedPy -c $pyProbe 2>&1
    if ($probe -notmatch 'OK') { throw "[$suffix] 运行环境校验失败: $probe" }

    foreach ($f in @('campus_login.py', 'campus_http.py')) {
        & $stagedPy -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" (Join-Path $Pkg $f)
        if ($LASTEXITCODE -ne 0) { throw "[$suffix] 语法校验失败: $f" }
    }
    Get-ChildItem -LiteralPath $RuntimeDst -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force

    if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Compress-Archive -Path $Pkg -DestinationPath $Zip -CompressionLevel Optimal

    $size = [math]::Round((Get-Item -LiteralPath $Zip).Length / 1KB, 0)
    Write-Host "[$suffix] 已生成: $Zip  ($size KB)" -ForegroundColor Green
}

if ($Both) { Build-Package -IsLite $false; Build-Package -IsLite $true }
elseif ($Lite) { Build-Package -IsLite $true }
else { Build-Package -IsLite $false }
