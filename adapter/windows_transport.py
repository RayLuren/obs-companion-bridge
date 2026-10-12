"""Exact-version Windows discovery and a closed SDK call allowlist.

No named pipe, process, or SDK access occurs merely by importing this module.
"""
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import ntpath
import os
import uuid

EXPECTED_EXE = r"D:\webcast_mate\13.1.1.502547155\直播伴侣.exe"
SOURCE_NAME = "OBS 本机桥接（Codex）"
SOURCE_URL = "http://127.0.0.1:18081/live?port=19360&app=obs&stream=main"


def _path(value):
    return ntpath.normcase(ntpath.normpath(value))


def discover_reviewed_processes():
    if os.name != "nt":
        return []
    kernel = C.WinDLL("kernel32", use_last_error=True)
    class Entry(C.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                    ("pid", W.DWORD), ("heap", C.c_size_t),
                    ("module", W.DWORD), ("threads", W.DWORD),
                    ("parent", W.DWORD), ("priority", W.LONG),
                    ("flags", W.DWORD), ("exe", W.WCHAR * 260)]
    def bind(name, args, result=W.BOOL):
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
        return fn
    snapshot = bind("CreateToolhelp32Snapshot", [W.DWORD, W.DWORD], W.HANDLE)
    first = bind("Process32FirstW", [W.HANDLE, C.POINTER(Entry)])
    next_entry = bind("Process32NextW", [W.HANDLE, C.POINTER(Entry)])
    close = bind("CloseHandle", [W.HANDLE])
    open_process = bind("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
    image = bind("QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)])
    times = bind("GetProcessTimes", [W.HANDLE, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p])
    handle = snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        raise RuntimeError("无法读取直播伴侣进程列表")
    candidates = []
    try:
        entry = Entry()
        entry.dwSize = C.sizeof(entry)
        found = first(handle, C.byref(entry))
        while found:
            if entry.exe.lower() == "直播伴侣.exe":
                process = open_process(0x1000, False, entry.pid)
                if process:
                    try:
                        buffer = C.create_unicode_buffer(32768)
                        size = W.DWORD(len(buffer))
                        stamps = [C.c_ulonglong() for _ in range(4)]
                        if (image(process, 0, buffer, C.byref(size)) and
                                _path(buffer.value) == _path(EXPECTED_EXE) and
                                times(process, *(C.byref(stamp) for stamp in stamps))):
                            created = stamps[0].value
                            utc = (dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) +
                                   dt.timedelta(microseconds=created // 10)).isoformat()
                            candidates.append({"server_pid": int(entry.pid),
                                               "server_start_filetime": created,
                                               "server_start_utc": utc,
                                               "server_path": buffer.value,
                                               "_parent": int(entry.parent)})
                    finally:
                        close(process)
            found = next_entry(handle, C.byref(entry))
    finally:
        close(handle)
    pids = {item["server_pid"] for item in candidates}
    return [{key: value for key, value in item.items() if key != "_parent"}
            for item in candidates if item["_parent"] not in pids]


class ReviewedTransport:
    def __init__(self, *, expected_pid):
        from . import reviewed_pipe_client as api
        self.api = api
        class Client(api.BridgeClient):
            @staticmethod
            def _readonly(method, args):
                if method == "getAudioData":
                    return len(args) == 1 and isinstance(args[0], str)
                if method == "CANVAS_CALL" and len(args) == 1 and isinstance(args[0], dict):
                    spec = args[0]
                    if (spec.get("type") == "GET" and type(spec.get("index")) is int and
                            spec["index"] in {0, 1} and spec.get("argArray") == [] and
                            spec.get("path") in [["sourceStore", "topSource"],
                              ["stage", "baseEnv", "sourceStore", "topSource"],
                              ["sourceStore", "source"],
                              ["stage", "baseEnv", "sourceStore", "source"]]):
                        return True
                return api.BridgeClient._readonly(method, args)
        self.client = Client(expected_pid=expected_pid, timeout=5, allow_mutations=True)
        # CANVAS_CALL remains private to this facade's exact scoped argument checks.
        self.client.write_methods = {"addSource", "sourceTileFill", "CANVAS_CALL"}

    def identity(self):
        return self.client.identity()

    def alive(self):
        return self.api.Wait(self.client.process_handle, 0) == 258

    def verify_identity(self, expected):
        if not self.alive() or self.client.failure:
            raise RuntimeError("原直播伴侣连接已失效，本次会话不重连")
        buffer = C.create_unicode_buffer(32768)
        size = W.DWORD(len(buffer))
        owner = W.ULONG()
        stamps = [C.c_ulonglong() for _ in range(4)]
        if (not self.api.GetImage(self.client.process_handle, 0, buffer, C.byref(size)) or
                not self.api.GetServerPid(self.client.handle, C.byref(owner)) or
                not self.api.GetProcessTimes(self.client.process_handle, *(C.byref(x) for x in stamps))):
            raise RuntimeError("无法重新核验直播伴侣进程身份")
        if (_path(buffer.value) != _path(EXPECTED_EXE) or
                owner.value != expected.get("server_pid") or
                stamps[0].value != expected.get("server_start_filetime") or
                _path(buffer.value) != _path(expected.get("server_path", ""))):
            raise RuntimeError("直播伴侣版本或进程身份改变，已停止操作")

    @staticmethod
    def _allowed(method, args):
        def source_id(value):
            if not isinstance(value, str):
                return False
            try:
                return str(uuid.UUID(value)) == value
            except (ValueError, TypeError):
                return False
        if not isinstance(args, list):
            return False
        if method in {"getUseNewSettings", "getCanvasScene"}:
            return args == []
        if method in {"getSourceData", "getAudioData", "sourceTileFill"}:
            return len(args) == 1 and source_id(args[0])
        if method == "addSource":
            if (len(args) != 3 or not isinstance(args[0], dict) or not source_id(args[1]) or
                    not isinstance(args[2], dict) or set(args[2]) != {"shouldCreate"} or
                    args[2]["shouldCreate"] is not True):
                return False
            params = args[0]
            if (set(params) != {"name", "type", "viewIndex", "secondSource", "visible", "alwaysTop", "liveScene", "payload"} or
                    params["name"] != SOURCE_NAME or params["type"] != "onlineMedia" or
                    type(params["viewIndex"]) is not int or params["viewIndex"] not in {0, 1} or
                    params["secondSource"] is not False or params["visible"] is not True or
                    params["alwaysTop"] is not False or params["liveScene"] != "" or
                    not isinstance(params["payload"], dict)):
                return False
            payload = params["payload"]
            audio = payload.get("audio")
            setting = audio.get("setting") if isinstance(audio, dict) else None
            return (set(payload) == {"url", "hardwareDecode", "bufferSize", "timeout", "activeRestartPlay",
                                     "playEndNoDisplay", "linearAlpha", "colorRange", "ffmpegOptions", "audio"} and
                    payload.get("url") == SOURCE_URL and payload.get("hardwareDecode") is False and
                    type(payload.get("bufferSize")) is int and payload["bufferSize"] == 2 and
                    type(payload.get("timeout")) is int and payload["timeout"] == 10 and
                    payload.get("activeRestartPlay") is False and payload.get("playEndNoDisplay") is False and
                    payload.get("linearAlpha") is False and type(payload.get("colorRange")) is int and
                    payload["colorRange"] == 3 and payload.get("ffmpegOptions") == "" and
                    isinstance(audio, dict) and set(audio) == {"setting"} and
                    isinstance(setting, dict) and
                    set(setting) == {"volume", "balancing", "monitorType", "syncOffset", "isDownmixMono"} and
                    type(setting["volume"]) is int and setting["volume"] in {0, 1} and
                    type(setting["monitorType"]) is int and setting["monitorType"] in {0, 1} and
                    type(setting["balancing"]) in {int, float} and setting["balancing"] == 0.5 and
                    type(setting["syncOffset"]) in {int, float} and setting["syncOffset"] == 0 and
                    setting["isDownmixMono"] is False)
        if method != "CANVAS_CALL" or len(args) != 1 or not isinstance(args[0], dict):
            return False
        spec = args[0]
        if (set(spec) != {"type", "index", "path", "argArray"} or
                type(spec["index"]) is not int or spec["index"] not in {0, 1}):
            return False
        path, params = spec["path"], spec["argArray"]
        if not isinstance(path, list) or not isinstance(params, list):
            return False
        prefixes = [["sourceStore"], ["stage", "baseEnv", "sourceStore"]]
        if spec["type"] == "GET":
            return path in [prefix + [field] for prefix in prefixes
                            for field in ("source", "topSource")] and params == []
        if spec["type"] != "APPLY":
            return False
        if path in [prefix + ["getAllSource"] for prefix in prefixes]:
            return params == []
        if path in [prefix + ["toggleVisible"] for prefix in prefixes]:
            return len(params) == 2 and source_id(params[0]) and type(params[1]) is bool
        if path in [prefix + ["setAudioData"] for prefix in prefixes]:
            if (len(params) != 5 or not source_id(params[0]) or
                    params[2:] != [True, True, False] or
                    any(type(x) is not bool for x in params[2:]) or
                    not isinstance(params[1], dict) or set(params[1]) != {"setting"}):
                return False
            setting = params[1]["setting"]
            return (isinstance(setting, dict) and set(setting) == {"volume", "monitorType"} and
                    type(setting["volume"]) is int and setting["volume"] in {0, 1} and
                    type(setting["monitorType"]) is int and setting["monitorType"] in {0, 1})
        return False

    def call(self, method, args):
        if not self._allowed(method, args):
            raise RuntimeError("该操作不在已审查的接口范围内")
        self.verify_identity(self.identity())
        return self.client.call(method, args)

    def close(self):
        if self.alive():
            raise RuntimeError("伴侣仍在运行，不能关闭已建立的管道连接")
        self.client.close()


def make_transport(*, expected_pid):
    return ReviewedTransport(expected_pid=expected_pid)
