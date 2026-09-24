// 复刻学校 security.js 里的 RSAUtils.encryptedString()。
//
// 和标准 PKCS#1 不一样：这里不做任何填充，直接把明文字节按**小端序**当成大整数，
// 尾部补 0 补满一块；指数 65537，模数见 rsa.cpp。所以同一个密码每次密文都一样。
// C++ 版必须和 Python 版逐字节一致，否则统一身份认证会直接报密码错。
#pragma once

#include <cstddef>
#include <string>

namespace cnc {

// 每块的字节数：2 * ((模数位数 - 1) / 16)。1024 位模数 → 126 字节。
int rsaChunkSize();

// 密码含非 ASCII 字节时返回空串（学校页面按单字节处理，多字节必错）。
std::string rsaEncryptPassword(const std::string& password);

}  // namespace cnc
