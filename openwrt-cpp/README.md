# 校园网自动登录 —— OpenWrt C++ 版

这是 [openwrt/campus_http.py](../openwrt/campus_http.py)（Python 版）的 C++ 重写。
功能一一对应，配置文件格式完全通用；换的只是"跑在路由器上那点东西"。

## 为什么要重写

Python 版在路由器上的实测开销：

| 项目 | Python 版 | C++ 版 |
| --- | --- | --- |
| 闪存占用（overlay） | 约 8 MB（python3 + 依赖） | 约 0.42 MB（一个静态二进制） |
| 常驻内存 RSS | 28 ~ 48 MB，且约 1 MB/小时增长 | **0.49 MB，稳定不增长** |

内存那一条是硬伤：路由器总共 233 MB，Python 版涨到 60 MB 就会被内核 OOM，
或者触发脚本自己的"内存自保"主动退出。实测就发生过一次 —— 2026-09-21 晚间
服务在内存自保里反复重启，procd 放弃拉起，**整晚没有自动登录，主备切换也失效**。

## 本机工具链情况（2026-09-22 核实）

| 工具 | 情况 | 能不能编 aarch64 |
| --- | --- | --- |
| g++ / gcc | `C:\MinGW\mingw64\bin` | 不能（只能编 Windows） |
| clang / clang++ | LLVM 23，`C:\Program Files\LLVM\bin` | 不能（有 aarch64 后端，但缺 Linux sysroot） |
| cmake | `C:\Program Files\CMake\bin` | — |
| make / ninja / MSVC / Docker / WSL | 都没有 | — |
| **zig 0.14.1** | 下载到 `.toolchain/`，自带各平台 libc | **能**，直接产出 `aarch64-linux-musl` 静态二进制 |

所以正式产物用 **zig 当交叉工具链**：一条命令就能编出路由器能跑的静态二进制，
不用装一整套 OpenWrt SDK。

已经过端到端验证：编译出的二进制在目标路由器（Kwrt 24.10 / MT7981 / aarch64 /
musl / 内核 6.6.116）上正常运行。

## 目录结构

```
openwrt-cpp/
├── src/
│   ├── main.cpp          命令行入口（--check / --login / --watch / --set-password / --mode / --probe）
│   ├── watch.cpp         看门狗循环、mwan3 联动、断网前提前切换、内存自保
│   ├── portal.cpp        门户网络流程：在线判断、Dr.COM 登录、统一身份认证（含滑块）
│   ├── portal_parse.cpp  门户页面的纯文本解析（不依赖平台，可本机测试）
│   ├── http.cpp          极简 HTTP 客户端 + 出口绑定（HTTPS 转交给 curl）
│   ├── rsa.cpp           复刻学校 security.js 的 RSA 加密
│   ├── config.cpp        配置合并、线路、账号类型、夜间时段、退避状态
│   ├── json.cpp          极简 JSON（读写 config/secret/retry）
│   ├── util.cpp          日志滚动、文件、子进程、网卡/内存查询
│   └── strings.cpp       字符串工具
├── tests/
│   ├── test_core.cpp         单元测试（JSON / 门户解析 / RSA 对照）
│   ├── gen_rsa_vectors.py    从 Python 参考实现生成 RSA 测试向量
│   └── rsa_vectors.json      生成出来的对照数据
├── tools/
│   ├── build.ps1         Windows 上构建（用 zig 交叉编译）
│   └── build.sh          Linux / macOS 上构建
├── openwrt/
│   ├── campus-net-login.init   procd 服务脚本
│   ├── install.sh              路由器上的一键安装（会保留已有的账号和配置）
│   └── config.json             默认配置（和 Python 版格式一样）
└── CMakeLists.txt        可选：用 CMake 在本机编译/调试
```

## 编译

先把 zig 放到 `<仓库>/.toolchain/zig-x86_64-windows-0.14.1/zig.exe`
（或设环境变量 `ZIG`，或用 `-Zig` 指定路径），然后：

```powershell
pwsh -File openwrt-cpp/tools/build.ps1 -Test
```

产物在 `openwrt-cpp/build/campus-net-login`，约 420 KB。
Linux / macOS 换成 `sh openwrt-cpp/tools/build.sh`。

## 部署 / 从 Python 版升级

把 `openwrt-cpp/openwrt/` 整个目录和编译好的二进制传到路由器，执行安装脚本：

```sh
scp -r openwrt-cpp/openwrt root@192.168.5.1:/tmp/cpp
scp openwrt-cpp/build/campus-net-login root@192.168.5.1:/tmp/cpp/
ssh root@192.168.5.1 'cd /tmp/cpp && sh install.sh'
```

安装脚本会自动：停掉旧的（Python）服务 → 换上新程序 → 保留已有的
`config.json` / `secret.json` / `profiles/`（账号密码不用重填）→ 重新启用服务。

从 Python 版升级后，想彻底省出那 8 MB 闪存，可以手动删掉它的文件：

```sh
rm -f /etc/campus-net-login/campus_http.py*
opkg remove --autoremove python3-light python3-logging python3-codecs python3-urllib python3-openssl
```

（先确认没有别的程序依赖 python3 再删。）

## 常用命令

