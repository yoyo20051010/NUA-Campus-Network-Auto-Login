// 交叉编译冒烟测试：确认工具链能产出可在路由器上运行的可执行文件。
// 只用到 C++ 运行时 + pthread + Linux syscall，不依赖任何第三方库。
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include <sys/utsname.h>
#include <unistd.h>

int main() {
    utsname u{};
    uname(&u);

    std::vector<std::string> hits;
    std::thread t([&hits] {
        hits.push_back("thread-ok");
    });
    t.join();

    std::printf("smoke: %s %s (%s)\n", u.sysname, u.machine, u.release);
    std::printf("smoke: c++%ld pthread=%s hello\n", __cplusplus, hits.empty() ? "no" : "yes");
    std::printf("smoke: /proc/self/status %s\n",
                access("/proc/self/status", R_OK) == 0 ? "readable" : "missing");
    return 0;
}
