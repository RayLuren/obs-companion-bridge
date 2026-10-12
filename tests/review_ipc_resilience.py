"""Exercise real restricted IPC without a GUI, SDK, OBS, or production pipe."""
from __future__ import annotations

import ctypes
import io
import json
import logging
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import ipc

TEST_PIPE = rf"\\.\pipe\OBS_MATE_BACKEND_REVIEW_S{ipc.current_session_id()}_{os.getpid()}"
ipc.backend_pipe_name = lambda session=None: TEST_PIPE
TEST_EXECUTABLE = ipc._process_path(os.getpid())


def request(command):
    # Windows virtualenv launchers report their base interpreter as the actual
    # process image. Test that verified image; production EXE checks stay strict.
    return ipc.rpc(command, expected_executable=TEST_EXECUTABLE)


def raw_request(data: bytes) -> dict:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        raw = ipc.kernel.CreateFileW(TEST_PIPE, 0xC0000000, 0, None, 3, 0x40120000, None)
        if raw not in (None, 0, ipc.INVALID_HANDLE):
            with ipc.Handle(raw) as pipe:
                ipc._verify_server(pipe, TEST_EXECUTABLE, ipc.current_session_id())
                ipc._pipe_io(pipe, write=data)
                return json.loads(ipc._read_line(pipe).decode("utf-8"))
        error = ctypes.get_last_error()
        if error not in (2, 231):
            raise ctypes.WinError(error)
        ipc.kernel.WaitNamedPipeW(TEST_PIPE, 50)
        time.sleep(0.02)
    raise TimeoutError("Test pipe unavailable")


class IpcResilience(unittest.TestCase):
    def setUp(self):
        self.mode = "healthy"
        self.calls = []
        self.log_output = io.StringIO()
        self.log_handler = logging.StreamHandler(self.log_output)
        logging.getLogger().addHandler(self.log_handler)
        self.server = ipc.PipeServer(self.handle)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        logging.getLogger().removeHandler(self.log_handler)

    def handle(self, command):
        self.calls.append(command)
        if self.mode == "raises":
            raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")
        if self.mode == "object":
            return {"ok": True, "value": object()}
        if self.mode == "circular":
            value = {"ok": True}
            value["value"] = value
            return value
        if self.mode == "invalid_shape":
            return {"ok": "yes"}
        if self.mode == "invalid_unicode":
            return {"ok": True, "value": "\ud800"}
        if self.mode == "large":
            return {"ok": True, "value": "x" * ipc.MAX_LINE}
        return {"ok": True, "value": "中文正常", "command": command}

    def assert_recovers(self):
        self.mode = "healthy"
        reply = request("status")
        self.assertEqual(reply["value"], "中文正常")
        self.assertTrue(self.server.thread.is_alive())
        self.assertIsNone(self.server.startup_error)

    def test_handler_errors_are_fixed_and_listener_recovers(self):
        self.mode = "raises"
        self.assertEqual(request("status"), {"ok": False, "error": "WORKER_ERROR"})
        self.assertNotIn("PRIVATE_EXCEPTION_SENTINEL", self.log_output.getvalue())
        self.assert_recovers()

    def test_unencodable_responses_are_fixed_and_listener_recovers(self):
        for mode in ("object", "circular", "invalid_shape", "invalid_unicode"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.assertEqual(request("status"), {"ok": False, "error": "WORKER_ERROR"})
                self.assert_recovers()

    def test_deep_and_invalid_requests_do_not_end_listener(self):
        for request in (b"\xff\n", b"{bad}\n", b"{}\n{}\n"):
            with self.subTest(length=len(request)):
                before = len(self.calls)
                self.assertEqual(raw_request(request), {"ok": False, "error": "INVALID_REQUEST"})
                self.assertEqual(len(self.calls), before)
                self.assert_recovers()

    def test_deep_json_is_rejected_without_listener_failure(self):
        # Python versions differ in the C JSON parser's nesting threshold.
        # A deeply nested list is either a parse error or an invalid command,
        # and must never reach the worker or end the listener in either case.
        reply = raw_request(b"[" * 1200 + b"0" + b"]" * 1200 + b"\n")
        self.assertFalse(reply["ok"])
        self.assertIn(reply["error"], ("INVALID_REQUEST", "INVALID_COMMAND"))
        self.assertEqual(self.calls, [])
        self.assert_recovers()

    def test_parser_recursion_error_is_isolated(self):
        original = ipc.json.loads

        def parser(value, *args, **kwargs):
            if value == "PARSER_RECURSION":
                raise RecursionError("PRIVATE_PARSER_SENTINEL")
            return original(value, *args, **kwargs)

        with patch.object(ipc.json, "loads", parser):
            self.assertEqual(raw_request(b"PARSER_RECURSION\n"),
                             {"ok": False, "error": "INVALID_REQUEST"})
            self.assert_recovers()

    def test_encoder_recursion_error_is_isolated(self):
        original = ipc.json.dumps

        def encoder(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("value") == "中文正常":
                raise RecursionError("PRIVATE_ENCODER_SENTINEL")
            return original(value, *args, **kwargs)

        with patch.object(ipc.json, "dumps", encoder):
            self.assertEqual(request("status"), {"ok": False, "error": "WORKER_ERROR"})
        self.assertNotIn("PRIVATE_ENCODER_SENTINEL", self.log_output.getvalue())
        self.assert_recovers()

    def test_whitelist_checks_before_handler(self):
        for value in ({"command": "unknown"}, {"command": []}, {"command": "status", "path": "x"}, []):
            with self.subTest(value=value):
                before = len(self.calls)
                self.assertEqual(raw_request(json.dumps(value).encode() + b"\n"),
                                 {"ok": False, "error": "INVALID_COMMAND"})
                self.assertEqual(len(self.calls), before)
                self.assert_recovers()

    def test_oversized_response_is_bounded(self):
        self.mode = "large"
        self.assertEqual(request("status"), {"ok": False, "error": "RESPONSE_TOO_LARGE"})
        self.assert_recovers()

    def test_denied_client_never_dispatches_and_listener_recovers(self):
        original = self.server._allowed_client
        self.server._allowed_client = lambda pipe: False
        self.assertEqual(request("start"), {"ok": False, "error": "CLIENT_DENIED"})
        self.assertEqual(self.calls, [])
        self.server._allowed_client = original
        self.assert_recovers()


if __name__ == "__main__":
    unittest.main(verbosity=2)
