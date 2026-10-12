"""Independent review checks: all dependencies are fake, no GUI/SDK/process actions."""
import hashlib
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import bridge_engine


class Adapter:
    def __init__(self):
        self.enables = []
        self.disconnect_result = {"ok": False, "disconnected": False}

    def discover(self):
        return {"ok": True, "pid": 7}

    def connect(self):
        return {"ok": True}

    def ensure_existing_source(self, **options):
        return {"ok": True}

    def set_enabled(self, value):
        self.enables.append(value)
        return {"ok": True}

    def disconnect_safe(self):
        return self.disconnect_result


class Server:
    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.running = True
        return {"ok": True, "started": True, "master_pid": 8}

    def status(self):
        return {"ok": True, "running": self.running, "listeners": []}

    def stop(self):
        self.stops += 1
        self.running = False
        return {"ok": True, "stopped": True}


class Obs:
    def __init__(self, response):
        self.response = response
        self.commands = []

    def request(self, command):
        self.commands.append(command)
        if command == "STOP_LOCAL":
            return {"ok": True, "requested": True, "stopped": False}
        return self.response


class ReviewBoundaries(unittest.TestCase):
    def make_engine(self, obs_state, stream_state):
        adapter, server, obs = Adapter(), Server(), Obs(obs_state)
        engine = bridge_engine.BridgeEngine(adapter=adapter, server=server, obs=obs,
            stream_reader=lambda: dict(stream_state), log_dir=False)
        return engine, adapter, server, obs

    def test_unknown_status_preserves_local_server(self):
        engine, adapter, server, _ = self.make_engine(
            {"ok": False, "available": False}, {"ok": False, "publishing": False})
        server.running = True
        engine.server_ready = True
        engine.stop()
        engine.tick()
        self.assertEqual(server.stops, 0, "Unknown OBS and /stat must not be treated as confirmed idle")
        self.assertEqual(adapter.enables, [], "Unknown status must not disable the source")

    def test_owned_server_failure_is_recovered(self):
        engine, _, server, _ = self.make_engine(
            {"ok": True, "available": True, "stream_active": False, "service_matches": True},
            {"ok": True, "publishing": False})
        engine.start()
        engine.tick()
        self.assertEqual(server.starts, 1)
        server.running = False
        engine.last_server_check = 0
        engine.tick()
        self.assertEqual(server.starts, 2, "A dead relay must be rechecked and restarted")

    def test_other_obs_service_is_never_stopped(self):
        engine, adapter, server, obs = self.make_engine(
            {"ok": True, "available": True, "stream_active": True, "service_matches": False},
            {"ok": True, "publishing": False})
        engine.stop()
        engine.tick()
        self.assertNotIn("STOP_LOCAL", obs.commands)
        self.assertEqual(adapter.enables, [False], "Confirmed idle local relay may disable the bridge source")

    def test_other_obs_service_with_local_publisher_preserves_relay(self):
        engine, adapter, server, obs = self.make_engine(
            {"ok": True, "available": True, "stream_active": True, "service_matches": False},
            {"ok": True, "publishing": True})
        engine.stop()
        engine.tick()
        self.assertNotIn("STOP_LOCAL", obs.commands)
        self.assertEqual(server.stops, 0)
        self.assertEqual(adapter.enables, [])

    def test_async_stop_must_not_report_idle(self):
        engine, adapter, server, obs = self.make_engine(
            {"ok": True, "available": True, "stream_active": True, "service_matches": True},
            {"ok": True, "publishing": True})
        engine.stop()
        engine.tick()
        self.assertIn("STOP_LOCAL", obs.commands)
        self.assertEqual(server.stops, 0)
        self.assertEqual(adapter.enables, [])

    def test_disconnect_uncertainty_prevents_process_exit(self):
        engine, _, _, _ = self.make_engine(
            {"ok": True, "available": True, "stream_active": False, "service_matches": True},
            {"ok": True, "publishing": False})
        engine.request_exit()
        engine.tick()
        self.assertFalse(engine.snapshot()["can_exit"])


if __name__ == "__main__":
    print("engine_sha256=" + hashlib.sha256((APP / "bridge_engine.py").read_bytes()).hexdigest())
    unittest.main(verbosity=2)
