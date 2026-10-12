"""One managed local bridge; GUI lifetime is deliberately independent."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as W
from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.request

ROOT = Path(r"D:\OBS伴侣桥接")
TOOL = ROOT / "outputs" / "OBS伴侣本机桥接"
APP = ROOT / "app"
LOCAL_SERVER = "rtmp://127.0.0.1:19360/obs"


def session_id():
    value = W.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(value)):
        raise OSError("Cannot identify Windows session")
    return value.value


def process_path(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError("Cannot verify OBS process")
    try:
        text = ctypes.create_unicode_buffer(32768)
        length = W.DWORD(len(text))
        if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(length)):
            raise OSError("Cannot verify OBS executable")
        return Path(text.value)
    finally:
        kernel.CloseHandle(handle)


class ObsClient:
    def __init__(self):
        self.pipe = rf"\\.\pipe\OBS_MATE_BRIDGE_S{session_id()}"

    def request(self, command="STATUS"):
        import ipc
        if command not in {"STATUS", "STOP_LOCAL", "PING"}:
            raise ValueError("Unknown OBS command")
        handle = None
        try:
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                raw = ipc.kernel.CreateFileW(self.pipe, 0xC0000000, 0, None, 3, 0x40120000, None)
                if raw not in (None, 0, ipc.INVALID_HANDLE):
                    handle = ipc.Handle(raw)
                    break
                if ctypes.get_last_error() not in (2, 231):
                    return {"ok": False, "available": True, "error": "OBS_PIPE_ACCESS_FAILED"}
                ipc.kernel.WaitNamedPipeW(self.pipe, 40)
                time.sleep(0.01)
            if handle is None:
                return {"ok": False, "available": False, "error": "OBS_PLUGIN_UNAVAILABLE"}
            owner = ipc._verify_server(handle.value, r"D:\obs-studio\bin\64bit\obs64.exe", session_id())
            ipc._pipe_io(handle.value, write=(command + "\n").encode("ascii"), timeout=2.0)
            response = json.loads(ipc._read_line(handle.value, timeout=2.5).decode("utf-8"))
            if (not isinstance(response, dict) or response.get("pid") != owner or
                    response.get("session_id") != session_id() or response.get("protocol") != 1 or
                    response.get("request") != command or not isinstance(response.get("ok"), bool)):
                return {"ok": False, "available": True, "error": "OBS_RESPONSE_MISMATCH"}
            response["available"] = True
            return response
        except TimeoutError:
            return {"ok": False, "available": True, "error": "OBS_IO_TIMEOUT"}
        except (OSError, ValueError, RecursionError):
            return {"ok": False, "available": True, "error": "OBS_RESPONSE_UNAVAILABLE"}
        finally:
            if handle is not None:
                handle.close()


class LocalServer:
    def __init__(self):
        self.script = TOOL / "rtmp_server" / "server.ps1"

    def command(self, mode):
        if mode not in {"Start", "Stop", "Status"}:
            raise ValueError("Unknown server operation")
        result = subprocess.run([
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), "-Mode", mode],
            capture_output=True, timeout=18, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            if mode == "Stop":
                observed = self.status()
                if observed.get("ok") and observed.get("running") is False and not observed.get("listeners"):
                    return {"ok": True, "stopped": True, "exit_observed_after_deadline": True}
            # Never log uncontrolled child stdout/stderr.
            return {"ok": False, "error": "LOCAL_SERVER_" + mode.upper() + "_FAILED"}
        try:
            data = json.loads(result.stdout.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            return {"ok": False, "error": "LOCAL_SERVER_STATUS_UNAVAILABLE"}
        expected = {"Start": "started", "Stop": "stopped", "Status": "running"}[mode]
        if not isinstance(data, dict) or not isinstance(data.get(expected), bool):
            return {"ok": False, "error": "LOCAL_SERVER_RESPONSE_UNAVAILABLE"}
        data["ok"] = mode == "Status" or data[expected]
        return data

    def start(self):
        return self.command("Start")

    def stop(self):
        return self.command("Stop")

    def status(self):
        return self.command("Status")


def read_stream():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open("http://127.0.0.1:18081/stat", timeout=2) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            return {"ok": False, "publishing": False}
        data = json.loads(raw)
        matches = [stream for server in data["http-flv"]["servers"] if server["port"] == 19360
                   for app in server.get("applications", []) if app["name"] == "obs"
                   for stream in app.get("live", {}).get("streams", []) if stream["name"] == "main"]
        if not matches:
            return {"ok": True, "publishing": False}
        if len(matches) != 1:
            return {"ok": False, "publishing": False}
        stream = matches[0]; meta = stream.get("meta", {})
        return {"ok": True, "publishing": stream.get("publishing") is True,
                "bytes_in": stream.get("bytes_in", 0), "time": stream.get("time", 0),
                "nginx_pid": data["http-flv"]["pid"],
                "h264": meta.get("video", {}).get("codec") == "H264",
                "aac": meta.get("audio", {}).get("codec") == "AAC"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": False, "publishing": False}


def safe_text(text):
    text = str(text)
    text = re.sub(r'''(?i)(["']?(?:memory_token|authorization|cookie|signature|token)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r"\1[filtered]", text)
    text = re.sub(r"(?i)(?:rtmps?|https?)://[^\s]+", "[endpoint]", text)
    return text[:1000]


class BridgeEngine:
    def __init__(self, adapter=None, server=None, obs=None, stream_reader=None, log_dir=None):
        if adapter is None:
            from adapter.bridge_adapter import BridgeAdapter
            adapter = BridgeAdapter(APP / "runtime", legacy_manifest=TOOL / "controller" / "bridge-manifest.json")
        self.adapter = adapter
        self.server = server or LocalServer()
        self.obs = obs or ObsClient()
        self.stream_reader = stream_reader or read_stream
        self.lock = threading.RLock()
        self.recent = deque(maxlen=15)
        self.log = logging.getLogger("obs_mate_bridge_" + str(id(self)))
        self.log.setLevel(logging.INFO)
        if log_dir is not False:
            directory = Path(log_dir or ROOT / "logs"); directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(directory / "bridge.log", maxBytes=1024*1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.log.addHandler(handler)
        self.desired = False
        self.exit_requested = False
        self.server_ready = False
        self.stopping = False
        self.previous_stream = None
        self.last_ensure = 0.0
        self.last_server_check = 0.0
        self.last_adapter = {}
        self.adapter_owner = None
        self.obs_stop_requested = False
        self.obs_stop_unknown = False
        self.stop_completed = False
        self._snapshot = {"state": "stopped", "reason_zh": "桥接已停止", "desired_running": False,
                          "obs": {}, "companion": {}, "server": {}, "can_exit": False}

    def note(self, message):
        clean = safe_text(message)
        with self.lock:
            self.recent.append(time.strftime("%H:%M:%S ") + clean)
            self.log.info(clean)

    def publish(self, state, reason, **updates):
        with self.lock:
            if state != self._snapshot["state"] or reason != self._snapshot["reason_zh"]:
                self.note(reason)
            self._snapshot.update(state=state, reason_zh=reason, desired_running=self.desired, **updates)

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self._snapshot, ensure_ascii=False))
            result["recent_logs"] = list(self.recent)
            return result

    def start(self):
        with self.lock:
            if self.exit_requested:
                return
            if self.desired:
                return
            self.desired = True
            self.stopping = False
            self.stop_completed = False
            self.last_ensure = 0; self.previous_stream = None
            self.obs_stop_requested = False; self.obs_stop_unknown = False
            self.publish("starting", "正在准备桥接", can_exit=False)

    def stop(self):
        with self.lock:
            if not self.desired and not self.stopping and self.stop_completed:
                return
            self.desired = False
            self.stopping = True
            # An explicit user retry may clear an unknown STOP_LOCAL result.
            self.obs_stop_unknown = False
            self.publish("stopping", "正在停止本机桥接")

    def request_exit(self):
        with self.lock:
            self.exit_requested = True
            self.stop()

    def tick(self):
        # Only the worker thread calls tick; UI commands change the requested state.
        try:
            obs = self.obs.request("STATUS")
            self.publish(self._snapshot["state"], self._snapshot["reason_zh"], obs=obs)
            if not self.desired:
                return self._stop_tick(obs)
            now = time.monotonic()
            if self.server_ready and now - self.last_server_check >= 6:
                status = self.server.status()
                self.last_server_check = now
                if not status.get("ok"):
                    return self.publish("attention", "本机中转状态尚未确认，已保留现有连接", server=status)
                self.server_ready = bool(status.get("running"))
                self.publish(self._snapshot["state"], self._snapshot["reason_zh"], server=status)
            if not self.server_ready:
                result = self.server.start()
                if not result.get("ok"):
                    return self.publish("attention", "本机中转启动失败，请查看诊断日志", server=result)
                self.server_ready = True
                self.last_server_check = time.monotonic()
                self.previous_stream = None
                self.publish("waiting", "本机中转已就绪，等待 OBS 输出", server={"running": True})
            stream = self.stream_reader()
            companion = self.adapter.discover()
            self.publish(self._snapshot["state"], self._snapshot["reason_zh"], companion=companion, stream=stream)
            if not companion.get("ok"):
                return self.publish("waiting_companion", companion.get("reason_zh", "等待直播伴侣启动"))
            if companion.get("state") == "waiting_companion":
                return self.publish("waiting_companion", companion.get("reason_zh", "等待直播伴侣启动"))
            connected = self.adapter.connect()
            if not connected.get("ok"):
                return self.publish("attention", connected.get("reason_zh", "伴侣连接需要处理"), companion=connected)
            if connected.get("state") == "waiting_companion":
                return self.publish("waiting_companion", connected.get("reason_zh", "等待直播伴侣启动"), companion=connected)
            owner = connected.get("owner", companion.get("owner"))
            if owner != self.adapter_owner:
                self.adapter_owner = owner; self.last_ensure = 0; self.last_adapter = {}
            if obs.get("available") and obs.get("ok") and not obs.get("service_matches"):
                return self.publish("attention", "OBS 当前配置不是本机桥接，请选择伴侣桥接配置")
            if not stream.get("publishing"):
                self.previous_stream = None
                if not obs.get("available"):
                    return self.publish("waiting_obs", "等待 OBS 打开并加载伴侣桥接入口")
                if not obs.get("ok"):
                    return self.publish("attention", "OBS 本机入口暂未就绪，请稍候")
                return self.publish("waiting_output", "等待 OBS 输出，请在 OBS 点击“开始直播”")
            if not stream.get("h264") or not stream.get("aac"):
                return self.publish("attention", "OBS 输出需要 H264 视频和 AAC 音频")
            previous = self.previous_stream; self.previous_stream = stream
            if (not previous or previous.get("nginx_pid") != stream.get("nginx_pid") or
                    stream.get("bytes_in", 0) <= previous.get("bytes_in", 0)):
                return self.publish("waiting_data", "正在确认 OBS 音视频持续传输")
            if time.monotonic() - self.last_ensure > 3:
                result = self.adapter.ensure_existing_source(enabled=True, audio=True)
                self.last_ensure = time.monotonic(); self.last_adapter = result
            result = self.last_adapter
            if not result.get("ok"):
                return self.publish("attention", result.get("reason_zh", "伴侣素材接入需要处理"), companion=result)
            if (result.get("source_present") is not True or result.get("source_enabled") is not True or
                    result.get("audio_setting_verified") is not True):
                return self.publish("waiting_source", result.get("reason_zh", "等待核验伴侣的桥接素材"), companion=result)
            return self.publish("running", "本机桥接已接入，请检查伴侣预览", companion=result)
        except Exception as exc:
            self.note("后台检查异常：" + type(exc).__name__)
            self.publish("attention", "后台检查出现异常，已保留现场；请查看诊断日志")

    def _stop_tick(self, obs):
        if self.stopping:
            stream = self.stream_reader()
            self.publish(self._snapshot["state"], self._snapshot["reason_zh"], stream=stream)
            if obs.get("available") and obs.get("ok") and obs.get("stream_active"):
                if not obs.get("service_matches"):
                    if not stream.get("ok") or stream.get("publishing"):
                        return self.publish("attention", "已保留其他 OBS 推流；本机输出状态需要检查")
                else:
                    if self.obs_stop_unknown:
                        return self.publish("attention", "OBS 停止结果尚未确认；可再次点停止或在 OBS 手动停止")
                    if not self.obs_stop_requested:
                        stopped = self.obs.request("STOP_LOCAL")
                        if not stopped.get("ok"):
                            self.obs_stop_unknown = stopped.get("error") != "OBS_CONNECTING"
                            return self.publish("attention", "OBS 本机输出尚未停止，已保留中转服务")
                        self.obs_stop_requested = bool(stopped.get("requested"))
                        if not stopped.get("stopped"):
                            return self.publish("stopping", "正在等待 OBS 本机输出停止")
                    else:
                        return self.publish("stopping", "正在等待 OBS 本机输出停止")
            elif stream.get("publishing"):
                return self.publish("attention", "OBS 入口尚未就绪，请先在 OBS 停止本机推流")
            if self.obs_stop_requested and not (obs.get("ok") and obs.get("stream_active") is False):
                return self.publish("stopping", "正在等待 OBS 确认本机输出已停止")
            if not stream.get("ok"):
                status = self.server.status()
                if (not status.get("ok") or status.get("running") or status.get("listeners")):
                    return self.publish("attention", "本机输出状态未知，已保留中转服务", server=status)
            if stream.get("publishing"):
                return self.publish("stopping", "正在等待本机输出完成停止")
            result = self.adapter.set_enabled(False)
            if not result.get("ok") and not result.get("local_stream_pause_required"):
                return self.publish("attention", result.get("reason_zh", "伴侣素材停止状态需要检查"), companion=result)
            stopped = self.server.stop()
            if not stopped.get("ok"):
                return self.publish("attention", "本机中转尚未确认停止，已保留控制连接", server=stopped)
            self.server_ready = False; self.stopping = False
            self.stop_completed = True
            self.previous_stream = None; self.last_ensure = 0
            self.obs_stop_requested = False; self.obs_stop_unknown = False
            self.publish("stopped", "桥接已停止；如需下播，请在伴侣里关播", server={"running": False},
                         stream={"ok": True, "publishing": False}, companion=result)
        if self.exit_requested:
            result = self.adapter.disconnect_safe()
            ready = bool(result.get("ok") and result.get("disconnected", result.get("can_exit", False)))
            if ready:
                self.publish("exiting", "后台已安全退出", can_exit=True)
            else:
                self.publish("stopped", "桥接已停止，等待直播伴侣退出后释放后台", can_exit=False)
