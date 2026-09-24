// 本机（Windows/Linux 都能跑）的单元测试：只覆盖与平台无关的核心逻辑。
// 用法: test_core [rsa_vectors.json]
//
// 重点是 RSA —— 它的输出必须和 Python 参考实现逐字节一致，
// 否则统一身份认证会直接报密码错，而且很难查。
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "../src/json.hpp"
#include "../src/portal_parse.hpp"
#include "../src/rsa.hpp"
#include "../src/strings.hpp"

namespace {

int g_failed = 0;
int g_passed = 0;

void check(bool condition, const std::string& what) {
    if (condition) {
        ++g_passed;
    } else {
        ++g_failed;
        std::printf("  [失败] %s\n", what.c_str());
    }
}

std::string readAll(const std::string& path, bool& ok) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        ok = false;
        return "";
    }
    std::ostringstream buffer;
    buffer << in.rdbuf();
    ok = true;
    return buffer.str();
}

std::string abbreviate(const std::string& text, size_t limit = 72) {
    if (text.size() <= limit) return text;
    return text.substr(0, limit) + "...";
}

void testRsa(const std::string& vectorPath) {
    std::printf("== RSA 与 Python 参考实现对照 ==\n");
    bool ok = false;
    std::string raw = readAll(vectorPath, ok);
    if (!ok) {
        ++g_failed;
        std::printf("  [失败] 读不到测试向量 %s\n", vectorPath.c_str());
        return;
    }
    bool parsed = false;
    cnc::Json root = cnc::Json::parse(raw, &parsed);
    if (!parsed) {
        ++g_failed;
        std::printf("  [失败] 测试向量不是合法 JSON\n");
        return;
    }

    const cnc::Json* chunk = root.find("chunk_size");
    check(chunk != nullptr && chunk->asInt() == cnc::rsaChunkSize(),
          "块大小应为 " + std::to_string(chunk ? chunk->asInt() : -1) +
              "，实际 " + std::to_string(cnc::rsaChunkSize()));

    const cnc::Json* samples = root.find("samples");
    if (samples == nullptr || !samples->isArray()) {
        ++g_failed;
        std::printf("  [失败] 测试向量里没有 samples 数组\n");
        return;
    }
    for (size_t i = 0; i < samples->items.size(); ++i) {
        const cnc::Json& item = samples->items[i];
        const cnc::Json* password = item.find("password");
        const cnc::Json* expected = item.find("cipher");
        if (!password || !expected) continue;
        std::string actual = cnc::rsaEncryptPassword(password->asString());
        std::string label = "样本 " + std::to_string(i) + " (" +
                            abbreviate(password->asString(), 20) + ")";
        if (actual == expected->asString()) {
            check(true, label);
        } else {
            check(false, label + "\n         期望 " + abbreviate(expected->asString(), 96) +
                             "\n         实际 " + abbreviate(actual, 96));
        }
    }
}

void testJson() {
    std::printf("== JSON 解析/序列化 ==\n");

    const char* sample =
        "{\"a\":1,\"b\":\"中文\",\"c\":[true,false,null,2.5],"
        "\"d\":{\"e\":\"x\\ny\"},\"f\":\"\\u00e9\\ud83d\\ude00\"}";
    bool ok = false;
    cnc::Json root = cnc::Json::parse(sample, &ok);
    check(ok, "能解析样例");
    check(root.find("a") && root.find("a")->asInt() == 1, "整数取值");
    check(root.find("b") && root.find("b")->asString() == "中文", "UTF-8 字符串");
    const cnc::Json* c = root.find("c");
    check(c && c->isArray() && c->items.size() == 4, "数组长度");
    check(c && c->items[0].asBool() == true, "布尔取值");
    check(c && c->items[2].isNull(), "null 识别");
    check(c && c->items[3].asNumber() == 2.5, "小数取值");
    check(root.find("d") && root.find("d")->find("e")->asString() == "x\ny", "嵌套 + 转义");
    check(root.find("f") && root.find("f")->asString() == "\xc3\xa9\xf0\x9f\x98\x80",
          "\\u 转义（含代理对）");

    bool again = false;
    cnc::Json reparsed = cnc::Json::parse(root.dump(), &again);
    check(again, "自己 dump 出来的还能解析回去");
    check(reparsed.find("b") && reparsed.find("b")->asString() == "中文",
          "序列化不丢中文");

    bool bad = true;
    cnc::Json::parse("{\"a\":}", &bad);
    check(!bad, "非法 JSON 要报错而不是崩掉");

    cnc::Json built = cnc::Json::object();
    built["account"] = cnc::Json::of("M1503210");
    built["failures"] = cnc::Json::of(static_cast<int64_t>(3));
    check(built.dump() == "{\"account\":\"M1503210\",\"failures\":3}", "构造后序列化");
}

