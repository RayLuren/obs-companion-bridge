"""Reviewed Companion IPC transport library.

BridgeClient preserves the existing protocol and read-only default.
The desktop adapter supplies its stricter closed method and argument policy.
No HTTP server or standalone diagnostic CLI is exposed.
"""
import codecs
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import json
import os
import threading
import time
import uuid

PIPE = r"\\.\pipe\mate-mediasdk-pipe"
MARKER = "#socketWrite"
CHANNEL = "MEDIASDK_CALL_FROM_RENDERER_ASYNC"
ROOT = r"D:\webcast_mate"
WRITE_METHODS = {"addSource","updateSource","removeSource","startNginx","sourceTileFill"}
K = C.WinDLL("kernel32", use_last_error=True)
INVALID_HANDLE = C.c_void_p(-1).value
_KEEP_CANCELLED_IO = []

class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", W.DWORD), ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]

def _api(name, args, result=W.BOOL):
    fn = getattr(K, name); fn.argtypes = args; fn.restype = result; return fn

CreateFile = _api("CreateFileW", [W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE], W.HANDLE)
CloseHandle = _api("CloseHandle", [W.HANDLE])
CreateEvent = _api("CreateEventW", [C.c_void_p,W.BOOL,W.BOOL,W.LPCWSTR], W.HANDLE)
Wait = _api("WaitForSingleObject", [W.HANDLE,W.DWORD], W.DWORD)
Cancel = _api("CancelIoEx", [W.HANDLE,C.c_void_p])
ReadFile = _api("ReadFile", [W.HANDLE,C.c_void_p,W.DWORD,C.c_void_p,C.c_void_p])
WriteFile = _api("WriteFile", [W.HANDLE,C.c_void_p,W.DWORD,C.c_void_p,C.c_void_p])
GetResult = _api("GetOverlappedResult", [W.HANDLE,C.c_void_p,C.c_void_p,W.BOOL])
GetServerPid = _api("GetNamedPipeServerProcessId", [W.HANDLE,C.c_void_p])
OpenProcess = _api("OpenProcess", [W.DWORD,W.BOOL,W.DWORD], W.HANDLE)
GetImage = _api("QueryFullProcessImageNameW", [W.HANDLE,W.DWORD,W.LPWSTR,C.c_void_p])
GetProcessTimes = _api("GetProcessTimes", [W.HANDLE,C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p])

class BridgeError(RuntimeError): pass
class BridgeTimeout(BridgeError): pass

