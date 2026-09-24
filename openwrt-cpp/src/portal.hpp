// 校园网门户的网络流程：判断在线状态、看门户走哪套登录、以及两套登录流程本身。
// 纯文本解析在 portal_parse.hpp / .cpp 里，那部分不依赖平台，能单独跑测试。
#pragma once

#include <string>

#include "config.hpp"
#include "http.hpp"
#include "portal_parse.hpp"

namespace cnc {

// 这次探测是不是"网络真的通了"。
// 认证通过后拿到的是正常响应：可能 200，也可能是 3xx 跳转（百度就回 302 跳 HTTPS）。
bool looksOnline(const Session& session, int code, const std::string& text);

bool isOnline(Session& session, const Config& cfg, bool quiet = false);
bool portalOk(Session& session, const Config& cfg);

// 返回 "ok" / "offsite" / "failed" / "fatal"
std::string login(Session& session, const Config& cfg, const Profile& profile,
                  const std::string& account, const std::string& password);

std::string drcomLogin(Session& session, const Config& cfg, const Profile& profile,
                       const std::string& portalHtml, const std::string& account,
                       const std::string& password);

bool casLogin(Session& session, const Config& cfg, const Profile& profile,
              const std::string& account, const std::string& password);

}  // namespace cnc
