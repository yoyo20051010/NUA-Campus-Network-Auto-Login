# OpenWrt 版（路由器 / 软路由）

把校园网认证放到路由器上：**路由器占用一个认证名额，家里/宿舍所有设备接上就能上网，不用每台设备单独登录。**

和电脑版的区别：路由器上跑不动浏览器，所以这一版是**纯 HTTP 实现**，只依赖 Python 标准库。

---

## 一、前提条件

| 项目 | 要求 |
| --- | --- |
| 路由器 | 能装 OpenWrt，剩余空间 ≥ 16MB（x86 软路由最省心） |
| Python | ≥ 3.8，安装脚本会自动用 opkg 装 |
| 接线 | **校园网网线插 WAN 口**，LAN 口接自己的设备（绝对不要用 LAN 口接校园网） |
| 账号 | 一个校园网账号（路由器会一直占用它） |

> 普通家用路由器（不能刷 OpenWrt、装不了 Python）跑不了这个版本。

---

## 二、安装与首次验证（完整流程）

### 第 0 步：确认路由器能上网

安装脚本要用 `opkg` 装 Python，所以路由器此刻必须能通外网。先在路由器上试一下：

```sh
ping -c 2 223.5.5.5
```

### 第 1~3 步：上传、登录、执行安装

```sh
# 在本机的 openwrt 目录的上一级执行；IP 按你路由器后台的实际地址改
scp -r openwrt root@192.168.5.1:/tmp/

ssh root@192.168.5.1

cd /tmp/openwrt && sh install.sh
```

Windows 自带 `scp.exe`（Win10 1809 以后），PowerShell 里直接就能用。实在没有的话，用路由器 LuCI 后台的"文件传输"把整个目录传上去也一样。

安装脚本按顺序做这几件事，每步都会打印进度：

1. 检查 Python，没有就用 `opkg` 装 `python3-light`（装完会验证 `ssl` 模块可用）
2. 把程序复制到 `/etc/campus-net-login/`
3. **问你校园网账号和密码** —— 密码写进 `secret.json`，权限 600
4. **问你账号类型** —— 输 `1` 学生 / `2` 教师（详见第六节）
5. 安装 procd 后台服务，设成开机自启并立刻启动

### 第 4 步：立刻验证

```sh
/etc/init.d/campus-net-login status                  # 第一行应该显示 running
tail -n 40 /etc/campus-net-login/logs/campus_http.log # 看它到底做了什么
python3 /etc/campus-net-login/campus_http.py --mode   # 确认账号类型对不对
```

日志里应该能看到"使用账号 xxx（…）"、以及"有线网段 → 优先走 Dr.COM 表单"这类行。走完一轮后，接在 LAN 口/WiFi 上的设备就能直接上网了。

---

## 三、日常使用

```sh
/etc/init.d/campus-net-login status     # 看服务状态
/etc/init.d/campus-net-login start      # 启动
/etc/init.d/campus-net-login stop       # 临时停掉（想让别的设备单独认证时用）
/etc/init.d/campus-net-login restart    # 重启服务
tail -f /etc/campus-net-login/logs/campus_http.log   # 看实时日志
python3 /etc/campus-net-login/campus_http.py --check # 手动检测
python3 /etc/campus-net-login/campus_http.py --login # 手动登录一次
python3 /etc/campus-net-login/campus_http.py --probe # 看门户走哪套流程（不登录）
python3 /etc/campus-net-login/campus_http.py --mode  # 看当前用的是学生还是教师账号
```

开机自启已经由 `enable` 配置好了，路由器重启后会自动运行。

---

## 四、它是怎么工作的

```
路由器 WAN (校园网 IP) ── 自动登录 ──> 10.255.255.2 门户 ──┬─> Dr.COM 表单（首选）
                                                        └─> c.nua.edu.cn 统一认证（回退）
        │
        └── NAT ──> LAN 口 / WiFi 上你自己的所有设备（无需任何认证）
```

主流程（和电脑版完全一致）：

