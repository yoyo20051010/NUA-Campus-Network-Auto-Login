#include "http.hpp"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include "util.hpp"

namespace cnc {
namespace {

const char* const kUserAgent =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

bool isUnreserved(unsigned char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
           c == '-' || c == '_' || c == '.' || c == '~';
}

std::string headerValue(const std::vector<std::pair<std::string, std::string>>& headers,
                        const std::string& name) {
    const std::string wanted = toLower(name);
    for (const auto& kv : headers) {
        if (toLower(kv.first) == wanted) return kv.second;
    }
    return "";
}

// 把 HTTP 响应头之后的内容解出来：支持 Content-Length / chunked / 读到断连。
std::string extractBody(const std::vector<std::pair<std::string, std::string>>& headers,
                        const std::string& raw) {
    const std::string encoding = toLower(headerValue(headers, "Transfer-Encoding"));
    if (contains(encoding, "chunked")) {
        std::string out;
        size_t pos = 0;
        while (pos < raw.size()) {
            size_t lineEnd = raw.find("\r\n", pos);
            if (lineEnd == std::string::npos) break;
            const std::string sizeLine = trim(raw.substr(pos, lineEnd - pos));
            const size_t semicolon = sizeLine.find(';');
            const std::string sizeText =
                semicolon == std::string::npos ? sizeLine : sizeLine.substr(0, semicolon);
            const long length = std::strtol(sizeText.c_str(), nullptr, 16);
            pos = lineEnd + 2;
            if (length <= 0) break;
            if (pos + static_cast<size_t>(length) > raw.size()) {
                out.append(raw, pos, std::string::npos);
                break;
            }
            out.append(raw, pos, static_cast<size_t>(length));
            pos += static_cast<size_t>(length);
            if (raw.compare(pos, 2, "\r\n") == 0) pos += 2;
        }
        return out;
    }

    const std::string lengthText = headerValue(headers, "Content-Length");
    if (!lengthText.empty()) {
        const long length = std::strtol(lengthText.c_str(), nullptr, 10);
        if (length >= 0 && static_cast<size_t>(length) <= raw.size()) {
            return raw.substr(0, static_cast<size_t>(length));
        }
    }
    return raw;
}

// 把 "HTTP/1.1 200 OK\r\n...\r\n\r\nbody" 拆成状态码、响应头、正文。
void parseResponse(const std::string& text, HttpResponse& out) {
    const size_t headEnd = text.find("\r\n\r\n");
    if (headEnd == std::string::npos) {
        out.error = "响应头不完整";
        return;
    }
    const std::string head = text.substr(0, headEnd);
    const std::string body = text.substr(headEnd + 4);

    std::vector<std::string> headerLines;
    std::vector<std::pair<std::string, std::string>> headers;
    bool first = true;
    for (const std::string& line : split(head, '\n')) {
        const std::string clean = trim(line);
        if (clean.empty()) continue;
        if (first) {
            first = false;
            const std::vector<std::string> parts = split(clean, ' ');
            if (parts.size() < 2) {
                out.error = "状态行无法识别";
                return;
            }
            out.status = std::atoi(parts[1].c_str());
            continue;
        }
        const size_t colon = clean.find(':');
        if (colon == std::string::npos) continue;
        headers.emplace_back(trim(clean.substr(0, colon)), trim(clean.substr(colon + 1)));
    }
    out.location = headerValue(headers, "Location");
    out.body = extractBody(headers, body);
}

int connectWithBinding(const std::string& host, int port, const std::string& sourceIp,
                       const std::string& device, int timeoutSeconds) {
    struct addrinfo hints {};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo* list = nullptr;
    if (::getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &list) != 0) {
        return -1;
    }

