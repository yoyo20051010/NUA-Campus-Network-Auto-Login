# 校园网自动登录（macOS 版）

南京艺术学院校园网自动认证，纯 Python 标准库实现，**不需要 pip、不需要装浏览器**。

- **有线**：统一身份认证（CAS）+ RSA 加密密码 + 滑块验证 —— 已实现
- **无线**：Dr.COM ePortal 原生表单（DDDDD / upass）—— 已实现
- **VPN 开着也能用**：所有请求绑定物理网卡（`IP_BOUND_IF`），DNS 自己解析绕过 fake-ip
- **Mac 专属优化**：电池感知轮询、插网线/切 WiFi/合盖唤醒立即检测、在线时每轮只发几十字节
- **iPhone/iPad**：`--ios` 会给三条接入路线（Mac 热点共享 / 快捷指令 / 系统自动弹门户）

> 原理、VPN/有线专项分析和实测数据见上一级目录的 `README.md`。

---

## 一、安装（3 步）

```sh
cd macos
sh install.sh
```

向导会做这些事：

1. 检查 Python 3（macOS 自带；没有就 `xcode-select --install`）
2. 问你要学号 → 存进 `config.json`
3. 让 macOS 钥匙串直接读取密码（**不显示、不进命令行历史、不落盘**）
4. 生成 LaunchAgent：`~/Library/LaunchAgents/com.campusnet.autologin.plist`
5. 自动跑一次体检 `--diagnose` 并打印结果

装完立即生效，不用重启。

选项：

```sh
sh install.sh --gui                 # 用 macOS 原生弹窗输账号密码（适合让 AI 代装）
sh install.sh --no-autostart        # 只存账号密码，不装自启
sh install.sh --interval 30         # 检查间隔（秒，默认 30）
sh install.sh --interface en7       # 强制指定网卡
```

## 二、日常使用

```sh
python3 campus_mac.py --diagnose         # 体检：网卡 / VPN / DNS / 门户 / 认证状态
python3 campus_mac.py --ios              # iPhone/iPad 接入方案(含 Mac 热点共享检查)
python3 campus_mac.py --check            # 只看状态（在线=0，需要登录=1）
python3 campus_mac.py --login            # 立即登录一次
python3 campus_mac.py --login --dry-run  # 演练：只检查流程，不发送密码
python3 campus_mac.py --login --dry-run --force   # 跳过状态闸门强制演练
python3 campus_mac.py --watch            # 前台常驻看门狗
python3 campus_mac.py --set-password     # 改账号/密码
```

日志与状态：

```sh
tail -f logs/campus_mac.log      # 只在状态变化时写日志，平时很安静是正常的
cat state/last_state.txt         # 上一次的判定结果
launchctl print gui/$UID/com.campusnet.autologin | head -20   # 自启进程状态
```

## 三、判定逻辑（为什么不会乱试密码）

每次检查按顺序走：

1. **挑网卡**：有线优先，逐张做 TCP 测试，谁能打开 `10.255.255.2` 就用谁
2. **门户可达？** 打不开就是"不在校园网"（家里 WiFi / 热点 / 手机热点）→ 什么都不做
3. **直连探针**（绑物理网卡 + 真实 IP 访问 `generate_204`）
   - 拿到真实内容 → **已在线**
   - 被跳到 Dr.COM 登录页 → **未认证，可以登录**
4. 门户模板编号 `Dr.COMWebLoginID_0.htm` → 未认证；`_3.htm` → 已在线
5. 以上都不明确 → 查统一认证状态接口；仍然不明确就**什么都不做**（宁可不动，也不乱试密码）

只有第 3/4 步拿到"确实未认证"的正面证据时才会提交账号密码。

## 四、有线 / 无线怎么选

程序自动判断，判据与学校门户页一致：

| 客户端 IP | 走哪条流程 |
| --- | --- |
| `≤ 10.51.255.255`（如 10.12.x 有线） | 统一认证 CAS + 滑块 |
| `10.128.0.1 ~ 10.129.255.255` | 统一认证 CAS + 滑块 |
| 其它（如 10.53.x / 10.54.x） | Dr.COM 原生表单 |

插着 USB-C 网线同时 WiFi 也开着时，程序会优先选**能真正打开门户**的那张网卡；想固定就用 `--interface enX`（先用 `--diagnose` 看候选网卡列表）。

## 五、VPN 开着的时候

三种情况，`--diagnose` 会直接告诉你是哪种：

