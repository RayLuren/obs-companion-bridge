"""Read-only desktop review using fake widgets; never creates Tk or a pipe."""
from pathlib import Path
import queue
import sys
import threading
import unittest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import desktop_app


class Widget:
    def __init__(self):
        self.calls = []

    def configure(self, **options):
        self.calls.append(("configure", options))

    def withdraw(self):
        self.calls.append(("withdraw",))

    def deiconify(self):
        self.calls.append(("deiconify",))

    def lift(self):
        self.calls.append(("lift",))

    def focus_force(self):
        self.calls.append(("focus",))

    def after(self, *_):
        self.calls.append(("after",))

    def set_tooltip(self, text):
        self.calls.append(("tooltip", text))


class Engine:
    def __init__(self):
        self.running = False
        self.can_exit = False

    def snapshot(self):
        return {"state": "stopped", "reason_zh": "桥接已停止", "desired_running": self.running,
                "obs": {}, "companion": {}, "server": {}, "recent_logs": [], "can_exit": self.can_exit}


def panel():
    value = desktop_app.DesktopPanel.__new__(desktop_app.DesktopPanel)
    value.engine = Engine()
    value.input_queue = queue.SimpleQueue()
    value.engine_queue = queue.SimpleQueue()
    value.exit_ack = threading.Event()
    value.exit_requested = False
    value.ui_error = ""
    value.pending_text = ""
    value._last_log_text = ""
    value._closed = False
    value.tray_available = True
    for name in ("root", "tray", "state_label", "detail_label", "footer", "start_button"):
        setattr(value, name, Widget())
    value._close = lambda: (_ for _ in ()).throw(AssertionError("unconfirmed exit"))
    return value


class DesktopLifetime(unittest.TestCase):
    def test_x_only_hides_and_keeps_state(self):
        value = panel()
        value.engine.running = True
        value.hide()
        self.assertTrue(value.engine.running)
        self.assertTrue(value.engine_queue.empty())
        self.assertIn(("withdraw",), value.root.calls)

    def test_repeated_show_does_not_restart_stopped_engine(self):
        value = panel()
        reply = value.handle_ipc("show")
        self.assertTrue(reply["ok"])
        value._refresh()
        self.assertFalse(value.engine.running)
        self.assertTrue(value.engine_queue.empty())
        self.assertIn(("deiconify",), value.root.calls)

    def test_quit_unknown_exit_state_keeps_panel_and_worker(self):
        value = panel()
        value.exit_requested = True
        value.exit_ack.set()
        value._refresh()
        self.assertFalse(value._closed)

    def test_ui_actions_enqueue_instead_of_mutating_engine(self):
        value = panel()
        value.enqueue("start")
        value._refresh()
        self.assertFalse(value.engine.running)
        self.assertEqual(value.engine_queue.get_nowait(), "start")


if __name__ == "__main__":
    unittest.main(verbosity=2)
