#include "config.hpp"

#include <algorithm>
#include <cstdlib>
#include <cstdio>
#include <ctime>

namespace cnc {
namespace {

std::vector<std::string> stringList(const Json* value) {
    std::vector<std::string> out;
    if (value == nullptr || !value->isArray()) return out;
    for (const Json& item : value->items) {
        if (item.isString()) out.push_back(item.text);
    }
    return out;
}

std::vector<int> intList(const Json* value) {
    std::vector<int> out;
    if (value == nullptr || !value->isArray()) return out;
    for (const Json& item : value->items) {
        if (item.isNumber()) out.push_back(static_cast<int>(item.number));
    }
    return out;
}

std::string stringField(const Json& json, const char* key, const std::string& fallback) {
    const Json* value = json.find(key);
    if (value == nullptr) return fallback;
    const std::string text = trim(value->asString());
    return text.empty() ? fallback : text;
}

int intField(const Json& json, const char* key, int fallback) {
    const Json* value = json.find(key);
    if (value == nullptr || value->isNull()) return fallback;
    return static_cast<int>(value->asInt(fallback));
}

bool boolField(const Json& json, const char* key, bool fallback) {
    const Json* value = json.find(key);
    if (value == nullptr || value->isNull()) return fallback;
    return value->asBool(fallback);
}

std::string readJsonFile(const std::string& path, Json& out) {
    std::string raw;
    if (!readFile(path, raw)) return "读不到 " + path;
    bool ok = false;
    out = Json::parse(raw, &ok);
    if (!ok) return path + " 不是合法 JSON";
    return "";
}

int hhmmToMinutes(const std::string& text) {
    const size_t colon = text.find(':');
    if (colon == std::string::npos) return -1;
    const int hour = std::atoi(text.substr(0, colon).c_str());
    const int minute = std::atoi(text.substr(colon + 1).c_str());
    return hour * 60 + minute;
}

std::tm localNow() {
    const std::time_t now = std::time(nullptr);
    std::tm parts{};
    localtime_r(&now, &parts);
    return parts;
}

std::tm addMinutes(const std::tm& base, int minutes) {
    std::tm copy = base;
    std::time_t stamp = std::mktime(&copy);
    stamp += static_cast<std::time_t>(minutes) * 60;
    std::tm result{};
    localtime_r(&stamp, &result);
    return result;
}

}  // namespace

Config defaultConfig() { return Config(); }

void applyJson(Config& cfg, const Json& json) {
    if (!json.isObject()) return;

    cfg.portalUrl = stringField(json, "portal_url", cfg.portalUrl);
    cfg.casLoginUrl = stringField(json, "cas_login_url", cfg.casLoginUrl);
    cfg.service = stringField(json, "service", cfg.service);
    cfg.statusUrl = stringField(json, "status_url", cfg.statusUrl);
    cfg.captchaUrl = stringField(json, "captcha_url", cfg.captchaUrl);
    cfg.probeUrl = stringField(json, "probe_url", cfg.probeUrl);

    const Json* interval = json.find("interval");
    if (interval != nullptr) cfg.interval = std::max(5, static_cast<int>(interval->asInt(cfg.interval)));
    const Json* loginTimeout = json.find("login_timeout");
    if (loginTimeout != nullptr) cfg.loginTimeout = static_cast<int>(loginTimeout->asInt(cfg.loginTimeout));
    const Json* timeout = json.find("timeout");
    if (timeout != nullptr) cfg.timeout = std::max(1, static_cast<int>(timeout->asInt(cfg.timeout)));

    cfg.wiredFlow = toLower(stringField(json, "wired_flow", cfg.wiredFlow));
    // wifi_suffix 允许显式留空（= 自动依次尝试），所以不能用 stringField 的"空即忽略"
    if (const Json* suffix = json.find("wifi_suffix")) cfg.wifiSuffix = trim(suffix->asString());
    if (const Json* type = json.find("account_type")) {
        cfg.accountType = normalizeAccountType(type->asString());
    }

    const Json* prefixes = json.find("teacher_account_prefixes");
    if (prefixes != nullptr) cfg.teacherAccountPrefixes = stringList(prefixes);
    const Json* exempt = json.find("quiet_hours_exempt_accounts");
    if (exempt != nullptr) cfg.quietHoursExemptAccounts = stringList(exempt);

    if (const Json* quiet = json.find("quiet_hours"); quiet != nullptr && quiet->isObject()) {
        cfg.quietEnabled = boolField(*quiet, "enabled", cfg.quietEnabled);
        cfg.quietStart = stringField(*quiet, "start", cfg.quietStart);
        cfg.quietEnd = stringField(*quiet, "end", cfg.quietEnd);
        const Json* days = quiet->find("days");
        if (days != nullptr) cfg.quietDays = intList(days);
    }

    cfg.preSwitchMinutes = intField(json, "pre_switch_minutes", cfg.preSwitchMinutes);
    cfg.maxRssMb = intField(json, "max_rss_mb", cfg.maxRssMb);
    const Json* backoff = json.find("failure_backoff");
    if (backoff != nullptr) {
        const std::vector<int> parsed = intList(backoff);
        if (!parsed.empty()) cfg.failureBackoff = parsed;
    }
    cfg.teacherBackoff = boolField(json, "teacher_backoff", cfg.teacherBackoff);
    cfg.teacherFatalCooldown =
        intField(json, "teacher_fatal_cooldown", cfg.teacherFatalCooldown);

    if (const Json* teacher = json.find("teacher"); teacher != nullptr && teacher->isObject()) {
        cfg.teacherPortalUrl = stringField(*teacher, "portal_url", cfg.teacherPortalUrl);
        cfg.teacherCasLoginUrl = stringField(*teacher, "cas_login_url", cfg.teacherCasLoginUrl);
        cfg.teacherService = stringField(*teacher, "service", cfg.teacherService);
        cfg.teacherStatusUrl = stringField(*teacher, "status_url", cfg.teacherStatusUrl);
        cfg.teacherWifiSuffix = stringField(*teacher, "wifi_suffix", cfg.teacherWifiSuffix);
    }
}

Config loadConfig(const Profile& profile, const std::string& appDir) {
    Config cfg = defaultConfig();
    // 三层合并：内置默认值 < 公共 config.json < 本线路的 config.json
    for (const std::string& path : {appDir + "/config.json", profile.configFile()}) {
        Json json;
        if (readJsonFile(path, json).empty()) applyJson(cfg, json);
    }
    return cfg;
}

std::vector<Profile> loadProfiles(const std::string& appDir) {
    Config top = defaultConfig();
    Json root;
    if (readJsonFile(appDir + "/config.json", root).empty()) applyJson(top, root);

    std::vector<Profile> profiles;
    const Json* list = root.find("profiles");
    if (list != nullptr && list->isArray()) {
        for (const Json& item : list->items) {
            if (!item.isObject()) continue;
            const std::string name = trim(item.find("name") ? item.find("name")->asString() : "");
            if (name.empty()) continue;
            Profile profile;
            profile.name = name;
            const Json* dir = item.find("dir");
            profile.base = (dir != nullptr && !dir->asString().empty())
                               ? dir->asString()
                               : appDir + "/profiles/" + name;
            profile.device = item.find("device") ? item.find("device")->asString() : "";
            profile.defaultAccountType =
                normalizeAccountType(item.find("account_type")
                                         ? item.find("account_type")->asString()
                                         : "");
            const Json* mwan3 = item.find("mwan3");
            // 和 Python 版保持一致：没写 mwan3 就是不和 mwan3 联动
            profile.mwan3 = mwan3 != nullptr ? trim(mwan3->asString()) : "";
            profiles.push_back(std::move(profile));
        }
    }

    if (profiles.empty()) {
        Profile single;
        single.name = "default";
        single.base = appDir;
        profiles.push_back(std::move(single));
    }
    return profiles;
}

std::string profileNames(const std::vector<Profile>& profiles) {
    std::vector<std::string> names;
    names.reserve(profiles.size());
    for (const Profile& profile : profiles) names.push_back(profile.name);
    return joinWith(names, "、");
}

bool resolveProfile(const std::string& appDir, const std::string& name, Profile& out,
                    std::string& error) {
    const std::vector<Profile> profiles = loadProfiles(appDir);
    if (!name.empty()) {
        for (const Profile& profile : profiles) {
            if (profile.name == name) {
                out = profile;
                return true;
            }
        }
        error = "没有叫 '" + name + "' 的线路。已配置的线路：" + profileNames(profiles);
        return false;
    }
    if (profiles.size() == 1) {
        out = profiles.front();
        return true;
    }
    error = "配了多条线路，请用 --profile 指定一条。已配置的线路：" + profileNames(profiles);
    return false;
}

// --------------------------------------------------------------------------
// 账号类型
// --------------------------------------------------------------------------

std::string normalizeAccountType(const std::string& value) {
    const std::string text = toLower(trim(value));
    if (text.empty()) return "";
    if (text == "student" || text == "stu" || text == "s" || text == "1") return "student";
    if (text == "teacher" || text == "tea" || text == "t" || text == "staff" || text == "2") {
        return "teacher";
    }
    if (contains(value, "教") || contains(value, "师")) return "teacher";
    if (contains(value, "学") || contains(value, "生")) return "student";
    return "";
}

std::string accountTypeLabel(const std::string& type) {
    if (type == "teacher") return "教师账号";
    if (type == "student") return "学生账号";
    return type.empty() ? "未设置" : type;
}

bool quietHoursExempt(const Config& cfg, const std::string& account) {
    const std::string name = trim(account);
    if (name.empty()) return false;
    const std::string upper = toLower(name);
    for (const std::string& prefix : cfg.teacherAccountPrefixes) {
        const std::string clean = toLower(trim(prefix));
        if (!clean.empty() && startsWith(upper, clean)) return true;
    }
    for (const std::string& exempt : cfg.quietHoursExemptAccounts) {
        if (name == trim(exempt)) return true;
    }
    return false;
}

std::string resolveAccountType(const Config& cfg, const std::string& storedType,
                               const std::string& account) {
    // 优先级：secret.json > config.json > 按账号前缀自动判断
    for (const std::string& candidate : {storedType, cfg.accountType}) {
        const std::string value = normalizeAccountType(candidate);
        if (!value.empty()) return value;
    }
    if (quietHoursExempt(cfg, account)) return "teacher";
    return "student";
}

Config applyAccountType(const Config& cfg, const std::string& accountType) {
    if (accountType != "teacher") return cfg;
    Config out = cfg;
    if (!cfg.teacherPortalUrl.empty()) out.portalUrl = cfg.teacherPortalUrl;
    if (!cfg.teacherCasLoginUrl.empty()) out.casLoginUrl = cfg.teacherCasLoginUrl;
    if (!cfg.teacherService.empty()) out.service = cfg.teacherService;
    if (!cfg.teacherStatusUrl.empty()) out.statusUrl = cfg.teacherStatusUrl;
    if (!cfg.teacherWifiSuffix.empty()) out.wifiSuffix = cfg.teacherWifiSuffix;
    return out;
}

// --------------------------------------------------------------------------
// 账号密码 / 退避状态 / 模式文件
// --------------------------------------------------------------------------

bool loadSecret(const Profile& profile, Secret& out, std::string& error) {
    if (!fileExists(profile.secretFile())) {
        error = "还没有保存账号密码，请先运行: campus-net-login --set-password";
        return false;
    }
    Json json;
    const std::string problem = readJsonFile(profile.secretFile(), json);
    if (!problem.empty()) {
        error = problem;
        return false;
    }
    const Json* account = json.find("account");
    const Json* password = json.find("password");
    if (account == nullptr || password == nullptr) {
        error = profile.secretFile() + " 里缺少 account / password";
        return false;
    }
    out.account = account->asString();
    out.password = password->asString();
    out.accountType = normalizeAccountType(json.find("account_type")
                                               ? json.find("account_type")->asString()
                                               : "");
    return true;
}

bool saveSecret(const Profile& profile, const Secret& secret) {
    Json json = Json::object();
    json.set("account", Json::of(secret.account));
    json.set("password", Json::of(secret.password));
    if (!secret.accountType.empty()) json.set("account_type", Json::of(secret.accountType));
    // 0600：只有 root 能读，密码不裸奔
    return writeFile(profile.secretFile(), json.dump(), 0600);
}

bool saveSecretAccountType(const Profile& profile, const std::string& accountType) {
    if (!fileExists(profile.secretFile())) return false;
    Json json;
    if (!readJsonFile(profile.secretFile(), json).empty()) return false;
    json.set("account_type", Json::of(accountType));
    return writeFile(profile.secretFile(), json.dump(), 0600);
}

std::string storedAccountType(const Profile& profile) {
    Json json;
    if (!readJsonFile(profile.secretFile(), json).empty()) return "";
    const Json* type = json.find("account_type");
    return type != nullptr ? normalizeAccountType(type->asString()) : "";
}

std::string storedAccount(const Profile& profile) {
    Json json;
    if (!readJsonFile(profile.secretFile(), json).empty()) return "";
    const Json* account = json.find("account");
    return account != nullptr ? account->asString() : "";
}

RetryState loadRetry(const Profile& profile) {
    RetryState state;
    Json json;
    if (!readJsonFile(profile.retryFile(), json).empty()) return state;
    if (const Json* failures = json.find("failures")) state.failures = static_cast<int>(failures->asInt());
    if (const Json* next = json.find("next_attempt")) {
        state.nextAttempt = static_cast<long long>(next->asNumber());
    }
    if (const Json* reason = json.find("reason")) state.reason = reason->asString();
    return state;
}

void saveRetry(const Profile& profile, const RetryState& state) {
    Json json = Json::object();
    json.set("failures", Json::of(static_cast<int64_t>(state.failures)));
    json.set("next_attempt", Json::of(static_cast<double>(state.nextAttempt)));
    json.set("reason", Json::of(state.reason));
    makeDirs(profile.logDir());
    writeFile(profile.retryFile(), json.dump());
}

void clearRetry(const Profile& profile) { removeFile(profile.retryFile()); }

int backoffSeconds(const Config& cfg, int failures) {
    if (cfg.failureBackoff.empty()) return 120;
    const int index = std::max(1, std::min<int>(failures, static_cast<int>(cfg.failureBackoff.size()))) - 1;
    return cfg.failureBackoff[static_cast<size_t>(index)];
}

bool noteMode(const Profile& profile, const std::string& mode, const std::string& message) {
    // 状态没变时连文件都不写：15 秒一轮的频率下，每轮写一次闪存没必要
    std::string previous;
    if (readFile(profile.modeFile(), previous)) {
        if (trim(previous) == mode) return false;
    }
    makeDirs(profile.logDir());
    writeFile(profile.modeFile(), mode);
    logInfo("%s", message.c_str());
    return true;
}

// --------------------------------------------------------------------------
// 夜间限制时段
// --------------------------------------------------------------------------

bool inQuietHours(const Config& cfg, const std::string& accountType, const std::tm& now) {
    if (accountType == "teacher") return false;   // 教师账号不受这条策略限制
    if (!cfg.quietEnabled) return false;

    const int weekday = now.tm_wday == 0 ? 6 : now.tm_wday - 1;   // tm_wday：0=周日
    if (!cfg.quietDays.empty() &&
        std::find(cfg.quietDays.begin(), cfg.quietDays.end(), weekday) == cfg.quietDays.end()) {
        return false;
    }

    const int start = hhmmToMinutes(cfg.quietStart);
    const int end = hhmmToMinutes(cfg.quietEnd);
    if (start < 0 || end < 0) return false;

    const int current = now.tm_hour * 60 + now.tm_min;
    if (start <= end) return current >= start && current < end;
    return current >= start || current < end;   // 跨天
}

bool inQuietHours(const Config& cfg, const std::string& accountType) {
    return inQuietHours(cfg, accountType, localNow());
}

bool inPreQuiet(const Config& cfg, const std::string& accountType, int leadMinutes,
                const std::tm& now) {
    if (accountType == "teacher" || leadMinutes <= 0) return false;
    if (inQuietHours(cfg, accountType, now)) return false;
    return inQuietHours(cfg, accountType, addMinutes(now, leadMinutes));
}

bool inPreQuiet(const Config& cfg, const std::string& accountType, int leadMinutes) {
    return inPreQuiet(cfg, accountType, leadMinutes, localNow());
}

void dumpPage(const Profile& profile, const std::string& tag, const std::string& text) {
    makeDirs(profile.logDir());
    const std::tm now = localNow();
    char stamp[32];
    std::strftime(stamp, sizeof(stamp), "%Y%m%d-%H%M%S", &now);
    const std::string name = std::string("http-") + stamp + "-" + tag + ".html";
    writeFile(profile.logDir() + "/" + name, text);
    logInfo("页面已存档: %s", name.c_str());

    // 高频重试时这些存档会飞快堆积，只保留最近的若干份
    std::vector<std::string> dumps;
    for (const std::string& entry : listDir(profile.logDir())) {
        if (startsWith(entry, "http-") && endsWith(entry, ".html")) dumps.push_back(entry);
    }
    if (static_cast<int>(dumps.size()) <= kDumpKeep) return;
    std::sort(dumps.begin(), dumps.end());   // 文件名里带时间戳，字典序就是时间序
    for (size_t i = 0; i + static_cast<size_t>(kDumpKeep) < dumps.size(); ++i) {
        removeFile(profile.logDir() + "/" + dumps[i]);
    }
}

}  // namespace cnc