// 这些样例的形状是从路由器上真实抓下来的门户 / 认证页里抄的。
void testPortalParsing() {
    std::printf("== 门户页面解析 ==\n");

    const std::string portal =
        "<html><head><script type=\"text/javascript\">\n"
        "  var authloginpath = '/eportal/?c=ACSetting&a=Login';\n"
        "  var authloginport = 801;\n"
        "  var authuserfield = 'DDDDD';\n"
        "  var authpassfield = 'upass';\n"
        "  var authloginparam = '';\n"
        "  var authsuccess = '';\n"
        "  var authfail = '';\n"
        "  var fileVersion = \"1755483350029\";\n"
        "  var v4serip = '10.255.255.2';\n"
        "  v46ip = '10.12.13.57';\n"
        "</script><title>Dr.COMWebLogin</title></head>\n"
        "<body><form name=\"f1\"></form></body></html>";

    const cnc::PortalConfig conf = cnc::parsePortalConfig(portal);
    check(conf.authLoginPath == "/eportal/?c=ACSetting&a=Login", "门户登录路径");
    check(conf.authLoginPort == "801", "门户登录端口");
    check(conf.authUserField == "DDDDD", "账号字段名");
    check(conf.authPassField == "upass", "密码字段名");
    check(conf.jsVersion == "1755483350029", "jsVersion（双引号那种）");
    check(conf.v4SerIp == "10.255.255.2", "门户服务地址");

    check(cnc::clientIpFromPortal(portal) == "10.12.13.57", "抠出客户端地址 v46ip");
    check(cnc::containsPortalMarker(portal), "识别门户标记");

    // 有线网段（10.12.x）走统一身份认证；无线网段（10.54.x）走 Dr.COM
    check(cnc::needsCas("10.12.13.57"), "10.12.x 判为有线网段");
    check(!cnc::needsCas("10.54.91.217"), "10.54.x 判为无线网段");
    check(cnc::needsCas(""), "地址未知时保守走有线那套");

    const std::string loginPage =
        "<html><body>"
        "<form id=\"fm4\" action=\"/cas/login?service=x\" method=\"post\">"
        "<input type=\"hidden\" name=\"execution\" value=\"e1s1\"/>"
        "<input name=\"lt\" value=\"LT-1\"/>"
        "<input type=\"submit\" name=\"submit\" value=\"登录\"/>"
        "</form>"
        "<form id=\"fm3\" action=\"/cas/login\">"
        "<input type=\"text\" name=\"username\" value=\"\"/>"
        "<input type=\"password\" name=\"password\" value=\"\"/>"
        "</form>"
        "</body></html>";

    const std::vector<cnc::Form> forms = cnc::parseForms(loginPage);
    check(forms.size() == 2, "解析出两个表单");
    check(!forms.empty() && forms[0].id == "fm4", "第一个表单 id");
    check(!forms.empty() && forms[0].action == "/cas/login?service=x", "表单 action");
    check(!forms.empty() && forms[0].hasInput("execution"), "取到 execution");
    check(!forms.empty() && !forms[0].hasInput("submit"), "submit 按钮要排除掉");
    check(forms.size() > 1 && forms[1].hasInput("password"), "第二个表单里有 password");
    check(cnc::findContextPath("<script>var contextPath = \"/cas\";</script>") == "/cas",
          "从页面里取出 contextPath");
    check(cnc::findContextPath("<script>var contextPath = '/cas2';</script>") == "/cas2",
          "contextPath 单引号也要认");
    check(cnc::findContextPath("<html></html>").empty(), "没有 contextPath 时返回空");

    // 服务器要求滑块验证时的判定
    check(cnc::needsCaptcha("<div>请完成安全验证</div>"), "要求验证的页面");
    check(!cnc::needsCaptcha(
              "<div>请完成安全验证</div><div class=\"slidingverification none\"></div>"),
          "滑块面板带 none 类 = 不需要验证");
    check(!cnc::needsCaptcha("<html>普通的登录页</html>"), "普通页面不需要验证");
}

}  // namespace

int main(int argc, char** argv) {
    std::string vectorPath = argc > 1 ? argv[1] : "openwrt-cpp/tests/rsa_vectors.json";
    testJson();
    testPortalParsing();
    testRsa(vectorPath);
    std::printf("\n通过 %d 项，失败 %d 项\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