1. 访问 `http://10.255.255.2/`，确认门户在（不在校园网就直接退出，不会乱试密码）
2. 门户页面里写着它看到的客户端地址，脚本按这个地址判断走哪一套
3. **优先走门户自己的 Dr.COM 表单** —— 实测（2026-09-16）Dr.COM 的登录接口并不拒绝有线客户端，直接走表单可以绕开统一认证的滑块 / 人脸验证，也不需要 RSA 加密
4. Dr.COM 表单成功后网络放行；每 60 秒检查一次，掉线自动重连

回退流程（Dr.COM 那套没成功时才有线网段再试一次统一身份认证）：

1. 拉取统一认证页，取出 `execution` 令牌
2. 用学校页面同款算法加密密码（**教科书式 RSA + 零填充、小端序**，不是标准 PKCS#1——用通用库会失败），提交
3. 服务器要求安全验证 → 调用 `/captchValid/checkCaptchImg` 上报验证结果
4. 提交登录表单，网络放行

**账号级问题直接停手**：只要服务器返回"密码错误 / 在线数超出限制 / 已在线 / 要图形验证码"，脚本立刻停止本次尝试并原样记录服务器的提示文字，不再换接口硬撞——这些情况换流程也没用，只会增加账号被锁的风险。"已在线"通常意味着旧会话还没释放（服务端会残留 7~10 分钟）。

---

## 五、注意事项

**关于验证码**

学校那个滑块是纯前端校验（页面只比较滑块与缺口的位置差 ≤ 2px），服务端只要求在 `/captchValid/checkCaptchImg` 上报一个结果，所以纯 HTTP 可以完成。**但这一步没有在路由器上实测过**（电脑版是实测通过的）——第一次跑建议盯着日志看，如果卡在"安全验证环节失败"，把日志发出来。

**关于网段**

程序会自己判断走哪套流程：

- **有线网段**（`10.12.x.x` 之类）→ 先走 Dr.COM 表单，没成功再回退统一身份认证 + 拼图滑块
- **无线网段**（`10.54.x.x` 之类）→ 门户自己的 Dr.COM 表单（字段 `DDDDD` / `upass`），通常没有滑块

判断依据是复刻门户页面的规则（它看客户端 IP 决定跳哪套）。想确认，运行：

```sh
python3 /etc/campus-net-login/campus_http.py --probe
```

会打印门户看到的客户端地址、应该走哪套流程、以及解析到的门户配置。

有线网段默认用 `drcom`（先表单、后统一认证）。万一哪天 Dr.COM 开始拒绝有线客户端，把 `config.json` 里的 `wired_flow` 改成 `"cas"`，就变成直接走统一身份认证。

如果无线那边出现**图形验证码**，纯 HTTP 模式无法自动识别（需要图片识别），日志里会明确写出来。

**关于占用名额**

路由器会一直占用你的账号。如果你的账号限制同时在线 1 台设备，那你自己手机电脑就不能再单独认证了——**这正是这套方案的目的**（所有设备共用路由器这一个名额）。

实测（2026-09-15）：电脑做网关时，手机 + 多台设备共用，校园网**没有**触发任何防共享拦截。但不同楼栋/不同时期的策略可能不同，请自行留意。

**关于安全**

`secret.json` 里是明文密码，务必保持 `chmod 600`，别把路由器借给别人用。

**关于学校政策**

私接路由器共享网络可能违反学校网络管理规定，请自行确认。本工具只是自动完成"你自己本来也要做的登录动作"。

---

## 六、学生账号 / 教师账号（教师模式整夜也能自动登录）

学校对学生账号有**夜间断网**策略（默认周一~周五 00:00-06:00 不允许认证），教师账号没有这条限制。脚本把这一点做成了一个开关：

| 账号类型 | 夜间（00:00-06:00）行为 |
| --- | --- |
| `student` 学生（默认） | 不尝试认证，只安静等待，避免每分钟去撞被学校关掉的接口 |
| `teacher` 教师 | **不套用夜间限制，整夜照常检测在线状态，掉线就自动登录** |

切换方式（不用重新输密码）：

```sh
python3 /etc/campus-net-login/campus_http.py --mode teacher   # 切到教师账号
python3 /etc/campus-net-login/campus_http.py --mode student   # 切回学生账号
python3 /etc/campus-net-login/campus_http.py --mode           # 只看当前状态
/etc/init.d/campus-net-login restart                          # 切完重启服务生效
```

