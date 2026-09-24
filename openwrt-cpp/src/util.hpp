// 平台相关的小工具：日志、文件、子进程、网卡/内存查询、字符串处理。
// 只依赖 POSIX（路由器是 musl），不用任何第三方库。
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "strings.hpp"

namespace cnc {

// --------------------------------------------------------------------------
// 日志
//
// 每条线路（profile）有自己的 Logger，各自写各自的文件。
// 看门狗的每条线路跑在一个线程里，线程用 thread_local 记录“当前是哪条线”。
// --------------------------------------------------------------------------
class Logger {
public:
    // logDir 不存在会自动创建；tag 是多线路时打在行首的线名。
    void configure(const std::string& logDir, const std::string& tag, bool alsoStdout);
    void write(const char* level, const std::string& message);
    void close();

private:
    void rotateIfNeeded();

    std::string path_;
    std::string tag_;
    bool stdout_ = false;
    long size_ = 0;
    bool ready_ = false;
};

void setThreadLogger(Logger* logger);
Logger& threadLogger();

// printf 风格的日志；末尾自动补换行。
void logInfo(const char* format, ...);
void logWarn(const char* format, ...);
void logError(const char* format, ...);

// --------------------------------------------------------------------------
// 文件
// --------------------------------------------------------------------------
bool fileExists(const std::string& path);
bool makeDirs(const std::string& path);
bool readFile(const std::string& path, std::string& out);
bool writeFile(const std::string& path, const std::string& content, int mode = 0644);
bool removeFile(const std::string& path);
long fileSize(const std::string& path);
long fileMTime(const std::string& path);
std::vector<std::string> listDir(const std::string& path);
void sleepSeconds(double seconds);
double monotonicSeconds();
long long unixSeconds();

// --------------------------------------------------------------------------
// 子进程
// --------------------------------------------------------------------------
struct CommandResult {
    int status = -1;          // 进程退出码；-1 表示没跑起来或超时
    std::string output;       // stdout + stderr
    bool ok() const { return status == 0; }
};

// 用 posix_spawn 执行；超时（秒）到点就 SIGKILL 再回收，不会留下僵尸或卡死。
CommandResult runCommand(const std::vector<std::string>& argv, int timeoutSeconds = 20,
                         bool capture = true);

// --------------------------------------------------------------------------
// 网络 / 系统
// --------------------------------------------------------------------------

// 网卡当前的 IPv4 地址；取不到返回空串。
std::string deviceIpv4(const std::string& device);

// 本进程常驻内存（MB）；读不到返回 0。
double selfRssMb();

// 可执行文件所在目录（配置文件都放在它旁边）。
std::string executableDir();

// 初始化时区。
//
// 注意：OpenWrt 给自己那份 musl 打过补丁，会读 /etc/TZ；而本程序用 zig 自带的
// 静态 musl，认不出 /etc/TZ，于是 localtime() 会当成本地时间 = UTC。
// 时区错了会让"夜间限制时段"整体偏 8 小时，所以启动时必须自己补这一步。
void initTimezone();

// 按线路名算一个固定路由表号（100~199），重启后不变。
int routeTableFor(const std::string& name);

// 给“从某个源地址出去的包”单独指定路由表。
// Linux 按目的地址选路，光绑源地址不改路由的话，认证请求还是会从默认网卡出去。
bool ensureSourceRoute(const std::string& device, const std::string& ip, int table);

}  // namespace cnc
