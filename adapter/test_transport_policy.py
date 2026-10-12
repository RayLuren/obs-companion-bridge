"""Pure allowlist tests: no transport instance, process lookup, or SDK client.

The only production operation invoked is ReviewedTransport._allowed.
"""
import copy
import unittest

from windows_transport import ReviewedTransport


SID = "71d02712-5c41-48d6-92aa-c6632b3737aa"
URL = "http://127.0.0.1:18081/live?port=19360&app=obs&stream=main"
NAME = "OBS 本机桥接（Codex）"


def reviewed_add(index=0, audio=True):
    return [{"name": NAME, "type": "onlineMedia", "viewIndex": index,
             "secondSource": False, "visible": True, "alwaysTop": False,
             "liveScene": "", "payload": {
                 "url": URL, "hardwareDecode": False, "bufferSize": 2,
                 "timeout": 10, "activeRestartPlay": False,
                 "playEndNoDisplay": False, "linearAlpha": False,
                 "colorRange": 3, "ffmpegOptions": "", "audio": {"setting": {
                     "volume": 1 if audio else 0, "balancing": 0.5,
                     "monitorType": 1 if audio else 0, "syncOffset": 0,
                     "isDownmixMono": False}}}}, SID, {"shouldCreate": True}]


def canvas(operation, args=None, index=0, new_settings=True, kind="APPLY"):
    prefix = ["stage", "baseEnv", "sourceStore"] if new_settings else ["sourceStore"]
    return [{"type": kind, "index": index, "path": prefix + [operation],
             "argArray": [] if args is None else copy.deepcopy(args)}]


