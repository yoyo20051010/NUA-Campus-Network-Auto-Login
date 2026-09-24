#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace cnc {

Json Json::object() {
    Json v;
    v.type = Type::Object;
    return v;
}

Json Json::array() {
    Json v;
    v.type = Type::Array;
    return v;
}

Json Json::of(const std::string& value) {
    Json v;
    v.type = Type::String;
    v.text = value;
    return v;
}

Json Json::of(const char* value) { return of(std::string(value ? value : "")); }

Json Json::of(bool value) {
    Json v;
    v.type = Type::Bool;
    v.boolean = value;
    return v;
}

Json Json::of(int64_t value) {
    Json v;
    v.type = Type::Number;
    v.number = static_cast<double>(value);
    return v;
}

Json Json::of(double value) {
    Json v;
    v.type = Type::Number;
    v.number = value;
    return v;
}

const Json* Json::find(const std::string& key) const {
    if (type != Type::Object) return nullptr;
    for (const auto& kv : fields) {
        if (kv.first == key) return &kv.second;
    }
    return nullptr;
}

Json& Json::operator[](const std::string& key) {
    if (type != Type::Object) {
        type = Type::Object;
        boolean = false;
        number = 0;
        text.clear();
        items.clear();
        fields.clear();
    }
    for (auto& kv : fields) {
        if (kv.first == key) return kv.second;
    }
    fields.emplace_back(key, Json());
    return fields.back().second;
}

void Json::set(const std::string& key, Json value) { (*this)[key] = std::move(value); }

std::string Json::asString(const std::string& fallback) const {
    switch (type) {
        case Type::String:
            return text;
        case Type::Number: {
            // config.json 里有人把端口写成数字，这里也接受
            char buf[64];
            if (number == std::floor(number) && std::fabs(number) < 1e15) {
                std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(number));
            } else {
                std::snprintf(buf, sizeof(buf), "%g", number);
            }
            return std::string(buf);
        }
        case Type::Bool:
            return boolean ? "true" : "false";
        default:
            return fallback;
    }
}

int64_t Json::asInt(int64_t fallback) const {
    if (type == Type::Number) return static_cast<int64_t>(number);
    if (type == Type::Bool) return boolean ? 1 : 0;
    if (type == Type::String && !text.empty()) {
        char* end = nullptr;
        long long parsed = std::strtoll(text.c_str(), &end, 10);
        if (end && *end == '\0') return parsed;
    }
    return fallback;
}

double Json::asNumber(double fallback) const {
    if (type == Type::Number) return number;
    if (type == Type::Bool) return boolean ? 1.0 : 0.0;
    if (type == Type::String && !text.empty()) {
        char* end = nullptr;
        double parsed = std::strtod(text.c_str(), &end);
        if (end && *end == '\0') return parsed;
    }
    return fallback;
}

bool Json::asBool(bool fallback) const {
    if (type == Type::Bool) return boolean;
    if (type == Type::Number) return number != 0;
    if (type == Type::String) {
        if (text == "true" || text == "1" || text == "yes") return true;
        if (text == "false" || text == "0" || text == "no") return false;
    }
    return fallback;
}

const Json* Json::at(size_t index) const {
    if (type != Type::Array || index >= items.size()) return nullptr;
    return &items[index];
}

namespace {

void appendUtf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
        out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

class Parser {
public:
    explicit Parser(const std::string& src) : src_(src) {}

    bool run(Json& out) {
        skipSpace();
        if (!parseValue(out)) return false;
        skipSpace();
        return pos_ == src_.size();
    }

private:
    const std::string& src_;
    size_t pos_ = 0;

    void skipSpace() {
        while (pos_ < src_.size()) {
            char c = src_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos_;
            } else {
                break;
            }
        }
    }

    bool literal(const char* word) {
        size_t len = std::strlen(word);
        if (src_.compare(pos_, len, word) != 0) return false;
        pos_ += len;
        return true;
    }

    bool parseValue(Json& out) {
        if (pos_ >= src_.size()) return false;
        char c = src_[pos_];
        if (c == '{') return parseObject(out);
        if (c == '[') return parseArray(out);
        if (c == '"') {
            out.type = Json::Type::String;
            return parseString(out.text);
        }
        if (c == 't') {
            if (!literal("true")) return false;
            out = Json::of(true);
            return true;
        }
        if (c == 'f') {
            if (!literal("false")) return false;
            out = Json::of(false);
            return true;
        }
        if (c == 'n') {
            if (!literal("null")) return false;
            out = Json();
            return true;
        }
        return parseNumber(out);
    }

    bool parseObject(Json& out) {
        out = Json::object();
        ++pos_;  // '{'
        skipSpace();
        if (pos_ < src_.size() && src_[pos_] == '}') {
            ++pos_;
            return true;
        }
        while (pos_ < src_.size()) {
            skipSpace();
            std::string key;
            if (pos_ >= src_.size() || src_[pos_] != '"') return false;
            if (!parseString(key)) return false;
            skipSpace();
            if (pos_ >= src_.size() || src_[pos_] != ':') return false;
            ++pos_;
            skipSpace();
            Json value;
            if (!parseValue(value)) return false;
            out.fields.emplace_back(std::move(key), std::move(value));
            skipSpace();
            if (pos_ < src_.size() && src_[pos_] == ',') {
                ++pos_;
                continue;
            }
            if (pos_ < src_.size() && src_[pos_] == '}') {
                ++pos_;
                return true;
            }
            return false;
        }
        return false;
    }

