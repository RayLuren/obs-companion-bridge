// SPDX-License-Identifier: GPL-2.0-or-later
// Uses the real plugin pipe/window lifecycle with a mocked OBS frontend.
#include "test_obs_api.h"
#include "bridge_policy.h"
#include <windows.h>
#include <aclapi.h>
#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <thread>

extern "C" bool obs_module_load();
extern "C" void obs_module_unload();

static DWORD ui_thread = 0;
static std::atomic<bool> active{false};
static std::atomic<bool> actual_output_active{false};
static std::atomic<bool> reconnecting_output{false};
static std::atomic<unsigned> stop_count{0};
static std::atomic<unsigned> frontend_stop_count{0};
static std::atomic<unsigned> wrong_thread_count{0};
static std::string server = bridge::local_server;
static std::string key = bridge::local_key;
static std::string menu_name;
static obs_frontend_event_cb event_cb = nullptr;
static obs_service_t service{};
static obs_data_t settings{};
static obs_output_t output{};
static obs_service_t output_service{};
static obs_data_t output_settings{};
static std::string actual_server = bridge::local_server;
static std::string actual_key = bridge::local_key;

static void check_ui_thread()
{
    if (GetCurrentThreadId() != ui_thread) wrong_thread_count.fetch_add(1);
}
void blog(int, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vprintf(format, args);
    va_end(args);
    puts("");
}
const char *obs_get_version_string() { return "32.2.1-test-host"; }
void obs_frontend_add_tools_menu_item(const char *name, obs_frontend_cb, void *)
{ check_ui_thread(); menu_name = name; }
void obs_frontend_add_event_callback(obs_frontend_event_cb callback, void *)
{ event_cb = callback; }
void obs_frontend_remove_event_callback(obs_frontend_event_cb, void *) { event_cb = nullptr; }
bool obs_frontend_streaming_active() { check_ui_thread(); return active.load(); }
bool obs_frontend_recording_active() { check_ui_thread(); return true; }
void obs_frontend_streaming_stop()
{ check_ui_thread(); frontend_stop_count.fetch_add(1); }
char *obs_frontend_get_current_profile()
{
    check_ui_thread();
    return _strdup("Codex SDK桥接测试\"\\\n");
}
obs_service_t *obs_frontend_get_streaming_service() { check_ui_thread(); return &service; }
obs_output_t *obs_frontend_get_streaming_output() { check_ui_thread(); return &output; }
bool obs_output_active(const obs_output_t *) { return actual_output_active.load(); }
bool obs_output_reconnecting(const obs_output_t *) { return reconnecting_output.load(); }
obs_service_t *obs_output_get_service(const obs_output_t *) { return &output_service; }
void obs_output_release(obs_output_t *) {}
void obs_output_stop(obs_output_t *)
{ check_ui_thread(); stop_count.fetch_add(1); /* intentionally async: active stays true */ }
obs_data_t *obs_service_get_settings(const obs_service_t *value)
{ return value == &output_service ? &output_settings : &settings; }
const char *obs_data_get_string(const obs_data_t *value, const char *name)
{
    const bool actual = value == &output_settings;
    return strcmp(name, "server") == 0 ? (actual ? actual_server.c_str() : server.c_str()) :
        (actual ? actual_key.c_str() : key.c_str());
}
void obs_data_release(obs_data_t *) {}
void bfree(void *pointer) { free(pointer); }

static std::wstring pipe_path()
{
    DWORD session = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &session);
    return L"\\\\.\\pipe\\OBS_MATE_BRIDGE_S" + std::to_wstring(session) +
        L"_TEST_" + std::to_wstring(GetCurrentProcessId());
}

static HANDLE connect_pipe()
{
    const auto path = pipe_path();
    for (unsigned i = 0; i < 100; ++i) {
        HANDLE pipe = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
            OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IMPERSONATION, nullptr);
        if (pipe != INVALID_HANDLE_VALUE) return pipe;
        WaitNamedPipeW(path.c_str(), 50);
        Sleep(10);
    }
    throw std::runtime_error("could not connect to test pipe");
}

static std::string roundtrip(const std::string &command)
{
    HANDLE pipe = connect_pipe();
    std::string sent = command + "\n";
    DWORD count = 0;
    if (!WriteFile(pipe, sent.data(), static_cast<DWORD>(sent.size()), &count, nullptr)) {
        CloseHandle(pipe); throw std::runtime_error("pipe write failed");
    }
    std::string response;
    char buffer[4096];
    while (ReadFile(pipe, buffer, sizeof(buffer), &count, nullptr) && count) {
        response.append(buffer, count);
        if (response.find('\n') != std::string::npos) break;
    }
    CloseHandle(pipe);
    if (response.empty()) throw std::runtime_error("empty pipe response");
    return response;
}

