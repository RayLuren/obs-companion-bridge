"""Behavioural tests with fake SDK, discovery, and publishing state only.

No test creates a real pipe client, queries a live process, contacts nginx,
or touches the controller's real manifest. All files live in TemporaryDirectory.
Run from this directory: python -m unittest -v test_bridge_adapter
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bridge_adapter import BridgeAdapter


# Independent copies of the reviewed protocol; do not import implementation
# constants, so an accidental change to the production endpoint fails a test.
NAME = "OBS 本机桥接（Codex）"
URL = "http://127.0.0.1:18081/live?port=19360&app=obs&stream=main"
EXE = r"D:\webcast_mate\13.1.1.502547155\直播伴侣.exe"
SID = "71d02712-5c41-48d6-92aa-c6632b3737aa"
OTHER_SID = "d9230dcb-01c6-42e1-9627-af44cc14c171"
THIRD_SID = "c97b4045-e7eb-4f3e-8f1a-06879d5c8b45"
UNSET = object()


def owner(pid=8100, started=134046576000000000):
    return {"server_pid": pid, "server_start_filetime": started,
            "server_start_utc": "2026-10-11T10:00:00+00:00", "server_path": EXE}


def source_row(index=0, enabled=True, audio=True):
    return {"name": NAME, "type": "onlineMedia", "viewIndex": index,
            "secondSource": False, "visible": enabled, "alwaysTop": False,
            "liveScene": "", "payload": {
                "url": URL, "hardwareDecode": False, "bufferSize": 2,
                "timeout": 10, "activeRestartPlay": False,
                "playEndNoDisplay": False, "linearAlpha": False,
                "colorRange": 3, "ffmpegOptions": "", "audio": {"setting": {
                    "volume": 1 if audio else 0, "balancing": 0.5,
                    "monitorType": 1 if audio else 0, "syncOffset": 0,
                    "isDownmixMono": False}}}}


def legacy_record(old_owner=None):
    return {"tool": "codex-local-obs-bridge-v1", "name": NAME,
            "type": "onlineMedia", "endpoint": "local-flv-18081",
            "source_id": SID, "view_index": 0, "new_settings": True,
            "audio": "obs", "stage": "attached",
            "owner": copy.deepcopy(old_owner or owner(7000, 133000000000000000)),
            "created_utc": "2026-10-10T10:00:00+00:00"}


class FakeTransport:
    """One simulated process lifetime, with strict allowed RPC grammar.

    fail_on makes the requested write happen before raising, modeling the
    dangerous case where the reply disappears after the server applied it.
    change_after models a process restart between two SDK calls.
    """
    def __init__(self, identity=None):
        self.owner = copy.deepcopy(identity or owner())
        self.is_alive = True
        self.calls = []
        self.verify_calls = []
        self.close_calls = 0
        # Shared sourceStore registration buckets: keys 0/1 mean data/data2,
        # not native canvas indices. A row in either bucket may have viewIndex 1.
        self.rows = {0: {}, 1: {}}
        self.top = {}
        self.index = 0
        self.missing_canvases = set()
        self.new_settings = True
        self.get_override = UNSET
        self.get_overrides = {}
        self.audio_override = UNSET
        self.source_override = UNSET
        self.top_override = UNSET
        self.audio_reply = True
        self.add_reply = True
        self.fail_on = None
        self.fail_for_id = None
        self.fail_once = False
        self.change_after = None
        self.changed_owner = owner(8101, 134046577000000000)
        self.before_write = None

    def identity(self):
        return copy.deepcopy(self.owner)

    def verify_identity(self, expected):
        self.verify_calls.append(copy.deepcopy(expected))
        if not self.is_alive or expected != self.owner:
            raise RuntimeError("PRIVATE_FAKE_DETAIL identity changed")
        return True

    def alive(self):
        return self.is_alive

    def close(self):
        self.close_calls += 1
        if self.is_alive:
            raise AssertionError("The adapter tried to close a live SDK pipe")

    def find(self, sid):
        for data in (self.rows[0], self.rows[1], self.top):
            if sid in data:
                return data[sid]
        return None

    def shared_source(self):
        return {"data": copy.deepcopy(self.rows[0]), "order": list(self.rows[0]),
                "data2": copy.deepcopy(self.rows[1]), "order2": list(self.rows[1])}

    @staticmethod
    def operation(method, args):
        if method == "CANVAS_CALL":
            return args[0]["path"][-1]
        return method

    def call(self, method, args):
        self.calls.append((method, copy.deepcopy(args)))
        operation = self.operation(method, args)
        if self.before_write and operation in {"addSource", "sourceTileFill", "toggleVisible", "setAudioData"}:
            self.before_write(operation)
        if method == "getUseNewSettings":
            assert args == []
            result = self.new_settings
        elif method == "getCanvasScene":
            assert args == []
            result = {"scene": "DEFAULT" if self.index == 0 else "SECOND", "viewIndex": self.index}
        elif method == "CANVAS_CALL":
            assert len(args) == 1 and isinstance(args[0], dict)
            request = args[0]
            assert request["index"] in (0, 1) and type(request["index"]) is int
            prefix = ["stage", "baseEnv", "sourceStore"] if self.new_settings else ["sourceStore"]
            assert request["path"] == prefix + [operation]
            index = request["index"]
            if index in self.missing_canvases:
                return None
            if operation == "source":
                assert request["type"] == "GET" and request["argArray"] == []
                result = self.source_override if self.source_override is not UNSET else self.shared_source()
            elif operation == "topSource":
                assert request["type"] == "GET" and request["argArray"] == []
                result = self.top_override if self.top_override is not UNSET else {
                    "order": list(self.top), "data": copy.deepcopy(self.top)}
            elif operation == "toggleVisible":
                assert request["type"] == "APPLY"
                sid, enabled = request["argArray"]
                assert type(enabled) is bool and self.find(sid) is not None
                self.find(sid)["visible"] = enabled
                # This acknowledges the store only; it proves no native frame state.
                result = None
            elif operation == "setAudioData":
                assert request["type"] == "APPLY"
                assert self.find(request["argArray"][0]) is not None
                result = self.apply_audio(request["argArray"])
            else:
                raise AssertionError("Unreviewed canvas operation: " + operation)
        elif method == "getSourceData":
            assert len(args) == 1 and isinstance(args[0], str)
            row = self.find(args[0])
            result = self.get_overrides.get(args[0], self.get_override)
            if result is UNSET:
                result = json.dumps(row, ensure_ascii=False) if row is not None else None
        elif method == "getAudioData":
            assert len(args) == 1
            row = self.find(args[0])
            result = self.audio_override if self.audio_override is not UNSET else (
                copy.deepcopy(row["payload"]["audio"]) if row is not None else None)
        elif method == "addSource":
            assert len(args) == 3
            params, sid, options = args
            assert options == {"shouldCreate": True}
            assert self.find(sid) is None
            assert params["secondSource"] is False and params["alwaysTop"] is False
            self.rows[0][sid] = copy.deepcopy(params)
            result = {"id": sid, "result": self.add_reply}
        elif method == "sourceTileFill":
            assert len(args) == 1 and self.find(args[0]) is not None
            result = None
        else:
            raise AssertionError("Unreviewed SDK method: " + method)
        if self.change_after == operation:
            self.owner = copy.deepcopy(self.changed_owner)
            self.change_after = None
        if (self.fail_on == operation and
                (self.fail_for_id is None or
                 (method in {"getSourceData", "getAudioData"} and args[0] == self.fail_for_id))):
            if self.fail_once:
                self.fail_on = None
            raise TimeoutError("PRIVATE_FAKE_DETAIL timed out after applying")
        return copy.deepcopy(result)

    def apply_audio(self, args):
        assert len(args) == 5
        sid, data, third, fourth, fifth = args
        assert third is True and fourth is True and fifth is False
        assert set(data) == {"setting"}
        setting = data["setting"]
        assert set(setting) == {"volume", "monitorType"}
        assert type(setting["volume"]) in (int, float) and setting["volume"] in (0, 1)
        assert type(setting["monitorType"]) is int and setting["monitorType"] in (0, 1)
        assert self.find(sid) is not None
        self.find(sid)["payload"]["audio"]["setting"].update(setting)
        return self.audio_reply

    def writes(self):
        return [(method, args) for method, args in self.calls
                if self.operation(method, args) in
                {"addSource", "sourceTileFill", "toggleVisible", "setAudioData"}]


class BridgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.legacy = self.root / "old-controller" / "bridge-manifest.json"
        self.legacy.parent.mkdir()
        self.record = legacy_record()
        self.legacy.write_text(json.dumps(self.record, ensure_ascii=False, indent=2), encoding="utf-8")
        self.original_legacy = self.legacy.read_bytes()
        self.sdk = FakeTransport()
        self.discovered = [copy.deepcopy(self.sdk.owner)]
        self.factory_calls = []
        self.stream_available = True
        self.case_counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, *, expected_pid):
        self.factory_calls.append(expected_pid)
        self.assertEqual(expected_pid, self.sdk.owner["server_pid"])
        return self.sdk

    def adapter(self, legacy=True):
        # Passing None selects the production legacy path. Always pass a temp
        # path, including for the deliberate "no manifest" case.
        legacy_path = self.legacy if legacy else self.root / "absent-legacy-manifest.json"
        return BridgeAdapter(self.runtime, legacy_manifest=legacy_path,
                             transport_factory=self.factory,
                             discoverer=lambda: copy.deepcopy(self.discovered),
                             stream_ready=lambda: self.stream_available)

    def fresh_case(self):
        """Subtests have independent durable state, not just fresh SDK rows."""
        self.case_counter += 1
        self.runtime = self.root / ("runtime-case-" + str(self.case_counter))
        self.sdk = FakeTransport()

    def connected(self, existing=True, legacy=True):
        if existing:
            self.sdk.rows[0][SID] = source_row()
        adapter = self.adapter(legacy)
        self.assert_result(adapter.connect(), ok=True)
        return adapter

    def same_owner_absent(self):
        """Normal create tests are distinct from an attached old restart record."""
        self.record["owner"] = copy.deepcopy(self.sdk.owner)
        self.legacy.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.original_legacy = self.legacy.read_bytes()
        return self.connected(existing=False)

    def recovery_candidate(self, *, enabled=True, audio=True):
        self.sdk.rows[0][OTHER_SID] = source_row(enabled=enabled, audio=audio)
        self.assertIsNone(self.sdk.find(SID))
        return self.connected(existing=False)

    def recovery_action(self, adapter, action):
        return adapter.ensure_existing_source() if action == "ensure" else adapter.set_enabled(False)

    def assert_restart_recovery_committed_before_setters(self, action):
        original = self.legacy.read_bytes()
        adapter = self.recovery_candidate(enabled=action != "ensure", audio=action != "ensure")
        checked = []
        def check_record_before_setting(operation):
            self.assertIn(operation, {"setAudioData", "toggleVisible"})
            archive = [json.loads(path.read_text(encoding="utf-8"))["record"]
                       for path in (self.runtime / "history").glob("bridge-manifest.*.json")]
            self.assertTrue(any(record["source_id"] == SID and record["owner"] == self.record["owner"]
                                for record in archive))
            active = json.loads((self.runtime / "bridge-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(active["source_id"], OTHER_SID)
            self.assertEqual(active["owner"], self.sdk.owner)
            checked.append(operation)
        self.sdk.before_write = check_record_before_setting
        result = self.assert_result(self.recovery_action(adapter, action), ok=True)
        self.assertEqual(result["source_id"], OTHER_SID)
        self.assertEqual(set(checked), {"setAudioData", "toggleVisible"})
        self.assertEqual(self.methods("addSource"), [])
        self.assertEqual(self.methods("removeSource"), [])
        self.assertEqual(self.methods("sourceTileFill"), [])
        self.assertIsNone(self.sdk.find(SID))
        self.assertEqual(self.legacy.read_bytes(), original)

    def assert_result(self, result, ok=None):
        self.assertIsInstance(result, dict)
        self.assertIs(type(result.get("ok")), bool)
        self.assertIsInstance(result.get("state"), str)
        self.assertTrue(result["state"])
        self.assertIsInstance(result.get("reason_zh"), str)
        self.assertTrue(result["reason_zh"])
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in result["reason_zh"]),
                        "The public reason must include a Chinese explanation")
        self.assertNotIn("PRIVATE_FAKE_DETAIL", json.dumps(result, ensure_ascii=False))
        if ok is not None:
            self.assertIs(result["ok"], ok)
        return result

    def assert_no_writes(self):
        self.assertEqual(self.sdk.writes(), [])

    def methods(self, name):
        return [args[0]["argArray"] if name == "setAudioData" else args
                for method, args in self.sdk.calls if FakeTransport.operation(method, args) == name]

    def assert_shared_store_checked(self):
        self.assertEqual(self.methods("getAllSource"), [])
        for operation in ("source", "topSource"):
            calls = self.methods(operation)
            self.assertTrue(calls)
            self.assertEqual({args[0]["index"] for args in calls}, {self.sdk.index})
            self.assertTrue(all(args[0]["type"] == "GET" and args[0]["argArray"] == [] for args in calls))

    def test_discovery_connect_and_status_are_read_only(self):
        self.sdk.rows[0][SID] = source_row()
        adapter = self.adapter()
        self.assert_result(adapter.discover(), ok=True)
        self.assert_result(adapter.connect(), ok=True)
        self.assert_result(adapter.read_status(), ok=True)
        self.assertEqual(self.factory_calls, [8100])
        self.assert_no_writes()
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)

    def test_no_or_ambiguous_owner_never_opens_sdk(self):
        for candidates, expected_ok in (([], True), ([owner(), owner(8101)], False)):
            with self.subTest(candidates=len(candidates)):
                self.discovered = candidates
                self.assert_result(self.adapter().connect(), ok=expected_ok)
        self.assertEqual(self.factory_calls, [])
        self.assert_no_writes()

    def test_unreviewed_process_path_refused(self):
        self.discovered[0]["server_path"] = r"D:\other\直播伴侣.exe"
        self.assert_result(self.adapter().connect(), ok=False)
        self.assertEqual(self.factory_calls, [])
        self.assert_no_writes()

    def test_invalid_legacy_identity_never_allows_source_writes(self):
        for field, value in (("source_id", "not-a-uuid"), ("name", "Other Bridge"),
                             ("endpoint", "remote-flv"), ("type", "image"),
                             ("view_index", False), ("new_settings", 1)):
            with self.subTest(field=field):
                self.fresh_case()
                bad_record = copy.deepcopy(self.record)
                bad_record[field] = value
                self.legacy.write_text(json.dumps(bad_record, ensure_ascii=False), encoding="utf-8")
                old_bytes = self.legacy.read_bytes()
                adapter = self.adapter()
                result = adapter.connect()
                self.assert_result(result)
                if result["ok"]:
                    self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()
                self.assertEqual(self.legacy.read_bytes(), old_bytes)

    def test_unsupported_canvas_or_settings_never_allows_writes(self):
        for field, value in (("index", 2), ("index", False), ("new_settings", 1)):
            with self.subTest(field=field):
                self.fresh_case()
                setattr(self.sdk, field, value)
                adapter = self.connected(existing=False)
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_connected_identity_must_match_discovered_process_lifetime(self):
        self.sdk.owner["server_start_filetime"] += 1
        self.assert_result(self.adapter().connect(), ok=False)
        self.assert_no_writes()
        self.assertEqual(self.sdk.close_calls, 0)

    def test_new_owner_adopts_recorded_existing_source_without_add(self):
        adapter = self.connected()
        result = self.assert_result(adapter.ensure_existing_source(enabled=True, audio=True), ok=True)
        self.assertEqual(result.get("source_id"), SID)
        self.assertEqual(self.methods("addSource"), [])
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)
        self.assert_shared_store_checked()
        documents = []
        for path in self.runtime.rglob("*.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            documents.append(document.get("record", document))
        self.assertTrue(any(item.get("source_id") == SID and item.get("owner") == self.sdk.owner
                            for item in documents), "Adoption must save the new process lifetime")
        self.assertTrue(any(item.get("owner") == self.record["owner"] for item in documents),
                        "The old owner's record must be archived separately")

    def test_absence_in_both_shared_buckets_creates_exact_reviewed_source_once(self):
        adapter = self.same_owner_absent()
        result = self.assert_result(adapter.ensure_existing_source(), ok=True)
        self.assert_shared_store_checked()
        calls = self.methods("addSource")
        self.assertEqual(len(calls), 1)
        params, sid, options = calls[0]
        self.assertEqual(params, source_row())
        self.assertEqual(options, {"shouldCreate": True})
        self.assertEqual(result.get("source_id"), sid)
        self.assertEqual(self.methods("sourceTileFill"), [[sid]])
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)
        self.assert_result(adapter.ensure_existing_source(), ok=True)
        self.assertEqual(len(self.methods("addSource")), 1)

    def test_adoption_archive_and_current_owner_commit_precede_sdk_write(self):
        adapter = self.connected()
        checked = []
        def check_durable_provenance(operation):
            archives = list((self.runtime / "history").glob("bridge-manifest.*.json"))
            self.assertTrue(archives, "Archive must exist before " + operation)
            self.assertTrue(any(json.loads(path.read_text(encoding="utf-8"))["record"].get("owner") == self.record["owner"]
                                for path in archives))
            active = json.loads((self.runtime / "bridge-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(active["owner"], self.sdk.owner)
            self.assertEqual(active["source_id"], SID)
            checked.append(operation)
        self.sdk.before_write = check_durable_provenance
        self.assert_result(adapter.ensure_existing_source(enabled=False, audio=False))
        self.assertTrue(checked, "The explicit disable/audio change should request SDK settings")
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)

    def test_second_canvas_creation_keeps_view_and_scope(self):
        self.sdk.index = 1
        adapter = self.connected(existing=False, legacy=False)
        result = self.assert_result(adapter.ensure_existing_source(), ok=True)
        params, sid, _ = self.methods("addSource")[0]
        self.assertEqual(params, source_row(index=1))
        self.assertEqual(result.get("source_id"), sid)
        self.assert_shared_store_checked()
        self.assertIn(sid, self.sdk.rows[0], "secondSource=False registers in data, independent of viewIndex")
        self.assertEqual(self.sdk.rows[1], {})

    def test_missing_inactive_second_canvas_does_not_block_existing_source(self):
        self.sdk.missing_canvases.add(1)
        adapter = self.connected()
        result = self.assert_result(adapter.ensure_existing_source(audio=False), ok=True)
        self.assertEqual(result.get("source_id"), SID)
        self.assertEqual(self.methods("addSource"), [])
        self.assert_shared_store_checked()
        scoped_calls = [args[0] for method, args in self.sdk.calls if method == "CANVAS_CALL"]
        self.assertTrue(scoped_calls)
        self.assertEqual({request["index"] for request in scoped_calls}, {0})

    def test_current_canvas_one_adopts_native_view_one_source_from_data(self):
        self.sdk.index = 1
        self.record["view_index"] = 1
        self.legacy.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        original = self.legacy.read_bytes()
        self.sdk.rows[0][SID] = source_row(index=1, enabled=False, audio=False)
        adapter = self.connected(existing=False)
        result = self.assert_result(adapter.ensure_existing_source(enabled=True, audio=True), ok=True)
        self.assertEqual(result.get("source_id"), SID)
        self.assertEqual(self.methods("addSource"), [])
        self.assert_shared_store_checked()
        for method, args in self.sdk.calls:
            if FakeTransport.operation(method, args) in {"toggleVisible", "setAudioData"}:
                self.assertEqual(args[0]["index"], 1)
        self.assertTrue(self.sdk.rows[0][SID]["visible"])
        self.assertEqual(self.sdk.rows[0][SID]["payload"]["audio"]["setting"]["volume"], 1)
        self.assertEqual(self.sdk.rows[1], {})
        self.assertEqual(self.legacy.read_bytes(), original)

    def test_second_source_false_recorded_only_in_data2_is_refused(self):
        self.sdk.rows[1][SID] = source_row(index=0)
        adapter = self.connected(existing=False)
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(self.methods("addSource"), [])
        self.assert_no_writes()
        self.assert_shared_store_checked()

    def test_native_view_one_source_uses_current_canvas_zero_shared_setters(self):
        self.sdk.index = 0
        self.sdk.missing_canvases.add(1)
        self.record["view_index"] = 1
        self.legacy.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.sdk.rows[0][SID] = source_row(index=1)
        adapter = self.connected(existing=False)
        result = self.assert_result(adapter.set_enabled(False), ok=True)
        self.assertTrue(result.get("local_stream_pause_required"))
        self.assertEqual(self.sdk.rows[0][SID]["viewIndex"], 1)
        self.assertFalse(self.sdk.rows[0][SID]["visible"])
        self.assertEqual(self.methods("addSource"), [])
        self.assert_shared_store_checked()
        for method, args in self.sdk.calls:
            if FakeTransport.operation(method, args) in {"toggleVisible", "setAudioData"}:
                self.assertEqual(args[0]["index"], 0)

    def test_same_name_different_id_in_either_shared_bucket_or_global_top_refused(self):
        # A unique data candidate can now be re-identified across a restart.
        # Candidates from data2 or global top remain unsafe to adopt.
        for bucket, top in ((1, False), (0, True)):
            with self.subTest(bucket=bucket, top=top):
                self.fresh_case()
                (self.sdk.top if top else self.sdk.rows[bucket])[OTHER_SID] = source_row()
                adapter = self.connected(existing=False)
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_existing_source_identity_and_decode_settings_are_strict(self):
        changes = [
            ("name", "Different Bridge"), ("id", OTHER_SID),
            ("type", "image"), ("viewIndex", 1), ("viewIndex", False),
            ("secondSource", True), ("visible", 1), ("alwaysTop", True),
            ("liveScene", "private-scene"), ("parentID", "group-id"),
            ("duplicate", True), ("payload.url", "http://example.invalid/private"),
            ("payload.hardwareDecode", True), ("payload.bufferSize", 4),
            ("payload.timeout", 11), ("payload.activeRestartPlay", True),
            ("payload.playEndNoDisplay", True), ("payload.linearAlpha", True),
            ("payload.colorRange", 2), ("payload.ffmpegOptions", "thread_queue_size=4096"),
            ("payload.audio.setting.volume", True),
            ("payload.audio.setting.balancing", 0.8),
            ("payload.audio.setting.monitorType", False),
            ("payload.audio.setting.syncOffset", 100),
            ("payload.audio.setting.syncOffset", False),
            ("payload.audio.setting.isDownmixMono", True),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.fresh_case()
                row = source_row()
                target = row
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                self.sdk.rows[0][SID] = row
                adapter = self.connected(existing=False)
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_missing_required_source_field_is_not_a_valid_identity(self):
        for field in ("visible", "alwaysTop", "liveScene", "payload.timeout",
                      "payload.audio.setting.syncOffset"):
            with self.subTest(field=field):
                self.fresh_case()
                row = source_row()
                target = row
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                del target[parts[-1]]
                self.sdk.rows[0][SID] = row
                adapter = self.connected(existing=False)
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_recorded_id_duplicated_across_shared_data_buckets_or_global_top_refused(self):
        for top in (False, True):
            with self.subTest(top=top):
                self.fresh_case()
                adapter = self.connected()
                (self.sdk.top if top else self.sdk.rows[1])[SID] = source_row()
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_top_entry_cannot_hide_same_name_collision_in_ordinary_map(self):
        adapter = self.connected(existing=False)
        self.sdk.rows[0][OTHER_SID] = source_row()
        self.sdk.top[OTHER_SID] = {"name": "Unrelated Top Source", "type": "image"}
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assert_no_writes()

    def test_none_getter_with_recorded_canvas_row_is_not_global_absence(self):
        adapter = self.connected()
        self.sdk.get_override = None
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assert_no_writes()

    def test_getter_malformed_value_is_not_source_absence(self):
        for value in ({}, [], "not-json", "null", False):
            with self.subTest(value=value):
                self.fresh_case()
                self.sdk.get_override = value
                adapter = self.connected(existing=False)
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_malformed_shared_source_or_global_top_never_allows_add(self):
        for operation, malformed in (("source", None), ("source", "null"), ("source", {}),
                                      ("source", {"data": {}, "order": [], "data2": {}}),
                                      ("source", {"data": {}, "order": None, "data2": {}, "order2": []}),
                                      ("source", {"data": {}, "order": [], "data2": {}, "order2": [1]}),
                                      ("source", {"data": {}, "order": [OTHER_SID], "data2": {}, "order2": []}),
                                      ("source", {"data": {OTHER_SID: {"name": "Other", "type": "image"}},
                                                  "order": [OTHER_SID, OTHER_SID], "data2": {}, "order2": []}),
                                      ("source", {"data": {}, "order": [], "data2": None, "order2": []}),
                                      ("topSource", {"data": {}, "order": [OTHER_SID]}),
                                      ("topSource", {"data": {OTHER_SID: {"name": "Other", "type": "image"}},
                                                     "order": [OTHER_SID, OTHER_SID]}),
                                      ("topSource", {"order": [], "data": []})):
            with self.subTest(operation=operation, malformed=malformed):
                self.fresh_case()
                if operation == "source":
                    self.sdk.source_override = malformed
                else:
                    self.sdk.top_override = malformed
                adapter = self.connected(existing=False)
                self.assert_result(adapter.ensure_existing_source(), ok=False)
                self.assert_no_writes()

    def test_owner_change_during_read_prevents_any_write(self):
        adapter = self.connected()
        self.sdk.change_after = "getUseNewSettings"
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assert_no_writes()
        self.assertEqual(self.sdk.close_calls, 0)

    def test_owner_change_after_add_prevents_followup_write(self):
        adapter = self.same_owner_absent()
        self.sdk.change_after = "addSource"
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(len(self.methods("addSource")), 1)
        self.assertEqual(self.methods("sourceTileFill"), [])
        self.assertEqual(self.methods("setAudioData"), [])
        self.assertEqual(self.methods("toggleVisible"), [])

    def test_add_timeout_never_retries_or_fills(self):
        adapter = self.same_owner_absent()
        self.sdk.fail_on = "addSource"
        self.sdk.fail_once = True
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(len(self.methods("addSource")), 1)
        self.assertEqual(self.methods("sourceTileFill"), [])
        self.assertEqual(self.methods("setAudioData"), [])

    def test_unconfirmed_add_never_fills(self):
        self.sdk.add_reply = False
        adapter = self.same_owner_absent()
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(len(self.methods("addSource")), 1)
        self.assertEqual(self.methods("sourceTileFill"), [])

    def test_audio_setting_requires_true_ack_even_if_readback_matches(self):
        for reply in (False, None, 1, {"result": True}):
            with self.subTest(reply=reply):
                self.fresh_case()
                self.sdk.audio_reply = reply
                adapter = self.connected()
                result = self.assert_result(adapter.set_enabled(False), ok=False)
                self.assertTrue(result.get("local_stream_pause_required"))
                self.assertEqual(len(self.methods("setAudioData")), 1)

    def test_audio_timeout_never_repeats_an_uncertain_write(self):
        adapter = self.connected()
        self.sdk.fail_on = "setAudioData"
        self.sdk.fail_once = True
        first = self.assert_result(adapter.set_enabled(False), ok=False)
        second = self.assert_result(adapter.set_enabled(False), ok=False)
        self.assertTrue(first.get("local_stream_pause_required"))
        self.assertTrue(second.get("local_stream_pause_required"))
        self.assertEqual(len(self.methods("setAudioData")), 1)

    def test_audio_readback_must_be_numeric_and_match_requested_setting(self):
        for value in (None, {"setting": None},
                      {"setting": {"volume": False, "monitorType": False}},
                      {"setting": {"volume": 1, "monitorType": 1}}):
            with self.subTest(readback=value):
                self.fresh_case()
                adapter = self.connected()
                self.sdk.audio_override = value
                self.assert_result(adapter.set_enabled(False), ok=False)
                self.assertLessEqual(len(self.methods("setAudioData")), 1)

    def test_disable_with_absent_source_never_creates_one(self):
        adapter = self.same_owner_absent()
        result = self.assert_result(adapter.set_enabled(False))
        self.assertTrue(result.get("local_stream_pause_required"))
        self.assert_no_writes()

    def test_disable_is_scoped_and_does_not_claim_native_visibility(self):
        adapter = self.connected()
        unrelated = {"name": "Other Source", "type": "image", "viewIndex": 1,
                     "payload": {"url": "PRIVATE_FAKE_DETAIL"}}
        self.sdk.rows[1][OTHER_SID] = copy.deepcopy(unrelated)
        result = self.assert_result(adapter.set_enabled(False))
        self.assertTrue(result.get("local_stream_pause_required"))
        self.assertIsNot(result.get("native_visible_verified"), True)
        self.assertEqual(self.methods("setAudioData"),
                         [[SID, {"setting": {"volume": 0, "monitorType": 0}}, True, True, False]])
        visible = self.methods("toggleVisible")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0][0]["index"], 0)
        self.assertEqual(visible[0][0]["argArray"], [SID, False])
        self.assertEqual(self.sdk.rows[1][OTHER_SID], unrelated)
        self.assertEqual(self.methods("addSource"), [])

    def test_missing_local_stream_blocks_create_but_still_allows_disable(self):
        adapter = self.same_owner_absent()
        self.stream_available = False
        result = self.assert_result(adapter.ensure_existing_source(enabled=True), ok=True)
        self.assertFalse(result.get("local_stream_verified"))
        self.assertEqual(result["state"], "waiting_stream")
        self.assert_no_writes()
        self.sdk.rows[0][SID] = source_row()
        result = self.assert_result(adapter.set_enabled(False))
        self.assertTrue(result.get("local_stream_pause_required"))
        self.assertEqual(len(self.methods("setAudioData")), 1)

    def test_runtime_archive_failure_precedes_any_sdk_write(self):
        adapter = self.connected()
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "history").write_text("archive cannot be a directory", encoding="utf-8")
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assert_no_writes()
        calls_after_failure = copy.deepcopy(self.sdk.calls)
        # A second button press must stay blocked even if the source vanishes
        # and the archive directory becomes writable in the meantime.
        self.sdk.rows[0].pop(SID)
        (self.runtime / "history").unlink()
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(self.sdk.calls, calls_after_failure)
        self.assertEqual(self.methods("addSource"), [])
        self.assert_no_writes()
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)

    def test_save_failure_after_successful_add_blocks_all_followup_writes(self):
        adapter = self.same_owner_absent()
        original_save = adapter._save_record
        def fail_only_attached_save(record):
            if record.get("stage") == "attached":
                raise OSError("PRIVATE_FAKE_DETAIL disk failure after successful add")
            return original_save(record)
        with mock.patch.object(adapter, "_save_record", side_effect=fail_only_attached_save):
            self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(len(self.methods("addSource")), 1)
        added_id = self.methods("addSource")[0][1]
        self.assertIsNotNone(self.sdk.find(added_id), "The simulated server did apply addSource")
        durable = json.loads((self.runtime / "bridge-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(durable["stage"], "planned")
        writes_after_failure = copy.deepcopy(self.sdk.writes())
        self.assert_result(adapter.ensure_existing_source(), ok=False)
        self.assertEqual(self.sdk.writes(), writes_after_failure)
        self.assertEqual(len(self.methods("addSource")), 1)
        self.assertEqual(self.methods("setAudioData"), [])
        self.assertEqual(self.methods("toggleVisible"), [])
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)

    def test_safe_disconnect_keeps_live_pipe_until_process_exit(self):
        adapter = self.connected()
        self.assertFalse(self.assert_result(adapter.disconnect_safe())["disconnected"])
        self.assertEqual(self.sdk.close_calls, 0)
        self.sdk.is_alive = False
        self.assertTrue(self.assert_result(adapter.disconnect_safe())["disconnected"])
        self.assertEqual(self.sdk.close_calls, 1)
        self.assert_no_writes()

    def test_safe_disconnect_then_new_process_connects(self):
        adapter = self.connected()
        old_sdk = self.sdk
        self.assert_result(adapter.disconnect_safe())
        self.assertEqual(old_sdk.close_calls, 0)
        old_sdk.is_alive = False
        self.assert_result(adapter.disconnect_safe())
        self.assertEqual(old_sdk.close_calls, 1)
        self.sdk = FakeTransport(owner(8200, 134046578000000000))
        self.sdk.rows[0][SID] = source_row()
        self.discovered = [copy.deepcopy(self.sdk.owner)]
        self.assert_result(adapter.connect(), ok=True)
        self.assert_result(adapter.read_status(), ok=True)
        self.assertEqual(self.factory_calls, [8100, 8200])
        self.assertEqual(self.sdk.close_calls, 0)
        self.assert_no_writes()

    def test_disable_without_connection_never_discovers_or_adds(self):
        adapter = self.adapter()
        result = self.assert_result(adapter.set_enabled(False), ok=True)
        self.assertIs(result.get("no_owned_source"), True)
        self.assertIs(result.get("local_stream_pause_required"), True)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.sdk.calls, [])

    def test_saved_metadata_and_public_status_never_copy_unknown_legacy_fields(self):
        record = copy.deepcopy(self.record)
        record.update(private_token="PRIVATE_FAKE_DETAIL", payload={"url": "PRIVATE_FAKE_DETAIL"})
        self.legacy.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        original = self.legacy.read_bytes()
        adapter = self.connected()
        self.assert_result(adapter.ensure_existing_source(), ok=True)
        self.assert_result(adapter.read_status(), ok=True)
        self.assertEqual(self.legacy.read_bytes(), original)
        documents = list(self.runtime.rglob("*.json"))
        self.assertTrue(documents)
        for path in documents:
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_FAKE_DETAIL", saved)
            self.assertNotIn("private_token", saved)
            self.assertNotIn('"payload"', saved)
            self.assertNotIn(URL, saved)

    def test_restart_new_uuid_ensure_commits_owner_and_id_before_settings(self):
        self.assert_restart_recovery_committed_before_setters("ensure")

    def test_restart_new_uuid_stop_commits_owner_and_id_before_settings(self):
        self.assert_restart_recovery_committed_before_setters("stop")

    def test_restart_attached_record_without_candidate_never_creates(self):
        for action in ("ensure", "stop"):
            with self.subTest(action=action):
                self.fresh_case()
                adapter = self.connected(existing=False)
                self.assert_result(self.recovery_action(adapter, action), ok=False)
                self.assert_no_writes()
                self.assertEqual(self.legacy.read_bytes(), self.original_legacy)
                self.assertFalse((self.runtime / "bridge-manifest.json").exists())

    def test_restart_new_uuid_rejects_unsafe_candidates_before_manifest_commit(self):
        cases = ("same_owner", "stage_planned", "stage_removed", "stage_unknown",
                 "multiple", "incomplete", "getter_none", "getter_json_null",
                 "getter_disagrees", "audio_none", "audio_malformed", "only_data2",
                 "only_top", "settings_differ", "view_differs", "old_getter_json_null",
                 "old_id_still_registered")
        for action in ("ensure", "stop"):
            for case in cases:
                with self.subTest(action=action, case=case):
                    self.fresh_case()
                    self.record = legacy_record()
                    self.sdk.rows[0][OTHER_SID] = source_row()
                    if case == "same_owner":
                        self.record["owner"] = copy.deepcopy(self.sdk.owner)
                    elif case.startswith("stage_"):
                        self.record["stage"] = {"stage_planned": "planned", "stage_removed": "removed",
                                                "stage_unknown": "add_outcome_unknown"}[case]
                    elif case == "multiple":
                        self.sdk.rows[0][THIRD_SID] = source_row()
                    elif case == "incomplete":
                        del self.sdk.rows[0][OTHER_SID]["payload"]["timeout"]
                    elif case == "getter_none":
                        self.sdk.get_overrides[OTHER_SID] = None
                    elif case == "getter_json_null":
                        self.sdk.get_overrides[OTHER_SID] = "null"
                    elif case == "getter_disagrees":
                        row = copy.deepcopy(self.sdk.rows[0][OTHER_SID])
                        row["visible"] = False
                        self.sdk.get_overrides[OTHER_SID] = json.dumps(row, ensure_ascii=False)
                    elif case == "audio_none":
                        self.sdk.audio_override = None
                    elif case == "audio_malformed":
                        self.sdk.audio_override = {"setting": {"volume": False, "monitorType": False}}
                    elif case == "only_data2":
                        self.sdk.rows[1][OTHER_SID] = self.sdk.rows[0].pop(OTHER_SID)
                    elif case == "only_top":
                        self.sdk.top[OTHER_SID] = self.sdk.rows[0].pop(OTHER_SID)
                    elif case == "settings_differ":
                        self.sdk.new_settings = False
                    elif case == "view_differs":
                        self.sdk.rows[0][OTHER_SID]["viewIndex"] = 1
                    elif case == "old_getter_json_null":
                        self.sdk.get_overrides[SID] = "null"
                    elif case == "old_id_still_registered":
                        self.sdk.rows[1][SID] = source_row()
                        self.sdk.get_overrides[SID] = None
                    self.legacy.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
                    original = self.legacy.read_bytes()
                    adapter = self.connected(existing=False)
                    self.assert_result(self.recovery_action(adapter, action), ok=False)
                    self.assert_no_writes()
                    self.assertEqual(self.legacy.read_bytes(), original)
                    self.assertFalse((self.runtime / "bridge-manifest.json").exists())
                    self.assertEqual(list((self.runtime / "history").glob("*.json")), [])

    def test_restart_recovery_archive_or_save_failure_blocks_settings_and_retry(self):
        for action in ("ensure", "stop"):
            for failure in ("archive", "save"):
                with self.subTest(action=action, failure=failure):
                    self.fresh_case()
                    adapter = self.recovery_candidate()
                    original = self.legacy.read_bytes()
                    self.runtime.mkdir(parents=True, exist_ok=True)
                    if failure == "archive":
                        (self.runtime / "history").write_text("not a directory", encoding="utf-8")
                        self.assert_result(self.recovery_action(adapter, action), ok=False)
                        (self.runtime / "history").unlink()
                    else:
                        original_save = adapter._save_record
                        def fail_recovered_record(record):
                            if record.get("source_id") == OTHER_SID:
                                raise OSError("PRIVATE_FAKE_DETAIL recovery commit failed")
                            return original_save(record)
                        with mock.patch.object(adapter, "_save_record", side_effect=fail_recovered_record):
                            self.assert_result(self.recovery_action(adapter, action), ok=False)
                    self.assert_no_writes()
                    calls_after_failure = copy.deepcopy(self.sdk.calls)
                    self.assert_result(self.recovery_action(adapter, action), ok=False)
                    self.assertEqual(self.sdk.calls, calls_after_failure)
                    self.assert_no_writes()
                    self.assertEqual(self.legacy.read_bytes(), original)
                    self.assertFalse((self.runtime / "bridge-manifest.json").exists())

    def test_restart_recovery_unknown_audio_setter_never_repeats(self):
        for action in ("ensure", "stop"):
            with self.subTest(action=action):
                self.fresh_case()
                adapter = self.recovery_candidate(enabled=action != "ensure", audio=action != "ensure")
                self.sdk.fail_on = "setAudioData"
                self.sdk.fail_once = True
                self.assert_result(self.recovery_action(adapter, action), ok=False)
                writes = copy.deepcopy(self.sdk.writes())
                self.assert_result(self.recovery_action(adapter, action), ok=False)
                self.assertEqual(self.sdk.writes(), writes)
                self.assertEqual(len(self.methods("setAudioData")), 1)
                self.assertEqual(self.methods("addSource"), [])
                self.assertEqual(self.methods("removeSource"), [])
                active = json.loads((self.runtime / "bridge-manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(active["source_id"], OTHER_SID)
                self.assertEqual(active["owner"], self.sdk.owner)

    def test_read_status_never_recovers_or_commits_a_restart_candidate(self):
        adapter = self.recovery_candidate()
        original = self.legacy.read_bytes()
        result = self.assert_result(adapter.read_status())
        self.assertEqual(result["source_id"], SID)
        self.assert_no_writes()
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertFalse((self.runtime / "bridge-manifest.json").exists())
        self.assertEqual(list((self.runtime / "history").glob("*.json")), [])

    def test_restart_candidate_getter_or_audio_timeout_blocks_retry_before_archive(self):
        for action in ("ensure", "stop"):
            for method in ("getSourceData", "getAudioData"):
                with self.subTest(action=action, method=method):
                    self.fresh_case()
                    adapter = self.recovery_candidate()
                    original = self.legacy.read_bytes()
                    self.sdk.fail_on = method
                    self.sdk.fail_for_id = OTHER_SID
                    self.sdk.fail_once = True
                    self.assert_result(self.recovery_action(adapter, action), ok=False)
                    calls = copy.deepcopy(self.sdk.calls)
                    self.assert_result(self.recovery_action(adapter, action), ok=False)
                    self.assertEqual(self.sdk.calls, calls)
                    self.assertEqual(sum(called == method and args == [OTHER_SID]
                                         for called, args in self.sdk.calls), 1)
                    self.assert_no_writes()
                    self.assertEqual(self.legacy.read_bytes(), original)
                    self.assertFalse((self.runtime / "bridge-manifest.json").exists())
                    self.assertEqual(list((self.runtime / "history").glob("*.json")), [])

    def test_restart_candidate_owner_change_after_audio_read_prevents_archive(self):
        for action in ("ensure", "stop"):
            with self.subTest(action=action):
                self.fresh_case()
                adapter = self.recovery_candidate()
                original = self.legacy.read_bytes()
                self.sdk.change_after = "getAudioData"
                self.assert_result(self.recovery_action(adapter, action), ok=False)
                calls = copy.deepcopy(self.sdk.calls)
                self.assert_result(self.recovery_action(adapter, action), ok=False)
                self.assertEqual(self.sdk.calls, calls)
                self.assert_no_writes()
                self.assertEqual(self.legacy.read_bytes(), original)
                self.assertFalse((self.runtime / "bridge-manifest.json").exists())
                self.assertEqual(list((self.runtime / "history").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
