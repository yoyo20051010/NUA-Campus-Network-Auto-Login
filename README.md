# NUA Campus Network Auto Login

**南京艺术学院校园网自动登录工具** · 开机自动联网，掉线自动重连，不用再手动点登录、拖滑块。

> Automatic campus network login for Nanjing University of the Arts (NUA).
> Logs in on boot, reconnects within a minute after a drop, and never touches
> other networks. Works on Windows; an OpenWrt router build is included too.

---

## 它能做什么

- **开机自动登录**：登录 Windows 20 秒后检查一次，之后每分钟检查一次
- **掉线自动重连**：断网后 1 分钟内自动恢复
- **两套认证流程都支持**：Dr.COM 门户表单（有线/无线通用）和统一身份认证（CAS）
  - ⚠️ 有线走 CAS 时，提交后的「安全验证」**不一定是拼图滑块**——部分账号会遇到**人脸识别**
    （`loginType=4`，需要真人对着摄像头），自动化无法完成。详见 [docs/技术细节.md](docs/技术细节.md)
- **默认走 Dr.COM 表单**：实测（2026-09-16）有线网段同样可用，可绕开 CAS 的滑块/人脸验证，
  也不需要 RSA 加密和浏览器。可用 `config.json` 的 `wired_flow` 改回 `cas`
- **多平台**：Windows（纯 HTTP / 浏览器）、路由器（OpenWrt）、**macOS（原生）**
- **不乱试密码**：只有在「校园网认证页能打开」且「当前确实未认证」时才动作，
  连着家里 WiFi、手机热点或 VPN 时直接跳过
- **夜间免打扰**：夜间限制时段（默认周一~周五 00:00–06:00）完全不尝试，也不发请求
- **失败退避**：登录失败后按 2/5/15/30 分钟放慢重试，日志不会被刷屏
- **密码只存本机**：Windows 用 DPAPI 加密（浏览器模式）或本地文件（HTTP 模式），不上传任何地方

---

## 快速开始

### 方式一：下载安装包（推荐，零依赖）

在 [Releases](../../releases) 里下载 `NUA-Campus-Network-Auto-Login_v*_standard.zip`（约 13MB，**自带 Python 运行环境**），
解压后双击 `AAA一键安装.bat`，按向导输入校园网账号密码即可。

### 方式二：从源码运行

需要 Python 3.10+。

```bash
git clone https://github.com/<your-name>/NUA-Campus-Network-Auto-Login.git
cd NUA-Campus-Network-Auto-Login

# 纯 HTTP 模式（推荐，只需要标准库）
python campus_http.py --set-password   # 保存账号密码
python campus_http.py --check          # 检测当前是否已认证
python campus_http.py --login          # 登录一次

# 浏览器模式（需要额外装 playwright）
pip install playwright
python campus_login.py --set-password
python campus_login.py --login --show  # 带窗口，能看见全过程
```

想要开机自启（Windows）：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Engine http
```

### 方式三：macOS（原生，开机自动联网）

macOS 版是原生实现（纯 Python 标准库，不需要 pip、不需要浏览器），针对 mac 做了专门处理：
绑定物理网卡绕过 TUN 模式 VPN、自带迷你 DNS 绕过 Clash 的 fake-ip、密码存**钥匙串**、
launchd 开机自启、夜间静默、掉线自动重连。

```bash
cd macos
sh install.sh          # 存账号密码到钥匙串 + 安装开机自启 + 自动体检
```

详见 [macos/README.md](macos/README.md)，实测记录见
[docs/macOS适配与实测记录.md](docs/macOS适配与实测记录.md)。

---

## 它是怎么工作的

学校的认证链路有两层：Dr.COM 门户 + 统一身份认证（CAS）。默认**两段都优先走 Dr.COM 表单**
（`config.json` 的 `wired_flow` 可改），CAS 作为后备：

```
连接校园网
   │
   ├─ 默认（有线/无线通用）── Dr.COM 门户表单
   │                          POST /eportal/?c=ACSetting&a=Login&ver=1.0
   │                          字段 DDDDD / upass → 成功返回 Dr.COMWebLoginID_3.htm
   │                          （服务类型默认「校园用户」，另有 @dx / @lt）
   │
   └─ 后备：统一身份认证（CAS）
                              门户按客户端 IP 把有线引导到这里
                              账号密码 → 提交 → 服务器返回「请完成安全验证」
                              → 拼图滑块（拖到位后 POST /cas/captchValid/checkCaptchImg）
                              ⚠️ 部分账号这里是**人脸识别**，自动化无法完成
