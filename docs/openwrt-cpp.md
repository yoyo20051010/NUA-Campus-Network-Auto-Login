# OpenWrt 版改用 C++ 重写：验证记录与经验教训

日期：2026-09-22
目标机型：小米 AX3000T / Kwrt 24.10-SNAPSHOT / MT7981 / aarch64_cortex-a53 / musl / 内核 6.6.116

代码在 [`openwrt-cpp/`](../openwrt-cpp/)，使用说明看
[`openwrt-cpp/README.md`](../openwrt-cpp/README.md)。这份文档记的是
**为什么这么定、怎么验证的、踩了哪些坑**。

## 为什么非改不可

Python 版功能已经完全没问题了，坏在资源占用上：

| 项目 | Python 版实测 | C++ 版实测 |
| --- | --- | --- |
| overlay 闪存 | 约 8 MB（python3 + 3 个依赖包） | 421 KB（一个静态二进制） |
| 常驻内存 RSS | 28 → 48 MB，约 1 MB/小时增长 | **492 KB，30 秒采样无增长** |
| 触发内存自保的历史 | 21 小时从 28 MB 涨到 48 MB | — |

内存这条最终真的出事了：2026-09-21 21:56 起，Python 版每隔 5 分钟就因为
"常驻内存超过 60 MB"主动退出，procd 在 22:01 放弃重启，
**服务停了整晚** —— 那段时间校园网掉线不会再自动登录，主备切换也不会发生。

排查日志：

```
2026-09-21 21:56:16 WARNING [wan] 常驻内存已到 61.0 MB（上限 60 MB），主动退出让服务重新拉起来
2026-09-21 22:01:22 WARNING [wan] 常驻内存已到 61.1 MB（上限 60 MB），主动退出让服务重新拉起来
（之后 procd 不再拉起，/etc/init.d/campus-net-login status → not running）
```

## 一、先确认本机工具链（用户要求的第一步）

结论：**本机没有任何现成的 aarch64 Linux 交叉编译器**。

| 工具 | 位置 | 结论 |
| --- | --- | --- |
| g++ / gcc | `C:\MinGW\mingw64\bin` | 只能编 Windows PE |
| clang / clang++ | `C:\Program Files\LLVM\bin`（LLVM 23） | 有 aarch64 后端，但缺 Linux sysroot（`lib/clang/23/lib/linux` 不存在） |
| cmake | `C:\Program Files\CMake\bin` | 可用 |
| make / ninja / MSVC / Docker / WSL | 均未安装 | — |

于是引入 **zig**：单文件解压，自带各平台的 libc，一条命令就能产出
`aarch64-linux-musl` 的静态二进制，不用装一整套 OpenWrt SDK。

zig 0.14.1 官方包 SHA256 与 ziglang.org 的 `index.json` 校验一致
（`554f5378…feed2c`，82,229,343 字节）。下载过程本身也踩了坑：
直连 ziglang.org 只有 20 KB/s 且总在 30~40 MB 处断流，
最后是 **分块下载（每块 4 MB）+ 逐块校验 + 拼接**才拿到完整的包。

## 二、最小验证：先把"能跑起来"这条路走通

动手写业务代码之前，先编了一个 hello world 级别的冒烟程序
（`openwrt-cpp/tools/smoke.cpp`，含 `std::thread` 和 `/proc` 读取），
传到路由器上跑：

```
smoke: Linux aarch64 (6.6.116)
smoke: c++201703 pthread=yes hello
smoke: /proc/self/status readable
```

这一步确认了：静态链接、pthread、C++17 运行时、`/proc` 在目标机上都没问题。
后面所有代码都建立在这个已验证的基础上。

## 三、正确性怎么保证

重写最怕的是"看起来能跑，实际某个细节错了"。所以对关键环节做了可对照的验证：

### 1. RSA 加密：与 Python 版逐字节对照

密码加密错了，统一身份认证只会回一个"密码错误"，极难查。
做法是：用 Python 参考实现生成一组测试向量（`tests/gen_rsa_vectors.py`
直接 import `openwrt/campus_http.py`），C++ 那边逐条比对输出。

样本覆盖：短密码、普通密码、各种符号、正好一块（126 字节）、跨块（127 字节）、
正好两块（252 字节）。**10 组全部逐字节一致。**

### 2. 门户页面解析：本机单元测试

把纯文本解析单独拆到 `portal_parse.cpp`（不引用任何网络/文件代码），
这样在 Windows 上用本机 g++ 就能跑测试。覆盖：portal 配置项解析、
客户端地址抠取、有线/无线网段判定、表单解析（submit 按钮要排除）、
`contextPath` 提取、滑块验证页判定。

### 3. 端到端：在真路由器上跑真流程

`--probe` 两条线都能正确判断：

```
[wan]  门户看到的客户端地址: 10.12.13.57  → 有线网段 → 先 Dr.COM 表单，失败再回退统一身份认证
[wanb] 门户看到的客户端地址: 10.54.91.217  → 无线网段 → 走 Dr.COM 门户登录
[wan]  门户配置 authloginpath = /eportal/?c=ACSetting&a=Login，端口 801，字段 DDDDD/upass
```

（与 Python 版解析出的完全一致，包括 `jsVersion = 1755483350029`。）

再跑一次真实登录（测试副本里临时关掉夜间静默），完整走了：
Dr.COM 四种服务类型 → 全部 `Authentication fail` → 跳过新版接口 →
回退统一身份认证 → 提交账号密码 → 服务器返回"请完成安全验证" →
POST `contextPath + /captchValid/checkCaptchImg` → 依次尝试三种提交方式 → 判定未上线。

