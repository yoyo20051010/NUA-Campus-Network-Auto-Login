// 门户页面的纯文本解析。这里刻意不引用任何网络/文件代码，
// 这样本机（Windows 的 g++）也能直接编译并跑单元测试。
#include "portal_parse.hpp"

#include <cstdlib>

#include "strings.hpp"

namespace cnc {

const char* const kPortalMarkers[] = {"Dr.COMWebLogin", "DrcomServer", "eportal", "10.255.255.2"};
const size_t kPortalMarkerCount = sizeof(kPortalMarkers) / sizeof(kPortalMarkers[0]);

bool containsPortalMarker(const std::string& text) {
    for (size_t i = 0; i < kPortalMarkerCount; ++i) {
        if (contains(text, kPortalMarkers[i])) return true;
    }
    return false;
}

namespace {

bool isSpace(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\v';
}

bool isNameChar(char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
           c == '_' || c == '-' || c == ':' || c == '.';
}

// 解析一段标签属性文本：name、name="value"、name='value'、name=value
std::vector<std::pair<std::string, std::string>> parseAttributes(const std::string& tag) {
    std::vector<std::pair<std::string, std::string>> out;
    size_t pos = 0;
    while (pos < tag.size()) {
        while (pos < tag.size() && !isNameChar(tag[pos])) ++pos;
        const size_t nameStart = pos;
        while (pos < tag.size() && isNameChar(tag[pos])) ++pos;
        if (pos == nameStart) break;
        std::string name = toLower(tag.substr(nameStart, pos - nameStart));

        while (pos < tag.size() && isSpace(tag[pos])) ++pos;
        if (pos >= tag.size() || tag[pos] != '=') continue;
        ++pos;
        while (pos < tag.size() && isSpace(tag[pos])) ++pos;
        if (pos >= tag.size()) break;

        std::string value;
        if (tag[pos] == '"' || tag[pos] == '\'') {
            const char quote = tag[pos++];
            while (pos < tag.size() && tag[pos] != quote) value.push_back(tag[pos++]);
            if (pos < tag.size()) ++pos;
        } else {
            while (pos < tag.size() && !isSpace(tag[pos]) && tag[pos] != '>') {
                value.push_back(tag[pos++]);
            }
        }
        out.emplace_back(std::move(name), std::move(value));
    }
    return out;
}

std::string attr(const std::vector<std::pair<std::string, std::string>>& attrs,
                 const std::string& name, const std::string& fallback = "") {
    for (const auto& kv : attrs) {
        if (kv.first == name) return kv.second;
    }
    return fallback;
}

// 找 name<空格>=<空格>引号 里的值；找不到返回 false。
bool findQuotedAssignment(const std::string& text, const std::string& name, char quote,
                          std::string& out) {
    size_t pos = 0;
    while ((pos = text.find(name, pos)) != std::string::npos) {
        size_t cursor = pos + name.size();
        while (cursor < text.size() && isSpace(text[cursor])) ++cursor;
        if (cursor < text.size() && text[cursor] == '=') {
            ++cursor;
            while (cursor < text.size() && isSpace(text[cursor])) ++cursor;
            if (cursor < text.size() && text[cursor] == quote) {
                ++cursor;
                const size_t end = text.find(quote, cursor);
                if (end == std::string::npos) return false;
                out = text.substr(cursor, end - cursor);
                return true;
            }
        }
        pos += name.size();
    }
    return false;
}

// 同上，但取的是纯数字（例如 authloginport = 801）。
bool findNumberAssignment(const std::string& text, const std::string& name, std::string& out) {
    size_t pos = 0;
    while ((pos = text.find(name, pos)) != std::string::npos) {
        size_t cursor = pos + name.size();
        while (cursor < text.size() && isSpace(text[cursor])) ++cursor;
        if (cursor < text.size() && text[cursor] == '=') {
            ++cursor;
            while (cursor < text.size() && isSpace(text[cursor])) ++cursor;
            const size_t begin = cursor;
            while (cursor < text.size() && text[cursor] >= '0' && text[cursor] <= '9') ++cursor;
            if (cursor > begin) {
                out = text.substr(begin, cursor - begin);
                return true;
            }
        }
        pos += name.size();
    }
    return false;
}

bool looksLikeIpv4(const std::string& text) {
    int dots = 0;
    int digits = 0;
    for (char c : text) {
        if (c == '.') {
            ++dots;
        } else if (c >= '0' && c <= '9') {
            ++digits;
        } else {
            return false;
        }
    }
    return dots == 3 && digits > 0;
}

bool ipToInt(const std::string& ip, int& value) {
    const std::vector<std::string> parts = split(ip, '.');
    if (parts.size() != 4) return false;
    int built = 0;
    for (const std::string& part : parts) {
        if (part.empty()) return false;
        const int octet = std::atoi(part.c_str());
        if (octet < 0 || octet > 255) return false;
        built = (built << 8) | octet;
    }
    value = built;
    return true;
}

}  // namespace

std::string clientIpFromPortal(const std::string& html) {
    for (const char* name : {"v46ip", "ss5", "v4serip"}) {
        std::string value;
        bool found = findQuotedAssignment(html, name, '\'', value);
        if (!found) found = findQuotedAssignment(html, name, '"', value);
        if (found && looksLikeIpv4(trim(value))) return trim(value);
    }
    return "";
}

bool needsCas(const std::string& clientIp) {
    if (clientIp.empty()) return true;   // 判断不了就按有线那套试，有后备
    int value = 0;
    if (!ipToInt(clientIp, value)) return true;
    struct Range {
        const char* low;
        const char* high;
    };
    static const Range ranges[] = {{"1.1.1.1", "10.51.255.255"}, {"10.128.0.1", "10.129.255.255"}};
    for (const Range& range : ranges) {
        int low = 0;
        int high = 0;
        if (ipToInt(range.low, low) && ipToInt(range.high, high) && value >= low && value <= high) {
            return true;
        }
    }
    return false;
}

PortalConfig parsePortalConfig(const std::string& html) {
    PortalConfig out;
    struct Entry {
        const char* key;
        std::string* target;
        char quote;   // 0 = 取数字
    };
    const Entry entries[] = {
        {"authloginpath", &out.authLoginPath, '\''},
        {"authloginport", &out.authLoginPort, 0},
        {"authuserfield", &out.authUserField, '\''},
        {"authpassfield", &out.authPassField, '\''},
        {"authloginparam", &out.authLoginParam, '\''},
        {"authsuccess", &out.authSuccess, '\''},
        {"authfail", &out.authFail, '\''},
        {"fileVersion", &out.jsVersion, '"'},
        {"v4serip", &out.v4SerIp, '\''},
    };
    for (const Entry& entry : entries) {
        std::string value;
        const bool found = entry.quote == 0 ? findNumberAssignment(html, entry.key, value)
                                            : findQuotedAssignment(html, entry.key, entry.quote, value);
        if (found) *entry.target = value;
    }
    return out;
}

bool Form::hasInput(const std::string& name) const {
    for (const auto& kv : inputs) {
        if (kv.first == name) return true;
    }
    return false;
}

std::string findContextPath(const std::string& page) {
    std::string value;
    if (findQuotedAssignment(page, "contextPath", '"', value)) return value;
    if (findQuotedAssignment(page, "contextPath", '\'', value)) return value;
    return "";
}

size_t findNoCase(const std::string& haystack, const std::string& needle, size_t from) {
    if (needle.empty()) return from;
    if (needle.size() > haystack.size()) return std::string::npos;
    return toLower(haystack).find(toLower(needle), from);
}

std::vector<Form> parseForms(const std::string& html) {
    std::vector<Form> forms;
    size_t pos = 0;
    while (true) {
        const size_t formStart = findNoCase(html, "<form", pos);
        if (formStart == std::string::npos) break;
        const size_t tagEnd = html.find('>', formStart);
        if (tagEnd == std::string::npos) break;
        const size_t formEnd = findNoCase(html, "</form>", tagEnd);
        const size_t bodyEnd = formEnd == std::string::npos ? html.size() : formEnd;

        Form form;
        const auto attrs = parseAttributes(html.substr(formStart + 5, tagEnd - formStart - 5));
        form.id = attr(attrs, "id");
        form.action = attr(attrs, "action");

        size_t cursor = tagEnd;
        while (true) {
            const size_t inputStart = findNoCase(html, "<input", cursor);
            if (inputStart == std::string::npos || inputStart >= bodyEnd) break;
            const size_t inputEnd = html.find('>', inputStart);
            if (inputEnd == std::string::npos || inputEnd > bodyEnd) break;
            const auto inputAttrs =
                parseAttributes(html.substr(inputStart + 6, inputEnd - inputStart - 6));
            const std::string name = attr(inputAttrs, "name");
            const std::string type = toLower(attr(inputAttrs, "type", "text"));
            if (!name.empty() && type != "submit" && type != "button" && type != "image") {
                form.inputs.emplace_back(name, attr(inputAttrs, "value"));
            }
            cursor = inputEnd + 1;
        }
        forms.push_back(std::move(form));
        pos = bodyEnd;
    }
    return forms;
}

bool needsCaptcha(const std::string& page) {
    // 服务器返回"请完成安全验证"页时，slider 面板会去掉 none 类
    if (!contains(page, "请完成安全验证")) return false;
    return !contains(page, "class=\"slidingverification none\"");
}

}  // namespace cnc
