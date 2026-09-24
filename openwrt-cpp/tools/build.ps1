<#
把 C++ 版交叉编译成路由器能跑的静态二进制（aarch64 + musl）。

为什么用 zig 当交叉工具链：
  这台机器上只有 MinGW（只能编 Windows）和 LLVM（缺 Linux sysroot）。
  zig 自带各平台的 libc，一条命令就能产出 aarch64-linux-musl 的静态二进制，
  不用装一整套 OpenWrt SDK。

用法:
  pwsh -File openwrt-cpp/tools/build.ps1                 # 编译
  pwsh -File openwrt-cpp/tools/build.ps1 -Test           # 先跑本机单元测试再编译
  pwsh -File openwrt-cpp/tools/build.ps1 -Zig C:\zig\zig.exe
#>
param(
    [string]$Zig = "",
    [string]$OutDir = "",
    [switch]$Test
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent $here          # openwrt-cpp
$repo = Split-Path -Parent $project          # 仓库根目录

$srcDir = Join-Path $project "src"
if (-not $OutDir) { $OutDir = Join-Path $project "build" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Resolve-Zig {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 zig 不存在: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    if ($env:ZIG) {
        if (-not (Test-Path $env:ZIG)) { throw "环境变量 ZIG 指向的文件不存在: $env:ZIG" }
        return (Resolve-Path $env:ZIG).Path
    }
    $bundled = Get-ChildItem -Path (Join-Path $repo ".toolchain") -Filter zig.exe -Recurse -ErrorAction SilentlyContinue |
               Select-Object -First 1
    if ($bundled) { return $bundled.FullName }
    $onPath = Get-Command zig -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw "找不到 zig。请把它放在 <仓库>/.toolchain/ 下，或用 -Zig 指定路径。"
}

$zigPath = Resolve-Zig -Explicit $Zig
Write-Host "使用工具链: $zigPath" -ForegroundColor Cyan
Write-Host ((& $zigPath version) + " (zig)") -ForegroundColor Cyan

# zig 默认把缓存放在用户目录，沙箱里可能没权限；统一放到仓库内的 .toolchain 下
$cacheRoot = Join-Path $repo ".toolchain"
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $cacheRoot "zigcache"
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $cacheRoot "zigglobal"
New-Item -ItemType Directory -Force -Path $env:ZIG_LOCAL_CACHE_DIR, $env:ZIG_GLOBAL_CACHE_DIR | Out-Null

if ($Test) {
    Write-Host "`n== 本机单元测试（用本机 g++ 编译，只测与平台无关的部分）==" -ForegroundColor Cyan
    $testExe = Join-Path $OutDir "test_core.exe"
    & g++ -std=c++17 -O2 -Wall -Wextra -o $testExe `
        (Join-Path $project "tests\test_core.cpp") `
        (Join-Path $srcDir "json.cpp") `
        (Join-Path $srcDir "rsa.cpp") `
        (Join-Path $srcDir "strings.cpp") `
        (Join-Path $srcDir "portal_parse.cpp")
    if ($LASTEXITCODE -ne 0) { throw "单元测试编译失败" }
    & $testExe (Join-Path $project "tests\rsa_vectors.json")
    if ($LASTEXITCODE -ne 0) { throw "单元测试没通过" }
}

Write-Host "`n== 交叉编译 aarch64-linux-musl ==" -ForegroundColor Cyan
$sources = Get-ChildItem (Join-Path $srcDir "*.cpp") | ForEach-Object { $_.FullName }
$output = Join-Path $OutDir "campus-net-login"

$zigArgs = @(
    "c++",
    "-target", "aarch64-linux-musl",
    "-std=c++17",
    "-Os",
    "-fno-exceptions",
    "-fno-rtti",
    "-ffunction-sections",
    "-fdata-sections",
    "-Wall", "-Wextra",
    "-static",
    "-pthread",
    "-Wl,--gc-sections",
    "-Wl,-s",
    "-o", $output
) + $sources

& $zigPath @zigArgs
if ($LASTEXITCODE -ne 0) { throw "交叉编译失败" }

$size = (Get-Item $output).Length
Write-Host ("`n完成: {0}" -f $output) -ForegroundColor Green
Write-Host ("体积: {0:N0} 字节（约 {1:N0} KB）" -f $size, ($size / 1KB)) -ForegroundColor Green
Write-Host "部署: python tools/router_ssh.py put <二进制> /tmp/campus-net-login" -ForegroundColor DarkGray
