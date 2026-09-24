// 看门狗：常驻循环、mwan3 联动、断网前提前切换、内存自保。
#pragma once

#include <string>

#include "config.hpp"

namespace cnc {

// 常驻看门狗。profileFilter 非空时只看护那一条线；bindIp 用于调试时强制出口。
int runWatch(const std::string& appDir, const std::string& profileFilter,
             const std::string& bindIp);

// 执行一次登录（被 procd 拉起前手动用、或装完自检用）。
int runLoginOnce(const std::string& appDir, const std::string& profileFilter,
                 const std::string& bindIp);

// 检测在线状态；probe=true 时只探测门户走哪套流程，不登录。
int runCheck(const std::string& appDir, const std::string& profileFilter, bool probe, bool quiet);

// 把认证状态同步给 mwan3（状态没变就不重复调用，10 分钟强制重申一次）。
void syncMwan3(const Profile& profile, bool online);

}  // namespace cnc