static std::string request_with_ui(const std::string &command)
{
    std::atomic<bool> done{false};
    std::string response;
    std::string error;
    std::thread client([&] {
        try { response = roundtrip(command); }
        catch (const std::exception &e) { error = e.what(); }
        done.store(true);
    });
    const auto started = GetTickCount64();
    while (!done.load()) {
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        if (GetTickCount64() - started > 10000) { puts("test UI wait exceeded timeout"); ExitProcess(20); }
        Sleep(1);
    }
    client.join();
    if (!error.empty()) throw std::runtime_error(error);
    DWORD session = 0;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &session) ||
        response.find("\"pid\":" + std::to_string(GetCurrentProcessId()) + ",") == std::string::npos ||
        response.find("\"session_id\":" + std::to_string(session) + ",") == std::string::npos)
        throw std::runtime_error("response server identity missing or incorrect");
    printf("%s -> %s", command.c_str(), response.c_str());
    return response;
}

static void require(bool condition, const char *message)
{
    if (!condition) throw std::runtime_error(message);
}

static void check_pipe_acl(HANDLE pipe)
{
    PACL dacl = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    const DWORD error = GetSecurityInfo(pipe, SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
        nullptr, nullptr, &dacl, nullptr, &descriptor);
    require(error == ERROR_SUCCESS && descriptor && dacl, "could not inspect real pipe DACL");
    ACL_SIZE_INFORMATION info{};
    require(GetAclInformation(dacl, &info, sizeof(info), AclSizeInformation) != FALSE, "invalid pipe ACL");
    require(info.AceCount == 2, "pipe ACL grants unexpected accounts");
    SECURITY_DESCRIPTOR_CONTROL control = 0;
    DWORD revision = 0;
    require(GetSecurityDescriptorControl(descriptor, &control, &revision) != FALSE &&
        (control & SE_DACL_PROTECTED) != 0, "pipe ACL unexpectedly inherits grants");
    HANDLE token = nullptr;
    require(OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token) != FALSE, "cannot inspect owner token");
    DWORD length = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &length);
    std::string token_buffer(length, '\0');
    require(GetTokenInformation(token, TokenUser, token_buffer.data(), length, &length) != FALSE, "owner SID unavailable");
    PSID owner = reinterpret_cast<TOKEN_USER *>(token_buffer.data())->User.Sid;
    BYTE system_buffer[SECURITY_MAX_SID_SIZE];
    DWORD system_length = sizeof(system_buffer);
    require(CreateWellKnownSid(WinLocalSystemSid, nullptr, system_buffer, &system_length) != FALSE, "SYSTEM SID unavailable");
    bool has_owner = false;
    bool has_system = false;
    for (DWORD i = 0; i < info.AceCount; ++i) {
        void *raw_ace = nullptr;
        require(GetAce(dacl, i, &raw_ace) != FALSE, "cannot read ACE");
        const auto ace = static_cast<ACCESS_ALLOWED_ACE *>(raw_ace);
        require(ace->Header.AceType == ACCESS_ALLOWED_ACE_TYPE, "unexpected ACE type");
        PSID sid = const_cast<DWORD *>(&ace->SidStart);
        has_owner |= EqualSid(sid, owner) != FALSE;
        has_system |= EqualSid(sid, system_buffer) != FALSE;
    }
    CloseHandle(token);
    LocalFree(descriptor);
    require(has_owner && has_system, "pipe ACL does not restrict to owner/SYSTEM");
}

