#include "rsa.hpp"

#include <cstdint>
#include <cstdio>
#include <vector>

namespace cnc {
namespace {

// 学校统一身份认证页面里写死的 RSA 公钥（指数 010001 = 65537）。
const char* const kModulusHex =
    "008aed7e057fe8f14c73550b0e6467b023616ddc8fa91846d2613cdb7f7621e3"
    "cada4cd5d812d627af6b87727ade4e26d26208b7326815941492b2204c3167ab"
    "2d53df1e3a2c9153bdb7c8c2e968df97a5e7e01cc410f92c4c2c2fba529b3e"
    "e988ebc1fca99ff5119e036d732c368acf8beba01aa2fdafa45b21e4de4928d"
    "0d403";

const uint32_t kPublicExponent = 65537;

// --------------------------------------------------------------------------
// 够用就好的大整数：32 位 limb，小端序，去掉高位多余的 0。
// 只服务于 RSA 这一件事（1024 位模数、指数只有两个 1），所以不追求通用性能。
// --------------------------------------------------------------------------
using Limbs = std::vector<uint32_t>;

void trim(Limbs& value) {
    while (!value.empty() && value.back() == 0) value.pop_back();
}

int compare(const Limbs& a, const Limbs& b) {
    if (a.size() != b.size()) return a.size() < b.size() ? -1 : 1;
    for (size_t i = a.size(); i-- > 0;) {
        if (a[i] != b[i]) return a[i] < b[i] ? -1 : 1;
    }
    return 0;
}

// a - b，要求 a >= b。
Limbs subtract(const Limbs& a, const Limbs& b) {
    Limbs out = a;
    uint64_t borrow = 0;
    for (size_t i = 0; i < out.size(); ++i) {
        uint64_t lhs = out[i];
        uint64_t rhs = (i < b.size() ? b[i] : 0u) + borrow;
        if (lhs < rhs) {
            out[i] = static_cast<uint32_t>(lhs + (1ull << 32) - rhs);
            borrow = 1;
        } else {
            out[i] = static_cast<uint32_t>(lhs - rhs);
            borrow = 0;
        }
    }
    trim(out);
    return out;
}

// 左移一位（原地），返回值丢弃最高位溢出（调用处保证不会溢出）。
void shiftLeft1(Limbs& value) {
    uint32_t carry = 0;
    for (auto& limb : value) {
        uint32_t next = limb >> 31;
        limb = static_cast<uint32_t>((limb << 1) | carry);
        carry = next;
    }
    if (carry) value.push_back(carry);
}

Limbs multiply(const Limbs& a, const Limbs& b) {
    if (a.empty() || b.empty()) return Limbs();
    Limbs out(a.size() + b.size(), 0);
    for (size_t i = 0; i < a.size(); ++i) {
        uint64_t carry = 0;
        for (size_t j = 0; j < b.size(); ++j) {
            uint64_t cur = static_cast<uint64_t>(out[i + j]) +
                           static_cast<uint64_t>(a[i]) * b[j] + carry;
            out[i + j] = static_cast<uint32_t>(cur);
            carry = cur >> 32;
        }
        size_t k = i + b.size();
        while (carry) {
            uint64_t cur = static_cast<uint64_t>(out[k]) + carry;
            out[k] = static_cast<uint32_t>(cur);
            carry = cur >> 32;
            ++k;
        }
    }
    trim(out);
    return out;
}

// 按位长除法求余：从被除数最高位开始逐位“移入 -> 比较 -> 减”。
// 被除数最多 2048 位，模数 1024 位，速度完全够。
Limbs modReduce(const Limbs& dividend, const Limbs& modulus) {
    if (modulus.empty()) return Limbs();
    if (compare(dividend, modulus) < 0) return dividend;

    const size_t bits = dividend.size() * 32 - 1;
    size_t top = bits;
    while (top > 0 && ((dividend[top / 32] >> (top % 32)) & 1u) == 0) --top;

    Limbs remainder;
    for (size_t i = top + 1; i-- > 0;) {
        shiftLeft1(remainder);
        if ((dividend[i / 32] >> (i % 32)) & 1u) {
            if (remainder.empty()) remainder.push_back(0);
            remainder[0] |= 1u;
        }
        if (compare(remainder, modulus) >= 0) remainder = subtract(remainder, modulus);
    }
    return remainder;
}

Limbs mulMod(const Limbs& a, const Limbs& b, const Limbs& modulus) {
    return modReduce(multiply(a, b), modulus);
}

Limbs modPow(Limbs base, uint32_t exponent, const Limbs& modulus) {
    Limbs result;
    result.push_back(1);
    base = modReduce(base, modulus);

    bool started = false;
    for (int bit = 31; bit >= 0; --bit) {
        if (!started) {
            if (((exponent >> bit) & 1u) == 0) continue;
            started = true;
        }
        result = mulMod(result, result, modulus);
        if ((exponent >> bit) & 1u) result = mulMod(result, base, modulus);
    }
    return result;
}

int hexValue(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

Limbs fromHex(const std::string& hex) {
    // 跳过前导 0，和 Python 的 int(hex, 16) 结果一致（前导 0 不改变数值）
    size_t start = 0;
    while (start < hex.size() && hex[start] == '0') ++start;
    std::string body = hex.substr(start);

    Limbs out;
    for (size_t end = body.size(); end > 0;) {
        size_t begin = (end > 8 ? end - 8 : 0);
        uint32_t limb = 0;
        for (size_t i = begin; i < end; ++i) {
            int digit = hexValue(body[i]);
            if (digit < 0) return Limbs();
            limb = (limb << 4) | static_cast<uint32_t>(digit);
        }
        out.push_back(limb);
        end = begin;
    }
    trim(out);
    return out;
}

std::string toHex(const Limbs& value) {
    if (value.empty()) return "0";
    std::string out;
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%x", value.back());
    out += buf;
    for (size_t i = value.size() - 1; i-- > 0;) {
        std::snprintf(buf, sizeof(buf), "%08x", value[i]);
        out += buf;
    }
    return out;
}

const Limbs& modulus() {
    static const Limbs value = fromHex(kModulusHex);
    return value;
}

}  // namespace

int rsaChunkSize() {
    // 模数位数 = 1024 -> 2 * ((1024 - 1) / 16) = 126
    size_t bits = modulus().size() * 32;
    uint32_t lead = modulus().back();
    int leadBits = 0;
    while (lead) {
        ++leadBits;
        lead >>= 1;
    }
    bits = bits - 32 + static_cast<size_t>(leadBits);
    return static_cast<int>(2 * ((bits - 1) / 16));
}

std::string rsaEncryptPassword(const std::string& password) {
    const int chunk = rsaChunkSize();
    if (chunk <= 0) return "";
    for (unsigned char c : password) {
        if (c > 0x7F) return "";  // 学校页面按单字节处理，非 ASCII 必然算错
    }

    std::string data = password;
    while (data.size() % static_cast<size_t>(chunk) != 0) data.push_back('\0');
    if (data.empty()) data.assign(static_cast<size_t>(chunk), '\0');

    const Limbs& n = modulus();
    std::string out;
    for (size_t start = 0; start < data.size(); start += static_cast<size_t>(chunk)) {
        // 小端序：块内第一个字节是最低有效字节。
        // 注意一块是 126 字节，不是 4 的整数倍，所以按字节归位、不能按 limb 边界切。
        Limbs block;
        for (size_t i = 0; i < static_cast<size_t>(chunk); ++i) {
            size_t index = start + i;
            uint32_t byte = index < data.size()
                                ? static_cast<uint32_t>(static_cast<unsigned char>(data[index]))
                                : 0u;
            size_t limbIndex = i / 4;
            size_t shift = (i % 4) * 8;
            if (limbIndex >= block.size()) block.push_back(0);
            block[limbIndex] |= byte << shift;
        }
        trim(block);

        Limbs cipher = modPow(block, kPublicExponent, n);
        std::string hex = toHex(cipher);
        // 学校库按 16 位字输出，最高字不足 4 个十六进制字符时左侧补零；
        // 整体补齐到 4 的整数倍后，每组 4 位重新输出，结果就是同一个串。
        if (hex.size() % 4 != 0) hex.insert(0, 4 - hex.size() % 4, '0');

        if (!out.empty()) out.push_back(' ');
        out += hex;
    }
    return out;
}

}  // namespace cnc
