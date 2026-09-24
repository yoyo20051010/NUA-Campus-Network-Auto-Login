// 配置 / 线路 / 账号类型 / 夜间时段 / 退避状态。
// 这里的字段名和 Python 版的 config.json 完全一致，升级只是换程序、不用改配置。
#pragma once

#include <ctime>
#include <string>
#include <vector>

#include "json.hpp"
#include "util.hpp"

namespace cnc {

// 一条线 = 一个账号 + 一个出口。每条线自己一套 config.json / secret.json / logs/。
struct Profile {
    std::string name;
    std::string base;                // 目录
    std::string device;              // 绑定的网卡名；空 = 按系统路由走
    std::string defaultAccountType;  // config.json 里给这条线写的默认类型
    std::string mwan3;               // 对应的 mwan3 接口名；空 = 不联动
    Logger logger;

    std::string logDir() const { return base + "/logs"; }
    std::string configFile() const { return base + "/config.json"; }
    std::string secretFile() const { return base + "/secret.json"; }
    std::string retryFile() const { return logDir() + "/retry_state.json"; }
    std::string modeFile() const { return logDir() + "/last_mode.txt"; }
};

struct Config {
    std::string portalUrl = "http://10.255.255.2/";
    std::string casLoginUrl = "https://c.nua.edu.cn/cas/login";
    std::string service = "https://c.nua.edu.cn/cas/wifiLogin/innerLogin.jsp";
    std::string statusUrl = "https://c.nua.edu.cn/cas/wifiLogin/isLogin";
    std::string captchaUrl = "https://c.nua.edu.cn/cas/captchValid/checkCaptchImg";
    std::string probeUrl = "http://www.baidu.com/";
    int interval = 60;          // 检测间隔（秒）
    int loginTimeout = 90;      // 登录后等网络恢复的上限（秒）
    int timeout = 8;            // 单次 HTTP 超时（秒）
    std::string wiredFlow = "drcom";   // 有线网段走哪套：drcom / cas
    std::string wifiSuffix;            // 无线服务类型后缀；空 = 依次尝试
    std::string accountType;           // config.json 里的兜底账号类型
    std::vector<std::string> teacherAccountPrefixes{"M"};
    std::vector<std::string> quietHoursExemptAccounts;
    bool quietEnabled = true;
    std::string quietStart = "00:00";
    std::string quietEnd = "06:00";
    std::vector<int> quietDays{0, 1, 2, 3, 4};   // 0=周一
    int preSwitchMinutes = 5;
    int maxRssMb = 60;
    std::vector<int> failureBackoff{120, 300, 900, 1800};
    bool teacherBackoff = false;
    int teacherFatalCooldown = 300;

    // 教师账号专用覆盖项（留空 = 和其它账号完全一样）
    std::string teacherPortalUrl;
    std::string teacherCasLoginUrl;
    std::string teacherService;
    std::string teacherStatusUrl;
    std::string teacherWifiSuffix;
};

Config defaultConfig();
void applyJson(Config& cfg, const Json& json);
Config loadConfig(const Profile& profile, const std::string& appDir);

std::vector<Profile> loadProfiles(const std::string& appDir);
// 按名字找线路；名字为空且只有一条线时就用那一条。
bool resolveProfile(const std::string& appDir, const std::string& name, Profile& out,
                    std::string& error);
std::string profileNames(const std::vector<Profile>& profiles);

// --------------------------------------------------------------------------
// 账号类型
// --------------------------------------------------------------------------
std::string normalizeAccountType(const std::string& value);   // 认不出来返回空
std::string accountTypeLabel(const std::string& type);        // 中文标签
bool quietHoursExempt(const Config& cfg, const std::string& account);
std::string resolveAccountType(const Config& cfg, const std::string& storedType,
                               const std::string& account);
// 教师模式下套用 teacher 段里的覆盖项。
Config applyAccountType(const Config& cfg, const std::string& accountType);

// --------------------------------------------------------------------------
// 账号密码 / 退避状态 / 模式文件
// --------------------------------------------------------------------------
struct Secret {
    std::string account;
    std::string password;
    std::string accountType;   // 已归一化，可能是空
};

bool loadSecret(const Profile& profile, Secret& out, std::string& error);
bool saveSecret(const Profile& profile, const Secret& secret);
// 只改账号类型，保留原来的账号密码；secret.json 不存在返回 false。
bool saveSecretAccountType(const Profile& profile, const std::string& accountType);
// 只读账号类型；没有/读不到返回空。
std::string storedAccountType(const Profile& profile);
std::string storedAccount(const Profile& profile);

struct RetryState {
    int failures = 0;
    long long nextAttempt = 0;
    std::string reason;
};

RetryState loadRetry(const Profile& profile);
void saveRetry(const Profile& profile, const RetryState& state);
void clearRetry(const Profile& profile);
int backoffSeconds(const Config& cfg, int failures);

// 模式（正常/夜间静默/退避/离线/尝试中）变化时才写一行日志并落状态文件。
bool noteMode(const Profile& profile, const std::string& mode, const std::string& message);

// --------------------------------------------------------------------------
// 夜间限制时段
// --------------------------------------------------------------------------
bool inQuietHours(const Config& cfg, const std::string& accountType,
                  const std::tm& now);
bool inQuietHours(const Config& cfg, const std::string& accountType);
// 夜间时段开始前 leadMinutes 分钟以内（用来提前把线路摘出备用池）。
bool inPreQuiet(const Config& cfg, const std::string& accountType, int leadMinutes,
                const std::tm& now);
bool inPreQuiet(const Config& cfg, const std::string& accountType, int leadMinutes);

// 调试页面存档；只保留最近 dumpKeep 份，免得把闪存写满。
const int kDumpKeep = 30;
void dumpPage(const Profile& profile, const std::string& tag, const std::string& text);

}  // namespace cnc
