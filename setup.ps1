<#
    校园网自动登录 —— 安装向导

    同学直接双击同目录下的「AAA一键安装.bat」即可，不需要敲任何命令。

    参数（一般不用管）:
      -Engine http|browser   指定登录方式
      -Yes                   全程默认选项
      -Uninstall             卸载
#>
param(
    [ValidateSet('http', 'browser')]
    [string]$Engine,
    [switch]$Yes,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8
try { $Host.UI.RawUI.WindowTitle = '校园网自动登录 - 安装向导' } catch { }

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

function Say($Text, $Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Title($Text) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor DarkCyan
}
function Pause-For($Message = "按回车继续") {
    Write-Host ""
    Read-Host $Message | Out-Null
}
function Ask($Prompt, $Default = 'y') {
    $hint = if ($Default -eq 'y') { '(Y/n)' } else { '(y/N)' }
    $answer = Read-Host "$Prompt $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return ($Default -eq 'y') }
    return ($answer -match '^[Yy]')
}

# --------------------------------------------------------------------------- #
# 卸载分支
# --------------------------------------------------------------------------- #
if ($Uninstall) {
    Title "卸载校园网自动登录"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AppDir 'uninstall_task.ps1')
    if (Ask "要同时删除已保存的账号密码吗?" 'n') {
        Remove-Item (Join-Path $AppDir 'secret.bin') -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $AppDir 'secret.json') -Force -ErrorAction SilentlyContinue
        Say "账号密码已删除。" 'Green'
    }
    Say "卸载完成。" 'Green'
    exit 0
}

# --------------------------------------------------------------------------- #
# 开场说明（先讲清楚是什么、做什么，避免不看文档就直接点）
# --------------------------------------------------------------------------- #
Title "校园网自动登录 · 安装向导"

Say "【这个程序是干什么的】" 'White'
Say "  · 开机（或重新联网）后自动帮你完成校园网登录"
Say "  · 不用再手动打开网页、输账号密码、拖滑块"
Say "  · 掉线后 1 分钟内自动重连"
Write-Host ""

Say "【它什么时候才会动手】" 'White'
Say "  · 必须同时满足：校园网认证页能打开、而且当前确实没登录"
Say "  · 连家里的 WiFi、手机热点、或走 VPN 时，它不会去试密码"
Write-Host ""

Say "【你的账号密码】" 'White'
Say "  · 只保存在这台电脑上，仅用于自动登录"
Say "  · 不会上传到任何地方，也不会发给别人"
Say "  · 卸载时可以选择一并删除"
Write-Host ""

Say "【需要你准备什么】" 'White'
Say "  · 什么都不用装，运行环境已经打包在里面了"
Say "  · 只需要知道你的校园网账号和密码"
Write-Host ""

Say "如果你不同意以上任意一条，现在直接关掉这个窗口即可。" 'Yellow'
Pause-For "了解并同意，按回车开始安装"

# --------------------------------------------------------------------------- #
Title "第 1 步 / 共 4 步：检查运行环境"

