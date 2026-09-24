// 门户页面的纯文本解析：不碰网络、不碰文件，方便在本机跑自动化测试。
#pragma once

#include <string>
#include <utility>
#include <vector>

namespace cnc {

// 未认证时校园网会把请求劫持到门户；页面或跳转地址里出现这些标记就说明没通。
extern const char* const kPortalMarkers[];
extern const size_t kPortalMarkerCount;

bool containsPortalMarker(const std::string& text);

// 门户页面里会写上它看到的客户端地址（v46ip / ss5 / v4serip）。
std::string clientIpFromPortal(const std::string& html);

// 有线网段（10.12.x 之类）走统一身份认证，无线网段（10.54.x 之类）走 Dr.COM。
bool needsCas(const std::string& clientIp);

// 门户页面里那段 js 配置（登录路径、端口、字段名、jsVersion 等）。
struct PortalConfig {
    std::string authLoginPath;
    std::string authLoginPort;
    std::string authUserField;
    std::string authPassField;
    std::string authLoginParam;
    std::string authSuccess;
    std::string authFail;
    std::string jsVersion;
    std::string v4SerIp;
};

PortalConfig parsePortalConfig(const std::string& html);

// 页面里的 var contextPath = "/cas"；验证结果必须上报到 contextPath 下面。
std::string findContextPath(const std::string& page);

struct Form {
    std::string id;
    std::string action;
    std::vector<std::pair<std::string, std::string>> inputs;

    bool hasInput(const std::string& name) const;
};

std::vector<Form> parseForms(const std::string& html);

// 服务器返回"请完成安全验证"页时，slider 面板会去掉 none 类。
bool needsCaptcha(const std::string& page);

}  // namespace cnc
