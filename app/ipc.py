"""Restricted local IPC for the OBS companion bridge desktop worker.

Only one JSON command is accepted per connection. The elevated worker exposes
no filenames, executable arguments, shell commands, or arbitrary SDK requests.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
import sys
import threading
import time
from typing import Callable

MAX_LINE = 8192
APP_VERSION = "1.0.2"
INSTANCE_VERSION = APP_VERSION.replace(".", "_")
COMMANDS = frozenset({"status", "show", "start", "stop", "quit"})
INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ULONG_PTR), ("InternalHigh", ULONG_PTR),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


def _declare(dll, name, restype, *argtypes):
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


_declare(kernel, "GetCurrentProcess", wintypes.HANDLE)
_declare(kernel, "GetCurrentThread", wintypes.HANDLE)
_declare(kernel, "ProcessIdToSessionId", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_declare(kernel, "CreateEventW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_declare(kernel, "SetEvent", wintypes.BOOL, wintypes.HANDLE)
_declare(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_declare(kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
_declare(kernel, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
_declare(kernel, "WaitForMultipleObjects", wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD)
_declare(kernel, "CreateNamedPipeW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES))
_declare(kernel, "ConnectNamedPipe", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(OVERLAPPED))
_declare(kernel, "DisconnectNamedPipe", wintypes.BOOL, wintypes.HANDLE)
_declare(kernel, "GetNamedPipeClientSessionId", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
_declare(kernel, "GetNamedPipeServerSessionId", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
_declare(kernel, "GetNamedPipeServerProcessId", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
_declare(kernel, "ReadFile", wintypes.BOOL, wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED))
_declare(kernel, "WriteFile", wintypes.BOOL, wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED))
_declare(kernel, "GetOverlappedResult", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL)
_declare(kernel, "CancelIoEx", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(OVERLAPPED))
_declare(kernel, "CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
         wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
_declare(kernel, "WaitNamedPipeW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD)
_declare(kernel, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_declare(kernel, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
_declare(kernel, "CreateMutexW", wintypes.HANDLE, ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.LPCWSTR)
_declare(kernel, "OpenMutexW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_declare(advapi, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
_declare(advapi, "OpenThreadToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE))
_declare(advapi, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_declare(advapi, "ConvertSidToStringSidW", wintypes.BOOL, wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
_declare(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
         wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID)
_declare(advapi, "ImpersonateNamedPipeClient", wintypes.BOOL, wintypes.HANDLE)
_declare(advapi, "RevertToSelf", wintypes.BOOL)


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


class Handle:
    def __init__(self, value):
        if value in (None, 0, INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error())
        self.value = value

    def close(self):
        if self.value is not None:
            kernel.CloseHandle(self.value)
            self.value = None

    def __enter__(self):
        return self.value

    def __exit__(self, *_):
        self.close()


def current_session_id() -> int:
    session = wintypes.DWORD()
    _check(kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)))
    return session.value


def _token_sid(token) -> str:
    required = wintypes.DWORD()
    advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
    if not required.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(required.value)
    _check(advapi.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(required)))
    user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
    text = wintypes.LPWSTR()
    _check(advapi.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(text)))
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, wintypes.HANDLE))


def current_user_sid() -> str:
    token = wintypes.HANDLE()
    _check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
    with Handle(token.value) as handle:
        return _token_sid(handle)


class SecurityDescriptor:
    def __init__(self):
        self.sid = current_user_sid()
        # The medium label is intentional: a normal OBS process may request a
        # narrowly permitted command from this elevated desktop worker.
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{self.sid})S:(ML;;NW;;;ME)"
        self.pointer = wintypes.LPVOID()
        _check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(self.pointer), None))
        self.attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), self.pointer, False)

    def close(self):
        if self.pointer:
            kernel.LocalFree(self.pointer)
            self.pointer = wintypes.LPVOID()


def backend_pipe_name(session: int | None = None) -> str:
    return rf"\\.\pipe\OBS_MATE_BACKEND_{INSTANCE_VERSION}_S{current_session_id() if session is None else session}"


def _cancel_operation(pipe, operation):
    kernel.CancelIoEx(pipe, ctypes.byref(operation))
    # Keep OVERLAPPED storage alive until named-pipe cancellation completes.
    kernel.WaitForSingleObject(operation.hEvent, 0xFFFFFFFF)


def _finish_operation(pipe, operation, count, timeout: float | None, stop_event=None):
    handles = [operation.hEvent] if stop_event is None else [stop_event, operation.hEvent]
    array = (wintypes.HANDLE * len(handles))(*handles)
    milliseconds = 0xFFFFFFFF if timeout is None else max(1, int(timeout * 1000))
    result = kernel.WaitForMultipleObjects(len(handles), array, False, milliseconds)
    completed = WAIT_OBJECT_0 + len(handles) - 1
    if result != completed:
        _cancel_operation(pipe, operation)
        if result == WAIT_TIMEOUT:
            raise TimeoutError("Local pipe operation timed out")
        raise InterruptedError("Local pipe stopped")
    _check(kernel.GetOverlappedResult(pipe, ctypes.byref(operation), ctypes.byref(count), False))


def _pipe_io(pipe, *, write: bytes | None = None, size=MAX_LINE, timeout=3.0, stop_event=None):
    with Handle(kernel.CreateEventW(None, True, False, None)) as event:
        operation = OVERLAPPED()
        operation.hEvent = event
        count = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(write if write is not None else size)
        if write is None:
            ok = kernel.ReadFile(pipe, buffer, size, ctypes.byref(count), ctypes.byref(operation))
        else:
            ok = kernel.WriteFile(pipe, buffer, len(write), ctypes.byref(count), ctypes.byref(operation))
        if not ok:
            error = ctypes.get_last_error()
            if error != ERROR_IO_PENDING:
                raise ctypes.WinError(error)
            _finish_operation(pipe, operation, count, timeout, stop_event)
        if write is not None:
            if count.value != len(write):
                raise OSError("Incomplete local pipe write")
            return count.value
        return buffer.raw[:count.value]


def _read_line(pipe, timeout=3.0, stop_event=None) -> bytes:
    deadline = time.monotonic() + timeout
    received = bytearray()
    while len(received) < MAX_LINE:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local pipe line timed out")
        data = _pipe_io(pipe, size=MAX_LINE - len(received), timeout=remaining, stop_event=stop_event)
        if not data:
            raise EOFError("Local pipe closed before a complete line")
        received.extend(data)
        if b"\n" in received:
            line, extra = bytes(received).split(b"\n", 1)
            if extra:
                raise ValueError("Only one local IPC line is accepted")
            return line.rstrip(b"\r")
    raise ValueError("Local IPC line exceeds 8 KiB")


def _process_path(pid: int) -> str:
    with Handle(kernel.OpenProcess(0x1000, False, pid)) as process:
        length = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        _check(kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)))
        return os.path.normcase(os.path.abspath(buffer.value))


def _verify_server(pipe, expected_executable: str, session: int):
    server_session = wintypes.ULONG()
    server_pid = wintypes.ULONG()
    _check(kernel.GetNamedPipeServerSessionId(pipe, ctypes.byref(server_session)))
    _check(kernel.GetNamedPipeServerProcessId(pipe, ctypes.byref(server_pid)))
    if server_session.value != session:
        raise PermissionError("Local IPC server belongs to another Windows session")
    if _process_path(server_pid.value) != os.path.normcase(os.path.abspath(expected_executable)):
        raise PermissionError("Local IPC server executable identity differs")
    return server_pid.value


def rpc(command: str, timeout=3.0, expected_executable: str | None = None, session: int | None = None) -> dict:
    if command not in COMMANDS:
        raise ValueError("Unsupported local bridge command")
    session = current_session_id() if session is None else session
    name = backend_pipe_name(session)
    deadline = time.monotonic() + timeout
    handle = None
    while time.monotonic() < deadline:
        # Overlapped and SECURITY_IMPERSONATION are required by the restricted server.
        raw = kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40120000, None)
        if raw not in (None, 0, INVALID_HANDLE):
            handle = Handle(raw)
            break
        error = ctypes.get_last_error()
        if error not in (2, 231):
            raise ctypes.WinError(error)
        kernel.WaitNamedPipeW(name, 50)
        time.sleep(0.02)
    if handle is None:
        raise TimeoutError("Local bridge worker is unavailable")
    with handle as pipe:
        server_pid = _verify_server(pipe, expected_executable or sys.executable, session)
        if command == "show":
            # The menu-launched process may pass its foreground permission to
            # the verified worker, so an existing panel can appear above OBS.
            window_api = ctypes.WinDLL("user32", use_last_error=True)
            window_api.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
            window_api.AllowSetForegroundWindow.restype = wintypes.BOOL
            window_api.AllowSetForegroundWindow(server_pid)
        request = json.dumps({"command": command}, separators=(",", ":")).encode("ascii") + b"\n"
        _pipe_io(pipe, write=request, timeout=max(0.05, deadline - time.monotonic()))
        response = _read_line(pipe, timeout=max(0.05, deadline - time.monotonic()))
        value = json.loads(response.decode("utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
            raise ValueError("Invalid local IPC response")
        return value


class SingleInstance:
    def __init__(self, preview=False):
        self.security = SecurityDescriptor()
        suffix = "_PREVIEW" if preview else ""
        name = f"Local\\OBS_MATE_WORKER_{INSTANCE_VERSION}_S{current_session_id()}{suffix}"
        ctypes.set_last_error(0)
        raw = kernel.CreateMutexW(ctypes.byref(self.security.attributes), False, name)
        error = ctypes.get_last_error()
        self.handle = Handle(raw)
        self.already_running = error == ERROR_ALREADY_EXISTS

    def close(self):
        self.handle.close()
        self.security.close()


def worker_instance_exists() -> bool:
    name = f"Local\\OBS_MATE_WORKER_{INSTANCE_VERSION}_S{current_session_id()}"
    raw = kernel.OpenMutexW(0x100000, False, name)  # SYNCHRONIZE only
    if raw in (None, 0, INVALID_HANDLE):
        if ctypes.get_last_error() == 2:
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    Handle(raw).close()
    return True


class PipeServer:
    def __init__(self, handler: Callable[[str], dict]):
        self.handler = handler
        self.session = current_session_id()
        self.security = SecurityDescriptor()
        self.stop_event = Handle(kernel.CreateEventW(None, True, False, None))
        self.thread = threading.Thread(target=self._run, name="bridge-local-ipc", daemon=True)
        self.ready = threading.Event()
        self.startup_error: Exception | None = None

    def start(self):
        self.thread.start()
        if not self.ready.wait(3.0):
            raise TimeoutError("Local IPC server did not initialize")
        if self.startup_error:
            raise self.startup_error

    def stop(self):
        kernel.SetEvent(self.stop_event.value)
        self.thread.join(4.0)
        if self.thread.is_alive():
            raise TimeoutError("Local IPC server did not stop")
        self.stop_event.close()
        self.security.close()

    def _allowed_client(self, pipe) -> bool:
        client_session = wintypes.ULONG()
        if not kernel.GetNamedPipeClientSessionId(pipe, ctypes.byref(client_session)) or client_session.value != self.session:
            return False
        if not advapi.ImpersonateNamedPipeClient(pipe):
            return False
        token = wintypes.HANDLE()
        allowed = False
        reverted = False
        try:
            if not advapi.OpenThreadToken(kernel.GetCurrentThread(), 8, True, ctypes.byref(token)):
                allowed = False
            else:
                with Handle(token.value) as handle:
                    allowed = _token_sid(handle) == self.security.sid
        finally:
            reverted = bool(advapi.RevertToSelf())
            if not reverted:
                kernel.SetEvent(self.stop_event.value)
                logging.error("Local IPC failed to leave client impersonation; server stopped")
        return allowed and reverted

    def _dispatch_line(self, line: bytes) -> dict:
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return {"ok": False, "error": "INVALID_REQUEST"}
        if (not isinstance(message, dict) or set(message) != {"command"}
                or not isinstance(message["command"], str)
                or message["command"] not in COMMANDS):
            return {"ok": False, "error": "INVALID_COMMAND"}
        try:
            return self.handler(message["command"])
        except Exception as error:
            # A single request must not end the listener or expose exception
            # details from the elevated worker to an unprivileged client.
            logging.error("Local IPC handler failed (%s)", type(error).__name__)
            return {"ok": False, "error": "WORKER_ERROR"}

    @staticmethod
    def _encode_response(response) -> bytes:
        try:
            if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                raise TypeError("Invalid worker response")
            data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except Exception as error:
            logging.error("Local IPC response encoding failed (%s)", type(error).__name__)
            return b'{"ok":false,"error":"WORKER_ERROR"}\n'
        if len(data) > MAX_LINE:
            return b'{"ok":false,"error":"RESPONSE_TOO_LARGE"}\n'
        return data

    def _run(self):
        try:
            while kernel.WaitForSingleObject(self.stop_event.value, 0) == WAIT_TIMEOUT:
                # Duplex, overlapped, first instance; byte pipe, local clients only.
                raw = kernel.CreateNamedPipeW(backend_pipe_name(self.session), 0x40080003, 0x8,
                    1, MAX_LINE, MAX_LINE, 0, ctypes.byref(self.security.attributes))
                with Handle(raw) as pipe:
                    self.ready.set()
                    with Handle(kernel.CreateEventW(None, True, False, None)) as event:
                        operation = OVERLAPPED()
                        operation.hEvent = event
                        ok = kernel.ConnectNamedPipe(pipe, ctypes.byref(operation))
                        if not ok:
                            error = ctypes.get_last_error()
                            if error != ERROR_PIPE_CONNECTED:
                                if error != ERROR_IO_PENDING:
                                    raise ctypes.WinError(error)
                                count = wintypes.DWORD()
                                _finish_operation(pipe, operation, count, None, self.stop_event.value)
                    try:
                        try:
                            line = _read_line(pipe, stop_event=self.stop_event.value)
                        except ValueError:
                            response = {"ok": False, "error": "INVALID_REQUEST"}
                        else:
                            try:
                                allowed = self._allowed_client(pipe)
                            except Exception as error:
                                logging.error("Local IPC client verification failed (%s)", type(error).__name__)
                                allowed = False
                            if not allowed:
                                response = {"ok": False, "error": "CLIENT_DENIED"}
                            else:
                                response = self._dispatch_line(line)
                        data = self._encode_response(response)
                        _pipe_io(pipe, write=data, stop_event=self.stop_event.value)
                        try:
                            # Wait briefly for the reader to close rather than discard
                            # an unread response or use unbounded FlushFileBuffers.
                            _pipe_io(pipe, size=1, timeout=0.5, stop_event=self.stop_event.value)
                        except (OSError, TimeoutError, InterruptedError):
                            pass
                    except (OSError, TimeoutError, EOFError, InterruptedError):
                        pass
                    finally:
                        kernel.DisconnectNamedPipe(pipe)
        except InterruptedError:
            pass
        except Exception as error:
            self.startup_error = error
            logging.exception("Local IPC server stopped unexpectedly")
        finally:
            self.ready.set()