```

几个实现上的关键点（踩过的坑，详见 [docs/技术细节.md](docs/技术细节.md)）：

- **滑块是提交之后才出现的**：登录页里那个滑块默认是隐藏的，点了「登录」服务器才会要求完成验证，
  所以自动化必须"先提交、再拖滑块"，顺序反了就永远失败。
- **密码加密不是标准 RSA**：学校页面用的是教科书式 RSA + 零填充 + 小端字节序，
  没有 PKCS#1 填充、也没有随机数，用通用库反而会对不上。
- **无线那套接口有三个必须一致的细节**：URL 要带 `ver=1.0`、请求要带 AJAX 头、
  Host 头不能带端口，否则 AC 会返回后台管理页面而不是登录结果。

---

## 目录结构

```
.
├── AAA一键安装.bat        给同学的安装入口（双击即可）
├── 卸载.bat               卸载入口
├── setup.ps1              安装向导（开场说明 / 选模式 / 存密码 / 引导自启）
├── campus_http.py         纯 HTTP 模式（默认，零依赖）
├── campus_login.py        浏览器模式（需要 playwright + 系统 Edge）
├── install_task.ps1       注册/更新开机自启任务
├── uninstall_task.ps1     删除开机自启任务
├── config.json            可调参数（夜间时段、退避、超时等）
├── AAA使用说明.txt        给同学的说明书
├── 常见问题.txt           给同学的 FAQ
├── openwrt/               路由器（OpenWrt）版本
│   ├── install.sh
│   ├── campus-net-login.init
│   ├── campus_http.py
│   └── README.md
├── docs/
│   └── 技术细节.md        认证流程、加密算法、调试方法
└── tools/                 开发/调试工具（不影响日常使用）
    ├── make_release.ps1   打包安装包（含自带运行环境）
    ├── selftest_*.py      自测（加密实现比对、表单识别、滑块拖拽）
    ├── probe_*.py         探测工具（网段判断、登录接口参数）
    ├── restart_nic.ps1    强制重新认证（禁用网卡 5 分钟）
    └── test_wifi_login.ps1 无线登录流程测试
```

---

## 常见问题

见 [常见问题.txt](常见问题.txt)（面向使用者）。

几个高频问题：

- **晚上 12 点断网能解决吗？** 不能——那是学校的账号策略。工具会在断网后 1 分钟内重连，
  但学生账号夜间能否重新认证由学校决定。工具默认在 00:00–06:00 干脆不尝试，避免无谓请求。
- **会不会在别的网络下乱试密码？** 不会，见上面的「不乱试密码」。
- **换电脑要重新配置吗？** 要。密码跟本机绑定，需要在每台电脑上各运行一次安装向导。
- **为什么手动点「注销」没用？** 学校统一认证的注销接口当前故障（返回 `Radius注销失败`），
  与本工具无关。

---

## 免责声明

- 本工具只是**替你完成你自己本来就要做的登录动作**，不修改认证结果、不绕过认证、不共享给他人。
- 请自行确认你的使用方式符合学校网络管理规定。
- 请勿把带密码的 `secret.json` / `secret.bin` 提交到仓库或发给他人。

---

## License

[MIT](LICENSE)