    int fd = -1;
    for (struct addrinfo* it = list; it != nullptr; it = it->ai_next) {
        fd = ::socket(it->ai_family, it->ai_socktype, it->ai_protocol);
        if (fd < 0) continue;

        struct timeval tv {};
        tv.tv_sec = timeoutSeconds;
        ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        int one = 1;
        ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

        if (!sourceIp.empty()) {
            struct sockaddr_in source {};
            source.sin_family = AF_INET;
            source.sin_port = 0;
            ::inet_pton(AF_INET, sourceIp.c_str(), &source.sin_addr);
            ::bind(fd, reinterpret_cast<struct sockaddr*>(&source), sizeof(source));
        } else if (!device.empty()) {
#ifdef SO_BINDTODEVICE
            // 拿不到源地址时的兜底；实测绑无线网卡可能直接 Host is unreachable
            ::setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, device.c_str(),
                         static_cast<socklen_t>(device.size()));
#endif
        }

        if (::connect(fd, it->ai_addr, it->ai_addrlen) == 0) break;
        ::close(fd);
        fd = -1;
    }
    ::freeaddrinfo(list);
    return fd;
}

std::string readAll(int fd, int timeoutSeconds) {
    std::string out;
    char buffer[4096];
    const double deadline = monotonicSeconds() + static_cast<double>(timeoutSeconds) + 1.0;
    while (monotonicSeconds() < deadline) {
        const ssize_t got = ::recv(fd, buffer, sizeof(buffer), 0);
        if (got > 0) {
            out.append(buffer, static_cast<size_t>(got));
            continue;
        }
        if (got == 0) break;
        if (errno == EINTR) continue;
        break;  // 超时或出错：把已经收到的部分交给上层判断
    }
    return out;
}

}  // namespace

std::string urlEncode(const std::string& text) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    out.reserve(text.size());
    for (unsigned char c : text) {
        if (isUnreserved(c)) {
            out.push_back(static_cast<char>(c));
        } else {
            out.push_back('%');
            out.push_back(hex[c >> 4]);
            out.push_back(hex[c & 0x0F]);
        }
    }
    return out;
}

std::string formEncode(const std::vector<std::pair<std::string, std::string>>& fields) {
    std::string out;
    for (const auto& kv : fields) {
        if (!out.empty()) out.push_back('&');
        out += urlEncode(kv.first);
        out.push_back('=');
        out += urlEncode(kv.second);
    }
    return out;
}

bool parseUrl(const std::string& text, Url& out) {
    const size_t schemeEnd = text.find("://");
    if (schemeEnd == std::string::npos) return false;
    out.scheme = toLower(text.substr(0, schemeEnd));
    size_t pos = schemeEnd + 3;
    const size_t pathStart = text.find('/', pos);
    const std::string authority =
        pathStart == std::string::npos ? text.substr(pos) : text.substr(pos, pathStart - pos);
    const std::string rest = pathStart == std::string::npos ? "/" : text.substr(pathStart);

    const size_t colon = authority.rfind(':');
    if (colon != std::string::npos) {
        out.host = authority.substr(0, colon);
        out.port = std::atoi(authority.c_str() + colon + 1);
    } else {
        out.host = authority;
    }
    if (out.port == 0) out.port = (out.scheme == "https") ? 443 : 80;

    const size_t question = rest.find('?');
    if (question == std::string::npos) {
        out.path = rest;
    } else {
        out.path = rest.substr(0, question);
        out.query = rest.substr(question + 1);
    }
    if (out.path.empty()) out.path = "/";
    return !out.host.empty();
}

std::string urlJoin(const std::string& base, const std::string& reference) {
    if (reference.empty()) return base;
    if (reference.find("://") != std::string::npos) return reference;
    if (reference[0] == '/') {
        const size_t schemeEnd = base.find("://");
        if (schemeEnd == std::string::npos) return reference;
        const size_t hostEnd = base.find('/', schemeEnd + 3);
        return (hostEnd == std::string::npos ? base : base.substr(0, hostEnd)) + reference;
    }
    const size_t slash = base.rfind('/');
    if (slash == std::string::npos) return base + "/" + reference;
    return base.substr(0, slash + 1) + reference;
}

Session::Session(int timeoutSeconds, std::string bindIp, std::string bindDevice, int routeTable)
    : timeout_(timeoutSeconds),
      bindIp_(std::move(bindIp)),
      bindDevice_(std::move(bindDevice)),
      routeTable_(routeTable > 0 ? routeTable : routeTableFor(bindDevice)) {}

