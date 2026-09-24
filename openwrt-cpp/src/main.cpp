// 命令行入口。参数和 Python 版保持一致，升级时用户的习惯不用改。
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <termios.h>
#include <unistd.h>

#include "config.hpp"
#include "util.hpp"
#include "watch.hpp"

namespace {

std::string readLine(const char* prompt) {
    std::fputs(prompt, stdout);
    std::fflush(stdout);
    std::string line;
    int c = 0;
    while ((c = std::fgetc(stdin)) != EOF && c != '\n') {
        if (c != '\r') line.push_back(static_cast<char>(c));
    }
    return cnc::trim(line);
}

// 密码提示时不回显（终端下才做得到；重定向输入时照常读）
std::string readPassword(const char* prompt) {
    std::fputs(prompt, stdout);
    std::fflush(stdout);
    struct termios saved {};
    const bool isTty = ::isatty(STDIN_FILENO) != 0;
    if (isTty && ::tcgetattr(STDIN_FILENO, &saved) == 0) {
        struct termios quiet = saved;
        quiet.c_lflag &= static_cast<tcflag_t>(~ECHO);
        ::tcsetattr(STDIN_FILENO, TCSANOW, &quiet);
    }
    std::string line;
    int c = 0;
    while ((c = std::fgetc(stdin)) != EOF && c != '\n') {
        if (c != '\r') line.push_back(static_cast<char>(c));
    }
    if (isTty) {
        ::tcsetattr(STDIN_FILENO, TCSANOW, &saved);
        std::fputs("\n", stdout);
    }
    return line;
}

void printUsage() {
    std::fputs(
        "校园网自动登录（C++ 版，给路由器用）\n"
        "\n"
        "  --check              检测在线状态\n"
        "  --login              执行一次登录\n"
        "  --watch              常驻看门狗（后台服务用）\n"
        "  --set-password       保存账号密码\n"
        "  --mode [类型]         查看/切换账号类型：student=学生，teacher=教师\n"
        "  --probe              探测门户走哪套登录流程（只读，不登录）\n"
        "  --profile 名字        只操作指定线路（配了多条线时用）\n"
        "  --bind-ip 地址        调试用：强制从这个源地址发请求\n"
        "  --quiet              不输出到控制台\n"
        "\n"
        "账号类型说明：\n"
        "  student  受学校夜间断网策略限制，quiet_hours 时段不尝试认证\n"
        "  teacher  没有这条限制，整夜照常检测在线状态并自动登录\n",
        stdout);
}

int setPassword(const std::string& appDir, const std::string& profileFilter,
                const std::string& modeValue) {
    cnc::Profile profile;
    std::string error;
    if (!cnc::resolveProfile(appDir, profileFilter, profile, error)) {
        std::fprintf(stderr, "%s\n", error.c_str());
        return 2;
    }
    cnc::setThreadLogger(&profile.logger);
    profile.logger.configure(profile.logDir(), "", true);

    const std::string account = readLine("校园网账号: ");
    if (account.empty()) {
        std::fprintf(stderr, "账号不能为空\n");
        return 2;
    }
    const std::string password = readPassword("密码(输入时不显示): ");
    if (password.empty()) {
        std::fprintf(stderr, "密码不能为空\n");
        return 2;
    }

    // 没显式指定就沿用已保存的类型，免得"只改个密码"把教师模式重置成学生
    std::string accountType = cnc::normalizeAccountType(modeValue);
    if (accountType.empty()) accountType = cnc::storedAccountType(profile);

    cnc::Secret secret;
    secret.account = account;
    secret.password = password;
    secret.accountType = accountType;
    if (!cnc::saveSecret(profile, secret)) {
        std::fprintf(stderr, "写不进 %s\n", profile.secretFile().c_str());
        return 1;
    }
    std::printf("已保存到 %s\n", profile.secretFile().c_str());
    if (!accountType.empty()) {
        std::printf("账号类型: %s（%s）\n", cnc::accountTypeLabel(accountType).c_str(),
                    accountType.c_str());
    } else {
        std::printf("账号类型沿用 config.json 的设置（默认学生账号）。\n");
        std::printf("教师账号请再执行一次: campus-net-login --mode teacher\n");
    }
    return 0;
}

int setMode(const std::string& appDir, const std::string& profileFilter,
            const std::string& modeValue, bool hasValue) {
    cnc::Profile profile;
    std::string error;
    if (!cnc::resolveProfile(appDir, profileFilter, profile, error)) {
        std::fprintf(stderr, "%s\n", error.c_str());
        return 2;
    }
    cnc::setThreadLogger(&profile.logger);
    profile.logger.configure(profile.logDir(), "", true);
    const cnc::Config cfg = cnc::loadConfig(profile, appDir);

    if (hasValue) {
        const std::string accountType = cnc::normalizeAccountType(modeValue);
        if (accountType.empty()) {
            std::printf("账号类型只能是 student（学生）或 teacher（教师）\n");
            return 2;
        }
        if (!cnc::saveSecretAccountType(profile, accountType)) {
            std::printf("还没有 %s，请先运行: campus-net-login --set-password\n",
                        profile.secretFile().c_str());
            return 1;
        }
        std::printf("账号类型已切换为: %s（%s）\n", cnc::accountTypeLabel(accountType).c_str(),
                    accountType.c_str());
        std::printf("改完记得重启服务: /etc/init.d/campus-net-login restart\n");
    }

    cnc::Secret secret;
    std::string problem;
    if (!cnc::loadSecret(profile, secret, problem)) {
        std::fprintf(stderr, "%s\n", problem.c_str());
        return 2;
    }
    const std::string accountType = cnc::resolveAccountType(cfg, secret.accountType, secret.account);
    std::printf("当前账号: %s\n", secret.account.c_str());
    std::printf("当前账号类型: %s（%s）\n", cnc::accountTypeLabel(accountType).c_str(),
                accountType.c_str());
    if (accountType == "teacher") {
        std::printf("夜间策略: 忽略夜间限制时段，整夜照常检测并自动登录\n");
    } else if (cfg.quietEnabled) {
        std::printf("夜间策略: %s-%s 暂停尝试（学生账号被学校限制的时段）\n",
                    cfg.quietStart.c_str(), cfg.quietEnd.c_str());
    } else {
        std::printf("夜间策略: 未启用夜间限制，整夜照常检测\n");
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    bool check = false;
    bool loginOnce = false;
    bool watch = false;
    bool setPwd = false;
    bool probe = false;
    bool quiet = false;
    bool hasMode = false;
    std::string modeValue;
    std::string profileFilter;
    std::string bindIp;

    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--check") {
            check = true;
        } else if (arg == "--login") {
            loginOnce = true;
        } else if (arg == "--watch") {
            watch = true;
        } else if (arg == "--set-password") {
            setPwd = true;
        } else if (arg == "--probe") {
            probe = true;
        } else if (arg == "--quiet") {
            quiet = true;
        } else if (arg == "--mode" || arg == "--account-type") {
            hasMode = true;
            if (i + 1 < argc && argv[i + 1][0] != '-') modeValue = argv[++i];
        } else if (arg == "--profile" && i + 1 < argc) {
            profileFilter = argv[++i];
        } else if (arg == "--bind-ip" && i + 1 < argc) {
            bindIp = argv[++i];
        } else if (arg == "-h" || arg == "--help") {
            printUsage();
            return 0;
        } else {
            std::fprintf(stderr, "无法识别的参数: %s\n\n", arg.c_str());
            printUsage();
            return 2;
        }
    }

    const std::string appDir = cnc::executableDir();
    cnc::initTimezone();

    if (setPwd) return setPassword(appDir, profileFilter, modeValue);
    if (hasMode) return setMode(appDir, profileFilter, modeValue, !modeValue.empty());
    if (watch) return cnc::runWatch(appDir, profileFilter, bindIp);
    if (loginOnce) return cnc::runLoginOnce(appDir, profileFilter, bindIp);
    if (!check && !probe) {
        printUsage();
        return 0;
    }
    return cnc::runCheck(appDir, profileFilter, probe, quiet);
}