这一趟的意义在于：**服务器没有报"密码错误"，而是进入了滑块验证页** ——
说明 C++ 算出来的 RSA 密文被服务端接受了；验证上报接口也回了
`{"msg":"","code":"","success":true}`，说明 `contextPath` 拼接正确。
最终没登上是因为当时（周二 00:50）正是学校对学生账号的封锁时段，与预期一致。

## 四、踩到的坑

### 坑 1：zig 的 musl 不认 OpenWrt 的 `/etc/TZ`（最隐蔽的一个）

OpenWrt 给自己那份 musl 打过补丁，会去读 `/etc/TZ`；zig 带的是原版 musl，
只认环境变量 `TZ`。后果是：静态二进制里 `localtime()` 得到的"本地时间"
其实是 UTC。

第一次在路由器上跑 `--check`，日志时间是 `16:47`，而路由器 `date` 显示 `00:47`
（CST-8）—— 差了 8 小时。

**这个 bug 的杀伤力在于它不会报错**：夜间限制时段配的是 00:00-06:00，
在 UTC 下会变成北京时间 08:00-14:00 生效，也就是白天该用学生账号的时候
脚本以为在夜间、不去登录，反而在人最多的时段把线摘掉。

修法是程序启动时自己读 `/etc/TZ` 再 `setenv("TZ")`（见 `util.cpp`
的 `initTimezone()`）。修完日志时间与 `date` 一致。

### 坑 2：`-fno-exceptions` 之后不能再写 try/catch

为了省体积关掉了异常，结果照搬 Python 的 `try/except 每轮兜底`写法编译不过。
改成不设兜底：真出意料之外的问题进程直接退出，由 procd 的 respawn 拉起来 ——
反正异常兜底也救不了 `std::bad_alloc`。

### 坑 3：RSA 的块长不是 4 的倍数

模数 1024 位 → 块长 126 字节，而 32 位 limb 是 4 字节一组，
按 limb 边界切块会算错。必须按字节归位（`block[i / 4] |= byte << (8 * (i % 4))`）。
测试向量里正好有一组 127 字节的样本会踩到这个。

### 坑 4：夜间静默分支把刚摘掉的线又标回可用（上线第一天抓到）

第一版是照搬 Python 逻辑写的：夜间静默时段里，每 5 分钟查一次这条线是否在线，
把结果同步给 mwan3。上线第一天的日志就露馅了：

```
09-22 23:55:02  已通知 mwan3：wan down   ← 提前切换，正常
09-23 00:00:03  已通知 mwan3：wan up     ← 又标回可用（当时学生会话还没被学校切断）
09-23 00:05:03  已通知 mwan3：wan down   ← 学校真断了才重新摘掉
```

0 点整那一下，学校还没来得及切断学生会话，`isOnline` 仍返回真，于是
**把 23:55 刚摘出备用池的线又标回 up**。如果 mwan3 跟踪确认了，流量会被拉回
有线，等学校真断时再切走 —— 0 点前后多弹一次，把"提前切换"的效果抵消一半。

修法：夜间静默分支**只允许摘线，不允许重新上线**。只在确认这条线确实不在线时
才通知 mwan3 摘掉它；它还活着就维持现状（提前切换摘掉的就继续摘着）。
这样既保留了"夜里掉线要告诉 mwan3"的初衷，也不会去撤销提前切换。

顺带记一笔取证边界：`logread` 的环形缓冲只留几小时，23:55 那段系统日志已经滚掉，
这个判断是从脚本自己的日志推出来的，没能直接看到当时的 mwan3 接口状态事件。

## 五、几个刻意的设计取舍

| 决策 | 原因 |
| --- | --- |
| HTTP 自己用 socket 写，HTTPS 转交路由器自带的 `curl` | 静态链 OpenSSL/mbedTLS 会让二进制涨到好几 MB，等于白重写。`curl` 本来就在系统里（8.12.1 + OpenSSL）。 |
| 参数用 `posix_spawn` 直接 exec，不经 shell | 没有转义和注入问题，也没有 shell 的额外内存。 |
| 不用 `std::regex` | 要解析的就几个固定形状，手写扫描更可控，也避免模板实例化把二进制撑大。 |
| 关掉异常和 RTTI，`-Os` + `--gc-sections` + `-s` | 4.3 MB → 421 KB。 |
| 时区自己初始化 | 见坑 1。 |
| 保留内存自保 | C++ 版不涨，但这个开关留着当保险，代价接近零。 |

## 六、升级与回退

配置文件格式与 Python 版**完全通用**（`config.json` / `secret.json` /
`profiles/<名字>/`），所以升级只需要换程序：
`openwrt-cpp/openwrt/install.sh` 会停掉旧服务、换上新二进制，
已有的账号密码和线路配置一个都不用重填。

回退同样简单：把 `/etc/init.d/campus-net-login` 里的启动命令换回
`/usr/bin/python3 ... --watch --quiet` 即可，Python 版的文件没有删。

## 七、还没做 / 可以接着做的

- **没有动路由器上正在运行的东西**：Python 版的服务目前是停着的，
  `/etc/campus-net-login/` 里还是原来的文件；C++ 版只在 `/tmp` 下验证过。
  要正式切换得专门做一次，并且盯着 mwan3 的主备切换表现。
- 旧 Python 版那两个修复（`c41379c` 主备切换由认证状态驱动、`54c97a6` 断网前
  提前切换 + 内存自保）这次是**一起带进 C++ 版**重写的，v1.4 Release 里的
  openwrt 包仍然不含它们。
- 想在路由器上进一步省空间的话，可以把旧 Python 版的文件和 python3 依赖包删掉，
  大约能还回 8 MB overlay。
