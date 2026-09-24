#include "util.hpp"

#include <cctype>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include <arpa/inet.h>
#include <dirent.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <signal.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char** environ;

namespace cnc {

// --------------------------------------------------------------------------
// 日志
// --------------------------------------------------------------------------

namespace {

const long kLogMaxBytes = 512 * 1024;
const int kLogBackups = 2;

thread_local Logger* g_logger = nullptr;

std::string timestamp() {
    char buffer[32];
    std::time_t now = std::time(nullptr);
    std::tm parts{};
    localtime_r(&now, &parts);
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &parts);
    return std::string(buffer);
}

}  // namespace

void setThreadLogger(Logger* logger) { g_logger = logger; }

Logger& threadLogger() {
    static Logger fallback;
    return g_logger ? *g_logger : fallback;
}

void Logger::configure(const std::string& logDir, const std::string& tag, bool alsoStdout) {
    makeDirs(logDir);
    path_ = logDir + "/campus_http.log";
    // 行首标出是哪条线；只有一条线时不加标记，日志更干净。
    tag_ = tag.empty() ? "" : "[" + tag + "] ";
    stdout_ = alsoStdout;
    size_ = fileSize(path_);
    ready_ = true;
}

void Logger::rotateIfNeeded() {
    if (size_ < kLogMaxBytes) return;
    // 15 秒一轮、失败还不退避时日志涨得很快，滚动一下免得把闪存写满。
    for (int i = kLogBackups - 1; i >= 1; --i) {
        const std::string from = path_ + "." + std::to_string(i);
        const std::string to = path_ + "." + std::to_string(i + 1);
        if (fileExists(from)) ::rename(from.c_str(), to.c_str());
    }
    ::rename(path_.c_str(), (path_ + ".1").c_str());
    size_ = 0;
}

void Logger::write(const char* level, const std::string& message) {
    const std::string line = timestamp() + " " + level + " " + tag_ + message + "\n";
    if (stdout_) {
        std::fputs(line.c_str(), stdout);
        std::fflush(stdout);
    }
    if (!ready_) return;
    rotateIfNeeded();
    FILE* file = std::fopen(path_.c_str(), "a");
    if (file == nullptr) return;
    std::fwrite(line.data(), 1, line.size(), file);
    std::fclose(file);
    size_ += static_cast<long>(line.size());
}

void Logger::close() { ready_ = false; }

namespace {

void logFormatted(const char* level, const char* format, va_list args) {
    char stack[1024];
    va_list copy;
    va_copy(copy, args);
    int needed = std::vsnprintf(stack, sizeof(stack), format, copy);
    va_end(copy);
    std::string message;
    if (needed < 0) {
        return;
    } else if (static_cast<size_t>(needed) < sizeof(stack)) {
        message.assign(stack, static_cast<size_t>(needed));
    } else {
        message.resize(static_cast<size_t>(needed) + 1);
        std::vsnprintf(&message[0], message.size(), format, args);
        message.resize(static_cast<size_t>(needed));
    }
    threadLogger().write(level, message);
}

}  // namespace

void logInfo(const char* format, ...) {
    va_list args;
    va_start(args, format);
    logFormatted("INFO   ", format, args);
    va_end(args);
}

void logWarn(const char* format, ...) {
    va_list args;
    va_start(args, format);
    logFormatted("WARNING", format, args);
    va_end(args);
}

void logError(const char* format, ...) {
    va_list args;
    va_start(args, format);
    logFormatted("ERROR  ", format, args);
    va_end(args);
}

// --------------------------------------------------------------------------
// 文件
// --------------------------------------------------------------------------

bool fileExists(const std::string& path) {
    struct stat info {};
    return ::stat(path.c_str(), &info) == 0;
}

bool makeDirs(const std::string& path) {
    if (path.empty() || fileExists(path)) return true;
    std::string built;
    for (const std::string& part : split(path, '/')) {
        if (built.empty() && part.empty()) {
            built = "/";
            continue;
        }
        if (part.empty()) continue;
        if (!built.empty() && built.back() != '/') built.push_back('/');
        built += part;
        if (fileExists(built)) continue;
        if (::mkdir(built.c_str(), 0755) != 0 && errno != EEXIST) return false;
    }
    return fileExists(path);
}

