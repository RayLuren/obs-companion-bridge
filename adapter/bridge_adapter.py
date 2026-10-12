"""One reviewed Companion connection and one explicitly owned local source.

Starting/importing this module never adds a source. Raw SDK data stays in memory;
public results, manifests and archives contain only a fixed field allowlist.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import threading
import time
import urllib.request
import uuid

EXPECTED_EXE = r"D:\webcast_mate\13.1.1.502547155\直播伴侣.exe"
NAME = "OBS 本机桥接（Codex）"
SOURCE_TYPE = "onlineMedia"
SOURCE_URL = "http://127.0.0.1:18081/live?port=19360&app=obs&stream=main"
STAT_URL = "http://127.0.0.1:18081/stat"
LEGACY_TOOL = "codex-local-obs-bridge-v1"
TOOL = "obs-companion-desktop-bridge-v1"
DEFAULT_LEGACY = Path(r"D:\OBS伴侣桥接\outputs\OBS伴侣本机桥接\controller\bridge-manifest.json")


class AdapterError(RuntimeError):
    pass


def _utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _path(value):
    return ntpath.normcase(ntpath.normpath(value))


def _owner(value):
    if (not isinstance(value, dict) or type(value.get("server_pid")) is not int or
            value["server_pid"] <= 0 or type(value.get("server_start_filetime")) is not int or
            value["server_start_filetime"] <= 0 or not isinstance(value.get("server_start_utc"), str) or
            not isinstance(value.get("server_path"), str) or
            _path(value["server_path"]) != _path(EXPECTED_EXE)):
        raise AdapterError("直播伴侣进程身份或版本不符合已审查范围")
    return {key: value[key] for key in
            ("server_pid", "server_start_filetime", "server_start_utc", "server_path")}


def _same_owner(a, b):
    return (a["server_pid"] == b["server_pid"] and
            a["server_start_filetime"] == b["server_start_filetime"] and
            _path(a["server_path"]) == _path(b["server_path"]))


def _unpack(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            raise AdapterError("伴侣返回的数据格式异常，已停止该操作")
    return value


def _audio_setting(value):
    value = _unpack(value)
    setting = value.get("setting", value) if isinstance(value, dict) else None
    if not isinstance(setting, dict):
        raise AdapterError("无法确认桥接源的音频设置")
    volume, monitor = setting.get("volume"), setting.get("monitorType")
    if (type(volume) not in {int, float} or not math.isfinite(volume) or not 0 <= volume <= 1 or
            type(monitor) is not int or monitor not in {0, 1, 2, 3}):
        raise AdapterError("桥接源的音量或监听设置异常")
    return {"volume": volume, "monitorType": monitor}


def _read_local_stream():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise AdapterError("本机码流统计发生重定向，已拒绝")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def sample():
        with opener.open(STAT_URL, timeout=1) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise AdapterError("本机码流统计超出允许大小")
        data = json.loads(raw)
        streams = [stream for server in data["http-flv"]["servers"] if server.get("port") == 19360
                   for app in server.get("applications", []) if app.get("name") == "obs"
                   for stream in app.get("live", {}).get("streams", []) if stream.get("name") == "main"]
        if len(streams) != 1:
            return None
        stream = streams[0]
        meta = stream.get("meta", {})
        if (stream.get("publishing") is not True or stream.get("active") is not True or
                meta.get("video", {}).get("codec") != "H264" or
                meta.get("audio", {}).get("codec") != "AAC"):
            return None
        count, pid, elapsed = stream.get("bytes_in"), data["http-flv"].get("pid"), stream.get("time")
        if (type(count) is not int or count < 0 or type(pid) is not int or pid <= 0 or
                type(elapsed) is not int or elapsed < 0):
            return None
        return pid, count, elapsed
    try:
        first = sample()
        if first is None:
            return False
        time.sleep(0.12)
        second = sample()
        return (second is not None and first[0] == second[0] and
                first[1] < second[1] and first[2] <= second[2])
    except Exception:
        return False


class BridgeAdapter:
    def __init__(self, runtime_dir, legacy_manifest=None, transport_factory=None,
                 discoverer=None, stream_ready=None):
        self.runtime_dir = Path(runtime_dir)
        self.manifest_path = self.runtime_dir / "bridge-manifest.json"
        self.legacy_manifest = Path(legacy_manifest) if legacy_manifest is not None else DEFAULT_LEGACY
        self._factory, self._discoverer = transport_factory, discoverer
        self._stream_ready = stream_ready or _read_local_stream
        self._lock = threading.RLock()
        self.transport = None
        self.owner = None
        self.record = None
        self._input_path = None
        self._input_hash = None
        self._record_error = None
        self._blocked_reason = None
        self._source_present = None
        self._source_enabled = None
        self._audio_verified = False
        self._stream_verified = False
        self._candidate = None
        self._mutation_in_operation = False
        try:
            self._load_record()
        except Exception:
            self._record_error = "已有桥接记录无效，已保留原文件并拒绝接管"

    def _result(self, ok, state, reason, **extra):
        result = {"ok": bool(ok), "state": state, "reason_zh": reason,
                  "owner": copy.deepcopy(self.owner),
                  "source_id": self.record.get("source_id") if self.record else None,
                  "source_present": self._source_present, "source_enabled": self._source_enabled,
                  "audio_setting_verified": self._audio_verified,
                  "local_stream_verified": self._stream_verified,
                  # Setting getters cannot prove decoded frames, real sound, or cloud playback.
                  "video_data_verified": False, "audio_data_verified": False,
                  "native_visible_verified": False,
                  "local_stream_pause_required": False}
        result.update(extra)
        return result

    def _fail(self, reason, *, block=False, pause=False):
        if block:
            self._blocked_reason = reason
        return self._result(False, "blocked" if self._blocked_reason else "needs_review", reason,
                            local_stream_pause_required=pause)

    @staticmethod
    def _clean_record(value):
        if (not isinstance(value, dict) or value.get("tool") not in {TOOL, LEGACY_TOOL} or
                value.get("name") != NAME or value.get("type") != SOURCE_TYPE or
                value.get("endpoint") != "local-flv-18081" or
                not isinstance(value.get("source_id"), str) or
                type(value.get("view_index")) is not int or value["view_index"] not in {0, 1} or
                type(value.get("new_settings")) is not bool or
                value.get("audio") not in {"mute", "obs"} or
                not isinstance(value.get("stage"), str)):
            raise AdapterError("旧桥接记录字段不完整，不能按名称猜测源")
        uuid.UUID(value["source_id"])
        clean = {key: value[key] for key in
                 ("tool", "name", "type", "endpoint", "source_id", "view_index", "new_settings", "audio", "stage")}
        clean["owner"] = _owner(value.get("owner"))
        for key in ("created_utc", "checked_utc"):
            if isinstance(value.get(key), str):
                clean[key] = value[key]
        return clean

    def _load_record(self):
        path = self.manifest_path if self.manifest_path.exists() else self.legacy_manifest
        if not path.exists():
            return
        if path.is_symlink():
            raise AdapterError("不接受链接形式的桥接记录")
        raw = path.read_bytes()
        if len(raw) > 32768:
            raise AdapterError("桥接记录超出允许大小")
        self.record = self._clean_record(json.loads(raw.decode("utf-8-sig")))
        self._input_path, self._input_hash = path, hashlib.sha256(raw).hexdigest()

    def _record_unchanged(self):
        if self._input_path is not None:
            if (not self._input_path.exists() or self._input_path.is_symlink() or
                    hashlib.sha256(self._input_path.read_bytes()).hexdigest() != self._input_hash):
                raise AdapterError("桥接记录被其他程序修改，已停止接管")

    def _archive_record(self):
        if not self.record:
            return
        self._record_unchanged()
        history = self.runtime_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        if history.is_symlink():
            raise AdapterError("记录归档目录不能是链接")
        name = "bridge-manifest." + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "." + uuid.uuid4().hex + ".json"
        with (history / name).open("x", encoding="utf-8") as output:
            json.dump({"archived_utc": _utc(), "record": self.record}, output, ensure_ascii=False, indent=2)

    def _save_record(self, record):
        self._record_unchanged()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.runtime_dir.is_symlink() or self.manifest_path.is_symlink():
            raise AdapterError("运行目录或记录不能是链接")
        saved = copy.deepcopy(record)
        saved.update(tool=TOOL, checked_utc=_utc())
        raw = json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8")
        pending = self.manifest_path.with_name("bridge-manifest." + uuid.uuid4().hex + ".tmp")
        try:
            with pending.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            self._record_unchanged()
            os.replace(pending, self.manifest_path)
        finally:
            if pending.exists():
                pending.unlink()
        self.record = saved
        self._input_path, self._input_hash = self.manifest_path, hashlib.sha256(raw).hexdigest()

    def _alive(self):
        return self.transport is not None and self.transport.alive()

    def _verify(self):
        if self._blocked_reason:
            raise AdapterError(self._blocked_reason)
        if self.transport is None or not self._alive():
            raise AdapterError("等待直播伴侣运行")
        try:
            self.transport.verify_identity(self.owner)
            _owner(self.transport.identity())
            if not _same_owner(_owner(self.transport.identity()), self.owner):
                raise AdapterError("直播伴侣进程身份改变，等待原进程正常退出")
        except Exception:
            self._blocked_reason = "直播伴侣连接或身份核验失败，本次会话不重连"
            raise AdapterError(self._blocked_reason)

    def _call(self, method, args):
        self._verify()
        if (method in {"addSource", "sourceTileFill"} or
                (method == "CANVAS_CALL" and args and isinstance(args[0], dict) and
                 args[0].get("path", [None])[-1] in {"setAudioData", "toggleVisible"})):
            self._mutation_in_operation = True
        try:
            return self.transport.call(method, args)
        except Exception:
            self._blocked_reason = "伴侣调用超时或失败，结果未知；本次会话不重试"
            raise AdapterError(self._blocked_reason)

    def discover(self):
        with self._lock:
            if self.transport is not None and self._alive():
                if self._blocked_reason:
                    return self._fail(self._blocked_reason)
                return self._result(True, "connected", "已保持当前伴侣连接")
            try:
                if self._discoverer is None:
                    from .windows_transport import discover_reviewed_processes
                    candidates = discover_reviewed_processes()
                else:
                    candidates = self._discoverer()
                if not isinstance(candidates, list):
                    raise AdapterError("进程发现结果无效")
                reviewed = [_owner(item) for item in candidates]
                if not reviewed:
                    self._candidate = None
                    return self._result(True, "waiting_companion", "等待已审查版本的直播伴侣")
                if len(reviewed) != 1:
                    self._candidate = None
                    return self._fail("发现多个伴侣主进程，无法确定唯一连接")
                self._candidate = reviewed[0]
                return self._result(True, "companion_available", "已找到可核验的直播伴侣", candidate=copy.deepcopy(self._candidate))
            except Exception:
                self._candidate = None
                return self._fail("无法确定已审查版本的唯一直播伴侣进程")

    def connect(self):
        with self._lock:
            if self.transport is not None:
                if self._alive():
                    if self._blocked_reason:
                        return self._fail(self._blocked_reason)
                    try:
                        self._verify()
                        return self._result(True, "connected", "当前伴侣连接已建立")
                    except AdapterError as exc:
                        return self._fail(str(exc), block=True)
                self.disconnect_safe()
            found = self.discover()
            if not found["ok"] or self._candidate is None:
                return found
            if self._record_error:
                return self._fail(self._record_error)
            try:
                if self._factory is None:
                    from .windows_transport import make_transport
                    transport = make_transport(expected_pid=self._candidate["server_pid"])
                else:
                    transport = self._factory(expected_pid=self._candidate["server_pid"])
                self.transport = transport
                self.owner = _owner(transport.identity())
                self._blocked_reason = None
                self._verify()
                if not _same_owner(self.owner, self._candidate):
                    raise AdapterError("发现与连接之间伴侣身份改变，已保留连接并拒绝操作")
                return self._result(True, "connected", "已连接伴侣，尚未新增或改变素材")
            except Exception:
                return self._fail("无法建立并核验伴侣连接；请检查管理员权限与版本", block=self.transport is not None)

    def _shared_source_rows(self, new, index):
        # Both canvas entries refer to one SourceStore. SECOND may not exist.
        # Read the unmerged tables through the currently valid canvas so a
        # repeated ID in data/data2 cannot be hidden by getAllSource's merge.
        prefix = ["stage", "baseEnv", "sourceStore"] if new else ["sourceStore"]
        spec = {"type": "GET", "index": index, "path": prefix + ["source"], "argArray": []}
        result = _unpack(self._call("CANVAS_CALL", [spec]))
        if not isinstance(result, dict):
            raise AdapterError("共享素材登记无法确认，不能把异常当成源不存在")
        ordinary = []
        for data_key, order_key in (("data", "order"), ("data2", "order2")):
            rows, order = result.get(data_key), result.get(order_key)
            self._validate_registration(rows, order)
            ordinary.append((rows, order))
        spec.update(path=prefix + ["topSource"])
        top = _unpack(self._call("CANVAS_CALL", [spec]))
        if not isinstance(top, dict):
            raise AdapterError("置顶素材登记无法确认，已停止操作")
        self._validate_registration(top.get("data"), top.get("order"))
        all_registered = set()
        for rows, order in (*ordinary, (top["data"], top["order"])):
            ids = set(rows) | set(order)
            if all_registered.intersection(ids):
                raise AdapterError("同一素材 ID 在普通、第二素材或置顶表中重复登记，已拒绝接管")
            all_registered.update(ids)
        # Keys 0/1 describe source.data/source.data2, not native view indices.
        # Store the one global top table only once for all collision checks.
        return {0: (*ordinary[0], top["data"], top["order"]),
                1: (*ordinary[1], {}, [])}

    @staticmethod
    def _validate_registration(rows, order):
        if (not isinstance(rows, dict) or not isinstance(order, list) or
                any(not isinstance(sid, str) for sid in order) or
                any(not isinstance(sid, str) or not isinstance(row, dict)
                    for sid, row in rows.items())):
            raise AdapterError("素材登记表字段异常，不能把异常当成源不存在")
        if len(order) != len(set(order)) or any(sid not in rows for sid in order):
            raise AdapterError("素材顺序重复或存在无记录的 ID，已停止操作")

    def _snapshot(self):
        self._verify()
        new = self._call("getUseNewSettings", [])
        canvas = _unpack(self._call("getCanvasScene", []))
        if (type(new) is not bool or not isinstance(canvas, dict) or "scene" not in canvas or
                type(canvas.get("viewIndex")) is not int or canvas["viewIndex"] not in {0, 1}):
            raise AdapterError("当前画布不在已审查的两个画布范围内")
        return new, canvas["viewIndex"], self._shared_source_rows(new, canvas["viewIndex"])

    @staticmethod
    def _valid_source(row, sid, index):
        if (not isinstance(row, dict) or row.get("name") != NAME or row.get("type") != SOURCE_TYPE or
                type(row.get("viewIndex")) is not int or row["viewIndex"] != index or
                row.get("secondSource") is not False or type(row.get("visible")) is not bool or
                row.get("alwaysTop") is not False or row.get("liveScene") != "" or
                row.get("parentID") or row.get("duplicate") or
                ("id" in row and row["id"] != sid) or not isinstance(row.get("payload"), dict)):
            raise AdapterError("旧源的完整身份不一致，拒绝按名称接管")
        payload = row["payload"]
        if (payload.get("url") != SOURCE_URL or payload.get("hardwareDecode") is not False or
                type(payload.get("bufferSize")) is not int or payload["bufferSize"] != 2 or
                type(payload.get("timeout")) is not int or payload["timeout"] != 10 or
                type(payload.get("colorRange")) is not int or payload["colorRange"] != 3 or
                any(payload.get(key) is not False for key in
                    ("activeRestartPlay", "playEndNoDisplay", "linearAlpha")) or
                payload.get("ffmpegOptions") != ""):
            raise AdapterError("旧源的本机地址或媒体设置不完整，拒绝接管")
        audio = payload.get("audio")
        setting = audio.get("setting") if isinstance(audio, dict) else None
        _audio_setting(setting)
        if (type(setting.get("balancing")) not in {int, float} or setting["balancing"] != 0.5 or
                type(setting.get("syncOffset")) not in {int, float} or setting["syncOffset"] != 0 or
                setting.get("isDownmixMono") is not False):
            raise AdapterError("旧源的音频基础设置不完整或已改变，拒绝接管")
        return row

    def _resolve(self, snapshot):
        new, _, tables = snapshot
        if not self.record:
            return None
        self._record_unchanged()
        sid, index = self.record["source_id"], self.record["view_index"]
        raw = self._call("getSourceData", [sid])
        registrations = []
        for bucket, (rows, order, top, top_order) in tables.items():
            if sid in rows or sid in order:
                registrations.append((bucket, "ordinary", rows.get(sid), order.count(sid)))
            if sid in top or sid in top_order:
                registrations.append((bucket, "top", top.get(sid), top_order.count(sid)))
        if raw is None:
            if registrations:
                raise AdapterError("源查询与画布登记矛盾，不能确认旧源已消失")
            self._source_present, self._source_enabled = False, None
            return None
        row = _unpack(raw)
        row = self._valid_source(row, sid, index)
        # A reviewed bridge has secondSource=False, so it belongs in data.
        # Its native viewIndex may be SECOND even though the table is data.
        if (len(registrations) != 1 or registrations[0][0:2] != (0, "ordinary") or
                registrations[0][3] != 1):
            raise AdapterError("旧源不是唯一普通素材，拒绝接管置顶源或重复映射")
        self._valid_source(registrations[0][2], sid, index)
        if new != self.record["new_settings"]:
            raise AdapterError("伴侣设置模式改变，旧记录不能直接接管")
        self._source_present, self._source_enabled = True, row["visible"]
        return row

    def _assert_no_collision(self, tables, own_sid=None):
        for rows, _, top, _ in tables.values():
            for collection in (rows, top):
                for sid, row in collection.items():
                    if row.get("name") == NAME and sid != own_sid:
                        raise AdapterError("发现同名但不同 ID 的素材，已拒绝创建或接管")

    def _resolve_or_recover(self, snapshot):
        row = self._resolve(snapshot)
        if row is not None or self.record is None:
            return row
        # _resolve proved the old getter was literal None and no registration
        # table contains its ID. Never recover from JSON null or malformed data.
        candidates = []
        for bucket, (rows, order, top, top_order) in snapshot[2].items():
            for kind, data, ordering in (("ordinary", rows, order), ("top", top, top_order)):
                for sid, candidate in data.items():
                    if candidate.get("name") == NAME:
                        candidates.append((bucket, kind, sid, candidate, ordering.count(sid)))
        restarted = not _same_owner(self.record["owner"], self.owner)
        if not restarted:
            if candidates:
                raise AdapterError("当前伴侣会话未改变，不能接管变更后的素材 ID")
            return None
        if self.record["stage"] != "attached":
            if candidates:
                raise AdapterError("旧记录不是已接入状态，不能跨重启接管新素材 ID")
            return None
        if len(candidates) != 1:
            raise AdapterError("重启后未找到唯一完整桥接源，已保留旧记录且不会新增素材")
        if snapshot[0] != self.record["new_settings"]:
            raise AdapterError("伴侣设置模式改变，不能恢复旧记录对应的新素材 ID")
        bucket, kind, sid, registered, count = candidates[0]
        if bucket != 0 or kind != "ordinary" or count != 1:
            raise AdapterError("重启后的候选不是唯一普通素材登记，已拒绝接管")
        try:
            if str(uuid.UUID(sid)) != sid:
                raise ValueError()
        except (ValueError, TypeError):
            raise AdapterError("重启后候选素材 ID 格式异常，已拒绝接管")
        index = self.record["view_index"]
        self._valid_source(registered, sid, index)
        current = _unpack(self._call("getSourceData", [sid]))
        self._valid_source(current, sid, index)
        if current != registered:
            raise AdapterError("候选素材查询与完整登记不一致，已拒绝恢复")
        _audio_setting(self._call("getAudioData", [sid]))
        # All ownership and source checks precede even the private record write.
        # Archive before committing the new ID; never create/delete a candidate.
        self._verify()
        self._archive_record()
        recovered = copy.deepcopy(self.record)
        recovered.update(owner=copy.deepcopy(self.owner), source_id=sid, stage="attached")
        self._save_record(recovered)
        self._source_present, self._source_enabled = True, current["visible"]
        self._audio_verified = False
        return current

    def _adopt(self, stage):
        if (self.record and _same_owner(self.record["owner"], self.owner) and
                self._input_path == self.manifest_path and self.record["stage"] == stage):
            return
        self._verify()
        self._archive_record()
        adopted = copy.deepcopy(self.record)
        adopted.update(owner=copy.deepcopy(self.owner), stage=stage)
        self._save_record(adopted)

    def read_status(self):
        with self._lock:
            if self.transport is None:
                return self._result(True, "waiting_companion", "尚未连接直播伴侣")
            if not self._alive():
                self.disconnect_safe()
                return self._result(True, "waiting_companion", "原伴侣已退出，等待新的伴侣会话")
            if self._blocked_reason:
                return self._fail(self._blocked_reason)
            try:
                snapshot = self._snapshot()
                row = self._resolve(snapshot)
                self._assert_no_collision(snapshot[2], self.record["source_id"] if row else None)
                self._audio_verified = False
                if row is not None:
                    audio = _audio_setting(self._call("getAudioData", [self.record["source_id"]]))
                    expected = {"volume": 1 if self.record["audio"] == "obs" else 0,
                                "monitorType": 1 if self.record["audio"] == "obs" else 0}
                    self._audio_verified = audio == expected
                    managed = (_same_owner(self.record["owner"], self.owner) and self._input_path == self.manifest_path)
                    return self._result(True, "source_available", "已找到记录对应的桥接源；画面与声音仍需实际验证",
                                        source_managed=managed)
                return self._result(True, "waiting_source", "尚无已核验桥接源，未自动添加素材", source_managed=False)
            except Exception as exc:
                return self._fail(str(exc) if isinstance(exc, AdapterError) else "无法读取桥接状态，原素材未改动")

    def _configure(self, snapshot, row, *, enabled, audio):
        sid, index = self.record["source_id"], self.record["view_index"]
        store_index = snapshot[1]
        desired = {"volume": 1 if enabled and audio else 0,
                   "monitorType": 1 if enabled and audio else 0}
        current = _audio_setting(self._call("getAudioData", [sid]))
        prefix = ["stage", "baseEnv", "sourceStore"] if snapshot[0] else ["sourceStore"]
        if current != desired:
            self._record_unchanged()
            self._valid_source(_unpack(self._call("getSourceData", [sid])), sid, index)
            spec = {"type": "APPLY", "index": store_index, "path": prefix + ["setAudioData"],
                    "argArray": [sid, {"setting": desired}, True, True, False]}
            result = self._call("CANVAS_CALL", [spec])
            if result is not True:
                self._blocked_reason = "伴侣未确认音频设置成功，本次会话不重试该操作"
                raise AdapterError(self._blocked_reason)
        verified = _audio_setting(self._call("getAudioData", [sid]))
        if verified != desired:
            self._blocked_reason = "音频设置读回不一致，本次会话不重试；不能确认已生效"
            raise AdapterError(self._blocked_reason)
        self._audio_verified = True
        if row["visible"] != enabled:
            self._record_unchanged()
            self._valid_source(_unpack(self._call("getSourceData", [sid])), sid, index)
            spec = {"type": "APPLY", "index": store_index, "path": prefix + ["toggleVisible"],
                    "argArray": [sid, enabled]}
            self._call("CANVAS_CALL", [spec])
        raw = self._call("getSourceData", [sid])
        after = self._valid_source(_unpack(raw), sid, index)
        self._source_enabled = after["visible"]
        if after["visible"] != enabled:
            self._blocked_reason = "源可见性保存值不一致，本次会话不重试；请停止本机码流回退"
            raise AdapterError(self._blocked_reason)
        updated = copy.deepcopy(self.record)
        desired_mode = "obs" if enabled and audio else "mute"
        if updated["audio"] != desired_mode or updated["stage"] != "attached":
            updated.update(audio=desired_mode, stage="attached")
            self._save_record(updated)
        return self._result(True, "source_enabled" if enabled else "source_disabled",
                            "桥接源设置已核验，云端画面声音仍需实际验证" if enabled else
                            "源静音设置已核验；可见性仅核验保存值，仍需停止本机码流",
                            local_stream_pause_required=not enabled, source_managed=True)

    def _create(self, snapshot, audio):
        new, index, _ = snapshot
        ready = self._stream_ready()
        self._stream_verified = type(ready) is bool and ready
        if not self._stream_verified:
            return self._result(True, "waiting_stream", "等待 OBS 向本机输出 H.264 和 AAC，尚未添加素材")
        sid = str(uuid.uuid4())
        if self._call("getSourceData", [sid]) is not None:
            raise AdapterError("计划源 ID 已存在，拒绝碰撞或异常 getter")
        record = {"tool": TOOL, "created_utc": _utc(), "owner": copy.deepcopy(self.owner),
                  "source_id": sid, "name": NAME, "type": SOURCE_TYPE,
                  "endpoint": "local-flv-18081", "view_index": index,
                  "new_settings": new, "audio": "obs" if audio else "mute", "stage": "planned"}
        self._save_record(record)
        setting = {"volume": 1 if audio else 0, "balancing": 0.5,
                   "monitorType": 1 if audio else 0, "syncOffset": 0, "isDownmixMono": False}
        params = {"name": NAME, "type": SOURCE_TYPE, "viewIndex": index,
                  "secondSource": False, "visible": True, "alwaysTop": False, "liveScene": "",
                  "payload": {"url": SOURCE_URL, "hardwareDecode": False, "bufferSize": 2,
                              "timeout": 10, "activeRestartPlay": False, "playEndNoDisplay": False,
                              "linearAlpha": False, "colorRange": 3, "ffmpegOptions": "",
                              "audio": {"setting": setting}}}
        result = _unpack(self._call("addSource", [params, sid, {"shouldCreate": True}]))
        if not isinstance(result, dict) or result.get("id") != sid or result.get("result") is not True:
            self._blocked_reason = "新增素材结果未确认，本次会话不重试；记录已保留"
            raise AdapterError(self._blocked_reason)
        self._source_present = True
        after = self._snapshot()
        row = self._resolve(after)
        if row is None:
            self._blocked_reason = "新增素材未完成普通登记，本次会话不重试"
            raise AdapterError(self._blocked_reason)
        self._assert_no_collision(after[2], sid)
        self._call("sourceTileFill", [sid])
        saved = copy.deepcopy(self.record)
        saved["stage"] = "attached"
        self._save_record(saved)
        return self._configure(after, row, enabled=True, audio=audio)

    def ensure_existing_source(self, enabled=True, audio=True):
        with self._lock:
            self._mutation_in_operation = False
            if type(enabled) is not bool or type(audio) is not bool:
                return self._fail("启用与音频参数必须是明确的布尔值")
            if not enabled:
                return self.set_enabled(False)
            if self.transport is None:
                return self._result(True, "waiting_companion", "尚未连接伴侣，未添加或改变素材")
            if self._blocked_reason:
                return self._fail(self._blocked_reason)
            try:
                snapshot = self._snapshot()
                row = self._resolve_or_recover(snapshot)
                self._assert_no_collision(snapshot[2], self.record["source_id"] if row else None)
                if row is not None:
                    # Prove audio getter shape before adopting or mutating saved ownership.
                    _audio_setting(self._call("getAudioData", [self.record["source_id"]]))
                    self._adopt("attached")
                    return self._configure(snapshot, row, enabled=True, audio=audio)
                if self.record is not None and self.record["stage"] != "removed":
                    self._adopt("removed")
                return self._create(snapshot, audio)
            except Exception as exc:
                reason = (str(exc) if isinstance(exc, AdapterError) else
                          "桥接记录归档或保存失败，已停止该操作并保留旧记录")
                return self._fail(reason, block=self._mutation_in_operation or isinstance(exc, OSError))

    def set_enabled(self, enabled=False):
        with self._lock:
            self._mutation_in_operation = False
            if type(enabled) is not bool:
                return self._fail("启用参数必须是明确的布尔值", pause=True)
            if enabled:
                return self.ensure_existing_source(enabled=True, audio=True)
            if self.transport is None:
                return self._result(True, "not_connected", "没有已连接的自建源，请停止本机输出即可",
                                    no_owned_source=True, local_stream_pause_required=True)
            if not self._alive():
                self.disconnect_safe()
                return self._result(True, "waiting_companion", "伴侣已退出，请停止本机输出即可",
                                    no_owned_source=True, local_stream_pause_required=True)
            if self._blocked_reason:
                return self._fail(self._blocked_reason, pause=True)
            try:
                snapshot = self._snapshot()
                row = self._resolve_or_recover(snapshot)
                self._assert_no_collision(snapshot[2], self.record["source_id"] if row else None)
                if row is None:
                    return self._result(True, "no_owned_source", "没有可核验的自建源，未添加素材；请停止本机输出",
                                        no_owned_source=True, local_stream_pause_required=True)
                _audio_setting(self._call("getAudioData", [self.record["source_id"]]))
                self._adopt("attached")
                return self._configure(snapshot, row, enabled=False, audio=False)
            except Exception as exc:
                reason = (str(exc) if isinstance(exc, AdapterError) else
                          "无法确认停止设置，请停止本机码流；源记录保持保留")
                return self._fail(reason, block=self._mutation_in_operation or isinstance(exc, OSError), pause=True)

    def disconnect_safe(self):
        with self._lock:
            if self.transport is None:
                return self._result(True, "disconnected", "没有活动管道连接", disconnected=True)
            try:
                if self._alive():
                    return self._result(True, "connection_held", "伴侣仍在运行，管道保持到它正常退出",
                                        disconnected=False)
                self.transport.close()
                self.transport = None
                self.owner = None
                self._blocked_reason = None
                self._source_present, self._source_enabled = None, None
                self._audio_verified = False
                self._candidate = None
                return self._result(True, "disconnected", "原伴侣已退出，管道已安全释放", disconnected=True)
            except Exception:
                return self._fail("无法安全确认管道已释放，保留后台等待", pause=True) | {"disconnected": False}
