#include "watch.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>

#include <unistd.h>

#include "http.hpp"
#include "portal.hpp"
#include "util.hpp"

namespace cnc {
namespace {

// 常量：状态文件里记的是"上次通知 mwan3 的动作"
const char* const kMarkerFile = "/mwan3_state.txt";

void checkMemory(const Config& cfg) {
    const int limit = cfg.maxRssMb;
    if (limit <= 0) return;
    const double rss = selfRssMb();
    if (rss > 0 && rss > static_cast<double>(limit)) {
        logWarn("常驻内存已到 %.1f MB（上限 %d MB），主动退出让服务重新拉起来", rss, limit);
        // 主动重启比被内核 OOM 杀掉可控得多：日志里有明确记录，中断只有一两秒
        std::fflush(nullptr);
        _exit(0);
    }
}

std::string humanWait(long long seconds) {
    if (seconds >= 60) return std::to_string(seconds / 60) + " 分钟";
    return std::to_string(seconds) + " 秒";
}

// 一条线路的看门狗循环。配了多条线时，每条线在自己的线程里跑这个。
int watchOne(Profile profile, const std::string& appDir, const std::string& bindIp) {
    setThreadLogger(&profile.logger);
    profile.logger.configure(profile.logDir(), profile.name == "default" ? "" : profile.name, false);

    Config cfg = loadConfig(profile, appDir);
    Secret secret;
    std::string problem;
    if (!loadSecret(profile, secret, problem)) {
        logError("%s", problem.c_str());
        return 2;
    }

    const std::string accountType =
        resolveAccountType(cfg, secret.accountType.empty() ? profile.defaultAccountType : secret.accountType,
                           secret.account);
    cfg = applyAccountType(cfg, accountType);

    Session session(cfg.timeout, bindIp, profile.device, routeTableFor(profile.name));

    const int interval = std::max(5, cfg.interval);
    // 教师模式默认不做失败退避：网络一恢复就立刻登录，不用干等 2/5/15/30 分钟。
    const bool useBackoff = accountType != "teacher" || cfg.teacherBackoff;
    // 但"账号级问题"（密码错 / 已在线 / 要验证码）再怎么重试也没用，还容易把账号
    // 撞锁，所以教师模式下这类情况仍然给一个固定冷却。
    const int fatalCooldown = useBackoff ? 0 : std::max(0, cfg.teacherFatalCooldown);

    logInfo("看门狗启动，每 %d 秒检测一次", interval);
    logInfo("线路 %s：出口 %s，账号 %s（%s）", profile.name.c_str(),
            profile.device.empty() ? "按系统路由" : profile.device.c_str(), secret.account.c_str(),
            accountTypeLabel(accountType).c_str());
    if (accountType == "teacher") {
        logInfo("教师账号模式：忽略夜间限制时段；在线时保持静默，不做状态刷屏");
        if (useBackoff) {
            logInfo("  登录失败仍按 failure_backoff 退避（teacher_backoff=true）");
        } else if (fatalCooldown > 0) {
            logInfo("  登录失败不退避，每个周期都重试；账号级问题冷却 %d 秒", fatalCooldown);
        } else {
            logInfo("  登录失败不退避，每个周期都重试");
        }
    }

    while (true) {
        const double started = monotonicSeconds();
        // 注意：编译时关掉了异常（省体积），所以这里没有 try/catch。
        // 真出了意料之外的问题，进程会直接退出，procd 的 respawn 会把它拉起来。

        // 占用太高就自己重启（procd 会拉起来），别等到被内核 OOM 杀掉
        checkMemory(cfg);

        // 学校是"到点断网"，等断了再切就已经晚了。提前几分钟把这条线摘出
        // 备用池，正在跑的连接能在原线路上跑完，新连接直接走备用线。
        const int lead = cfg.preSwitchMinutes;
        if (lead > 0 && inPreQuiet(cfg, accountType, lead)) {
            syncMwan3(profile, false);
            noteMode(profile, "pre-quiet",
                     format("距夜间限制时段不到 %d 分钟，提前把这条线摘出备用池，"
                            "避免到点断网时的卡顿",
                            lead));
            sleepSeconds(60);
            continue;
        }

        if (inQuietHours(cfg, accountType)) {
            noteMode(profile, "quiet",
                     format("进入夜间限制时段(%s-%s)，学校此时不允许学生账号认证，暂停尝试",
                            cfg.quietStart.c_str(), cfg.quietEnd.c_str()));
            // 夜间不尝试登录。这一段只允许把这条线摘出去，**绝不能再把它标回可用**。
            //
            // 实测踩到的坑（2026-09-23 00:00:03 的日志）：
            //   23:55:02 提前切换把 wan 摘出备用池
            //   00:00:03 静默分支又去查在线状态，而那一刻学生会话还没被学校切断，
            //            isOnline 仍为真，于是又把 wan 标回 up
            //   00:05:03 学校真断了之后才重新标 down
            // 结果就是 0 点前后多弹一次，把"提前切换"想要的效果抵消掉一半。
            //
            // 所以现在只在**确认这条线确实不在线**时才通知 mwan3 摘掉它；
            // 它还活着就维持 mwan3 现状（提前切换摘掉的就继续摘着，别去动它）。
            if (!isOnline(session, cfg, true)) {
                syncMwan3(profile, false);
            }
            sleepSeconds(300);
            continue;
        }

        if (isOnline(session, cfg, true)) {
            clearRetry(profile);
            syncMwan3(profile, true);
            noteMode(profile, "normal", "网络已恢复，回到常规检查");
        } else {
            const RetryState retry = loadRetry(profile);
            if (retry.nextAttempt > unixSeconds()) {
                const long long wait = retry.nextAttempt - unixSeconds() + 1;
                noteMode(profile, "backoff-" + std::to_string(retry.nextAttempt),
                         format("%s，%s后再试",
                                retry.reason.empty() ? "上次登录失败" : retry.reason.c_str(),
                                humanWait(wait).c_str()));
            } else if (!portalOk(session, cfg)) {
                // 门户都打不开 = 压根不在校园网（网线没插 / 上游断了 / 学校断网）。
                // 这不算"登录失败"，不能计入退避，否则等网络恢复后还要白等半小时。
                clearRetry(profile);
                noteMode(profile, "normal", "不在校园网环境（门户不可达），跳过本次尝试");
            } else {
                noteMode(profile, "login", "检测到未认证，开始尝试登录");
                // 在校园网、但这条线没通过认证 = 这条线现在不可用，先摘出去
                syncMwan3(profile, false);
                const std::string result =
                    login(session, cfg, profile, secret.account, secret.password);
                if (result == "ok") {
                    clearRetry(profile);
                    syncMwan3(profile, true);
                } else if (result == "offsite") {
                    clearRetry(profile);
                    noteMode(profile, "normal", "不在校园网环境（门户不可达），跳过本次尝试");
                } else if (!useBackoff && result != "fatal") {
                    // 教师模式：普通失败不退避，下个周期马上再试，保持静默
                    clearRetry(profile);
                } else if (!useBackoff && fatalCooldown > 0) {
                    RetryState state;
                    state.failures = retry.failures + 1;
                    state.nextAttempt = unixSeconds() + fatalCooldown;
                    state.reason = "账号级问题（需要人工处理）";
                    saveRetry(profile, state);
                } else {
                    RetryState state;
                    state.failures = retry.failures + 1;
                    const int delay = backoffSeconds(cfg, state.failures);
                    state.nextAttempt = unixSeconds() + delay;
                    state.reason = format("上次登录失败（累计 %d 次）", state.failures);
                    saveRetry(profile, state);
                    logWarn("登录失败，%d 秒内不再重试（累计失败 %d 次）", delay, state.failures);
                }
            }
        }
        // 让"每 N 秒检测一次"名副其实：扣掉这次检测本身花掉的时间
        sleepSeconds(std::max(1.0, static_cast<double>(interval) - (monotonicSeconds() - started)));
    }
    return 0;
}

int loginWith(const Profile& profile, const std::string& appDir, const std::string& bindIp) {
    Config cfg = loadConfig(profile, appDir);
    Secret secret;
    std::string problem;
    if (!loadSecret(profile, secret, problem)) {
        logError("%s", problem.c_str());
        return 2;
    }

    const std::string accountType =
        resolveAccountType(cfg, secret.accountType.empty() ? profile.defaultAccountType : secret.accountType,
                           secret.account);
    cfg = applyAccountType(cfg, accountType);
    Session session(cfg.timeout, bindIp, profile.device, routeTableFor(profile.name));

    if (inQuietHours(cfg, accountType)) {
        noteMode(profile, "quiet",
                 format("进入夜间限制时段（%s-%s），学校此时不允许学生账号认证，暂停尝试",
                        cfg.quietStart.c_str(), cfg.quietEnd.c_str()));
        return 0;
    }
    if (isOnline(session, cfg, true)) {
        clearRetry(profile);
        noteMode(profile, "normal", "网络已恢复，回到常规检查");
        return 0;
    }
    if (!portalOk(session, cfg)) {
        clearRetry(profile);
        noteMode(profile, "offsite", "不在校园网环境（门户不可达），跳过本次尝试");
        return 0;
    }

    const RetryState retry = loadRetry(profile);
    if (retry.nextAttempt > unixSeconds()) {
        const long long wait = retry.nextAttempt - unixSeconds() + 1;
        noteMode(profile, "backoff-" + std::to_string(retry.nextAttempt),
                 format("%s，%s后再试", retry.reason.empty() ? "上次登录失败" : retry.reason.c_str(),
                        humanWait(wait).c_str()));
        return 0;
    }

    noteMode(profile, "login", "检测到未认证，开始尝试登录");
    const std::string result = login(session, cfg, profile, secret.account, secret.password);
    if (result == "ok") {
        clearRetry(profile);
        return 0;
    }
    if (result == "offsite") {
        clearRetry(profile);
        noteMode(profile, "offsite", "不在校园网环境（门户不可达），跳过本次尝试");
        return 0;
    }

    RetryState state;
    state.failures = retry.failures + 1;
    const int delay = backoffSeconds(cfg, state.failures);
    state.nextAttempt = unixSeconds() + delay;
    state.reason = format("上次登录失败（累计 %d 次）", state.failures);
    saveRetry(profile, state);
    logWarn("登录失败，%d 秒内不再重试（累计失败 %d 次）", delay, state.failures);
    return 1;
}

void prepare(Profile& profile) {
    setThreadLogger(&profile.logger);
    profile.logger.configure(profile.logDir(), profile.name == "default" ? "" : profile.name, false);
}

std::vector<Profile> selectedProfiles(const std::string& appDir, const std::string& filter,
                                      std::string& error) {
    std::vector<Profile> profiles = loadProfiles(appDir);
    if (filter.empty()) return profiles;
    for (const Profile& profile : profiles) {
        if (profile.name == filter) return {profile};
    }
    error = "没有叫 '" + filter + "' 的线路。已配置的线路：" + profileNames(profiles);
    return {};
}

}  // namespace

void syncMwan3(const Profile& profile, bool online) {
    // 为什么不靠 mwan3 自己的 ping 探测：路由器上实测踩过一次 —— mwan3 的跟踪
    // 进程会卡死，结果一条线明明已经掉了认证，mwan3 却一直以为它在线，主备切换
    // 彻底失效。而本程序每 15 秒就在查校园网状态接口，本来就知道每条线的真相。
    const std::string name = trim(profile.mwan3);
    if (name.empty()) return;

    const std::string want = online ? "up" : "down";
    const std::string marker = profile.logDir() + kMarkerFile;
    std::string previous;
    if (readFile(marker, previous) && trim(previous) == want &&
        unixSeconds() - fileMTime(marker) < 600) {
        return;   // 状态没变就不重复调用；但 10 分钟强制重申一次
    }

    const CommandResult result = runCommand({"mwan3", want, name}, 20);
    if (!result.ok()) {
        logWarn("通知 mwan3 %s %s 失败", name.c_str(), want.c_str());
        return;
    }
    makeDirs(profile.logDir());
    writeFile(marker, want);
    logInfo("已通知 mwan3：%s %s（%s）", name.c_str(), want.c_str(),
            online ? "这条线已认证" : "这条线未认证，让流量走其它线");
}

int runWatch(const std::string& appDir, const std::string& profileFilter,
             const std::string& bindIp) {
    std::string error;
    std::vector<Profile> profiles = selectedProfiles(appDir, profileFilter, error);
    if (!error.empty()) {
        std::fprintf(stderr, "%s\n", error.c_str());
        return 2;
    }
    if (profiles.size() == 1) {
        return watchOne(profiles.front(), appDir, bindIp);
    }

    // 多条线跑在同一个进程里：只占一份运行时内存，而且互不阻塞
    std::vector<std::thread> threads;
    prepare(profiles.front());
    logInfo("检测到 %zu 条线路，在同一个进程里并行看护：%s", profiles.size(),
            profileNames(profiles).c_str());
    for (Profile& profile : profiles) {
        prepare(profile);
        Profile copy = profile;
        threads.emplace_back([copy, appDir, bindIp]() mutable {
            watchOne(std::move(copy), appDir, bindIp);
        });
    }
    for (std::thread& worker : threads) worker.join();
    return 0;
}

int runLoginOnce(const std::string& appDir, const std::string& profileFilter,
                 const std::string& bindIp) {
    std::string error;
    std::vector<Profile> profiles = selectedProfiles(appDir, profileFilter, error);
    if (!error.empty()) {
        std::fprintf(stderr, "%s\n", error.c_str());
        return 2;
    }
    int code = 0;
    for (Profile& profile : profiles) {
        prepare(profile);
        code |= loginWith(profile, appDir, bindIp);
    }
    return code;
}

int runCheck(const std::string& appDir, const std::string& profileFilter, bool probe, bool quiet) {
    std::string error;
    std::vector<Profile> profiles = selectedProfiles(appDir, profileFilter, error);
    if (!error.empty()) {
        std::fprintf(stderr, "%s\n", error.c_str());
        return 2;
    }

    int code = 0;
    for (Profile& profile : profiles) {
        setThreadLogger(&profile.logger);
        profile.logger.configure(profile.logDir(), profile.name == "default" ? "" : profile.name,
                                 !quiet);
        Config cfg = loadConfig(profile, appDir);
        Session session(cfg.timeout, "", profile.device, routeTableFor(profile.name));

        if (probe) {
            // 只读探测：看看门户把这条线判成有线还是无线、走哪套登录
            const HttpResponse page = session.request(cfg.portalUrl);
            const std::string ip = clientIpFromPortal(page.body);
            logInfo("门户 HTTP %d，页面 %zu 字节", page.status, page.body.size());
            logInfo("门户看到的客户端地址: %s", ip.empty() ? "（没解析出来）" : ip.c_str());
            std::string flow;
            if (!needsCas(ip)) {
                flow = "无线网段 → Dr.COM 门户登录";
            } else if (toLower(cfg.wiredFlow) == "cas") {
                flow = "有线网段 → 统一身份认证 + 拼图滑块（config.json 里强制指定）";
            } else {
                flow = "有线网段 → 先 Dr.COM 表单，失败再回退统一身份认证";
            }
            Secret secret;
            std::string ignored;
            loadSecret(profile, secret, ignored);
            const std::string accountType = resolveAccountType(
                cfg, secret.accountType.empty() ? profile.defaultAccountType : secret.accountType,
                secret.account);
            logInfo("应该走: %s", flow.c_str());
            logInfo("账号类型: %s", accountTypeLabel(accountType).c_str());
            const PortalConfig conf = parsePortalConfig(page.body);
            if (!conf.authLoginPath.empty()) logInfo("  门户配置 authloginpath = %s", conf.authLoginPath.c_str());
            if (!conf.authLoginPort.empty()) logInfo("  门户配置 authloginport = %s", conf.authLoginPort.c_str());
            if (!conf.authUserField.empty()) logInfo("  门户配置 authuserfield = %s", conf.authUserField.c_str());
            if (!conf.authPassField.empty()) logInfo("  门户配置 authpassfield = %s", conf.authPassField.c_str());
            if (!conf.jsVersion.empty()) logInfo("  门户配置 jsVersion = %s", conf.jsVersion.c_str());
            logInfo("当前是否已在线: %s", isOnline(session, cfg, true) ? "是" : "否");
            continue;
        }

        bool online = false;
        online = isOnline(session, cfg);
        logInfo("当前状态: %s", online ? "已在线" : "未认证/已断网");
        if (!online) code = 1;
    }
    return code;
}

}  // namespace cnc
