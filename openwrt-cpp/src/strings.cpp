#include "strings.hpp"

#include <cctype>
#include <cstdarg>
#include <cstdio>

namespace cnc {

std::string trim(const std::string& text) {
    size_t begin = 0;
    size_t end = text.size();
    while (begin < end && std::isspace(static_cast<unsigned char>(text[begin]))) ++begin;
    while (end > begin && std::isspace(static_cast<unsigned char>(text[end - 1]))) --end;
    return text.substr(begin, end - begin);
}

std::string toLower(const std::string& text) {
    std::string out = text;
    for (char& c : out) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return out;
}

bool startsWith(const std::string& text, const std::string& prefix) {
    return text.size() >= prefix.size() && text.compare(0, prefix.size(), prefix) == 0;
}

bool endsWith(const std::string& text, const std::string& suffix) {
    return text.size() >= suffix.size() &&
           text.compare(text.size() - suffix.size(), suffix.size(), suffix) == 0;
}

bool contains(const std::string& text, const std::string& needle) {
    if (needle.empty()) return true;
    return text.find(needle) != std::string::npos;
}

std::vector<std::string> split(const std::string& text, char separator) {
    std::vector<std::string> out;
    std::string current;
    for (char c : text) {
        if (c == separator) {
            out.push_back(current);
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    out.push_back(current);
    return out;
}

std::string replaceAll(const std::string& text, const std::string& from, const std::string& to) {
    if (from.empty()) return text;
    std::string out;
    size_t pos = 0;
    while (true) {
        const size_t hit = text.find(from, pos);
        if (hit == std::string::npos) {
            out.append(text, pos, std::string::npos);
            break;
        }
        out.append(text, pos, hit - pos);
        out += to;
        pos = hit + from.size();
    }
    return out;
}

std::string joinWith(const std::vector<std::string>& parts, const std::string& separator) {
    std::string out;
    for (size_t i = 0; i < parts.size(); ++i) {
        if (i) out += separator;
        out += parts[i];
    }
    return out;
}

std::string format(const char* fmt, ...) {
    char stack[1024];
    va_list args;
    va_start(args, fmt);
    va_list copy;
    va_copy(copy, args);
    const int needed = std::vsnprintf(stack, sizeof(stack), fmt, copy);
    va_end(copy);
    std::string out;
    if (needed < 0) {
        va_end(args);
        return out;
    }
    if (static_cast<size_t>(needed) < sizeof(stack)) {
        out.assign(stack, static_cast<size_t>(needed));
    } else {
        out.resize(static_cast<size_t>(needed) + 1);
        std::vsnprintf(&out[0], out.size(), fmt, args);
        out.resize(static_cast<size_t>(needed));
    }
    va_end(args);
    return out;
}

}  // namespace cnc