bool readFile(const std::string& path, std::string& out) {
    FILE* file = std::fopen(path.c_str(), "rb");
    if (file == nullptr) return false;
    out.clear();
    char buffer[4096];
    size_t got = 0;
    while ((got = std::fread(buffer, 1, sizeof(buffer), file)) > 0) {
        out.append(buffer, got);
    }
    std::fclose(file);
    return true;
}

bool writeFile(const std::string& path, const std::string& content, int mode) {
    const std::string temp = path + ".tmp";
    FILE* file = std::fopen(temp.c_str(), "wb");
    if (file == nullptr) return false;
    bool ok = std::fwrite(content.data(), 1, content.size(), file) == content.size();
    if (std::fclose(file) != 0) ok = false;
    if (!ok) {
        ::unlink(temp.c_str());
        return false;
    }
    ::chmod(temp.c_str(), static_cast<mode_t>(mode));
    // 先写临时文件再改名：掉电时不会留下半截配置
    return ::rename(temp.c_str(), path.c_str()) == 0;
}

bool removeFile(const std::string& path) { return ::unlink(path.c_str()) == 0; }

long fileSize(const std::string& path) {
    struct stat info {};
    if (::stat(path.c_str(), &info) != 0) return 0;
    return static_cast<long>(info.st_size);
}

long fileMTime(const std::string& path) {
    struct stat info {};
    if (::stat(path.c_str(), &info) != 0) return 0;
    return static_cast<long>(info.st_mtime);
}

std::vector<std::string> listDir(const std::string& path) {
    std::vector<std::string> out;
    DIR* dir = ::opendir(path.c_str());
    if (dir == nullptr) return out;
    while (struct dirent* entry = ::readdir(dir)) {
        out.push_back(entry->d_name);
    }
    ::closedir(dir);
    return out;
}

void sleepSeconds(double seconds) {
    if (seconds <= 0) return;
    struct timespec request {};
    request.tv_sec = static_cast<time_t>(seconds);
    request.tv_nsec = static_cast<long>((seconds - static_cast<double>(request.tv_sec)) * 1e9);
    while (::nanosleep(&request, &request) == -1 && errno == EINTR) {
    }
}

double monotonicSeconds() {
    struct timespec now {};
    clock_gettime(CLOCK_MONOTONIC, &now);
    return static_cast<double>(now.tv_sec) + static_cast<double>(now.tv_nsec) / 1e9;
}

long long unixSeconds() { return static_cast<long long>(::time(nullptr)); }

// --------------------------------------------------------------------------
// 子进程
// --------------------------------------------------------------------------

CommandResult runCommand(const std::vector<std::string>& argv, int timeoutSeconds, bool capture) {
    CommandResult result;
    if (argv.empty()) return result;

    int pipeFd[2] = {-1, -1};
    if (capture && ::pipe(pipeFd) != 0) return result;

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    if (capture) {
        posix_spawn_file_actions_adddup2(&actions, pipeFd[1], STDOUT_FILENO);
        posix_spawn_file_actions_adddup2(&actions, pipeFd[1], STDERR_FILENO);
        posix_spawn_file_actions_addclose(&actions, pipeFd[0]);
        posix_spawn_file_actions_addclose(&actions, pipeFd[1]);
    } else {
        posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
    }

    std::vector<char*> args;
    args.reserve(argv.size() + 1);
    for (const std::string& item : argv) args.push_back(const_cast<char*>(item.c_str()));
    args.push_back(nullptr);

    pid_t pid = -1;
    int spawned = posix_spawnp(&pid, argv[0].c_str(), &actions, nullptr, args.data(), environ);
    posix_spawn_file_actions_destroy(&actions);
    if (capture) {
        ::close(pipeFd[1]);
        if (spawned != 0) {
            ::close(pipeFd[0]);
            return result;
        }
    } else if (spawned != 0) {
        return result;
    }

    const double deadline = monotonicSeconds() + static_cast<double>(timeoutSeconds);
    bool finished = false;
    int status = 0;
    while (monotonicSeconds() < deadline) {
        pid_t done = ::waitpid(pid, &status, WNOHANG);
        if (done == pid) {
            finished = true;
            break;
        }
        if (done == -1 && errno != EINTR) break;
        sleepSeconds(0.05);
    }
    if (!finished) {
        ::kill(pid, SIGKILL);
        ::waitpid(pid, &status, 0);
        if (capture) ::close(pipeFd[0]);
        result.status = -1;
        result.output = "超时";
        return result;
    }

    if (capture) {
        // 子进程已经退出，管道里剩下的就是全部输出
        char buffer[2048];
        ssize_t got = 0;
        while ((got = ::read(pipeFd[0], buffer, sizeof(buffer))) > 0) {
            result.output.append(buffer, static_cast<size_t>(got));
        }
        ::close(pipeFd[0]);
    }
    result.status = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    return result;
}