装的时候也会问一次：`install.sh` 第 4 步会让你选学生还是教师。也可以直接 `--set-password --mode teacher` 一步搞定。

### 不设也能自动判断

**一般不用手动设账号类型。** 实测学校是按**账号**分夜间时段的，所以脚本会按账号前缀自己认：

| 账号 | 前缀 | 时段 |
| --- | --- | --- |
| 教师工号 | `M` 开头 | 所有时段都能认证 |
| 学生学号 | `B` 开头 | 只有周六日 24 小时可用，非周六日 00:00–06:00 无法认证 |

也就是说：**抄起一个 `M` 开头的工号，脚本自动按教师模式跑，半夜也照常重连。**

优先级从高到低：`secret.json`（`--set-password` / `--mode` 写的）> `config.json` 的 `account_type`（非空时）> **按前缀自动判断**。

前缀可以在 `config.json` 里改：`"teacher_account_prefixes": ["M"]`；如果有不按前缀惯例的账号，也可以列进 `"quiet_hours_exempt_accounts": ["工号xxx"]`。

> 如果你用的是学生账号但现在就想让它整夜也跑，也可以把 `config.json` 里 `quiet_hours.enabled` 改成 `false`，效果一样（只是会被学校那边拒绝，日志里会看到失败重试）。

### 检测频率与失败退避

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `interval` | `15` | 检测间隔（秒）。断网后最快 15 秒内发现并开始重连。在线时每轮只发一个状态查询请求，不写日志 |
| `teacher_backoff` | `false` | 教师模式下是否仍按 `failure_backoff` 退避。`false` = 不退避，每个周期都重试 |
| `teacher_fatal_cooldown` | `300` | 教师模式下遇到**账号级问题**时的固定冷却秒数，设 `0` 关闭 |
| `failure_backoff` | `[120,300,900,1800]` | 学生模式的失败退避：2/5/15/30 分钟 |

### 多条线路（一个进程管两个账号 / 两条上行）

白天两条线一起负载均衡、晚上学生账号被断网时自动落到教师账号，是这个模式的主用途。

在 `config.json` 里填 `profiles` 就开启多线路模式：

```json
"profiles": [
  {"name": "wan",  "device": "wan",    "account_type": "teacher"},
  {"name": "wanb", "device": "br-lan", "account_type": "student"}
]
```

每条线在 `profiles/<名字>/` 下有自己的 `config.json`（可选，覆盖公共配置）、`secret.json`、`logs/`。留空数组 `[]` 就是单线路模式，行为和以前完全一样。

| 字段 | 说明 |
| --- | --- |
| `name` | 线路名，日志里会以 `[名字]` 形式标出来 |
| `device` | 绑定的网卡名。脚本用 `SO_BINDTODEVICE` 把这条线的请求绑到该网卡 —— **校园网按终端 IP 认证，认证请求必须从对应那条线出去，绑错出口就白登了** |
| `account_type` | 这条线的默认账号类型；`secret.json` 里写的优先级更高 |
| `mwan3` | 这条线对应的 **mwan3 接口名**。填了就会把"认证状态"同步给 mwan3（见下一节）；留空 = 不联动 |

### 主备切换由「认证状态」驱动，不靠 ping

如果路由器上装了 mwan3 做多线主备，**建议把 `mwan3` 填上**，让切换由本脚本驱动：

```json
"profiles": [
  {"name": "wan",  "device": "wan",       "mwan3": "wan"},
  {"name": "wanb", "device": "phy0-sta0", "mwan3": "wanb"}
]
```

脚本每次检测完就会通知 mwan3：

| 检测结果 | 动作 |
| --- | --- |
| 这条线**已认证** | `mwan3 ifup <名字>` —— 放回可用池 |
| 这条线**在校园网但未认证** | `mwan3 ifdown <名字>` —— 摘出去，流量走别的线 |
| 不在校园网（门户都打不开） | 不动 —— 跟这条线没关系 |

**为什么不靠 mwan3 自己的 ping 探测：**

