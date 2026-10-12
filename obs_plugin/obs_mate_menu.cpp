// SPDX-License-Identifier: GPL-2.0-or-later
// This plugin changes no OBS configuration and never controls recording.
#ifdef BRIDGE_TEST_HOST
#include "test_obs_api.h"
#else
#include <obs-module.h>
#include <obs-frontend-api.h>
#endif
#include "bridge_policy.h"
#include <windows.h>
#include <sddl.h>
#include <shellapi.h>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

OBS_DECLARE_MODULE()
OBS_MODULE_AUTHOR("OBS companion local bridge")

namespace {
constexpr wchar_t window_class[] = L"ObsMateBridge.Dispatch.v1";
constexpr wchar_t launcher_path[] = L"D:\\OBS伴侣桥接\\app\\1.0.2\\OBS伴侣桥接.exe";
constexpr wchar_t launcher_directory[] = L"D:\\OBS伴侣桥接\\app\\1.0.2";
constexpr UINT dispatch_message = WM_APP + 417;
constexpr DWORD request_timeout_ms = 5000;

struct Handle {
    HANDLE value = nullptr;
    explicit Handle(HANDLE h = nullptr) : value(h) {}
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    Handle(const Handle &) = delete;
    Handle &operator=(const Handle &) = delete;
};

struct Request {
    std::string command;
    std::string result;
    Handle complete{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
    std::atomic<bool> abandoned{false};
};

HANDLE stop_event = nullptr;
HWND dispatch_window = nullptr;
HINSTANCE module_instance = nullptr;
DWORD session_id = 0;
std::wstring pipe_name;
std::vector<BYTE> owner_sid;
std::thread pipe_thread;
std::mutex requests_mutex;
std::unordered_map<UINT_PTR, std::shared_ptr<Request>> requests;
std::atomic<UINT_PTR> next_request{1};
std::atomic<bool> shutting_down{false};
std::atomic<bool> frontend_ready{false};
std::atomic<bool> stream_connecting{false};

std::string error_json(const std::string &request, const char *error)
{
    return "{\"ok\":false,\"protocol\":1,\"request\":" + bridge::json_string(request) +
        ",\"pid\":" + std::to_string(GetCurrentProcessId()) +
        ",\"session_id\":" + std::to_string(session_id) +
        ",\"error\":" + bridge::json_string(error) + "}\n";
}

std::vector<BYTE> token_sid(HANDLE token)
{
    DWORD length = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &length);
    if (!length) return {};
    std::vector<BYTE> info(length);
    if (!GetTokenInformation(token, TokenUser, info.data(), length, &length)) return {};
    const auto user = reinterpret_cast<TOKEN_USER *>(info.data());
    if (!IsValidSid(user->User.Sid)) return {};
    std::vector<BYTE> sid(GetLengthSid(user->User.Sid));
    if (!CopySid(static_cast<DWORD>(sid.size()), sid.data(), user->User.Sid)) return {};
    return sid;
}

bool is_allowed_client(HANDLE pipe)
{
    ULONG client_session = 0;
    ULONG client_pid = 0;
    if (!GetNamedPipeClientSessionId(pipe, &client_session) || client_session != session_id ||
        !GetNamedPipeClientProcessId(pipe, &client_pid) || client_pid == 0)
        return false;
    if (!ImpersonateNamedPipeClient(pipe)) return false;
    HANDLE raw_token = nullptr;
    const bool opened = OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &raw_token) != FALSE;
    Handle token(raw_token);
    auto sid = opened ? token_sid(token.value) : std::vector<BYTE>{};
    const bool reverted = RevertToSelf() != FALSE;
    if (!reverted) {
        // Never keep running while impersonating another process.
        SetEvent(stop_event);
        return false;
    }
    return !sid.empty() && EqualSid(sid.data(), owner_sid.data()) != FALSE;
}

bool matches_service(obs_service_t *service)
{
    bool matching = false;
    if (service) {
        if (obs_data_t *settings = obs_service_get_settings(service)) {
            const char *server = obs_data_get_string(settings, "server");
            const char *key = obs_data_get_string(settings, "key");
            matching = bridge::service_matches(server ? server : "", key ? key : "");
            obs_data_release(settings);
        }
    }
    return matching;
}