    bool parseArray(Json& out) {
        out = Json::array();
        ++pos_;  // '['
        skipSpace();
        if (pos_ < src_.size() && src_[pos_] == ']') {
            ++pos_;
            return true;
        }
        while (pos_ < src_.size()) {
            skipSpace();
            Json value;
            if (!parseValue(value)) return false;
            out.items.push_back(std::move(value));
            skipSpace();
            if (pos_ < src_.size() && src_[pos_] == ',') {
                ++pos_;
                continue;
            }
            if (pos_ < src_.size() && src_[pos_] == ']') {
                ++pos_;
                return true;
            }
            return false;
        }
        return false;
    }

    bool parseHex4(uint32_t& value) {
        if (pos_ + 4 > src_.size()) return false;
        value = 0;
        for (int i = 0; i < 4; ++i) {
            char c = src_[pos_ + static_cast<size_t>(i)];
            value <<= 4;
            if (c >= '0' && c <= '9') {
                value |= static_cast<uint32_t>(c - '0');
            } else if (c >= 'a' && c <= 'f') {
                value |= static_cast<uint32_t>(c - 'a' + 10);
            } else if (c >= 'A' && c <= 'F') {
                value |= static_cast<uint32_t>(c - 'A' + 10);
            } else {
                return false;
            }
        }
        pos_ += 4;
        return true;
    }

    bool parseString(std::string& out) {
        ++pos_;  // '"'
        out.clear();
        while (pos_ < src_.size()) {
            unsigned char c = static_cast<unsigned char>(src_[pos_]);
            if (c == '"') {
                ++pos_;
                return true;
            }
            if (c == '\\') {
                ++pos_;
                if (pos_ >= src_.size()) return false;
                char esc = src_[pos_++];
                switch (esc) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'u': {
                        uint32_t cp = 0;
                        if (!parseHex4(cp)) return false;
                        if (cp >= 0xD800 && cp <= 0xDBFF && pos_ + 1 < src_.size() &&
                            src_[pos_] == '\\' && src_[pos_ + 1] == 'u') {
                            size_t save = pos_;
                            pos_ += 2;
                            uint32_t low = 0;
                            if (parseHex4(low) && low >= 0xDC00 && low <= 0xDFFF) {
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                            } else {
                                pos_ = save;
                            }
                        }
                        appendUtf8(out, cp);
                        break;
                    }
                    default:
                        return false;
                }
                continue;
            }
            if (c < 0x20) return false;
            out.push_back(static_cast<char>(c));
            ++pos_;
        }
        return false;
    }

    bool parseNumber(Json& out) {
        size_t start = pos_;
        if (pos_ < src_.size() && (src_[pos_] == '-' || src_[pos_] == '+')) ++pos_;
        bool any = false;
        while (pos_ < src_.size() && src_[pos_] >= '0' && src_[pos_] <= '9') {
            ++pos_;
            any = true;
        }
        if (pos_ < src_.size() && src_[pos_] == '.') {
            ++pos_;
            while (pos_ < src_.size() && src_[pos_] >= '0' && src_[pos_] <= '9') {
                ++pos_;
                any = true;
            }
        }
        if (!any) return false;
        if (pos_ < src_.size() && (src_[pos_] == 'e' || src_[pos_] == 'E')) {
            ++pos_;
            if (pos_ < src_.size() && (src_[pos_] == '-' || src_[pos_] == '+')) ++pos_;
            while (pos_ < src_.size() && src_[pos_] >= '0' && src_[pos_] <= '9') ++pos_;
        }
        out = Json::of(std::strtod(src_.substr(start, pos_ - start).c_str(), nullptr));
        return true;
    }
};

void dumpString(const std::string& value, std::string& out) {
    out.push_back('"');
    for (unsigned char c : value) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
}

void dumpNumber(double value, std::string& out) {
    char buf[64];
    if (value == std::floor(value) && std::fabs(value) < 9.0e15) {
        std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(value));
    } else {
        std::snprintf(buf, sizeof(buf), "%.17g", value);
    }
    out += buf;
}

void dumpValue(const Json& value, std::string& out) {
    switch (value.type) {
        case Json::Type::Null:
            out += "null";
            break;
        case Json::Type::Bool:
            out += value.boolean ? "true" : "false";
            break;
        case Json::Type::Number:
            dumpNumber(value.number, out);
            break;
        case Json::Type::String:
            dumpString(value.text, out);
            break;
        case Json::Type::Array: {
            out.push_back('[');
            for (size_t i = 0; i < value.items.size(); ++i) {
                if (i) out.push_back(',');
                dumpValue(value.items[i], out);
            }
            out.push_back(']');
            break;
        }
        case Json::Type::Object: {
            out.push_back('{');
            for (size_t i = 0; i < value.fields.size(); ++i) {
                if (i) out.push_back(',');
                dumpString(value.fields[i].first, out);
                out.push_back(':');
                dumpValue(value.fields[i].second, out);
            }
            out.push_back('}');
            break;
        }
    }
}

}  // namespace

std::string Json::dump() const {
    std::string out;
    dumpValue(*this, out);
    return out;
}

Json Json::parse(const std::string& source, bool* ok) {
    Json out;
    Parser parser(source);
    bool success = parser.run(out);
    if (ok) *ok = success;
    return success ? out : Json();
}

}  // namespace cnc