| 情况 | 表现 | 处理 |
| --- | --- | --- |
| VPN 只代理部分流量 | `10.255.255.2` 路由指向 utun，但绑网卡后仍可达 | **不用管**，本工具照常工作 |
| VPN 全隧道 | 绑网卡也打不开门户 | 在 VPN 客户端把 `10.0.0.0/8`、`*.nua.edu.cn` 设为直连；或在终端加静态路由（见上级 README） |
| DNS 被 fake-ip 劫持 | 系统解析出 `198.18.x.x` | 本工具已自动绕过：Direct 查校园 DNS 拿真实 IP，无需处理 |

推荐的 VPN 直连规则：

```
# Clash / Mihomo
- IP-CIDR,10.0.0.0/8,DIRECT
- DOMAIN-SUFFIX,nua.edu.cn,DIRECT

# Surge / Quantumult X
IP-CIDR,10.0.0.0/8,DIRECT
DOMAIN-SUFFIX,nua.edu.cn,DIRECT
```

## 六、Mac 与 iPhone/iPad 优化说明

> ⚠️ **验证状态**：**Mac 部分是完整实测过的**；**iPhone/iPad 部分只做到"能生成并导入快捷指令"，没有在真实手机上跑通认证流程**，属实验性内容，请谨慎参考。

### Mac 端

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| 检查间隔（校园网内） | 30 秒 | `config.json` 的 `interval` |
| 检查间隔（电池供电） | ≥120 秒 | `interval_battery`，MacBook 省电 |
| 检查间隔（不在校园网） | ≥300 秒 | `interval_offcampus`，在家/热点时几乎不费电 |
| 网络变化 | 立即 | 盯 `/Library/Preferences/SystemConfiguration/preferences.plist`，插网线/切 WiFi/开关 VPN 后 5 秒内响应 |
| 合盖唤醒 | 立即 | 检测时间跳变，唤醒后先重新认证再干别的 |
| 在线时每轮开销 | 1 个 TCP 握手 + 1 个几十字节的 204 请求 | 判定分三级，只有"可能没认证"时才抓门户页 |
| 日志 | 只在状态变化时写 | 保护 SSD，平时很安静 |

### iPhone / iPad（实验性，未完整验证）

三条路线（`--ios` 会按你当前是否插网线、是否接电源给出结论）：

**A. Mac 做热点，手机平板全免认证**（最彻底）
1. Mac 用**网线**连校园网（macOS 不能"Wi-Fi 转发给 Wi-Fi"，必须有线）
2. 系统设置 → 通用 → 共享 → 互联网共享 → 来源选网线接口、共享给 Wi-Fi → Wi-Fi 选项里设网络名和密码
3. 打开左侧开关 → iPhone/iPad 连这个热点即可
> 取舍：好处是所有设备只占 1 个校园网名额、完全免认证；代价是 **MacBook Air 合盖/睡眠就断**，只适合固定位置插着电。

**B. 快捷指令一键打开门户**（Mac 不在身边时）
快捷指令 App → 新建 → 添加"打开 URL"动作 → `http://10.255.255.2/` → 命名为"校园网登录" → 加到主屏幕或绑定"轻点背面"。打开后密码由 iCloud 钥匙串自动填充。

**C. 什么都不装**
用 Safari 登录一次并让钥匙串记住密码即可，系统以后会自动弹出门户页，点一下登录就行。

**两个必做设置：**
- **专用 Wi-Fi 地址 → 关闭 / 固定**（设置 → 无线局域网 → 校园网 (i)）。校园网按 MAC 记会话，轮换 MAC 会导致反复掉线重认证。
- **自动加入 → 开**，并让 iCloud 钥匙串同步密码。

### 全局梯子一开，门户就弹不出来（原因 + 三条解法）

**为什么会死锁：** iOS 判断"要不要弹认证页"，靠的是后台探测 `captive.apple.com`。全局模式下这个探测被塞进隧道，系统拿到的是梯子的正常响应，于是**认为网络没问题，永远不弹窗**；同时 `10.255.255.2` 也可能被隧道抢走，你自己也打不开门户。于是变成"要先认证才能上网，但梯子挡住了认证"。

解法按优先级：

1. **在梯子里把校园网放行**（最根本）
   ```
   IP-CIDR,10.0.0.0/8,DIRECT
   DOMAIN-SUFFIX,nua.edu.cn,DIRECT
   ```
   Shadowrocket：设置 → 全局路由 → 用**"配置"**而不是"代理"，并打开"绕过局域网"。
   另外把梯子的"按需连接/自动连接"关掉，**顺序永远是：先连 WiFi → 完成认证 → 再开梯子**。