class BridgeClient:
    def __init__(self, *, expected_pid=None, timeout=3.0, allow_mutations=False):
        self.timeout = min(max(float(timeout), 0.2), 10.0)
        self.allow_mutations = bool(allow_mutations)
        self.write_methods = set(WRITE_METHODS) if allow_mutations else set()
        self.handle = None; self.process_handle = None
        self.closed = threading.Event(); self.condition = threading.Condition()
        self.replies = {}; self.pending = set(); self.failure = None
        self.write_lock = threading.Lock()
        deadline = time.monotonic() + self.timeout
        while True:
            h = CreateFile(PIPE, 0xC0000000, 0, None, 3, 0x40000000, None)
            if h != INVALID_HANDLE: self.handle = h; break
            error = C.get_last_error()
            if error != 231 or time.monotonic() >= deadline:
                raise BridgeError(f"Open existing pipe failed, Win32 {error}")
            time.sleep(0.05)
        try:
            pid = W.ULONG()
            if not GetServerPid(self.handle, C.byref(pid)):
                raise BridgeError("Cannot verify actual named-pipe server PID")
            if expected_pid is not None and pid.value != int(expected_pid):
                raise BridgeError("Named-pipe server PID changed")
            self.server_pid = pid.value
            self.process_handle = OpenProcess(0x00101000, False, pid.value)
            if not self.process_handle: raise BridgeError("Cannot open server for identity verification")
            path = C.create_unicode_buffer(32768); size = W.DWORD(len(path))
            if not GetImage(self.process_handle, 0, path, C.byref(size)):
                raise BridgeError("Cannot verify server executable path")
            self.server_path = os.path.normpath(path.value)
            prefix = os.path.normcase(os.path.abspath(ROOT)) + os.sep
            if not os.path.normcase(self.server_path).startswith(prefix):
                raise BridgeError("Pipe server is outside the Companion installation")
            if os.path.basename(self.server_path).lower() not in {"直播伴侣.exe", "webcast_mate.exe"}:
                raise BridgeError("Unexpected pipe-server executable")
            times = [C.c_ulonglong() for _ in range(4)]
            if not GetProcessTimes(self.process_handle, *(C.byref(x) for x in times)):
                raise BridgeError("Cannot verify server start time")
            self.server_start_filetime = times[0].value
            self.server_start_utc = (dt.datetime(1601,1,1,tzinfo=dt.timezone.utc) +
                                     dt.timedelta(microseconds=times[0].value // 10)).isoformat()
            if Wait(self.process_handle, 0) != 258: raise BridgeError("Pipe server has exited")
            self.reader = threading.Thread(target=self._read_loop, name="CompanionReadOnlyPipe", daemon=True)
            self.reader.start()
        except Exception:
            if self.handle: CloseHandle(self.handle)
            if self.process_handle: CloseHandle(self.process_handle)
            raise

    def identity(self):
        return {"server_pid": self.server_pid, "server_path": self.server_path,
                "server_start_utc": self.server_start_utc,
                "server_start_filetime": self.server_start_filetime}

    def _io(self, *, payload=None, timeout=0.5):
        size = len(payload) if payload is not None else 8192
        buf = C.create_string_buffer(payload if payload is not None else size)
        event = CreateEvent(None, True, False, None)
        if not event: raise BridgeError("CreateEvent failed")
        ov = OVERLAPPED(); ov.hEvent = event; count = W.DWORD(); retained = False
        try:
            fn = WriteFile if payload is not None else ReadFile
            ok = fn(self.handle, buf, size, C.byref(count), C.byref(ov))
            if not ok and C.get_last_error() != 997:
                raise BridgeError(f"Pipe I/O failed, Win32 {C.get_last_error()}")
            status = Wait(event, max(1, int(timeout * 1000)))
            if status != 0:
                Cancel(self.handle, C.byref(ov))
                if Wait(event, 1000) != 0:
                    _KEEP_CANCELLED_IO.append((buf, ov, event)); retained = True
                raise BridgeTimeout("Pipe I/O deadline exceeded")
            if not GetResult(self.handle, C.byref(ov), C.byref(count), False):
                raise BridgeError(f"Pipe completion failed, Win32 {C.get_last_error()}")
            if payload is not None and count.value != len(payload):
                raise BridgeError("Partial request write; never retry automatically")
            return bytes(buf.raw[:count.value])
        finally:
            if not retained: CloseHandle(event)

    def _read_loop(self):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        parser = json.JSONDecoder(); text = ""; inside_frame = False
        try:
            while not self.closed.is_set():
                try: chunk = self._io(timeout=0.5)
                except BridgeTimeout: continue
                if not chunk: raise BridgeError("Pipe peer disconnected")
                text += decoder.decode(chunk)
                if len(text) > 2 * 1024 * 1024: raise BridgeError("Response exceeded memory limit")
                while text:
                    if not inside_frame:
                        if len(text) < len(MARKER): break
                        if not text.startswith(MARKER): raise BridgeError("Unexpected response framing")
                        text = text[len(MARKER):]; inside_frame = True
                    try: item, end = parser.raw_decode(text)
                    except json.JSONDecodeError: break
                    text = text[end:]; inside_frame = False
                    if not isinstance(item, dict): raise BridgeError("Unexpected response object")
                    if item.get("type") == "invoke-res" and item.get("channel") == CHANNEL:
                        call_id = item.get("callId")
                        with self.condition:
                            if call_id in self.pending:
                                self.replies[call_id] = item.get("data")
                                self.condition.notify_all()
                    # Broadcast events and unmatched replies are drained and discarded.
        except Exception as exc:
            with self.condition:
                self.failure = str(exc); self.condition.notify_all()

    @staticmethod
    def _readonly(method, args):
        if method in {"getVideoFormats", "getUseNewSettings", "getCanvasScene"}: return args == []
        if method in {"getSourceData", "getSourceViewIndex"}: return len(args) == 1 and isinstance(args[0], (str,int))
        if method != "CANVAS_CALL" or len(args) != 1 or not isinstance(args[0], dict): return False
        spec = args[0]; path = spec.get("path"); prefix = ["stage","baseEnv","sourceStore"]
        if not isinstance(spec.get("index"),int) or spec["index"] not in range(3): return False
        if spec.get("type") == "APPLY":
            return path in [prefix+["getAllSource"], ["sourceStore","getAllSource"]] and spec.get("argArray",[]) == []
        paths = [prefix+["source"], prefix+["settings","resolution"],
                 ["sourceStore","source"], ["sourceStore","settings","resolution"]]
        return spec.get("type") == "GET" and path in paths and spec.get("argArray",[]) == []

    def call(self, method, args=None, *, timeout=None):
        args = [] if args is None else args
        if not isinstance(args, list): raise BridgeError("args must be an array")
        readonly = self._readonly(method, args)
        if not readonly and not (self.allow_mutations and method in self.write_methods):
            raise BridgeError("Call blocked by default read-only policy")
        if self.closed.is_set() or self.failure: raise BridgeError("Connection is unavailable")
        if Wait(self.process_handle, 0) != 258: raise BridgeError("Verified server has exited")
        call_id = str(uuid.uuid4())
        body = json.dumps({"channel":CHANNEL,"data":[method,args],"callId":call_id,"type":"invoke"},
                          separators=(",", ":"), ensure_ascii=True)
        if MARKER in body or len(body) > 4096: raise BridgeError("Unsafe delimiter or oversized request")
        with self.condition: self.pending.add(call_id)
        limit = self.timeout if timeout is None else min(max(float(timeout),0.2),10.0)
        deadline = time.monotonic() + limit
        try:
            with self.write_lock: self._io(payload=(MARKER+body).encode("utf-8"), timeout=limit)
            with self.condition:
                while call_id not in self.replies:
                    if self.failure: raise BridgeError(self.failure)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise BridgeTimeout("Getter response deadline exceeded; no retry")
                    self.condition.wait(remaining)
                return self.replies.pop(call_id)
        finally:
            with self.condition: self.pending.discard(call_id)

    def close(self):
        if self.closed.is_set(): return
        self.closed.set(); Cancel(self.handle, None); self.reader.join(2)
        CloseHandle(self.handle); CloseHandle(self.process_handle)