// All frontend reads and the stop request execute on the original OBS UI thread.
std::string process_request(const std::string &command)
{
    if (!frontend_ready.load()) return error_json(command, "OBS_NOT_READY");
    bool frontend_active = obs_frontend_streaming_active();
    obs_output_t *output = obs_frontend_get_streaming_output(); // owned reference
    bool output_active = output && obs_output_active(output);
    bool reconnecting = output && obs_output_reconnecting(output);
    const bool connecting = stream_connecting.load();
    bool active = frontend_active || output_active || reconnecting || connecting;
    const bool current_matching = matches_service(obs_frontend_get_streaming_service());
    const bool output_matching = output && matches_service(obs_output_get_service(output));
    const bool matching = current_matching && (!active || output_matching);
    bool requested = false;
    if (command == "STOP_LOCAL") {
        if (!matching) {
            if (output) obs_output_release(output);
            return error_json(command, "SERVICE_MISMATCH");
        }
        if (connecting && !frontend_active && !output_active && !reconnecting) {
            if (output) obs_output_release(output);
            return error_json(command, "OBS_CONNECTING");
        }
        if (active) {
            // Stopping this exact output preserves recording even when the OBS
            // "stop recording with stream" preference is enabled.
            if (output) obs_output_stop(output);
            requested = true;
            frontend_active = obs_frontend_streaming_active();
            output_active = output && obs_output_active(output);
            reconnecting = output && obs_output_reconnecting(output);
            active = frontend_active || output_active || reconnecting || stream_connecting.load();
        }
    }
    if (output) obs_output_release(output);
    char *raw_profile = obs_frontend_get_current_profile();
    const std::string profile = raw_profile ? raw_profile : "";
    bfree(raw_profile);
    const char *version = obs_get_version_string();
    std::string result = "{\"ok\":true,\"protocol\":1,\"request\":" + bridge::json_string(command) +
        ",\"pid\":" + std::to_string(GetCurrentProcessId()) +
        ",\"session_id\":" + std::to_string(session_id) +
        ",\"stream_active\":" + (active ? "true" : "false") +
        ",\"frontend_stream_active\":" + (frontend_active ? "true" : "false") +
        ",\"output_active\":" + (output_active ? "true" : "false") +
        ",\"output_reconnecting\":" + (reconnecting ? "true" : "false") +
        ",\"stream_connecting\":" + (connecting ? "true" : "false") +
        ",\"recording_active\":" + (obs_frontend_recording_active() ? "true" : "false") +
        ",\"service_matches\":" + (matching ? "true" : "false") +
        ",\"profile\":" + bridge::json_string(profile) +
        ",\"obs_version\":" + bridge::json_string(version ? version : "") +
        ",\"plugin_version\":\"1.0.0\"";
    if (command == "STOP_LOCAL")
        result += ",\"requested\":" + std::string(requested ? "true" : "false") +
            ",\"stopped\":" + (!active ? "true" : "false");
    return result + "}\n";
}

LRESULT CALLBACK dispatch_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    if (message == dispatch_message) {
        std::shared_ptr<Request> request;
        {
            std::lock_guard<std::mutex> lock(requests_mutex);
            const auto found = requests.find(static_cast<UINT_PTR>(wparam));
            if (found != requests.end()) request = found->second;
        }
        if (request && !request->abandoned.load()) {
            request->result = shutting_down.load() ? error_json(request->command, "SHUTTING_DOWN") :
                process_request(request->command);
            SetEvent(request->complete.value);
        }
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

std::string dispatch_to_ui(const std::string &command)
{
    auto request = std::make_shared<Request>();
    request->command = command;
    if (!request->complete.value) return error_json(command, "EVENT_FAILED");
    const UINT_PTR id = next_request.fetch_add(1);
    {
        std::lock_guard<std::mutex> lock(requests_mutex);
        requests.emplace(id, request);
    }
    const bool posted = PostMessageW(dispatch_window, dispatch_message, id, 0) != FALSE;
    HANDLE wait_handles[] = {stop_event, request->complete.value};
    const DWORD result = posted ? WaitForMultipleObjects(2, wait_handles, FALSE, request_timeout_ms) : WAIT_FAILED;
    if (result != WAIT_OBJECT_0 + 1) request->abandoned.store(true);
    {
        std::lock_guard<std::mutex> lock(requests_mutex);
        requests.erase(id);
    }
    if (result == WAIT_OBJECT_0 + 1) return request->result;
    return error_json(command, result == WAIT_OBJECT_0 ? "SHUTTING_DOWN" : "UI_TIMEOUT");
}

bool complete_io(HANDLE pipe, OVERLAPPED &operation, DWORD &count, DWORD timeout)
{
    HANDLE wait_handles[] = {stop_event, operation.hEvent};
    if (WaitForMultipleObjects(2, wait_handles, FALSE, timeout) != WAIT_OBJECT_0 + 1) {
        CancelIoEx(pipe, &operation);
        // Named-pipe cancellation must complete before stack OVERLAPPED storage is freed.
        WaitForSingleObject(operation.hEvent, INFINITE);
        return false;
    }
    return GetOverlappedResult(pipe, &operation, &count, FALSE) != FALSE;
}

bool pipe_io(HANDLE pipe, bool write, void *buffer, DWORD size, DWORD &count, DWORD timeout = request_timeout_ms)
{
    Handle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!event.value) return false;
    OVERLAPPED operation{};
    operation.hEvent = event.value;
    const BOOL ok = write ? WriteFile(pipe, buffer, size, &count, &operation) :
        ReadFile(pipe, buffer, size, &count, &operation);
    if (ok) return true;
    return GetLastError() == ERROR_IO_PENDING && complete_io(pipe, operation, count, timeout);
}

