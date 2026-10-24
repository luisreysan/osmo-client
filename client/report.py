"""Camera report (osmo-client --report): what a camera answers to Osmo Client, so that the app can be
checked on cameras its author doesn't have.

The app connects as usual. Once the live view is up the report runs by itself (about ten minutes), puts
back what it changed, and saves osmo-client-report-<model>-<time>.json in the downloads folder.

On the camera it reads the settings and the status, subscribes to status topics, switches through the
capture modes and writes each video format, slow-motion choice, photo format and colour profile the app
offers (each once, recording the camera's reply), reads the file list and asks the file server for a few
thumbnails. It never records, takes a photo or deletes anything, and puts the capture mode, formats and
colour profile back as they were.

The report holds the model, the app's version and the system, the camera's replies and status data, a
summary of the messages it sent and the app's log. It leaves out the camera's name and Bluetooth address,
Wi-Fi names and passwords, IP addresses, folders on the computer, pictures, and file names and dates (the
file list is kept with its names and dates blanked, to check how it is read).
"""

from __future__ import annotations

import collections
import io
import json
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from camlink import __version__, camera as cam, media
from camlink.models import MODELS, TESTED_MODELS
from camlink.protocol import ADDR_TOPICS, CAMERA_HOST, topic_subscription
from camlink.util import log

from .downloads import open_folder
from .server import exposure_options

# Status topics to subscribe to (names documented in docs/PROTOCOL.md or by the OpenPocketCine project).
TOPICS = tuple(sorted(set(cam.SUBSCRIBED_TOPICS) | {
    "cam_status", "cam_storage", "cam_record_time", "cam_video_param_v2", "cam_photo_param_new", "cam_fov",
    "cam_capture_aspect_type", "cam_style_filter_status", "cam_portrait_mode", "cam_pano_params",
    "cam_steady_preferred_status", "camcap_exposure_mode", "camcap_iso_auto_max", "camcap_shutter_max",
    "camcap_capture_aspect_type", "camcap_countdown", "camcap_loop_video_duration", "camcap_slowmotion_ratio",
    "camcap_mode_profile", "camcap_style_filter_mode", "camcap_aperture"}))
SETTING_GETS = (0x1F, 0x2B, 0x29, 0x19, 0x13, 0x2D)
PRIVATE_SETS = {0x07}                     # Wi-Fi: network names and passwords
MAX_WRITES = 60                           # format writes per capture mode
LIVE_WAIT = 600                           # s: pairing (approving on the camera) and joining can take a while


