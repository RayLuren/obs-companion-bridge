"""Lifecycle scenarios with simulated processes, publishers and SDK results."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from bridge_engine import BridgeEngine, LocalServer, safe_text
from types import SimpleNamespace


class Adapter:
    def __init__(self):
        self.present = False
        self.ensure_count = 0
        self.disables = 0
        self.ensure_result = {"ok": True, "source_present": True,
                              "source_enabled": True, "audio_setting_verified": True}
        self.disable_result = {"ok": True}
        self.disconnected = False

    def discover(self):
        return {"ok": self.present, "owner": {"pid": 9}, "reason_zh": "等待伴侣"}

    def connect(self):
        return {"ok": self.present, "owner": {"pid": 9}}

    def ensure_existing_source(self, **kwargs):
        self.ensure_count += 1
        return self.ensure_result

    def set_enabled(self, value):
        assert value is False
        self.disables += 1
        return self.disable_result

    def disconnect_safe(self):
        return {"ok": True, "disconnected": self.disconnected}


class Server:
    def __init__(self):
        self.running = False
        self.starts = self.stops = 0
        self.stop_ok = True

    def start(self):
        self.starts += 1; self.running = True
        return {"ok": True, "started": True}

    def stop(self):
        self.stops += 1
        if self.stop_ok:
            self.running = False
        return {"ok": self.stop_ok}

    def status(self):
        return {"ok": True, "running": self.running, "listeners": []}


class Obs:
    def __init__(self):
        self.status = {"ok": False, "available": False}
        self.stops = 0
        self.stop_result = {"ok": True, "requested": True, "stopped": False}

    def request(self, command):
        if command == "STOP_LOCAL":
            self.stops += 1
            return self.stop_result
        return self.status


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter(); self.server = Server(); self.obs = Obs()
        self.stream = {"ok": True, "publishing": False}
        self.engine = BridgeEngine(adapter=self.adapter, server=self.server, obs=self.obs,
            stream_reader=lambda: dict(self.stream), log_dir=False)
        self.clock = patch("bridge_engine.time.monotonic", return_value=100)
        self.clock.start(); self.addCleanup(self.clock.stop)

    def publishing(self, amount=100):
        self.stream.update(ok=True, publishing=True, bytes_in=amount, nginx_pid=8, h264=True, aac=True)

    def obs_ready(self, active=False):
        self.obs.status = {"ok": True, "available": True, "service_matches": True, "stream_active": active}

    def test_both_start_orders_converge(self):
        for mate_first in [False, True]:
            self.setUp()
            self.engine.start()
            if mate_first:
                self.adapter.present = True
            else:
                self.obs_ready(); self.publishing()
            self.engine.tick()
            self.assertNotEqual(self.engine.snapshot()["state"], "running")
            self.adapter.present = True; self.obs_ready(True); self.publishing(200)
            self.engine.tick(); self.publishing(300); self.engine.tick()
            self.assertEqual(self.engine.snapshot()["state"], "running")
            self.assertEqual(self.server.starts, 1)
            self.clock.stop()

    def test_repeated_start_and_completed_stop_are_idempotent(self):
        self.engine.start(); self.engine.tick(); self.engine.start(); self.engine.tick()
        self.assertEqual(self.server.starts, 1)
        self.obs_ready(); self.engine.stop(); self.engine.tick()
        self.engine.stop(); self.engine.tick()
        self.assertEqual(self.server.stops, 1)
        self.assertEqual(self.adapter.disables, 1)

    def test_stalled_bytes_never_claim_running(self):
        self.adapter.present = True; self.obs_ready(True); self.publishing()
        self.engine.start(); self.engine.tick(); self.engine.tick()
        self.assertEqual(self.adapter.ensure_count, 0)
        self.assertEqual(self.engine.snapshot()["state"], "waiting_data")

    def test_wrong_output_codec_is_not_attached(self):
        self.adapter.present = True; self.obs_ready(True); self.publishing()
        self.stream["h264"] = False
        self.engine.start(); self.engine.tick()
        self.assertEqual(self.adapter.ensure_count, 0)

    def test_stop_timeout_is_not_reissued_without_explicit_retry(self):
        self.obs_ready(True); self.publishing()
        self.obs.stop_result = {"ok": False, "error": "OBS_IO_TIMEOUT"}
        self.engine.stop(); self.engine.tick(); self.engine.tick()
        self.assertEqual(self.obs.stops, 1); self.assertEqual(self.server.stops, 0)
        self.engine.stop(); self.engine.tick()
        self.assertEqual(self.obs.stops, 2)

    def test_accepted_stop_is_polled_and_not_issued_twice(self):
        self.obs_ready(True); self.publishing()
        self.engine.stop(); self.engine.tick(); self.engine.tick()
        self.assertEqual(self.obs.stops, 1); self.assertEqual(self.server.stops, 0)
        self.obs_ready(); self.stream = {"ok": True, "publishing": False}
        self.engine.tick()
        self.assertEqual(self.server.stops, 1)

    def test_foreign_stream_with_local_publisher_is_preserved(self):
        self.obs_ready(True); self.obs.status["service_matches"] = False; self.publishing()
        self.engine.stop(); self.engine.tick()
        self.assertEqual(self.obs.stops, 0); self.assertEqual(self.server.stops, 0)

    def test_uncertain_source_disable_can_pause_transport_only_when_flagged(self):
        self.obs_ready()
        self.adapter.disable_result = {"ok": False, "local_stream_pause_required": False}
        self.engine.stop(); self.engine.tick(); self.assertEqual(self.server.stops, 0)
        self.adapter.disable_result["local_stream_pause_required"] = True
        self.engine.tick(); self.assertEqual(self.server.stops, 1)

    def test_failed_relay_stop_retains_stopping_state(self):
        self.obs_ready(); self.server.stop_ok = False
        self.engine.stop(); self.engine.tick()
        self.assertTrue(self.engine.stopping)
        self.assertFalse(self.engine.snapshot()["can_exit"])

    def test_exit_waits_for_safe_disconnect_then_finishes(self):
        self.obs_ready(); self.engine.request_exit(); self.engine.tick()
        self.assertFalse(self.engine.snapshot()["can_exit"])
        self.adapter.disconnected = True; self.engine.tick()
        self.assertTrue(self.engine.snapshot()["can_exit"])
        self.engine.start(); self.assertFalse(self.engine.desired)

    def test_log_snapshot_concurrency(self):
        def add_notes():
            for i in range(1000):
                self.engine.note(str(i))
        worker = threading.Thread(target=add_notes); worker.start()
        for _ in range(1000):
            self.engine.snapshot()
        worker.join()
        self.assertLessEqual(len(self.engine.snapshot()["recent_logs"]), 15)

    def test_endpoint_and_token_are_filtered(self):
        result = safe_text("memory_token=secret signature=abc https://example.test/?token=xyz")
        self.assertNotIn("secret", result); self.assertNotIn("abc", result); self.assertNotIn("xyz", result)

    def test_quoted_log_tokens_are_filtered(self):
        result = safe_text('{"memory_token": "sensitive", "Authorization": "Bearer secret"}')
        self.assertNotIn("sensitive", result); self.assertNotIn("secret", result)

    def test_waiting_adapter_cannot_report_successful_attachment(self):
        self.adapter.present = True; self.obs_ready(True); self.publishing()
        self.adapter.ensure_result = {"ok": True, "state": "waiting_stream"}
        self.engine.start(); self.engine.tick(); self.publishing(200); self.engine.tick()
        self.assertEqual(self.engine.snapshot()["state"], "waiting_source")


class RelayDeadline(unittest.TestCase):
    def test_late_exit_is_accepted_only_after_confirmed_port_release(self):
        server = LocalServer()
        with patch('bridge_engine.subprocess.run', return_value=SimpleNamespace(returncode=1)), \
                patch.object(server, 'status', return_value={"ok": True, "running": False, "listeners": []}):
            self.assertTrue(server.stop()["ok"])

    def test_unknown_or_occupied_port_never_claims_successful_stop(self):
        for status in [{"ok": False}, {"ok": True, "running": True},
                       {"ok": True, "running": False, "listeners": [{"LocalPort": 19360}]}]:
            with self.subTest(status=status), patch('bridge_engine.subprocess.run',
                    return_value=SimpleNamespace(returncode=1)), \
                    patch.object(LocalServer, 'status', return_value=status):
                self.assertFalse(LocalServer().stop()["ok"])

    def test_background_logs_rotate_and_remain_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = BridgeEngine(adapter=Adapter(), server=Server(), obs=Obs(),
                                  stream_reader=lambda: {}, log_dir=directory)
            try:
                for _ in range(3200):
                    engine.note("可排查的状态 " + "x" * 880)
                files = list(Path(directory).glob("bridge.log*"))
                self.assertGreater(len(files), 1)
                self.assertLessEqual(len(files), 6)
                self.assertTrue(all(file.stat().st_size <= 1024 * 1024 + 1024 for file in files))
            finally:
                for handler in engine.log.handlers[:]:
                    handler.close(); engine.log.removeHandler(handler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
