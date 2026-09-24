#include "portal.hpp"

#include <algorithm>

#include "rsa.hpp"
#include "util.hpp"

namespace cnc {
namespace {

std::string originOf(const std::string& url) {
    Url parsed;
    if (!parseUrl(url, parsed)) return "";
    return parsed.scheme + "://" + parsed.host + ":" + std::to_string(parsed.port);
}

std::vector<std::string> captchaEndpoints(const std::string& loginUrl, const std::string& page) {
    const std::string origin = originOf(loginUrl);
    // 学校页面里写的是 var contextPath = "/cas"，少了这个前缀就会打到错误的地方
    std::string context = findContextPath(page);
    if (context.empty()) context = "/cas";
    while (!context.empty() && context.back() == '/') context.pop_back();

    std::vector<std::string> urls;
    if (!context.empty()) urls.push_back(origin + context + "/captchValid/checkCaptchImg");
    urls.push_back(origin + "/captchValid/checkCaptchImg");   // 后备
    return urls;
}

}  // namespace

bool looksOnline(const Session& session, int code, const std::string& text) {
    if (containsPortalMarker(text)) return false;
    if (containsPortalMarker(session.lastLocation())) return false;
    if (code == 200) return true;
    return code >= 300 && code < 400;
}

bool isOnline(Session& session, const Config& cfg, bool quiet) {
    const HttpResponse status = session.request(cfg.statusUrl, nullptr, true);
    if (status.status == 200) {
        const std::string body = trim(status.body);
        if (!body.empty() && body[0] == '{') {
            bool ok = false;
            const Json json = Json::parse(body, &ok);
            if (ok) {
                const Json* success = json.find("success");
                if (success != nullptr && success->asBool()) return true;
            }
        }
    }
    // 接口不可用或返回未登录时，用真实访问复核（未认证时会被门户劫持）
    const HttpResponse probe = session.request(cfg.probeUrl);
    if (looksOnline(session, probe.status, probe.body)) return true;
    if (!quiet) {
        logInfo("判定为未认证（状态接口返回 %s）", status.body.substr(0, 120).c_str());
    }
    return false;
}

bool portalOk(Session& session, const Config& cfg) {
    const HttpResponse page = session.request(cfg.portalUrl);
    if (page.status != 200) return false;
    return contains(page.body, "Dr.COMWebLogin") || contains(page.body, "eportal") ||
           contains(page.body, "DrcomServer");
}

std::string login(Session& session, const Config& cfg, const Profile& profile,
                  const std::string& account, const std::string& password) {
    const HttpResponse portal = session.request(cfg.portalUrl);
    if (portal.status != 200 ||
        !(contains(portal.body, "Dr.COMWebLogin") || contains(portal.body, "eportal") ||
          contains(portal.body, "DrcomServer"))) {
        logWarn("访问不到校园网门户，判定不在校园网，放弃登录（HTTP %d）", portal.status);
        return "offsite";
    }

    const std::string ip = clientIpFromPortal(portal.body);
    const bool wired = needsCas(ip);
    const std::string wiredFlow = toLower(cfg.wiredFlow);

    if (wired && wiredFlow == "cas") {
        logInfo("有线网段（客户端 %s）→ 按配置走统一身份认证", ip.empty() ? "?" : ip.c_str());
        return casLogin(session, cfg, profile, account, password) ? "ok" : "failed";
    }
    if (wired) {
        // 实测：Dr.COM 的登录接口并不拒绝有线客户端，直接走表单可以绕开
        // 统一认证必须做的滑块 / 人脸验证。
        logInfo("有线网段（客户端 %s）→ 优先走 Dr.COM 表单（可绕开统一认证的验证环节）",
                ip.empty() ? "?" : ip.c_str());
    } else {
        logInfo("无线网段（客户端 %s）→ 走 Dr.COM 门户登录", ip.empty() ? "?" : ip.c_str());
    }

    const std::string result = drcomLogin(session, cfg, profile, portal.body, account, password);
    if (result == "ok") return "ok";
    if (result == "fatal") {
        logError("Dr.COM 登录遇到需要人工处理的提示，本次不再尝试其它登录方式");
        return "fatal";
    }
    if (wired && wiredFlow != "cas") {
        logInfo("Dr.COM 方式没有成功，改用统一身份认证再试一次");
        return casLogin(session, cfg, profile, account, password) ? "ok" : "failed";
    }
    return "failed";
}

std::string drcomLogin(Session& session, const Config& cfg, const Profile& profile,
                       const std::string& portalHtml, const std::string& account,
                       const std::string& password) {
    const PortalConfig conf = parsePortalConfig(portalHtml);

    Url portalUrl;
    parseUrl(cfg.portalUrl, portalUrl);
    const std::string host = portalUrl.host.empty() ? "10.255.255.2" : portalUrl.host;
    const std::string port = conf.authLoginPort.empty() ? "801" : conf.authLoginPort;
    const std::string userField = conf.authUserField.empty() ? "DDDDD" : conf.authUserField;
    const std::string passField = conf.authPassField.empty() ? "upass" : conf.authPassField;
    std::string loginPath =
        conf.authLoginPath.empty() ? "/eportal/?c=ACSetting&a=Login" : conf.authLoginPath;
    if (!contains(loginPath, "ver=")) {
        // 门户页面里这个路径不带 ver，但实测 AC 需要 ver=1.0 才会走登录接口
        // （否则返回的是后台管理页面）
        loginPath += (contains(loginPath, "?") ? "&" : "?") + std::string("ver=1.0");
    }
    const std::string base = "http://" + host + ":" + port;

    logInfo("门户配置: 登录路径=%s 端口=%s 账号字段=%s 密码字段=%s", loginPath.c_str(),
            port.c_str(), userField.c_str(), passField.c_str());

    // 服务类型：默认"校园用户"（账号不带后缀），另有电信/联通。
    // 配置里写了 wifi_suffix 就只用那一种，避免多余尝试。
    std::vector<std::pair<std::string, std::string>> accountForms;
    if (!cfg.wifiSuffix.empty()) {
        accountForms.emplace_back("配置指定 " + cfg.wifiSuffix, account + cfg.wifiSuffix);
        logInfo("按配置使用服务类型后缀: %s", cfg.wifiSuffix.c_str());
    } else {
        accountForms.emplace_back("校园用户(默认)", account);
        accountForms.emplace_back("校园网后缀 @njxy", account + "@njxy");
        accountForms.emplace_back("校园电信 @dx", account + "@dx");
        accountForms.emplace_back("校园联通 @lt", account + "@lt");
    }

    struct Endpoint {
        std::string name;
        std::string url;
        std::vector<std::pair<std::string, std::string>> extra;
        size_t formCount;   // 只试前几个服务类型
    };
    const std::vector<Endpoint> endpoints = {
        {"ACSetting", base + loginPath, {{"url", "drappall"}}, accountForms.size()},
        // 新版接口在本校实测始终返回"无法获取用户认证账号"，只在默认服务类型下试一次
        {"portal/login", base + "/eportal/portal/login", {}, 1},
    };

    std::string lastError;
    bool triedAcSetting = false;
    for (const Endpoint& endpoint : endpoints) {
        // 新版接口在本校实测不可用；只有当旧接口连账号字段都认不出时才去试它
        if (endpoint.name == "portal/login" && triedAcSetting &&
            !contains(lastError, "无法获取用户认证账号")) {
            logInfo("旧接口能识别账号，跳过新版接口");
            break;
        }
        const size_t limit = std::min(endpoint.formCount, accountForms.size());
        for (size_t i = 0; i < limit; ++i) {
            const std::string& formName = accountForms[i].first;
            const std::string& userValue = accountForms[i].second;

            std::vector<std::pair<std::string, std::string>> data;
            data.emplace_back(userField, userValue);
            data.emplace_back(passField, password);
            data.emplace_back("0MKKey", "123456");
            data.emplace_back("R1", "");
            data.emplace_back("R2", "");
            data.emplace_back("R3", "");
            data.emplace_back("R6", "0");
            data.emplace_back("para", "");
            data.emplace_back("v6ip", "");
            data.emplace_back("terminal_type", "1");
            data.emplace_back("lang", "zh-cn");
            if (!conf.jsVersion.empty()) data.emplace_back("jsVersion", conf.jsVersion);
            for (const auto& kv : endpoint.extra) data.push_back(kv);

            logInfo("Dr.COM 登录：接口 %s，服务类型 %s", endpoint.name.c_str(), formName.c_str());
            // 必须带 AJAX 头：否则 AC 会返回后台管理页面而不是登录结果
            const HttpResponse response = session.request(endpoint.url, &data, true);
            std::string text = trim(response.body);
            text = replaceAll(text, "\n", " ");
            text = text.substr(0, 220);
            logInfo("  → HTTP %d: %s", response.status, text.c_str());
            dumpPage(profile, "drcom-" + endpoint.name + "-" + formName, response.body);

            if (contains(response.body, "验证码")) {
                logError("门户要求图形验证码，纯 HTTP 模式无法自动识别（可改用浏览器模式）");
                return "fatal";
            }

            // 提交后给服务器一点时间放行
            const double deadline = monotonicSeconds() + 8.0;
            while (monotonicSeconds() < deadline) {
                if (isOnline(session, cfg, true)) {
                    logInfo("登录成功，网络已恢复（服务类型：%s）", formName.c_str());
                    return "ok";
                }
                sleepSeconds(2);
            }

            lastError = text;
            if (endpoint.name == "ACSetting") triedAcSetting = true;

            // 这几类错误换接口、换服务类型都没用，继续试还可能把账号撞锁，立刻停手。
            // 注意："账号错误" / "Authentication fail" 不算致命 —— 那往往只是
            // 服务类型(后缀)选错了，应该继续试下一个。
            const char* fatalWords[] = {"密码", "验证码", "在线数超出限制", "Limit Users", "已在线"};
            std::string hit;
            for (const char* word : fatalWords) {
                if (contains(response.body, word)) {
                    hit = word;
                    break;
                }
            }
            if (!hit.empty()) {
                logError("服务器返回需要人工处理的提示 [%s]：%s", hit.c_str(), text.c_str());
                if (hit == "已在线") {
                    logError("  说明该账号已经有一个会话在线（学校限制并发设备数）。");
                    logError("  旧会话在服务端会残留一段时间才释放，这期间新设备登录会被拒。");
                }
                return "fatal";
            }
        }
        logInfo("  接口 %s 未成功，换下一个接口", endpoint.name.c_str());
    }

    logError("Dr.COM 门户登录失败，最后一次返回: %s", lastError.c_str());
    return "failed";
}

namespace {

bool passCaptcha(Session& session, const Config& cfg, const Profile& profile,
                 const std::string& loginUrl, const std::string& action,
                 const std::string& account, const std::string& page) {
    logInfo("服务器要求安全验证，上报验证结果");
    // 上报地址必须是 contextPath + /captchValid/checkCaptchImg
    for (const std::string& url : captchaEndpoints(loginUrl, page)) {
        const std::vector<std::pair<std::string, std::string>> payload = {
            {"request_username", account}, {"captchResult", "1"}};
        const HttpResponse response = session.request(url, &payload, true);
        const std::string body = replaceAll(trim(response.body), "\n", " ");
        logInfo("  POST %s", url.c_str());
        logInfo("  → HTTP %d 返回: %s", response.status, body.substr(0, 160).c_str());
        if (response.status == 200 &&
            (contains(response.body, "true") || contains(response.body, "success") ||
             contains(response.body, "1"))) {
            break;
        }
    }

    const std::vector<Form> forms = parseForms(page);
    const Form* fm4 = nullptr;
    const Form* fm3 = nullptr;
    for (const Form& form : forms) {
        if (form.id == "fm4") fm4 = &form;
        if (form.id == "fm3") fm3 = &form;
    }

    struct Strategy {
        std::string name;
        std::string target;
        std::vector<std::pair<std::string, std::string>> payload;
    };
    std::vector<Strategy> strategies;
    if (fm4 != nullptr) {
        strategies.push_back({"fm4(空表单)",
                              urlJoin(loginUrl, fm4->action.empty() ? action : fm4->action),
                              fm4->inputs});
    }
    if (fm3 != nullptr) {
        std::vector<std::pair<std::string, std::string>> payload = fm3->inputs;
        payload.emplace_back("_eventId", "checkCaptchaSubmit");
        strategies.push_back({"fm3(checkCaptchaSubmit)",
                              urlJoin(loginUrl, fm3->action.empty() ? action : fm3->action),
                              payload});
    }
    strategies.push_back({"直接重提登录地址", action, {}});

    for (const Strategy& strategy : strategies) {
        logInfo("验证后提交方式: %s", strategy.name.c_str());
        const HttpResponse response = session.request(strategy.target, &strategy.payload, false);
        const bool still = needsCaptcha(response.body);
        logInfo("  -> HTTP %d, 页面 %zu 字节, 仍需验证=%s", response.status, response.body.size(),
                still ? "是" : "否");
        if (!still) dumpPage(profile, "after-" + strategy.name, response.body);
        const double deadline = monotonicSeconds() + 20.0;
        while (monotonicSeconds() < deadline) {
            if (isOnline(session, cfg, true)) return true;
            sleepSeconds(2);
        }
    }
    return false;
}

}  // namespace

bool casLogin(Session& session, const Config& cfg, const Profile& profile,
              const std::string& account, const std::string& password) {
    const std::string loginUrl = cfg.casLoginUrl + "?service=" + urlEncode(cfg.service);
    logInfo("打开认证页 %s", loginUrl.c_str());
    const HttpResponse page = session.request(loginUrl);
    if (page.status != 200) {
        logError("认证页打不开: HTTP %d", page.status);
        return false;
    }

    const std::vector<Form> forms = parseForms(page.body);
    const Form* chosen = nullptr;
    for (const Form& form : forms) {
        if (form.hasInput("password") || form.hasInput("username")) {
            chosen = &form;
            break;
        }
    }
    if (chosen == nullptr) {
        logError("认证页里没有登录表单");
        dumpPage(profile, "no-form", page.body);
        return false;
    }

    const std::string action =
        urlJoin(loginUrl, chosen->action.empty() ? loginUrl : chosen->action);
    std::vector<std::pair<std::string, std::string>> data = chosen->inputs;

    const std::string cipher = rsaEncryptPassword(password);
    if (cipher.empty() && !password.empty()) {
        logError("密码含非 ASCII 字符，学校页面的 RSA 处理不了");
        return false;
    }
    auto upsert = [](std::vector<std::pair<std::string, std::string>>& list,
                     const std::string& key, const std::string& value) {
        for (auto& kv : list) {
            if (kv.first == key) {
                kv.second = value;
                return;
            }
        }
        list.emplace_back(key, value);
    };
    upsert(data, "username", account);
    upsert(data, "password", cipher);
    upsert(data, "encrypted", "true");
    upsert(data, "_eventId", "submit");
    if (!chosen->hasInput("loginType")) upsert(data, "loginType", "1");

    std::vector<std::string> keys;
    keys.reserve(data.size());
    for (const auto& kv : data) keys.push_back(kv.first);
    std::sort(keys.begin(), keys.end());
    logInfo("提交账号密码 (表单字段: %s)", joinWith(keys, ", ").c_str());

    const HttpResponse response = session.request(action, &data, false);
    if (response.status != 200) {
        logError("提交账号密码失败: HTTP %d", response.status);
        return false;
    }

    if (needsCaptcha(response.body)) {
        dumpPage(profile, "captcha-page", response.body);
        if (!passCaptcha(session, cfg, profile, loginUrl, action, account, response.body)) {
            logError("安全验证环节失败");
            return false;
        }
    } else {
        logInfo("服务器未要求安全验证，直接检查结果");
    }

    const double deadline = monotonicSeconds() + static_cast<double>(cfg.loginTimeout);
    while (monotonicSeconds() < deadline) {
        if (isOnline(session, cfg, true)) {
            logInfo("登录成功，网络已恢复");
            return true;
        }
        sleepSeconds(3);
    }
    logError("登录后 %d 秒仍未恢复网络", cfg.loginTimeout);
    return false;
}

}  // namespace cnc
