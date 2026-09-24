// 极简 JSON：只需要能读写 config.json / secret.json / retry_state.json 这点结构，
// 所以不引入任何第三方库，保持二进制体积和内存都可控。
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace cnc {

class Json {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    Type type = Type::Null;
    bool boolean = false;
    double number = 0;
    std::string text;
    std::vector<Json> items;                                  // Array
    std::vector<std::pair<std::string, Json>> fields;         // Object（保序）

    Json() = default;

    static Json object();
    static Json array();
    static Json of(const std::string& value);
    static Json of(const char* value);
    static Json of(bool value);
    static Json of(int64_t value);
    static Json of(double value);

    bool isNull() const { return type == Type::Null; }
    bool isObject() const { return type == Type::Object; }
    bool isArray() const { return type == Type::Array; }
    bool isString() const { return type == Type::String; }
    bool isNumber() const { return type == Type::Number; }
    bool isBool() const { return type == Type::Bool; }

    // 对象查找；找不到返回 nullptr。
    const Json* find(const std::string& key) const;

    // 对象取/建字段（不存在就插入空值）。
    Json& operator[](const std::string& key);
    void set(const std::string& key, Json value);

    // 宽容取值：类型不对就返回默认值，调用方不用到处判类型。
    std::string asString(const std::string& fallback = "") const;
    std::string asStringOr(const std::string& fallback) const { return asString(fallback); }
    int64_t asInt(int64_t fallback = 0) const;
    double asNumber(double fallback = 0) const;
    bool asBool(bool fallback = false) const;

    // 数组里取第 i 个元素；越界返回 nullptr。
    const Json* at(size_t index) const;

    std::string dump() const;

    // 解析失败时返回 Null 值，并把 ok 置 false（如果传了）。
    static Json parse(const std::string& source, bool* ok = nullptr);
};

}  // namespace cnc