std::string Session::cookieHeader() const {
    std::string out;
    for (const auto& kv : cookies_) {
        if (!out.empty()) out += "; ";
        out += kv.first + "=" + kv.second;
    }
    return out;
}

void Session::storeCookies(const std::vector<std::pair<std::string, std::string>>& headers) {
    for (const auto& kv : headers) {
        if (toLower(kv.first) != "set-cookie") continue;
        const size_t equals = kv.second.find('=');
        if (equals == std::string::npos) continue;
        const std::string name = trim(kv.second.substr(0, equals));
        std::string value = kv.second.substr(equals + 1);
        const size_t semicolon = value.find(';');
        if (semicolon != std::string::npos) value = value.substr(0, semicolon);
        if (name.empty()) continue;
        bool replaced = false;
        for (auto& stored : cookies_) {
            if (stored.first == name) {
                stored.second = value;
                replaced = true;
                break;
            }
        }
        if (!replaced) cookies_.emplace_back(name, value);
    }
}

bool Session::resolveSource(std::string& ip, std::string& device) {
    ip.clear();
    device.clear();
    if (!bindIp_.empty()) {
        ip = bindIp_;
        return true;
    }
    if (bindDevice_.empty()) return true;  // 按系统路由走

    // DHCP 续约后地址可能变，所以缓存 60 秒就重新解析
    if (!cachedIp_.empty() && monotonicSeconds() - cachedAt_ < 60) {
        ip = cachedIp_;
        return true;
    }
    cachedIp_ = deviceIpv4(bindDevice_);
    cachedAt_ = monotonicSeconds();
    if (!cachedIp_.empty()) {
        // 光绑源地址不够：Linux 按目的地址选路，还得给这个源地址单独挂一张路由表
        ensureSourceRoute(bindDevice_, cachedIp_, routeTable_);
        ip = cachedIp_;
        return true;
    }
    device = bindDevice_;
    return true;
}

HttpResponse Session::request(const std::string& url,
                              const std::vector<std::pair<std::string, std::string>>* form,
                              bool ajax, const std::string& method) {
    Url parsed;
    if (!parseUrl(url, parsed)) {
        HttpResponse bad;
        bad.error = "URL 无法解析: " + url;
        return bad;
    }
    if (parsed.scheme == "https") {
        return requestViaCurl(parsed, form, ajax, method);
    }
    return requestPlain(parsed, form, ajax, method);
}

HttpResponse Session::requestPlain(const Url& url,
                                   const std::vector<std::pair<std::string, std::string>>* form,
                                   bool ajax, const std::string& method) {
    HttpResponse out;
    std::string sourceIp;
    std::string device;
    resolveSource(sourceIp, device);

    const int fd = connectWithBinding(url.host, url.port, sourceIp, device, timeout_);
    if (fd < 0) {
        out.error = "连接 " + url.host + ":" + std::to_string(url.port) + " 失败";
        return out;
    }

    std::string target = url.path;
    if (!url.query.empty()) target += "?" + url.query;

    std::string body;
    if (form != nullptr) body = formEncode(*form);

    const std::string verb = !method.empty() ? method : (form != nullptr ? "POST" : "GET");
    std::string head = verb + " " + target + " HTTP/1.1\r\n";
    head += "Host: " + url.host + "\r\n";
    head += std::string("User-Agent: ") + kUserAgent + "\r\n";
    head += "Accept: */*\r\n";
    head += "Accept-Language: zh-CN,zh;q=0.9\r\n";
    head += "Connection: close\r\n";
    if (ajax) head += "X-Requested-With: XMLHttpRequest\r\n";
    if (form != nullptr) {
        head += "Content-Type: application/x-www-form-urlencoded\r\n";
        head += "Content-Length: " + std::to_string(body.size()) + "\r\n";
    }
    const std::string cookie = cookieHeader();
    if (!cookie.empty()) head += "Cookie: " + cookie + "\r\n";
    head += "\r\n";
    head += body;

    size_t sent = 0;
    while (sent < head.size()) {
        const ssize_t chunk = ::send(fd, head.data() + sent, head.size() - sent, 0);
        if (chunk <= 0) {
            ::close(fd);
            out.error = "发送请求失败";
            return out;
        }
        sent += static_cast<size_t>(chunk);
    }

    const std::string raw = readAll(fd, timeout_);
    ::close(fd);
    if (raw.empty()) {
        out.error = "没有收到响应";
        return out;
    }

    // 先把响应头拆出来，好更新 Cookie；正文解析再做一遍也不贵
    const size_t headEnd = raw.find("\r\n\r\n");
    if (headEnd != std::string::npos) {
        std::vector<std::pair<std::string, std::string>> headers;
        bool first = true;
        for (const std::string& line : split(raw.substr(0, headEnd), '\n')) {
            const std::string clean = trim(line);
            if (clean.empty()) continue;
            if (first) {
                first = false;
                continue;
            }
            const size_t colon = clean.find(':');
            if (colon == std::string::npos) continue;
            headers.emplace_back(trim(clean.substr(0, colon)), trim(clean.substr(colon + 1)));
        }
        storeCookies(headers);
    }

    parseResponse(raw, out);
    lastLocation_ = out.location;
    return out;
}

