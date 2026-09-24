// 纯字符串工具（不依赖平台），单独一份是为了能在本机跑单元测试。
#pragma once

#include <string>
#include <vector>

namespace cnc {

std::string trim(const std::string& text);
std::string toLower(const std::string& text);
bool startsWith(const std::string& text, const std::string& prefix);
bool endsWith(const std::string& text, const std::string& suffix);
bool contains(const std::string& text, const std::string& needle);
std::vector<std::string> split(const std::string& text, char separator);
std::string replaceAll(const std::string& text, const std::string& from, const std::string& to);
std::string joinWith(const std::vector<std::string>& parts, const std::string& separator);
std::string format(const char* fmt, ...);

}  // namespace cnc