void run_pipe()
{
    LPWSTR sid_string = nullptr;
    if (!ConvertSidToStringSidW(owner_sid.data(), &sid_string)) return;
    const std::wstring sddl = L"D:P(A;;GA;;;SY)(A;;GA;;;" + std::wstring(sid_string) + L")";
    LocalFree(sid_string);
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &descriptor, nullptr)) return;
    SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), descriptor, FALSE};
    while (WaitForSingleObject(stop_event, 0) == WAIT_TIMEOUT) {
        Handle pipe(CreateNamedPipeW(pipe_name.c_str(), PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
            FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
            PIPE_REJECT_REMOTE_CLIENTS, 1, 4096, 4096, 0, &security));
        if (pipe.value == INVALID_HANDLE_VALUE) {
            blog(LOG_WARNING, "[obs-mate-menu] Cannot create local pipe (Windows error %lu)", GetLastError());
            break;
        }
        Handle connected(CreateEventW(nullptr, TRUE, FALSE, nullptr));
        if (!connected.value) break;
        OVERLAPPED operation{};
        operation.hEvent = connected.value;
        DWORD ignored = 0;
        bool ready = ConnectNamedPipe(pipe.value, &operation) != FALSE;
        if (!ready) {
            const DWORD error = GetLastError();
            ready = error == ERROR_PIPE_CONNECTED ||
                (error == ERROR_IO_PENDING && complete_io(pipe.value, operation, ignored, INFINITE));
        }
        if (!ready) continue;
        std::string command;
        bool line_complete = false;
        while (command.size() < 256 && !line_complete) {
            char buffer[256];
            DWORD count = 0;
            if (!pipe_io(pipe.value, false, buffer, static_cast<DWORD>(256 - command.size()), count) || !count) break;
            command.append(buffer, count);
            const auto newline = command.find('\n');
            if (newline != std::string::npos) {
                line_complete = newline + 1 == command.size();
                command.resize(newline);
                if (!command.empty() && command.back() == '\r') command.pop_back();
                break;
            }
        }
        std::string response;
        if (!is_allowed_client(pipe.value)) response = error_json("", "CLIENT_DENIED");
        else if (!line_complete || !bridge::supported_request(command)) response = error_json("", "INVALID_REQUEST");
        else response = dispatch_to_ui(command);
        DWORD written = 0;
        if (pipe_io(pipe.value, true, response.data(), static_cast<DWORD>(response.size()), written) &&
            written == response.size()) {
            // The client closes after receiving its line. This bounded wait gives it time
            // to read buffered bytes without an unbounded FlushFileBuffers call.
            char ignored_byte = 0;
            DWORD ignored_count = 0;
            (void)pipe_io(pipe.value, false, &ignored_byte, 1, ignored_count, 1000);
        }
        // No FlushFileBuffers: a client which stops reading must never block OBS shutdown.
        DisconnectNamedPipe(pipe.value);
    }
    LocalFree(descriptor);
}