$BundledPython = Join-Path $AppDir 'runtime\python.exe'
if (Test-Path $BundledPython) {
    $py = $BundledPython
    $ver = (& $py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim()
    Say "已使用随包自带的运行环境（Python $ver），无需安装任何东西。" 'Green'
    $hasRuntime = $true
} else {
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        Say "没有找到运行环境，也没有检测到系统 Python。" 'Yellow'
        Say "请重新完整解压安装包（完整包里自带运行环境）。"
        Pause-For "按回车退出"
        exit 1
    }
    $py = $pythonCmd.Source
    $ver = (& $py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim()
    Say "已找到系统 Python $ver" 'Green'
    $hasRuntime = $false
}

# --------------------------------------------------------------------------- #
Title "第 2 步 / 共 4 步：选择登录方式"

if (-not $Engine) {
    Say "  [1] 纯 HTTP 模式  ← 推荐，直接回车即可"
    Say "      不开浏览器，直接用网络请求完成登录；实测不到 1 秒完成。"
    Say "  [2] 浏览器模式"
    Say "      像人一样打开 Edge 操作登录页面，兼容性更好；首次需要下载约 40MB 组件。"
    Write-Host ""
    Say "  平时用哪种都行，纯 HTTP 更快更省资源；如果哪天学校改了页面导致"
    Say "  纯 HTTP 登录失败，可以重新运行本安装程序改选浏览器模式。" 'DarkGray'
    $choice = Read-Host "请输入 1 或 2（直接回车 = 1）"
    $Engine = if ($choice -eq '2') { 'browser' } else { 'http' }
}

if ($Engine -eq 'http') {
    $mainScript = Join-Path $AppDir 'campus_http.py'
    Say "已选择：纯 HTTP 模式" 'Green'
} else {
    $mainScript = Join-Path $AppDir 'campus_login.py'
    Say "已选择：浏览器模式" 'Green'
}

# --------------------------------------------------------------------------- #
if ($Engine -eq 'browser') {
    Title "准备浏览器组件"
    $probe = & $py -c "import playwright; print('OK')" 2>&1
    if ($probe -match 'OK') {
        Say "本包已自带 playwright，跳过下载。" 'Green'
    } else {
        Say "正在安装 playwright（首次需要一两分钟）..."
        & $py -m pip install --disable-pip-version-check -q playwright
        if ($LASTEXITCODE -ne 0) {
            Say "默认源失败，改用清华镜像重试..." 'Yellow'
            & $py -m pip install --disable-pip-version-check -q playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
        }
        if ($LASTEXITCODE -ne 0) {
            Say "组件安装失败。建议改用纯 HTTP 模式：重新运行安装程序并选 [1]。" 'Red'
            Pause-For "按回车退出"
            exit 1
        }
        Say "playwright 安装完成。" 'Green'
    }

    $edge = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($edge) {
        Say "检测到系统 Edge，直接复用它登录（不用再下载浏览器）。" 'Green'
    } else {
        Say "没检测到 Edge，需要额外下载浏览器内核（约 130MB）..." 'Yellow'
        & $py -m playwright install chromium
    }
}

# --------------------------------------------------------------------------- #
Title "第 3 步 / 共 4 步：保存账号密码"
Say "接下来请输入你的校园网账号和密码（输入密码时屏幕不会显示，属正常现象）。"
Say "如果只在宿舍用有线网，就填平时登录校园网用的那个学号和密码即可。"
Write-Host ""
& $py $mainScript --set-password
if ($LASTEXITCODE -ne 0) {
    Say "账号密码保存失败，请重试。" 'Red'
    Pause-For "按回车退出"
    exit 1
}

Write-Host ""
Say "顺手检测一下当前网络状态："
& $py $mainScript --check

# --------------------------------------------------------------------------- #
Title "第 4 步 / 共 4 步：设置开机自启"

Say "开启后，每次登录 Windows 会自动检查一次，之后每分钟检查一次，"
Say "发现掉线就自动重连。它只在校园网环境下动作，不会影响其他网络。"
Write-Host ""

$wantAuto = $true
if (-not $Yes) {
    $wantAuto = Ask "要开启开机自动运行吗?" 'y'
}

if ($wantAuto) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AppDir 'install_task.ps1') -Engine $Engine
    if ($LASTEXITCODE -eq 0) {
        Say "已开启开机自动运行（登录 Windows 20 秒后首次执行）。" 'Green'
    } else {
        Say "自启设置失败，可以稍后手动运行 install_task.ps1。" 'Yellow'
    }
} else {
    Say "已跳过。以后想开启，运行同目录下的 install_task.ps1 即可。" 'Yellow'
}

# --------------------------------------------------------------------------- #
Write-Host ""
if (-not $Yes) {
    if (Ask "现在测试一次登录吗?（已在线的话会提示跳过）" 'y') {
        Write-Host ""
        & $py $mainScript --login
    }
}

Title "安装完成"
Say "登录方式：$(if ($Engine -eq 'http') { '纯 HTTP' } else { '浏览器' })"
Say "账号密码：已保存，不需要重复输入"
Say "开机自启：$(if ($wantAuto) { '已开启' } else { '未开启' })"
Say "运行日志：$AppDir\logs\"
Say "想卸载：双击「卸载.bat」"
Write-Host ""
Say "小提示：如果校园网在晚上 12 点断网，脚本会在断网后 1 分钟内自动重连；" 'DarkGray'
Say "但学生账号能否在夜里重新认证由学校策略决定，脚本本身无法改变。" 'DarkGray'