> 实测踩过一次：mwan3 的跟踪进程会卡死 —— 带 `-W 2` 超时的 ping 子进程挂住不返回，
> 整个跟踪循环冻住，状态文件 26 小时没更新。结果一条线明明在 00:00 被学校断了认证，
> mwan3 却一直以为它在线，主备切换彻底失效，宿舍断了一晚上网。
>
> 而本脚本每 15 秒就在查校园网状态接口，**本来就知道每条线的真相**，由它来通知最可靠。

顺带一个好处：夜间静默时段（默认 00:00–06:00）虽然不尝试登录，**但仍然会检查这条线是否已掉认证并同步给 mwan3** —— 因为学校正是断在这个时段，不通知的话主备切换就不会发生。

常用命令：

```sh
python3 campus_http.py --check                    # 逐条检查
python3 campus_http.py --mode --profile wanb      # 看某条线的账号
python3 campus_http.py --set-password --profile wanb   # 改某条线的账号密码
/etc/init.d/campus-net-login restart              # 多线路时一个进程并行看护全部线路
```

**为什么是一个进程而不是两个进程**：实测这个脚本单独一个进程 RSS 就有 24MB，而路由器只剩 20MB 出头可用，起两个进程根本装不下；一个进程里多一条线只多一个 Session 对象（约 +2~4MB）。而且并行线程互不阻塞——一条线在登录时，另一条照常检查。

> 每条线的日志行首会带 `[名字]` 前缀，日志文件各自独立，互不干扰。
### 万一教师账号走的是另一套门户

目前教师模式除了"不受夜间限制"之外，登录流程和学生账号完全一样。如果哪天发现学校给教师另开了门户地址，不用改代码，只要在 `config.json` 的 `teacher` 段里填上地址即可（留空=和学生一样，只在教师模式下生效）：

```json
"teacher": {
  "portal_url": "",
  "cas_login_url": "",
  "service": "",
  "status_url": "",
  "wifi_suffix": ""
}
```

---

## 七、出问题怎么排查

按这个顺序看，基本能定位到是哪一层出的问题。

**1. 服务在不在跑**

```sh
/etc/init.d/campus-net-login status     # 有 running 才算在跑
```

没跑起来就直接手动执行一次，报错会打在屏幕上：

```sh
python3 /etc/campus-net-login/campus_http.py --check
```

**2. 是不是根本不在校园网**

```sh
python3 /etc/campus-net-login/campus_http.py --probe
```

看"门户看到的客户端地址"和"应该走"两行。门户打不开（HTTP 不是 200）说明 WAN 没接到校园网，先确认网线插的是 **WAN 口**。

**3. 看日志最后卡在哪一步**

```sh
tail -n 60 /etc/campus-net-login/logs/campus_http.log
```

| 日志里出现 | 含义 | 怎么办 |
| --- | --- | --- |
| `需要人工处理的提示 [密码]` | 账号或密码不对 | 重新跑 `--set-password` |
| `[已在线]` / `[在线数超出限制]` | 账号已有会话没释放 | 等 7~10 分钟；期间别让别的设备再登录 |
| `门户要求图形验证码` | 纯 HTTP 认不了图片验证码 | 这套流程走不通，需要换浏览器方案 |
| `安全验证环节失败` | 统一认证的滑块那步没通过 | 同目录会存下 `http-*-captcha-page.html`，把它和日志一起发出来 |
| `访问不到校园网门户` | 不在校园网 / WAN 没通 | 检查接线和 WAN 状态 |

失败后脚本会按 2 / 5 / 15 / 30 分钟退避重试，避免每分钟去撞墙，日志里会写"上次登录失败（累计 N 次），X 分钟后再试"。

**4. 登录成功但设备上不了网**

这多半不是登录的问题，看路由：

```sh
ip route                                 # default 应指向 10.12.0.1 之类的校园网网关
uci show network | grep -E 'wan|lan'     # wan 必须是 proto='dhcp'
```

**绝对不要把校园网网线插到 LAN 口。**

**5. 想临时让某台设备单独认证**

```sh
/etc/init.d/campus-net-login stop
# …… 用完记得开回来 ……
/etc/init.d/campus-net-login start
```