// --------------------------------------------------------------------------
// 网络 / 系统
// --------------------------------------------------------------------------

std::string deviceIpv4(const std::string& device) {
    if (device.empty()) return "";
    struct ifaddrs* head = nullptr;
    if (::getifaddrs(&head) != 0) return "";
    std::string found;
    for (struct ifaddrs* it = head; it != nullptr; it = it->ifa_next) {
        if (it->ifa_addr == nullptr || it->ifa_addr->sa_family != AF_INET) continue;
        if (device != it->ifa_name) continue;
        const auto* addr = reinterpret_cast<const struct sockaddr_in*>(it->ifa_addr);
        char text[INET_ADDRSTRLEN] = {0};
        if (::inet_ntop(AF_INET, &addr->sin_addr, text, sizeof(text)) != nullptr) {
            found = text;
            break;
        }
    }
    ::freeifaddrs(head);
    return found;
}

double selfRssMb() {
    std::string content;
    if (!readFile("/proc/self/status", content)) return 0.0;
    size_t pos = content.find("VmRSS:");
    if (pos == std::string::npos) return 0.0;
    pos += 6;
    while (pos < content.size() && content[pos] == ' ') ++pos;
    long kb = std::strtol(content.c_str() + pos, nullptr, 10);
    return static_cast<double>(kb) / 1024.0;
}

std::string executableDir() {
    char buffer[4096];
    const ssize_t length = ::readlink("/proc/self/exe", buffer, sizeof(buffer) - 1);
    if (length <= 0) return ".";
    buffer[length] = '\0';
    std::string path(buffer);
    const size_t slash = path.rfind('/');
    return slash == std::string::npos ? "." : path.substr(0, slash);
}

void initTimezone() {
    const char* existing = std::getenv("TZ");
    if (existing != nullptr && *existing != '\0') {
        ::tzset();
        return;
    }
    // OpenWrt 把时区写在 /etc/TZ（POSIX 格式，例如 "CST-8"）
    std::string zone;
    if (readFile("/etc/TZ", zone)) {
        zone = trim(zone);
        if (!zone.empty()) {
            ::setenv("TZ", zone.c_str(), 1);
            ::tzset();
        }
    }
}

int routeTableFor(const std::string& name) {
    if (name.empty()) return 0;
    int sum = 0;
    for (unsigned char c : name) sum += c;
    return 100 + (sum % 100);
}

namespace {

// 从 /proc/net/route 里取某张网卡的默认网关（直接读文件，省一次 fork）
std::string defaultGateway(const std::string& device) {
    std::string content;
    if (!readFile("/proc/net/route", content)) return "";
    for (const std::string& line : split(content, '\n')) {
        const std::vector<std::string> cells = split(trim(line), ' ');
        std::vector<std::string> compact;
        for (const std::string& cell : cells) {
            if (!cell.empty()) compact.push_back(cell);
        }
        if (compact.size() < 3) continue;
        if (compact[0] != device) continue;
        if (compact[1] != "00000000") continue;  // 目的地址全 0 = 默认路由
        unsigned int value = 0;
        if (std::sscanf(compact[2].c_str(), "%x", &value) != 1) continue;
        return format("%u.%u.%u.%u", value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF,
                      (value >> 24) & 0xFF);
    }
    return "";
}

}  // namespace

bool ensureSourceRoute(const std::string& device, const std::string& ip, int table) {
    if (device.empty() || ip.empty() || table <= 0) return false;
    const std::string gateway = defaultGateway(device);
    if (gateway.empty()) return false;

    const CommandResult route = runCommand(
        {"ip", "route", "replace", "default", "via", gateway, "dev", device, "table",
         std::to_string(table)},
        5);
    if (!route.ok()) return false;

    // 先删后加：已存在时 del 会失败，忽略即可
    runCommand({"ip", "rule", "del", "from", ip, "lookup", std::to_string(table)}, 5);
    const CommandResult rule = runCommand(
        {"ip", "rule", "add", "from", ip, "lookup", std::to_string(table), "priority", "500"}, 5);
    return rule.ok();
}

}  // namespace cnc