int main()
{
    try {
        ui_thread = GetCurrentThreadId();
        require(bridge::service_matches(bridge::local_server, bridge::local_key), "local service match failed");
        require(!bridge::service_matches("rtmp://example.com/obs", "main"), "external service matched");
        require(!bridge::service_matches("rtmp://127.0.0.1:19360/obs/", "main"), "nonexact server matched");
        require(!bridge::service_matches(bridge::local_server, "other"), "nonexact key matched");
        require(bridge::json_string(std::string("\"\\\n\t\0", 5)) == "\"\\\"\\\\\\n\\t\\u0000\"", "JSON escaping failed");
        require(obs_module_load(), "module load failed");
        require(menu_name == "启动伴侣桥接", "menu name differs");
        auto response = request_with_ui("PING");
        require(response.find("OBS_NOT_READY") != std::string::npos, "incomplete frontend reported ready");
        event_cb(OBS_FRONTEND_EVENT_FINISHED_LOADING, nullptr);
        response = request_with_ui("PING");
        require(response.find("\"ok\":true") != std::string::npos, "PING failed");
        response = request_with_ui("STATUS");
        require(response.find("\"recording_active\":true") != std::string::npos, "recording status missing");
        require(response.find("\"profile\":\"Codex SDK桥接测试\\\"\\\\\\n\"") != std::string::npos, "UTF8 profile escaping failed");
        response = request_with_ui("STOP_LOCAL");
        require(response.find("\"stopped\":true") != std::string::npos && stop_count.load() == 0, "idle stop called frontend");
        event_cb(OBS_FRONTEND_EVENT_STREAMING_STARTING, nullptr);
        response = request_with_ui("STATUS");
        require(response.find("\"stream_active\":true") != std::string::npos &&
            response.find("\"stream_connecting\":true") != std::string::npos,
            "first connection reported stopped before capture began");
        response = request_with_ui("STOP_LOCAL");
        require(response.find("OBS_CONNECTING") != std::string::npos && stop_count.load() == 0,
            "first connection prematurely reported stopped");
        event_cb(OBS_FRONTEND_EVENT_STREAMING_STARTED, nullptr);
        active.store(true);
        server = "rtmp://example.invalid/live";
        response = request_with_ui("STOP_LOCAL");
        require(response.find("SERVICE_MISMATCH") != std::string::npos && stop_count.load() == 0, "external stream stopped");
        server = bridge::local_server;
        key = "other";
        response = request_with_ui("STOP_LOCAL");
        require(response.find("SERVICE_MISMATCH") != std::string::npos && stop_count.load() == 0, "wrong key stopped");
        key = bridge::local_key;
        actual_server = "rtmp://example.invalid/active";
        response = request_with_ui("STOP_LOCAL");
        require(response.find("SERVICE_MISMATCH") != std::string::npos && stop_count.load() == 0,
            "actual external service stopped after current settings changed");
        actual_server = bridge::local_server;
        response = request_with_ui("STOP_LOCAL");
        require(response.find("\"requested\":true") != std::string::npos &&
            response.find("\"stopped\":false") != std::string::npos && stop_count.load() == 1, "async stop reported false success");
        active.store(false);
        reconnecting_output.store(true);
        response = request_with_ui("STATUS");
        require(response.find("\"stream_active\":true") != std::string::npos, "reconnecting output reported idle");
        response = request_with_ui("STOP_LOCAL");
        require(response.find("\"requested\":true") != std::string::npos &&
            response.find("\"stopped\":false") != std::string::npos && stop_count.load() == 2,
            "reconnecting output stop not requested");
        reconnecting_output.store(false);
        response = request_with_ui("STATUS");
        require(response.find("\"stream_active\":false") != std::string::npos, "final stop status wrong");
        response = request_with_ui("START_LOCAL");
        require(response.find("INVALID_REQUEST") != std::string::npos, "unknown command accepted");
        require(wrong_thread_count.load() == 0, "frontend called from worker thread");
        require(frontend_stop_count.load() == 0, "frontend stop can indirectly stop recording");
        // The worker must release an idle client promptly on shutdown.
        HANDLE idle_client = connect_pipe();
        check_pipe_acl(idle_client);
        const auto shutdown_start = GetTickCount64();
        obs_module_unload();
        const auto elapsed = GetTickCount64() - shutdown_start;
        CloseHandle(idle_client);
        require(elapsed < 1500, "shutdown blocked on idle client");
        // Exercise unload while a STATUS request is queued and the UI is not pumping.
        require(obs_module_load(), "module could not reload after shutdown");
        event_cb(OBS_FRONTEND_EVENT_FINISHED_LOADING, nullptr);
        std::thread pending_client([] {
            try { (void)roundtrip("STATUS"); } catch (const std::exception &) {}
        });
        Sleep(100);
        const auto queued_shutdown_start = GetTickCount64();
        obs_module_unload();
        pending_client.join();
        const auto queued_elapsed = GetTickCount64() - queued_shutdown_start;
        require(queued_elapsed < 1500, "shutdown blocked on queued UI request");
        require(wrong_thread_count.load() == 0, "frontend called from worker during shutdown");
        printf("PASS: policy, UTF8 JSON, success/error server identity, protected owner/SYSTEM DACL, real named pipe, UI dispatch, guarded async stop, connecting output, recording untouched, shutdown idle=%llu ms queued=%llu ms.\n", elapsed, queued_elapsed);
        return 0;
    } catch (const std::exception &e) {
        fprintf(stderr, "FAIL: %s\n", e.what());
        obs_module_unload();
        return 1;
    }
}