class _Lines(io.TextIOBase):
    """Keeps the console's last lines for the report while still printing them."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.lines: collections.deque[str] = collections.deque(maxlen=400)
        self._part = ""

    def write(self, text: str) -> int:
        self._part += text
        *done, self._part = self._part.split("\n")
        self.lines.extend(done)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def reconfigure(self, **kwargs) -> None:
        self.stream.reconfigure(**kwargs)


class Report:
    def __init__(self, app, decoder) -> None:
        self.app, self.decoder = app, decoder
        self.data: dict = {"report": 1, "app": f"Osmo Client {__version__}", "errors": []}
        self.messages: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.console = _Lines(sys.stdout)
        sys.stdout = self.console
        self.started = time.monotonic()

    # ---- what the camera sends
    def tap(self, m) -> None:
        if m.cmd_set in PRIVATE_SETS or m.command in ((0x00, 0x27), (0x00, 0x99)):
            return
        kind = "reply" if m.is_reply else "request" if m.needs_reply else "push"
        key = f"{m.cmd_set:02x}/{m.cmd_id:02x} {kind} from {m.sender:02x}"
        with self.lock:
            entry = self.messages.setdefault(key, {"count": 0, "samples": []})
            entry["count"] += 1
            sample = bytes(m.payload[:64]).hex()
            if len(entry["samples"]) < 3 and sample not in entry["samples"]:
                entry["samples"].append(sample)

    # ---- helpers
    def req(self, cmd_set: int, cmd_id: int, payload: bytes = b"", receiver: int | None = None,
            timeout: float = 2.0) -> dict:
        r = self.app.request(cmd_set, cmd_id, payload, timeout=timeout, quiet=True, receiver=receiver)
        return {k: r[k] for k in ("ok", "reply", "error") if k in r}

    def model(self) -> int | None:
        return (self.app.registry.get(self.app.target) or {}).get("model_id")

    def grace(self, seconds: float) -> None:
        self.app.video_grace = max(self.app.video_grace, time.monotonic() + seconds)

    def step(self, name: str, function) -> None:
        log(f"Report: {name}…")
        try:
            self.data[name] = function()
        except Exception as error:  # noqa: BLE001 - the report goes on with the next step
            self.data["errors"].append(f"{name}: {error!r}")
            log(f"Report: {name} failed ({error})")

    # ---- the run
    def run(self, stop: threading.Event) -> None:
        app = self.app
        log("Camera report: it starts once the live view is up (connect from the page if needed)")
        deadline = time.monotonic() + LIVE_WAIT
        while not stop.is_set() and time.monotonic() < deadline and not (
                app.link == "on" and app.datalink is not None and time.monotonic() - app.last_frame < 2):
            time.sleep(0.5)
        live = app.link == "on" and time.monotonic() - app.last_frame < 2
        self.data["camera"] = self.camera_info(live)
        if live and not stop.is_set():
            time.sleep(3)
            snap = app.state.snapshot()
            if snap.get("record") and snap["record"]["state"] != "idle":
                self.data["errors"].append("the camera was recording: settings were not changed")
                for name, function in (("status", self.status), ("topics", self.topics), ("settings", self.settings)):
                    self.step(name, function)
            else:
                for name, function in (("status", self.status), ("topics", self.topics), ("settings", self.settings),
                                       ("modes", self.modes), ("gallery", self.gallery)):
                    if stop.is_set():
                        break
                    self.step(name, function)
        else:
            self.data["errors"].append(f"no live view within {LIVE_WAIT // 60} minutes "
                                       f"(stage: {app.stage}, {app.stage_error or 'no error'})")
        self.save()

    def camera_info(self, live: bool) -> dict:
        model = self.model()
        known = self.app.registry.get(self.app.target) or {}
        return {
            "model_id": None if model is None else f"0x{model:04x}",
            "model": MODELS.get(model, known.get("model")),
            "tested_with_app": model in TESTED_MODELS,
            "connection": "shared network" if known.get("network") else "camera's own Wi-Fi",
            "transport": known.get("transport"),
            "live_view": live,
            "live_view_codec": getattr(self.decoder, "codec_name", None),
            "minutes_to_live_view": round((time.monotonic() - self.started) / 60, 1) if live else None,
            "system": {"os": platform.system(), "release": platform.release(), "python": platform.python_version()},
        }

    def status(self) -> dict:
        s = self.app.status()
        keep = ("capture_mode", "capture_mode_name", "record", "storage", "stores", "battery", "settings",
                "image", "pocket", "playback", "raw_status")
        return {k: s.get(k) for k in keep}

    def topics(self) -> dict:
        replies = {}
        for i, name in enumerate(TOPICS):
            r = self.req(0x00, 0x99, topic_subscription(name, 0x7300 + i), receiver=ADDR_TOPICS, timeout=1.5)
            replies[name] = (r.get("reply") or "")[:2] or r.get("error")
        time.sleep(6)
        with self.app.state._lock:
            seen = {n: {"data": t["data"].hex()[:512] if isinstance(t.get("data"), (bytes, bytearray)) else None,
                        "count": t.get("count")} for n, t in self.app.state.topics.items()}
        _, caps = self.app.state.capabilities()
        return {"subscribe_replies": replies, "received": seen,
                "capability_tables": {k: v for k, v in caps.items()}}

    def settings(self) -> dict:
        gets = {f"02/{c:02x}": self.req(0x02, c) for c in SETTING_GETS}
        params = {}
        for p in range(0x01, 0x41):
            r = self.req(0x02, 0x8E, cam.parameter_tlv_get(p), timeout=1.0)
            params[f"{p:04x}"] = r.get("reply", r.get("error"))
        return {"gets": gets, "parameters": params}

    def modes(self) -> dict:
        """Each capture mode: what the app offers there, and each video format / photo format / colour
        profile written once (the camera's reply and the format read back), then put back."""
        app = self.app
        _, caps = app.state.capabilities()
        listed = caps.get("camcap_base")
        photo = 0x17 if self.model() in cam.POCKET4S else 0x05       # the Osmo Pocket 4 / 4P's Photo is 0x17
        modes = list(listed) if listed else [photo, 0x01, 0x28, 0x00, 0x02, 0x0A]
        original_mode = app.state.capture_mode()
        original_color = (app.status().get("image") or {}).get("color")
        out = {"modes_listed_by_camera": listed, "original_mode": original_mode, "each": {}}
        for mode in modes:
            self.grace(30)
            entry: dict = {"set": app.set_capture_mode(mode)}
            time.sleep(4)
            entry["capture_mode_now"] = app.state.capture_mode()
            opts = exposure_options(app)
            entry["offered"] = {
                "source": opts["source"],
                "formats": [[opts["resolutions"].get(r, r), [cam.FRAME_RATES.get(f, f) for f in fs]] for r, fs in opts["formats"]],
                "slowmo": [s["label"] for s in opts["slowmo"]],
                "photo": [p["label"] for p in opts["photo_formats"]] if mode in cam.PHOTO_MODES else [],
                "colors": [c["label"] for c in opts["color_modes"]],
                "iso": [i["value"] for i in opts["iso"]], "fov": [f["label"] for f in opts["fov"]],
                "eis": [e["label"] for e in opts["eis"]]}
            if entry["capture_mode_now"] == mode:
                entry["writes"] = self.write_formats(mode, opts)
                if mode == cam.MODE_VIDEO and original_color is not None:
                    entry["colors"] = self.write_colors(opts, original_color)
            out["each"][f"{mode:02x}"] = entry
        if original_mode is not None:
            self.grace(30)
            out["restore_mode"] = app.set_capture_mode(original_mode)
        return out

    def write_formats(self, mode: int, opts: dict) -> dict:
        app, writes = self.app, {}
        if mode in cam.PHOTO_MODES:
            before = bytes.fromhex(self.req(0x02, 0x13).get("reply", ""))
            for p in opts["photo_formats"]:
                result = app.set_setting(0x12, bytes((p["size"], p["ratio"])))
                time.sleep(1.5)
                back = self.req(0x02, 0x13).get("reply")
                writes[p["label"]] = {"ok": result.get("ok"), "reply": result.get("reply"),
                                      "error": result.get("error"), "read_back": back}
            if len(before) >= 3:
                writes["restore"] = app.set_setting(0x12, before[1:3])
            return writes
        pairs = ([(s["res"], s["fps"]) for s in opts["slowmo"]] if mode == cam.MODE_SLOWMO
                 else [(r, f) for r, fs in opts["formats"] for f in fs])
        before = bytes.fromhex(self.req(0x02, 0x19).get("reply", ""))
        for res, fps in pairs[:MAX_WRITES]:
            self.grace(20)
            result = app.set_video_format(res, fps)
            time.sleep(2)
            back = self.req(0x02, 0x19).get("reply")
            writes[f"{res:02x}/{fps:02x}"] = {"ok": result.get("ok"), "reply": result.get("reply"),
                                              "error": result.get("error"), "read_back": back}
        if len(before) >= 3:
            self.grace(20)
            writes["restore"] = app.set_video_format(before[1], before[2])
        return writes

    def write_colors(self, opts: dict, original: int) -> dict:
        out = {}
        for c in opts["color_modes"]:
            self.grace(15)
            out[c["label"]] = self.req(0x02, 0x42, bytes((c["code"],)))
            time.sleep(2)
        self.grace(15)
        out["restore"] = self.req(0x02, 0x42, bytes((original,)))
        return out

    def gallery(self) -> dict:
        app = self.app
        result = app.list_media()
        files = list(app.media.get("files") or [])
        out = {"listing": {k: result.get(k) for k in ("ok", "count", "stores", "error")},
               "partial": app.media.get("partial"), "note": bool(app.media.get("note")),
               "media_mode_refused": app.media_mode_refused,
               "types": dict(collections.Counter(f["type"] for f in files)),
               "storages": sorted({f["storage"] for f in files}),
               "resolutions": sorted({cam.RESOLUTIONS.get(f["res_code"], f"0x{f['res_code']:02x}")
                                      for f in files if f.get("res_code") is not None}),
               "fps": sorted({f["fps"] for f in files if f.get("fps")}),
               "favourites": sum(1 for f in files if f.get("starred")),
               "paths_match_names": all(f["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0] == f["name"].rsplit(".", 1)[0]
                                        for f in files)}
        out["first_pages"] = {store: self.raw_page(cursor) for store, cursor in
                              (("sd", media.CURSOR_SD), ("internal", media.CURSOR_INTERNAL))}
        out["files"] = self.file_server(files)
        return out

    def raw_page(self, cursor: int) -> dict:
        """The camera's first listing page as received, file names and dates blanked."""
        app = self.app
        app._media_counter = app._media_counter % 0xFFF0 + 1
        counter = app._media_counter
        with app._media_cond:
            app._media_chunks[counter] = {}
        answer = self.req(0x00, 0x26, media.list_payload(counter, cursor), timeout=3.0)
        end = time.monotonic() + 8
        with app._media_cond:
            while time.monotonic() < end and not any(k == media.CHUNK_END for k, _ in app._media_chunks.get(counter, {})):
                app._media_cond.wait(0.3)
            page = app._media_chunks.pop(counter, {})
        data = b"".join(page[k]["data"] for k in sorted(k for k in page if k[0] == media.CHUNK_DATA))
        return {"reply": answer.get("reply", answer.get("error")), "chunks": len(page),
                "records_parsed": len(media.parse_manifest(data)) if data else 0,
                "data": blank_manifest(data).hex()}

    def file_server(self, files: list[dict]) -> list[dict]:
        """A few files from the camera's file server: what answers, its size and the thumbnails' size."""
        picks = files[:2] + [f for f in files[2:] if f["type"] == "photo"][:1]
        out = []
        for f in picks:
            stem = (f.get("thumb") or "").rsplit(".", 1)[0]
            checks = {"type": f["type"]}
            for label, path, whole in (("thm", stem + ".THM", True), ("scr", stem + ".SCR", True),
                                       ("file", f["path"], False), ("lrf", f["path"].rsplit(".", 1)[0] + ".LRF", False)):
                if not stem and label in ("thm", "scr"):
                    continue
                url = media.http_url(f["storage"], path, self.app.camera_host)
                try:
                    headers = {} if whole else {"Range": "bytes=0-15"}
                    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8) as r:
                        body = r.read(2_000_000)
                        info = {"status": r.status, "bytes": len(body), "range": r.headers.get("Content-Range")}
                    if whole and body:
                        try:
                            with Image.open(io.BytesIO(body)) as image:
                                info["size"] = list(image.size)
                        except Exception:  # noqa: BLE001
                            info["size"] = None
                except urllib.error.HTTPError as error:
                    info = {"status": error.code}
                except OSError as error:
                    info = {"error": "refused" if media.refused(error) else type(error).__name__}
                checks[label] = info
            out.append(checks)
        return out

    # ---- saving
    def secrets(self) -> list[str]:
        """Everything that must not leave the computer: names, addresses, Wi-Fi details, the home folder."""
        found = {str(Path.home()), Path.home().as_posix()}
        reg = self.app.registry
        for camera in reg.cameras:
            found.update(str(camera.get(k) or "") for k in ("address", "name", "ssid", "password", "network", "host"))
        found.update(reg.networks.keys())
        found.update(reg.networks.values())
        found.add(self.app.home or "")
        return sorted((s for s in found if len(s) >= 4), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self.secrets():
            text = text.replace(secret, "<private>")
            text = text.replace(json.dumps(secret)[1:-1], "<private>")            # as JSON writes it
            text = text.replace(secret.encode().hex(), "00" * len(secret.encode()))   # inside payloads
        text = re.sub(r"\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b", "<address>", text, flags=re.I)
        text = re.sub(r"\b(?:Osmo|OSMO|Xtra)[A-Za-z0-9]*-[0-9A-F]{4}\b", "<camera>", text)
        return re.sub(r"\b(?!192\.168\.2\.|127\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<ip>", text)

    def save(self) -> None:
        with self.lock:
            self.data["messages"] = dict(sorted(self.messages.items()))
        self.data["log"] = list(self.console.lines)
        self.data["minutes"] = round((time.monotonic() - self.started) / 60, 1)
        text = self.scrub(json.dumps(self.data, indent=1, default=str, ensure_ascii=False))
        model = (self.data.get("camera") or {}).get("model") or "camera"
        name = f"osmo-client-report-{re.sub(r'[^A-Za-z0-9]+', '-', model).strip('-').lower()}-{time.strftime('%Y%m%d-%H%M')}.json"
        folder = self.app.downloader.base
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            path.write_text(text, encoding="utf-8")
        except OSError:
            path = Path.cwd() / name
            path.write_text(text, encoding="utf-8")
        log(f"Camera report saved: {path}")
        log("Attach it to a new issue: https://github.com/luisreysan/osmo-client/issues/new?template=camera-report.yml "
            "(you can close the app now: Ctrl+C)")
        try:
            open_folder(path.parent)
        except Exception:  # noqa: BLE001 - the path is in the log anyway
            pass


def blank_manifest(data: bytes) -> bytes:
    """A listing page with the file names' dates and the records' times zeroed (same length, so it still
    reads the same way)."""
    out = bytearray(data)
    for m in re.finditer(rb"DJI_\d{14}", data):
        out[m.start() + 4:m.end()] = b"0" * 14
    names = [m.start() for m in media._NAME.finditer(data) if len(m.group(2)) == m.group(1)[0]]
    for i, pos in enumerate(names):
        tag = data.rfind(b"\x19\x06", names[i - 1] if i else 0, pos)
        if tag >= 18:
            out[tag - 18:tag - 14] = bytes(4)
    return bytes(out)


def start(app, decoder, stop: threading.Event) -> Report:
    report = Report(app, decoder)
    app.tap = report.tap
    threading.Thread(target=report.run, args=(stop,), daemon=True, name="camera-report").start()
    return report