2. **干脆不依赖弹窗**：用快捷指令直接发认证请求（见下），全局梯子下也能用。

3. **自动关梯子**：如果你的梯子 App 出现在 设置 → 通用 → VPN 与设备管理 → VPN 里，快捷指令可以用"设定 VPN"动作先关掉它再认证。

### 每个设备都要单独登录 → 就在每台设备上各放一个"自动登录"

| 设备 | 方案 | 效果 |
| --- | --- | --- |
| Mac | 本工具（`--watch` + launchd） | 全自动，开机/唤醒/掉线都自己搞定 |
| iPhone / iPad | **快捷指令 + 无线局域网自动化**（下面 5 步） | 连上校园 WiFi 自动发起认证，不用点门户 |

> ⚠️ 既然你们是**按设备计数**的，就别用"Mac 做热点给手机共享"那套了——NAT 共享可能被判定为违规，也可能触发防共享拦截。每台设备各自认证才是安全的做法。

### iPhone / iPad 快捷指令：5 步搞定（不依赖门户弹窗）

1. 在 Mac 上二选一：
   ```sh
   python3 make_ios_shortcut.py     # 推荐：直接生成「校园网登录.shortcut」，AirDrop 到手机导入
   python3 campus_mac.py --make-ios-url   # 或者：生成的网址写进 state/ios_login_url.txt
   ```
   用前者的话跳过第 3~5 步，直接把它 AirDrop 给 iPhone/iPad，然后在手机上做自动化（第 5 步）。
2. iPhone/iPad 打开**快捷指令** App → 新建 → 添加动作**"获取 URL 内容"** → 方法改成 **GET** → 把网址粘进去。
3. 再加一个**"显示结果"**动作，方便看到返回内容（出现 `"result":1` 就是成功）。
4. 命名成"**校园网登录**"。
5. 做自动化：快捷指令 → **自动化** → 新建 → **"无线局域网"** → 选择校园网的 SSID → **关闭"运行前询问"** → 添加动作"运行快捷指令 → 校园网登录"。

**注意事项：**
- 那行网址里有明文密码，别截图转发；介意的话去 设置 → Apple 账户 → iCloud → 关掉"快捷指令"同步。
- **一定不要**把 Mac 的 IP 填进 `wlan_user_ip`，那会把登录算到 Mac 头上。方式一故意不带这个参数，让服务器按请求来源 IP 记账。
- 如果方式一返回 `result:0`，改用命令输出的**方式二**：加一个"获取当前 IP 地址"动作，拼进网址里的 `【本机IP】`（手机自己的 IP）。
- 方式二还不行，再用**方式三**（POST 表单）。
- 校园网按 MAC 记账，务必把"专用 Wi-Fi 地址"设成**固定/关闭**，否则换了 MAC 还要重新认证。
- 掉线时也可以在"轻点背面"或主屏幕图标上手动点一下这个快捷指令。

## 七、常见问题

**Q：日志一直是"不在校园网环境"**
说明门户打不开。先 `--diagnose`：如果 VPN 全隧道把 `10.255.255.2` 抢走了，按上面加直连规则。

**Q：提示"还没有保存账号密码"**
跑 `python3 campus_mac.py --set-password`。

**Q：钥匙串老弹窗要密码**
安装时用 `install.sh` 写入（带 `-T /usr/bin/security`）不会弹；如果是老条目，删掉重存：
`security delete-generic-password -s campus-net-login -a <学号>`

**Q：密码能在多台 Mac 上共用吗**
不能也不建议。密码在各自机器的钥匙串里，每台机器跑一次 `install.sh`。

**Q：想主动断线测试**
`sudo ifconfig en0 down && sleep 60 && sudo ifconfig en0 up`，看它是否 1 分钟内自动重连。

**Q：占用几个名额**
一台机器一个（有线/无线各算一条会话）。想全屋只占一个，把认证放到路由器上。

**Q：学校页面改版了怎么办**
浏览器模式不怕改版，纯 HTTP 模式怕。真失败时把 `logs/` 里的存档 HTML 发出来，改几个选择器/字段即可。

## 八、安全说明

- 密码存 macOS 登录钥匙串（`campus-net-login`），`config.json` 只存学号
- 所有请求都带"安全闸"：只有确认"在校园网且未认证"才提交密码
- 不修改认证结果、不共享给他人
- 学校网络管理规定请自行确认