HttpResponse Session::requestViaCurl(const Url& url,
                                     const std::vector<std::pair<std::string, std::string>>* form,
                                     bool ajax, const std::string& method) {
    // 统一身份认证是 HTTPS。路由器上本来就有带 OpenSSL 的 curl，
    // 直接让它去跑：不用静态链 TLS 库，二进制能小一个数量级。
    HttpResponse out;
    std::string sourceIp;
    std::string device;
    resolveSource(sourceIp, device);

    std::vector<std::string> argv = {"curl", "-s", "-i", "--max-time",
                                     std::to_string(timeout_)};
    if (!sourceIp.empty()) {
        argv.push_back("--interface");
        argv.push_back(sourceIp);
    } else if (!device.empty()) {
        argv.push_back("--interface");
        argv.push_back(device);
    }
    argv.push_back("-A");
    argv.push_back(kUserAgent);
    const std::string cookie = cookieHeader();
    if (!cookie.empty()) {
        argv.push_back("-b");
        argv.push_back(cookie);
    }
    if (ajax) {
        argv.push_back("-H");
        argv.push_back("X-Requested-With: XMLHttpRequest");
    }
    if (form != nullptr) {
        argv.push_back("--data-raw");
        argv.push_back(formEncode(*form));
    }
    if (!method.empty()) {
        argv.push_back("-X");
        argv.push_back(method);
    }
    argv.push_back(url.scheme + "://" + url.host + ":" + std::to_string(url.port) + url.path +
                    (url.query.empty() ? "" : "?" + url.query));

    const CommandResult result = runCommand(argv, timeout_ + 10);
    if (result.status != 0 && result.output.empty()) {
        out.error = "curl 执行失败（返回码 " + std::to_string(result.status) + "）";
        return out;
    }

    // curl -i 的输出可能有好几段（例如 100 Continue 后跟正式响应），取最后一段
    size_t begin = result.output.find("HTTP/");
    size_t lastBegin = begin;
    while (begin != std::string::npos) {
        lastBegin = begin;
        begin = result.output.find("HTTP/", begin + 5);
    }
    if (lastBegin == std::string::npos) {
        out.error = "curl 没有返回 HTTP 响应";
        return out;
    }
    const std::string text = result.output.substr(lastBegin);

    const size_t headEnd = text.find("\r\n\r\n");
    if (headEnd != std::string::npos) {
        std::vector<std::pair<std::string, std::string>> headers;
        bool first = true;
        for (const std::string& line : split(text.substr(0, headEnd), '\n')) {
            const std::string clean = trim(line);
            if (clean.empty()) continue;
            if (first) {
                first = false;
                continue;
            }
            const size_t colon = clean.find(':');
            if (colon == std::string::npos) continue;
            headers.emplace_back(trim(clean.substr(0, colon)), trim(clean.substr(colon + 1)));
        }
        storeCookies(headers);
    }
    parseResponse(text, out);
    lastLocation_ = out.location;
    return out;
}

}  // namespace cnc
