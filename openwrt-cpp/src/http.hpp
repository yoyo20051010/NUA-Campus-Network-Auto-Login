// 极简 HTTP/HTTPS 客户端，够校园网登录用就行。
//
// 为什么不用 libcurl、也不静态链 OpenSSL：
//   路由器是 aarch64 + musl，静态链 OpenSSL 会让二进制涨到好几 MB，
//   而我们真正需要的只是"把请求从指定网卡发出去"这一件事。
//   纯 HTTP 全部自己用 socket 实现；HTTPS（统一身份认证那几步）转发给路由器上
//   本来就有的 curl 命令，既不增加体积也不引入新的失败点。
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace cnc {

std::string urlEncode(const std::string& text);
std::string formEncode(const std::vector<std::pair<std::string, std::string>>& fields);

struct Url {
    std::string scheme;
    std::string host;
    std::string path;
    std::string query;
    int port = 0;
};

bool parseUrl(const std::string& text, Url& out);
std::string urlJoin(const std::string& base, const std::string& reference);

struct HttpResponse {
    int status = -1;         // HTTP 状态码；-1 表示这次请求根本没完成
    std::string body;
    std::string location;
    std::string error;

    bool reached() const { return status > 0; }
};

class Session {
public:
    // bind_ip：直接绑定源地址（调试时用）。
    // bind_device：绑定网卡（路由器上两条上行同时在线时用这个）。
    Session(int timeoutSeconds, std::string bindIp, std::string bindDevice, int routeTable);

    // form 为空 = 不带 body；否则按 application/x-www-form-urlencoded 提交。
    HttpResponse request(const std::string& url,
                         const std::vector<std::pair<std::string, std::string>>* form = nullptr,
                         bool ajax = false, const std::string& method = "");

    const std::string& lastLocation() const { return lastLocation_; }

private:
    std::string cookieHeader() const;
    void storeCookies(const std::vector<std::pair<std::string, std::string>>& headers);
    bool resolveSource(std::string& ip, std::string& device);

    HttpResponse requestPlain(const Url& url,
                              const std::vector<std::pair<std::string, std::string>>* form,
                              bool ajax, const std::string& method);
    HttpResponse requestViaCurl(const Url& url,
                                const std::vector<std::pair<std::string, std::string>>* form,
                                bool ajax, const std::string& method);

    int timeout_;
    std::string bindIp_;
    std::string bindDevice_;
    int routeTable_;
    std::string lastLocation_;
    std::string cachedIp_;
    double cachedAt_ = 0;
    std::vector<std::pair<std::string, std::string>> cookies_;
};

}  // namespace cnc