class TransportPolicyTests(unittest.TestCase):
    allow = staticmethod(ReviewedTransport._allowed)

    def assert_allowed(self, method, args, expected):
        result = self.allow(method, args)
        self.assertIs(type(result), bool)
        self.assertIs(result, expected)

    def test_exact_reviewed_add_is_allowed_on_both_canvases_with_audio_or_mute(self):
        for index in (0, 1):
            for audio in (False, True):
                with self.subTest(index=index, audio=audio):
                    self.assert_allowed("addSource", reviewed_add(index, audio), True)

    def test_exact_getters_and_fill_are_allowed(self):
        for method, args in (("getUseNewSettings", []), ("getCanvasScene", []),
                             ("getSourceData", [SID]), ("getAudioData", [SID]),
                             ("sourceTileFill", [SID])):
            with self.subTest(method=method):
                self.assert_allowed(method, args, True)

    def test_reviewed_canvas_getters_and_two_scoped_setters_are_allowed(self):
        for index in (0, 1):
            for new_settings in (False, True):
                requests = [canvas("getAllSource", index=index, new_settings=new_settings),
                            canvas("source", index=index, new_settings=new_settings, kind="GET"),
                            canvas("topSource", index=index, new_settings=new_settings, kind="GET")]
                for enabled in (False, True):
                    requests.append(canvas("toggleVisible", [SID, enabled], index, new_settings))
                    setting = {"volume": int(enabled), "monitorType": int(enabled)}
                    requests.append(canvas("setAudioData", [SID, {"setting": setting}, True, True, False],
                                           index, new_settings))
                for request in requests:
                    with self.subTest(request=request):
                        self.assert_allowed("CANVAS_CALL", request, True)

    def test_unreviewed_and_top_level_mutations_are_refused(self):
        for method, args in (("arbitraryRPC", []), ("startNginx", []),
                             ("removeSource", [SID, 0]), ("updateSource", [SID, {}]),
                             ("toggleVisible", [SID, False]),
                             ("setAudioData", [SID, {"setting": {"volume": 0, "monitorType": 0}},
                                               True, True, False])):
            with self.subTest(method=method):
                self.assert_allowed(method, args, False)

    def test_canvas_index_alias_path_and_request_kind_are_refused(self):
        original = canvas("toggleVisible", [SID, False])
        changes = [("index", 2), ("index", True), ("index", False), ("index", 0.0),
                   ("path", ["stage", "sourceStore", "toggleVisible"]),
                   ("path", ["stage", "baseEnv", "sourceStore", "toggleVisible", "extra"]),
                   ("path", ["stage", "baseEnv", "sourceStore", "setVisible"]),
                   ("path", "stage.baseEnv.sourceStore.toggleVisible"),
                   ("type", "GET"), ("type", "SET"), ("type", "apply")]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                request = copy.deepcopy(original)
                request[0][field] = value
                self.assert_allowed("CANVAS_CALL", request, False)

    def test_shared_source_getter_requires_exact_property_path_and_no_arguments(self):
        for new_settings in (False, True):
            for index in (0, 1):
                with self.subTest(new_settings=new_settings, index=index):
                    self.assert_allowed("CANVAS_CALL", canvas("source", index=index,
                                        new_settings=new_settings, kind="GET"), True)
        for request in (canvas("source", [SID], kind="GET"),
                        canvas("source", kind="APPLY"),
                        canvas("source", kind="SET"),
                        canvas("source", index=True, kind="GET"),
                        canvas("source", index=2, kind="GET")):
            self.assert_allowed("CANVAS_CALL", request, False)
        request = canvas("source", kind="GET")
        request[0]["path"] += ["data"]
        self.assert_allowed("CANVAS_CALL", request, False)

    def test_toggle_requires_explicit_boolean_visibility_and_no_extra_arguments(self):
        for args in ([], [SID], [SID, None], [SID, 0], [SID, 1], [SID, "false"],
                     [SID, False, "extra"], ["non-uuid", False]):
            with self.subTest(args=args):
                self.assert_allowed("CANVAS_CALL", canvas("toggleVisible", args), False)

    def test_audio_setter_rejects_boolean_numbers_and_changed_flags(self):
        for field in ("volume", "monitorType"):
            for value in (False, True, 0.0, -1, 2, "0"):
                with self.subTest(field=field, value=value):
                    setting = {"volume": 0, "monitorType": 0}
                    setting[field] = value
                    args = [SID, {"setting": setting}, True, True, False]
                    self.assert_allowed("CANVAS_CALL", canvas("setAudioData", args), False)
        for flags in ([1, True, False], [True, 1, False], [True, True, 0],
                      [False, True, False], [True, True, True]):
            with self.subTest(flags=flags):
                args = [SID, {"setting": {"volume": 0, "monitorType": 0}}] + flags
                self.assert_allowed("CANVAS_CALL", canvas("setAudioData", args), False)

    def test_add_rejects_external_or_aliased_url(self):
        for url in ("https://example.invalid/live", "http://127.0.0.1:18081/other",
                    "http://localhost:18081/live?port=19360&app=obs&stream=main", URL + "&extra=1"):
            with self.subTest(url=url):
                args = reviewed_add()
                args[0]["payload"]["url"] = url
                self.assert_allowed("addSource", args, False)

    def test_add_requires_reviewed_identity_and_explicit_creation_visibility(self):
        for field, value in (("name", "Other"), ("type", "image"), ("viewIndex", 2),
                             ("viewIndex", True), ("viewIndex", False), ("secondSource", 0),
                             ("secondSource", True), ("visible", False), ("visible", 1),
                             ("visible", None), ("alwaysTop", 0), ("alwaysTop", True),
                             ("liveScene", "OtherScene")):
            with self.subTest(field=field, value=value):
                args = reviewed_add()
                args[0][field] = value
                self.assert_allowed("addSource", args, False)
        for value in (False, 1, 0, None, "true"):
            with self.subTest(should_create=value):
                args = reviewed_add()
                args[2]["shouldCreate"] = value
                self.assert_allowed("addSource", args, False)
        for args in (reviewed_add()[:2], reviewed_add() + ["extra"]):
            self.assert_allowed("addSource", args, False)
        args = reviewed_add()
        del args[0]["visible"]
        self.assert_allowed("addSource", args, False)

    def test_add_rejects_changed_media_settings_and_boolean_audio_numbers(self):
        fields = [("hardwareDecode", True), ("bufferSize", 3), ("bufferSize", True),
                  ("timeout", 11), ("activeRestartPlay", True), ("playEndNoDisplay", True),
                  ("linearAlpha", True), ("colorRange", 2), ("ffmpegOptions", "anything")]
        for field, value in fields:
            with self.subTest(field=field, value=value):
                args = reviewed_add()
                args[0]["payload"][field] = value
                self.assert_allowed("addSource", args, False)
        for field, value in (("volume", True), ("monitorType", False), ("balancing", 0.4),
                             ("syncOffset", False), ("syncOffset", 100), ("isDownmixMono", True)):
            with self.subTest(audio_field=field, value=value):
                args = reviewed_add()
                args[0]["payload"]["audio"]["setting"][field] = value
                self.assert_allowed("addSource", args, False)

    def test_getter_arguments_and_extra_request_fields_are_refused(self):
        for method, args in (("getUseNewSettings", [True]), ("getCanvasScene", [0]),
                             ("getSourceData", []), ("getAudioData", ["non-uuid"]),
                             ("getSourceData", [SID, "extra"]), ("sourceTileFill", [True]),
                             ("getUseNewSettings", ()), ("CANVAS_CALL", {})):
            with self.subTest(method=method, args=args):
                self.assert_allowed(method, args, False)
        request = canvas("getAllSource")
        request[0]["extra"] = True
        self.assert_allowed("CANVAS_CALL", request, False)
        args = reviewed_add()
        args[2]["other"] = True
        self.assert_allowed("addSource", args, False)


if __name__ == "__main__":
    unittest.main()