void shutdown()
{
    if (shutting_down.exchange(true)) return;
    if (stop_event) SetEvent(stop_event);
    // A worker waiting on the UI observes stop_event and leaves without requiring another UI callback.
    if (pipe_thread.joinable()) pipe_thread.join();
    if (dispatch_window) { DestroyWindow(dispatch_window); dispatch_window = nullptr; }
    if (module_instance) UnregisterClassW(window_class, module_instance);
    {
        std::lock_guard<std::mutex> lock(requests_mutex);
        requests.clear();
    }
    if (stop_event) { CloseHandle(stop_event); stop_event = nullptr; }
}

void frontend_event(obs_frontend_event event, void *)
{
    if (event == OBS_FRONTEND_EVENT_FINISHED_LOADING) frontend_ready.store(true);
    if (event == OBS_FRONTEND_EVENT_STREAMING_STARTING) stream_connecting.store(true);
    if (event == OBS_FRONTEND_EVENT_STREAMING_STARTED || event == OBS_FRONTEND_EVENT_STREAMING_STOPPED)
        stream_connecting.store(false);
    if (event == OBS_FRONTEND_EVENT_EXIT) { frontend_ready.store(false); shutdown(); }
}

void open_launcher(void *)
{
    if (shutting_down.load()) return;
    const auto result = reinterpret_cast<INT_PTR>(ShellExecuteW(nullptr, L"open", launcher_path,
        nullptr, launcher_directory, SW_SHOWNORMAL));
    if (result <= 32) {
        blog(LOG_WARNING, "[obs-mate-menu] Launcher failed (ShellExecute code %lld)", static_cast<long long>(result));
        const wchar_t *message = result == SE_ERR_ACCESSDENIED ?
            L"没有启动桥接程序。若刚才取消了管理员授权，可以再次点击菜单重试。" :
            L"无法启动桥接程序，请检查 D:\\OBS伴侣桥接\\app\\1.0.2\\OBS伴侣桥接.exe 是否存在。";
        MessageBoxW(static_cast<HWND>(nullptr), message, L"OBS 伴侣桥接", MB_OK | MB_ICONINFORMATION);
    } else {
        blog(LOG_INFO, "[obs-mate-menu] Tools menu launcher request accepted");
    }
}
} // namespace

MODULE_EXPORT const char *obs_module_name() { return "OBS Companion Bridge Menu"; }
MODULE_EXPORT const char *obs_module_description() { return "Local bridge launcher and guarded OBS output control"; }

MODULE_EXPORT bool obs_module_load()
{
    shutting_down.store(false);
    frontend_ready.store(false);
    stream_connecting.store(false);
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &session_id)) return false;
    HANDLE raw_token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw_token)) return false;
    Handle token(raw_token);
    owner_sid = token_sid(token.value);
    if (owner_sid.empty()) return false;
    pipe_name = L"\\\\.\\pipe\\OBS_MATE_BRIDGE_S" + std::to_wstring(session_id);
#ifdef BRIDGE_TEST_HOST
    pipe_name += L"_TEST_" + std::to_wstring(GetCurrentProcessId());
#endif
    stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!stop_event) return false;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&dispatch_proc), &module_instance)) { shutdown(); return false; }
    WNDCLASSW window{};
    window.lpfnWndProc = dispatch_proc;
    window.hInstance = module_instance;
    window.lpszClassName = window_class;
    if (!RegisterClassW(&window)) { shutdown(); return false; }
    dispatch_window = CreateWindowExW(0, window_class, L"", 0, 0, 0, 0, 0, HWND_MESSAGE,
        nullptr, module_instance, nullptr);
    if (!dispatch_window) { shutdown(); return false; }
    obs_frontend_add_event_callback(frontend_event, nullptr);
    obs_frontend_add_tools_menu_item("启动伴侣桥接", open_launcher, nullptr);
    try { pipe_thread = std::thread(run_pipe); }
    catch (...) { obs_frontend_remove_event_callback(frontend_event, nullptr); shutdown(); return false; }
    blog(LOG_INFO, "[obs-mate-menu] Loaded version 1.0.0; local pipe session %lu", session_id);
    return true;
}

MODULE_EXPORT void obs_module_unload()
{
    obs_frontend_remove_event_callback(frontend_event, nullptr);
    shutdown();
}