```sh
/etc/init.d/campus-net-login status          # 服务状态
tail -f /etc/campus-net-login/logs/campus_http.log
/etc/campus-net-login/campus-net-login --check                 # 检测在线状态
/etc/campus-net-login/campus-net-login --probe --profile wan   # 只看门户走哪套流程
/etc/campus-net-login/campus-net-login --mode                  # 看当前账号类型
/etc/campus-net-login/campus-net-login --mode teacher          # 切教师模式
grep VmRSS /proc/$(pgrep -f 'campus-net-login --watch')/status # 看内存占用
```

## 和 Python 版的功能对照

移植时逐条对齐了 Python 版里那些"踩坑换来的"细节：

- RSA 加密：教科书式零填充 + 小端序，指数 65537。**输出与 Python 版逐字节一致**
  （`tests/rsa_vectors.json` 里 10 组样本，含跨块的情况）。
- 认证流程：门户 → 判断网段（有线 10.12.x 优先 Dr.COM、无线 10.54.x 走 Dr.COM）
  → 服务类型依次尝试（默认 / `@njxy` / `@dx` / `@lt`）→ 统一身份认证 + 滑块上报。
- 在线判断：2xx 和 3xx 都算通，但页面或 Location 里出现
  `Dr.COMWebLogin` / `DrcomServer` / `eportal` / `10.255.255.2` 一律算没通。
- 账号级问题停手：`密码` / `验证码` / `在线数超出限制` / `Limit Users` / `已在线`
  → 直接 `fatal`，不再换服务类型重试（避免把账号撞锁）。
- 账号类型：`student` / `teacher`，留空按前缀自动判断（`M` 开头 = 教师）。
  教师模式忽略夜间时段、不退避（`teacher_backoff=false`），只在账号级问题上冷却。
- 夜间时段内仍然检查这条线是否掉线并同步给 mwan3（学校正是断在这个时段），
  但**只允许把它摘出去，绝不再标回可用** —— 否则 0 点整那一下（学生会话还没被
  学校切断时）会把 23:55 刚摘掉的线又标回 up，白弹一次。详见下面的坑 3。
- 断网前提前切换（`pre_switch_minutes`）：夜间时段开始前几分钟就 `ifdown`，
  让正在跑的连接在原线路上跑完。
- mwan3 联动由认证结果驱动，状态没变不重复调用，每 10 分钟强制重申一次。
- 出口绑定：网卡名 → 当前 IPv4 按源地址绑定，并自动维护
  `from <ip> lookup <table> priority 500` 的策略路由（Linux 按目的地址选路，
  光绑源地址不够）。
- 多线路：一个进程里多条线并行看护，每条线独立目录、独立日志。
- 日志滚动（512 KB × 2）、页面存档只留最近 30 份、内存自保上限。

### 几处刻意的取舍

**HTTP 自己写，HTTPS 交给 curl。** 路由器上本来就带 `curl`（含 OpenSSL），
而静态链 OpenSSL/mbedTLS 会让二进制涨到好几 MB —— 那就白重写了。
所以纯 HTTP 走自己的 socket 实现，HTTPS（统一身份认证那几步）转交给 curl，
命令行参数直接 `exec` 过去、不经 shell，没有注入和转义问题。
如果哪天路由器上没有 curl，HTTPS 分支会失败，但 Dr.COM 这条主路（本校有线和无线
用的都是它）不受影响。

**不用 `std::regex`。** 要解析的就几个固定形状，手写扫描更可控，也不会因模板
实例化把二进制撑大。

**关掉异常和 RTTI**（`-fno-exceptions -fno-rtti`）。省体积；真出了意料之外的
问题进程会退出，procd 会拉起来。

## 两个实际踩到的坑

**1. zig 自带的 musl 不认识 OpenWrt 的 `/etc/TZ`。**
OpenWrt 给自家那份 musl 打过补丁，会读 `/etc/TZ`；zig 的是原版，只认环境变量
`TZ`。结果是静态二进制里 `localtime()` 得来的"本地时间"其实是 UTC ——
**"夜间限制时段"会整体偏 8 小时**（00:00-06:00 变成北京时间 08:00-14:00）。
程序启动时会自己读 `/etc/TZ` 再 `setenv("TZ")`，已修正。
验证方法：`campus-net-login --check` 打的时间戳应该和 `date` 一致。

**2. 内存自保要有，但不再是主角。**
Python 版的内存自保是"超阈值就把自己重启"，本身很脆（procd 重试次数用完就不再
拉起）。C++ 版常驻 0.5 MB、不增长，这个开关留着纯粹当保险。

**3. 夜间静默分支里不能"重新上线"这条线。**
第一版照搬 Python 的逻辑，在静默时段里用 `isOnline` 的结果去同步 mwan3。
上线第一天就被日志抓到了：23:55 提前切换到教师线，0 点整静默分支又查到
学生会话还活着，于是把刚摘掉的线标回 `up`，等学校真断时再切一次 ——
0 点前后多弹一回。现在改成只在确认掉线时才通知摘线；它还活着就不去动它。

## 回退

Python 版没有删，随时可以回退：把 `/etc/init.d/campus-net-login` 里的启动命令
换回 `/usr/bin/python3 /etc/campus-net-login/campus_http.py --watch --quiet` 即可。
配置、账号、日志的路径两边完全一样，来回切换不用改任何数据。
