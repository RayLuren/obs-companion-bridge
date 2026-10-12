// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include <string>

namespace bridge {
inline constexpr char local_server[] = "rtmp://127.0.0.1:19360/obs";
inline constexpr char local_key[] = "main";
inline constexpr char protocol_version[] = "1";

inline bool service_matches(const std::string &server, const std::string &key)
{
    // Exact matching is deliberate: never stop an arbitrary OBS output.
    return server == local_server && key == local_key;
}

inline std::string json_string(const std::string &input)
{
    constexpr char hex[] = "0123456789abcdef";
    std::string out = "\"";
    for (const unsigned char c : input) {
        switch (c) {
        case '\"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\b': out += "\\b"; break;
        case '\f': out += "\\f"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) {
                out += "\\u00";
                out += hex[c >> 4];
                out += hex[c & 15];
            } else {
                out += static_cast<char>(c);
            }
        }
    }
    return out + "\"";
}

inline bool supported_request(const std::string &request)
{
    return request == "PING" || request == "STATUS" || request == "STOP_LOCAL";
}
} // namespace bridge
