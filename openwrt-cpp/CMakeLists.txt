# 可选：给"想用 CMake 在本机编译/调试"的人用。
# 路由器上的正式产物请用 tools/build.ps1（zig 交叉编译，静态、体积最小）。
cmake_minimum_required(VERSION 3.16)
project(campus_net_login CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 只在类 Unix 上编正式程序（用到了 socket / /proc / posix_spawn）
if (UNIX)
    add_executable(campus-net-login
        src/main.cpp
        src/config.cpp
        src/http.cpp
        src/json.cpp
        src/portal.cpp
        src/portal_parse.cpp
        src/rsa.cpp
        src/strings.cpp
        src/util.cpp
        src/watch.cpp
    )
    target_compile_definitions(campus-net-login PRIVATE _GNU_SOURCE)
    find_package(Threads REQUIRED)
    target_link_libraries(campus-net-login PRIVATE Threads::Threads)
endif ()

# 单元测试只依赖与平台无关的几个文件，所以 Windows 上也能编
enable_testing()
add_executable(test_core
    tests/test_core.cpp
    src/json.cpp
    src/rsa.cpp
    src/strings.cpp
    src/portal_parse.cpp
)
add_test(NAME core
         COMMAND test_core ${CMAKE_CURRENT_SOURCE_DIR}/tests/rsa_vectors.json)
