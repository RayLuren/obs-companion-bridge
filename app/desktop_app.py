"""OBS companion bridge launcher, small desktop panel, and tray lifetime.

The launcher stays asInvoker. It only requests elevation when no verified local
worker exists. The elevated worker owns the engine and remains alive while its
panel is hidden. All engine mutations run on one background thread.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import ipc

APP_TITLE = "OBS伴侣桥接"
LOG_DIRECTORY = Path(r"D:\OBS伴侣桥接\logs")
user = ctypes.WinDLL("user32", use_last_error=True)
shell = ctypes.WinDLL("shell32", use_last_error=True)


def configure_logging():
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if not logging.getLogger().handlers:
        handler = RotatingFileHandler(LOG_DIRECTORY / "desktop-ui.log", maxBytes=2_000_000,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)


def is_admin() -> bool:
    shell.IsUserAnAdmin.restype = wintypes.BOOL
    return bool(shell.IsUserAnAdmin())


def message_box(message: str, title=APP_TITLE, error=False):
    user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user.MessageBoxW.restype = ctypes.c_int
    user.MessageBoxW(None, message, title, 0x10 if error else 0x40)


def worker_executable() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else Path(sys.executable))


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", wintypes.LPVOID),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE)]


def launch_elevated_worker() -> bool:
    executable = worker_executable()
    if getattr(sys, "frozen", False):
        parameters = "--worker"
    else:
        parameters = subprocess.list2cmdline([str(Path(__file__).resolve()), "--worker"])
    request = SHELLEXECUTEINFOW()
    request.cbSize = ctypes.sizeof(request)
    request.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    request.lpVerb = "runas"
    request.lpFile = executable
    request.lpParameters = parameters
    request.lpDirectory = str(Path(executable).parent)
    request.nShow = 1
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    if not shell.ShellExecuteExW(ctypes.byref(request)):
        error = ctypes.get_last_error()
        if error == 1223:
            logging.info("Administrator consent was cancelled")
            message_box("已取消管理员授权，桥接未启动。需要使用时，请再次从 OBS 工具菜单打开。")
            return False
        raise ctypes.WinError(error)
    if request.hProcess:
        ipc.kernel.CloseHandle(request.hProcess)
    return True


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HANDLE)]


ipc.kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
ipc.kernel.GetModuleHandleW.restype = wintypes.HINSTANCE
user.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user.RegisterClassW.restype = wintypes.WORD
user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user.UnregisterClassW.restype = wintypes.BOOL
user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user.CreateWindowExW.restype = wintypes.HWND
user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user.DefWindowProcW.restype = LRESULT
user.DestroyWindow.argtypes = [wintypes.HWND]
user.DestroyWindow.restype = wintypes.BOOL
user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user.PostMessageW.restype = wintypes.BOOL
user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user.GetMessageW.restype = wintypes.BOOL
user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.restype = LRESULT
user.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user.LoadIconW.restype = wintypes.HANDLE
user.CreatePopupMenu.restype = wintypes.HMENU
user.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user.SetForegroundWindow.argtypes = [wintypes.HWND]
user.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
user.TrackPopupMenu.restype = wintypes.UINT
user.DestroyMenu.argtypes = [wintypes.HMENU]
user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user.RegisterWindowMessageW.restype = wintypes.UINT
shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell.Shell_NotifyIconW.restype = wintypes.BOOL


class TrayIcon:
    CALLBACK = 0x8000 + 71
    UPDATE = 0x8000 + 72

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.window = None
        self.ready = threading.Event()
        self.error = None
        self._tooltip = APP_TITLE
        self._tooltip_lock = threading.Lock()
        self._proc = WNDPROC(self._window_proc)
        self._class_name = f"ObsMateTray_{os.getpid()}"
        self._data = NOTIFYICONDATAW()
        self._taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
        self.thread = threading.Thread(target=self._run, name="bridge-tray", daemon=True)

    def start(self) -> bool:
        self.thread.start()
        return self.ready.wait(2.0) and self.error is None

    def set_tooltip(self, value: str):
        text = f"{APP_TITLE}：{value}"[:110]
        with self._tooltip_lock:
            if text == self._tooltip:
                return
            self._tooltip = text
        if self.window:
            user.PostMessageW(self.window, self.UPDATE, 0, 0)

    def stop(self):
        if self.window:
            user.PostMessageW(self.window, 0x10, 0, 0)  # WM_CLOSE
        self.thread.join(2.0)

    def _window_proc(self, window, message, wparam, lparam):
        try:
            if message == self.CALLBACK:
                event = lparam & 0xFFFF
                if event in (0x202, 0x203, 0x400):  # button up/double click/NIN_SELECT
                    self.dispatch("show")
                elif event in (0x205, 0x7B):  # right click/context menu
                    self._popup(window)
                return 0
            if message == self.UPDATE:
                with self._tooltip_lock:
                    self._data.szTip = self._tooltip
                shell.Shell_NotifyIconW(1, ctypes.byref(self._data))
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                shell.Shell_NotifyIconW(0, ctypes.byref(self._data))
                self._data.uVersion = 4
                shell.Shell_NotifyIconW(4, ctypes.byref(self._data))
                return 0
            if message == 0x10:
                shell.Shell_NotifyIconW(2, ctypes.byref(self._data))
                user.DestroyWindow(window)
                return 0
            if message == 0x2:
                user.PostQuitMessage(0)
                return 0
        except Exception:
            logging.exception("Tray event failed")
        return user.DefWindowProcW(window, message, wparam, lparam)

    def _popup(self, window):
        menu = user.CreatePopupMenu()
        try:
            entries = [(1, "打开面板"), (2, "启动桥接"), (3, "停止桥接")]
            for command_id, label in entries:
                user.AppendMenuW(menu, 0, command_id, label)
            user.AppendMenuW(menu, 0x800, 0, None)
            user.AppendMenuW(menu, 0, 4, "退出后台")
            point = wintypes.POINT()
            user.GetCursorPos(ctypes.byref(point))
            user.SetForegroundWindow(window)
            selected = user.TrackPopupMenu(menu, 0x182, point.x, point.y, 0, window, None)
            action = {1: "show", 2: "start", 3: "stop", 4: "quit"}.get(selected)
            if action:
                self.dispatch(action)
            user.PostMessageW(window, 0, 0, 0)
        finally:
            user.DestroyMenu(menu)

    def _run(self):
        instance = ipc.kernel.GetModuleHandleW(None)
        registered = False
        try:
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._proc
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            if not user.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self.window = user.CreateWindowExW(0, self._class_name, "", 0, 0, 0, 0, 0,
                                              None, None, instance, None)
            if not self.window:
                raise ctypes.WinError(ctypes.get_last_error())
            self._data.cbSize = ctypes.sizeof(self._data)
            self._data.hWnd = self.window
            self._data.uID = 1
            self._data.uFlags = 0x87  # message, icon, tooltip, v4 tooltip enabled
            self._data.uCallbackMessage = self.CALLBACK
            self._data.hIcon = user.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR))
            self._data.szTip = APP_TITLE
            if not shell.Shell_NotifyIconW(0, ctypes.byref(self._data)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._data.uVersion = 4
            shell.Shell_NotifyIconW(4, ctypes.byref(self._data))
            self.ready.set()
            message = wintypes.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as error:
            self.error = error
            logging.exception("Tray icon failed")
        finally:
            self.ready.set()
            self.window = None
            if registered:
                user.UnregisterClassW(self._class_name, instance)


class PreviewEngine:
    """UI-only preview; deliberately performs no OBS/SDK/server operations."""
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.logs = ["界面预览模式：不会连接 OBS、直播伴侣或中转服务。"]

    def start(self):
        with self.lock:
            self.running = True
            self.logs.append("已点击启动桥接（仅界面预览）。")

    def stop(self):
        with self.lock:
            self.running = False
            self.logs.append("已点击停止桥接（仅界面预览）。")

    def request_exit(self):
        self.stop()

    def tick(self):
        pass

    def snapshot(self):
        with self.lock:
            return {"state": "waiting" if self.running else "stopped",
                    "reason_zh": "预览模式：桥接已启动，等待 OBS 输出" if self.running else "预览模式：桥接已停止",
                    "desired_running": self.running, "obs": {"available": False},
                    "companion": {"available": False}, "server": {"running": False},
                    "recent_logs": self.logs[-12:], "can_exit": not self.running}


class DesktopPanel:
    def __init__(self, engine, preview=False):
        try:
            user.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            user.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            user.SetProcessDpiAwarenessContext(wintypes.HANDLE(-2))  # system-aware; before Tk creates HWNDs
        except AttributeError:
            pass
        self.engine = engine
        self.preview = preview
        self.input_queue = queue.SimpleQueue()
        self.engine_queue = queue.SimpleQueue()
        self.stop_loop = threading.Event()
        self.exit_ack = threading.Event()
        self.exit_requested = False
        self.ui_error = ""
        self.pending_text = ""
        self._last_log_text = ""
        self._closed = False
        self.root = tk.Tk()
        self.root.title(APP_TITLE + (" · 界面预览" if preview else ""))
        self.root.geometry("720x600")
        self.root.minsize(650, 540)
        self.root.configure(bg="#f4f6fa")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._build_ui()
        self.tray = TrayIcon(self.enqueue)
        self.tray_available = self.tray.start()
        self.server = ipc.PipeServer(self.handle_ipc)
        self.server.start()
        self.thread = threading.Thread(target=self._engine_loop, name="bridge-engine", daemon=True)
        self.thread.start()
        self.engine_queue.put("start")
        self.root.after(100, self._refresh)

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", font=("Microsoft YaHei UI", 11), padding=(18, 10))
        style.configure("Start.TButton", foreground="white", background="#28755f")
        style.map("Start.TButton", background=[("active", "#20634f")])
        style.configure("Stop.TButton", foreground="#9b3542", background="#f7e9ec")
        style.map("Stop.TButton", background=[("active", "#f0d6dc")])
        body = tk.Frame(self.root, bg="#f4f6fa", padx=26, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="OBS 伴侣桥接", bg="#f4f6fa", fg="#1e2b3c",
                 font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        tk.Label(body, text="在 OBS 准备画面和声音，在直播伴侣确认预览。", bg="#f4f6fa",
                 fg="#627084", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(7, 18))
        card = tk.Frame(body, bg="white", padx=18, pady=16, highlightthickness=1,
                        highlightbackground="#dfe5ed")
        card.pack(fill="x")
        self.state_label = tk.Label(card, text="正在准备桥接…", bg="white", fg="#28755f",
                                   wraplength=600, justify="left", anchor="w",
                                   font=("Microsoft YaHei UI", 13, "bold"))
        self.state_label.pack(fill="x")
        self.detail_label = tk.Label(card, text="", bg="white", fg="#627084", anchor="w",
                                    justify="left", font=("Microsoft YaHei UI", 10))
        self.detail_label.pack(fill="x", pady=(12, 0))
        buttons = tk.Frame(body, bg="#f4f6fa")
        buttons.pack(fill="x", pady=(18, 17))
        self.start_button = ttk.Button(buttons, text="启动桥接", style="Start.TButton",
                                       command=lambda: self.enqueue("start"))
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="停止桥接", style="Stop.TButton",
                                      command=lambda: self.enqueue("stop"))
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(8, 0))
        tk.Label(body, text="最近状态", bg="#f4f6fa", fg="#3a485b",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 7))
        log_frame = tk.Frame(body, bg="white", highlightthickness=1, highlightbackground="#dfe5ed")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", bg="white", fg="#4a586b",
                                relief="flat", padx=12, pady=10,
                                font=("Microsoft YaHei UI", 9), state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.footer = tk.Label(body, text="关闭窗口会收起到托盘，桥接保持当前状态。", bg="#f4f6fa",
                               fg="#627084", anchor="w", wraplength=660,
                               font=("Microsoft YaHei UI", 9))
        self.footer.pack(fill="x", side="bottom", before=log_frame, pady=(13, 0))

    def enqueue(self, command: str):
        self.input_queue.put(command)

    def handle_ipc(self, command: str) -> dict:
        if command != "status":
            self.enqueue(command)
        value = self.engine.snapshot()
        # Keep IPC bounded even if a future engine provides unusually long logs.
        value = dict(value)
        value["recent_logs"] = [str(line)[:280] for line in value.get("recent_logs", [])[-10:]]
        return {"ok": True, "accepted": command != "status", "snapshot": value}

    def _engine_loop(self):
        while not self.stop_loop.is_set():
            try:
                while True:
                    try:
                        command = self.engine_queue.get_nowait()
                    except queue.Empty:
                        break
                    if command == "start":
                        self.engine.start()
                    elif command == "stop":
                        self.engine.stop()
                    elif command == "quit":
                        self.engine.request_exit()
                        self.exit_ack.set()
                    self.pending_text = ""
                self.engine.tick()
                self.ui_error = ""
            except Exception as error:
                self.ui_error = f"处理暂未完成：{type(error).__name__}。请查看后台日志。"
                logging.exception("Bridge engine operation failed")
            self.stop_loop.wait(0.8)

    @staticmethod
    def _available(section: dict) -> bool:
        return bool(section.get("available", section.get("connected", section.get("running",
                    section.get("pid", section.get("owner_pid", False))))))

    def _status_details(self, snapshot: dict) -> str:
        obs = snapshot.get("obs", {})
        companion = snapshot.get("companion", {})
        server = snapshot.get("server", {})
        obs_text = "已打开" if self._available(obs) else "等待打开"
        if obs.get("stream_active"):
            obs_text = "本机输出中" if obs.get("service_matches") else "当前输出未使用本机桥接配置"
        mate_text = "已打开" if self._available(companion) or companion.get("ok") or companion.get("owner") else "等待打开"
        relay_text = "已就绪" if self._available(server) else "未运行"
        return f"OBS：{obs_text}    直播伴侣：{mate_text}\n本机中转：{relay_text}    手机端画面和声音需开播后检查"

    def _refresh(self):
        if self._closed:
            return
        try:
            while True:
                try:
                    command = self.input_queue.get_nowait()
                except queue.Empty:
                    break
                if command == "show":
                    self.show()
                elif command in {"start", "stop", "quit"}:
                    if command == "quit":
                        self.exit_requested = True
                        self.pending_text = "正在停止桥接并准备退出…"
                        self.start_button.configure(state="disabled")
                        self.footer.configure(text="已请求退出；需要保留连接时，将在直播伴侣正常退出后结束。")
                    elif command == "start":
                        if self.exit_requested:
                            continue
                        self.pending_text = "正在启动桥接…"
                    elif command == "stop":
                        self.pending_text = "正在停止桥接…"
                    self.engine_queue.put(command)
            snapshot = self.engine.snapshot()
            reason = self.ui_error or self.pending_text or snapshot.get("reason_zh") or "等待桥接状态"
            state = str(snapshot.get("state", "")).lower()
            color = "#a13e4a" if self.ui_error or "error" in state or "fault" in state else (
                "#28755f" if snapshot.get("desired_running") else "#627084")
            self.state_label.configure(text=reason, fg=color)
            self.detail_label.configure(text=self._status_details(snapshot))
            self.tray.set_tooltip(reason)
            log_text = "\n".join(str(line) for line in snapshot.get("recent_logs", [])[-40:])
            if log_text != self._last_log_text:
                self.log_text.configure(state="normal")
                self.log_text.delete("1.0", "end")
                self.log_text.insert("end", log_text)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                self._last_log_text = log_text
            if self.exit_requested and self.exit_ack.is_set() and snapshot.get("can_exit", False):
                self._close()
                return
            if not self.tray_available and not self.exit_requested:
                self.footer.configure(text="关闭窗口会最小化，桥接保持当前状态；也可从 OBS 菜单再次打开。")
        except Exception:
            logging.exception("Panel state refresh failed")
        self.root.after(400, self._refresh)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if self.tray_available:
            self.root.withdraw()
        else:
            self.root.iconify()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        self.stop_loop.set()
        if self.server:
            self.server.stop()
        self.tray.stop()
        self.thread.join(3.0)
        self.root.destroy()

    def run(self):
        self.root.mainloop()

    def verify_panel_lifecycle(self, attempts=300):
        """Fixed-path opt-in QA of the actual elevated panel and live engine.

        Runs the registered WM close callback on our own Tk event loop. It never
        grants Windows permissions or changes the bridge's requested state.
        """
        before = self.engine.snapshot()
        if before.get("state") != "running":
            if attempts > 0:
                self.root.after(1000, lambda: self.verify_panel_lifecycle(attempts - 1))
            return
        callback = self.root.protocol("WM_DELETE_WINDOW")
        self.root.tk.call(callback)
        hidden = self.root.state()
        def finish():
            during = self.engine.snapshot()
            self.show()
            self.root.update_idletasks()
            after = self.engine.snapshot()
            sid = before.get("companion", {}).get("source_id")
            report = {"version": ipc.APP_VERSION, "pid": os.getpid(), "administrator": is_admin(),
                      "actual_engine": not self.preview, "registered_close_callback_called": True,
                      "hidden_window_state": hidden, "reopened_window_state": self.root.state(),
                      "desired_before": before.get("desired_running"),
                      "desired_during": during.get("desired_running"),
                      "desired_after": after.get("desired_running"),
                      "source_id_unchanged": bool(sid) and sid == during.get("companion", {}).get("source_id")
                                            == after.get("companion", {}).get("source_id"),
                      "actual_stream_continued": during.get("stream", {}).get("publishing") is True
                                                 and after.get("stream", {}).get("publishing") is True}
            destination = Path(r"D:\OBS伴侣桥接\validation")
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "actual-panel-lifecycle.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.root.after(1500, finish)


def _show_existing(timeout=0.8) -> bool:
    response = ipc.rpc("show", timeout=timeout, expected_executable=worker_executable())
    return response.get("ok", False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--preview-command", choices=sorted(ipc.COMMANDS))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verify-panel-lifecycle", action="store_true")
    arguments = parser.parse_args(argv)
    configure_logging()
    if arguments.self_test:
        # Fixed-path packaging check; no native connection, mutations or UAC.
        from adapter.bridge_adapter import BridgeAdapter
        from adapter.windows_transport import ReviewedTransport
        from adapter import reviewed_pipe_client
        from bridge_engine import ObsClient, LocalServer
        report = {"version": ipc.APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                  "administrator": is_admin(), "sdk_connected": False,
                  "adapter_imported": BridgeAdapter is not None,
                  "pipe_library_imported": reviewed_pipe_client.BridgeClient is not None,
                  "server_status": LocalServer().status(), "obs_status": ObsClient().request()}
        destination = Path(r"D:\OBS伴侣桥接\validation")
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "packaged-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if arguments.preview or arguments.preview_command:
        session = ipc.current_session_id()
        ipc.backend_pipe_name = lambda session_id=None: rf"\\.\pipe\OBS_MATE_PREVIEW_S{session}"
    if arguments.preview_command:
        report = ipc.rpc(arguments.preview_command, expected_executable=worker_executable())
        destination = Path(r"D:\OBS伴侣桥接\validation")
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "preview-status.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if arguments.preview:
        instance = ipc.SingleInstance(preview=True)
        try:
            if instance.already_running:
                return 0 if _show_existing(timeout=3.0) else 1
            DesktopPanel(PreviewEngine(), preview=True).run()
            return 0
        finally:
            instance.close()
    if not arguments.worker:
        try:
            if _show_existing():
                return 0
        except PermissionError:
            logging.exception("Existing local pipe server identity rejected")
            message_box("本机桥接接口被其他程序占用，已拒绝连接。请查看后台日志。", error=True)
            return 1
        except (OSError, TimeoutError, ValueError):
            pass
        if ipc.worker_instance_exists():
            for _ in range(2):
                try:
                    if _show_existing(timeout=3.0):
                        return 0
                except (OSError, TimeoutError, ValueError):
                    pass
            message_box("桥接后台正在准备或暂时忙碌。请稍后再次从 OBS 工具菜单打开，已有后台状态会保留。")
            return 1
        return 0 if launch_elevated_worker() else 1
    if not is_admin():
        message_box("桥接后台需要管理员权限。请从 OBS 的工具菜单打开，让 Windows 显示授权提示。")
        return 1
    instance = ipc.SingleInstance()
    try:
        if instance.already_running:
            for _ in range(10):
                try:
                    if _show_existing(timeout=0.5):
                        return 0
                except (OSError, TimeoutError, ValueError):
                    time.sleep(0.2)
            return 0
        from bridge_engine import BridgeEngine
        logging.info("Elevated bridge desktop worker starting in session %s", ipc.current_session_id())
        panel = DesktopPanel(BridgeEngine())
        if arguments.verify_panel_lifecycle:
            panel.root.after(1000, panel.verify_panel_lifecycle)
        panel.run()
        logging.info("Bridge desktop worker exited normally")
        return 0
    finally:
        instance.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exception:
        try:
            configure_logging()
            logging.exception("Desktop bridge startup failed")
            message_box(f"桥接程序没有完成启动（{type(exception).__name__}）。请查看 D:\\OBS伴侣桥接\\logs 下的后台日志。", error=True)
        except Exception:
            pass
        raise SystemExit(1)
