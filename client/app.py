"""State shared by the camera session (asyncio + threads) and the web UI, and the actions the UI calls."""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import io
import threading
import time
import urllib.error
import urllib.request

from PIL import Image

from camlink import ble, camera as cam, media
from camlink.datalink import Datalink
from camlink.duml import Message
from camlink.models import MODELS, NANO, POCKETS, SD_ONLY, TESTED_MODELS
from camlink.protocol import ADDR_GIMBAL, CAMERA_HOST
from camlink.registry import CameraRegistry
from camlink.util import data_dir, log

from .downloads import Downloader, default_download_dir
from .library import MediaCache
from .multiview import MAX_EXTRA, Tile
from .state import CameraState


# Thumbnails narrower than this (the Osmo Pocket 3 keeps 90 × 90 ones) are made from the camera's larger
# preview instead (.SCR, 720 px there), shrunk to fit THUMB_SIZE.
THUMB_MIN = 200
THUMB_SIZE = 512


def _image_width(data: bytes) -> int:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size[0]
    except Exception:  # noqa: BLE001 - not an image we can read: leave it as it is
        return 0


def _shrink(data: bytes) -> bytes | None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.draft("RGB", (THUMB_SIZE, THUMB_SIZE))     # JPEG: decode at a reduced size, much faster
            image = image.convert("RGB")
            image.thumbnail((THUMB_SIZE, THUMB_SIZE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=82)
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _tested(camera: dict) -> bool:
    """Tested with this app: by model, so that a camera linked before its model was tested shows it too."""
    model = camera.get("model_id")
    return model in TESTED_MODELS if model is not None else bool(camera.get("tested"))


class Pending:
    """A command waiting for the camera's reply."""

    def __init__(self, cmd_set: int, cmd_id: int) -> None:
        self.key = (cmd_set, cmd_id)
        self.seq: int | None = None
        self.payload = b""
        self.done = threading.Event()


class App:
    def __init__(self) -> None:
        self.state = CameraState()
        self.latest_frame = None              # av.VideoFrame for the live view stream
        self.vcam = None                      # camlink.vcam.VirtualCamera (--vcam): the live view as a webcam
        self.frame_seq = 0
        self.last_frame = 0.0
        self.frame_cond = threading.Condition()
        self.datalink: Datalink | None = None  # current session, for sending commands
        self.camera_host = CAMERA_HOST         # its own access point, or its address on a shared network
        self._command_lock = threading.Lock()  # one command at a time: a mode switch takes ~0.8 s
        self._pending: list[Pending] = []
        self._pending_lock = threading.Lock()
        self.settings_due = threading.Event()  # read the settings back now (after a mode switch)
        self.video_grace = 0.0                 # monotonic() until which a pause in the video is expected
        self.recovery: str | None = None       # what the live view watchdog is doing (for the page)
        # Osmo Pocket controls held from the page: refreshed by it while held, stopped when it goes quiet
        self._stick = (0.0, 0.0)               # tilt, pan in -1..1
        self._stick_seen = 0.0
        self._stick_on = False
        self._zoom_seen = 0.0
        self._zoom_moving = False
        # Media library. Listing runs in the camera's media mode (no live view meanwhile).
        self.media = {"state": "idle", "error": None, "updated": None, "stores": {}, "files": []}
        self._media_lock = threading.Lock()
        self._media_chunks: dict[int, dict[tuple[int, int], dict]] = {}   # counter -> {(type, seq): chunk}
        self._media_cond = threading.Condition()
        self._media_counter = 1
        self._thumbs: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.cache = MediaCache(data_dir() / "media")    # last listing and thumbnails, per camera
        self.allow_delete = True               # deleting files on the camera is offered (--no-delete: not)
        self._media_action = 0                 # counter carried by favourite / delete requests
        self.downloader = Downloader(default_download_dir())
        # While the gallery is open the camera stays in media mode: no live view to encode, so it runs
        # cooler and serves files about 6x faster. The page renews the hold; it lapses 30 s after the
        # page goes quiet.
        self.media_hold = False
        self.media_hold_seen = 0.0
        # The Osmo Pocket 3 refuses media mode (02/0C answers e0) yet lists its newest files: don't keep asking.
        self.media_mode_refused = False
        self.want_link = threading.Event()     # the UI's connect / disconnect switch
        self.reconnect = threading.Event()     # restart the session (the camera's Wi-Fi setting changed)
        self.want_link.set()
        self.link = "connecting"               # select | off | connecting | on
        self.home = ""                         # Wi-Fi network the computer returns to when disconnected
        # Cameras: linked ones are remembered; `target` is the one to connect to (None = let the user
        # pick); `nearby` holds the cameras heard over BLE; `stage` tracks the connection steps.
        self.registry = CameraRegistry()
        self.target: str | None = None
        self.target_changed = threading.Event()
        self.nearby: dict[str, dict] = {}
        self.scanning = False
        self.stage: str | None = None
        self.stage_error: str | None = None
        self.stage_note: str | None = None     # progress within the current step (network join attempts...)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state_camera: str | None = None   # camera the state and gallery shown belong to
        self.tiles: dict[str, Tile] = {}       # Multiview: extra cameras, by address
        self.tap = None                        # sees every camera message (the camera report)

    # ---- incoming data -------------------------------------------------------------------------
    def on_frame(self, frame, _first_packet: float = 0.0) -> None:
        if self.vcam is not None:
            self.vcam.send(frame)
        with self.frame_cond:
            self.latest_frame = frame
            self.frame_seq += 1
            self.last_frame = time.monotonic()
            self.frame_cond.notify_all()

    def on_message(self, m: Message) -> None:
        if self.tap is not None:              # the camera report (--report) keeps a summary of the messages
            self.tap(m)
        if m.is_reply:
            with self._pending_lock:
                for pending in self._pending:
                    if pending.key == m.command and pending.seq in (None, m.seq):
                        pending.payload = bytes(m.payload)
                        pending.done.set()
                        break
            return
        if m.command == (0x00, 0x27):
            chunk = media.parse_chunk(bytes(m.payload))
            if chunk is not None:
                with self._media_cond:
                    self._media_chunks.setdefault(chunk["counter"], {})[(chunk["type"], chunk["seq"])] = chunk
                    self._media_cond.notify_all()
        elif m.command == (0x02, 0x80):
            self.state.update_status(m.payload)
        elif m.command == (0x0D, 0x02):
            self.state.update_battery(m.payload)
        elif m.command == (0x02, 0xDC):
            self.state.update_stores(m.payload)
        elif m.command == (0x04, 0x05):
            self.state.update_attitude(m.payload)
        elif m.command == (0x04, 0x27):
            self.state.update_facing(m.payload)
        elif m.command == (0x00, 0x99):
            self.state.update_topic(m.payload)

    # ---- commands ------------------------------------------------------------------------------
    def request(self, cmd_set: int, cmd_id: int, payload: bytes = b"", timeout: float = 2.5,
                quiet: bool = False, receiver: int | None = None) -> dict:
        """Send a command and wait for the camera's reply (matched by sequence). Result code 00 = OK."""
        link = self.datalink
        if link is None or not link.ready.is_set():  # nothing before the handshake and the live view start
            return {"ok": False, "error": "not connected to the camera"}
        if not self._command_lock.acquire(timeout=timeout):
            return {"ok": False, "error": "another command is in progress"}
        pending = Pending(cmd_set, cmd_id)
        try:
            with self._pending_lock:
                self._pending.append(pending)
            try:
                pending.seq = link.send(cmd_set, cmd_id, payload, receiver)
            except OSError:              # the session closed its socket meanwhile (disconnect / restart)
                return {"ok": False, "error": "not connected to the camera"}
            answered = pending.done.wait(timeout)
        finally:
            with self._pending_lock:
                self._pending.remove(pending)
            self._command_lock.release()
        label = f"{cmd_set:02x}/{cmd_id:02x} [{payload.hex()}]"
        if not answered:
            log(f"{label} -> no reply")
            return {"ok": False, "error": "the camera did not reply"}
        reply = pending.payload.hex()
        code = pending.payload[0] if pending.payload else 0
        if not quiet:
            log(f"{label} -> {reply or 'ok'}")
        if code == 0:
            return {"ok": True, "reply": reply}
        return {"ok": False, "code": code, "reply": reply,
                "error": cam.RESULT_TEXT.get(code, f"refused (0x{code:02x})")}

    def read_settings(self) -> None:
        """Read the exposure and format back. Kept as they were when the camera doesn't answer (another
        command in flight), so the page doesn't flicker."""
        answers = [self.request(0x02, cmd_id, timeout=1.5, quiet=True)
                   for cmd_id in (0x1F, 0x2B, 0x29, 0x19, 0x13, 0x2D)]
        settings = cam.parse_settings(*(bytes.fromhex(a.get("reply", "")) for a in answers))
        if settings is None:
            return
        # Field of view and stabilisation (02/8E parameters), when the current capture mode offers them.
        _, caps = self.state.capabilities()
        old = self.state.settings or {}
        if self.pocket_model() == cam.POCKET3:
            answer = self.request(0x02, 0x8E, cam.parameter_tlv_get(cam.PARAM_SELFIE_FLIP), timeout=1.5, quiet=True)
            value = cam.parameter_value(bytes.fromhex(answer.get("reply", "")), cam.PARAM_SELFIE_FLIP)
            settings["selfie_flip"] = bool(value) if value is not None else old.get("selfie_flip")
        for key, table, param in (("fov", "camcap_fov", cam.PARAM_FOV), ("eis", "camcap_eis", cam.PARAM_EIS)):
            settings[key] = None
            if caps.get(table):
                answer = self.request(0x02, 0x8E, cam.parameter_tlv_get(param), timeout=1.5, quiet=True)
                value = cam.parameter_value(bytes.fromhex(answer.get("reply", "")), param)
                settings[key] = value if value is not None else old.get(key)
        self.state.update_settings(settings)

    def settings_loop(self, stop: threading.Event) -> None:
        """Keep the settings current: changes made on the camera itself, and each capture mode's own
        values after a mode switch (settings_due wakes it early)."""
        while not stop.is_set():
            self.settings_due.wait(2.0)
            self.settings_due.clear()
            if self.datalink is not None and self.state.connected and not self.state.in_playback():
                try:
                    self.read_settings()
                except Exception as error:  # noqa: BLE001 - never let this thread die
                    log(f"Reading the settings failed: {error}")

    def set_capture_mode(self, mode: int) -> dict:
        self.video_grace = max(self.video_grace, time.monotonic() + 4)   # a mode change pauses the video
        result = self.request(0x02, 0xE1, bytes((mode,)))
        self.settings_due.set()           # each capture mode keeps its own exposure
        return result

    def take_photo(self) -> dict:
        return self.request(0x02, 0x01, b"\x01")

    def record(self, start: bool) -> dict:
        """Timelapse is interval shooting: on the Osmo Nano and Pocket 3 it starts and stops with take-photo
        (record answers DF there). Every other video mode uses record (see camera.record_command)."""
        cmd_id = cam.record_command(self.pocket_model(), self.state.capture_mode())
        return self.request(0x02, cmd_id, b"\x01" if start else b"\x00")

    def record_video(self, start: bool) -> dict:
        """Record from Multiview: a camera left in Photo mode is switched to Video first."""
        if start and self.state.capture_mode() in cam.PHOTO_MODES:
            switched = self.set_capture_mode(cam.MODE_VIDEO)
            if not switched.get("ok"):
                return {"ok": False, "error": f"in Photo mode, and switching to Video failed ({switched.get('error')})"}
            end = time.monotonic() + 3
            while time.monotonic() < end and self.state.capture_mode() in cam.PHOTO_MODES:
                time.sleep(0.1)
            log("Active camera: it was in Photo mode, switched to Video to record")
        return self.record(start)

    def set_setting(self, cmd_id: int, payload: bytes) -> dict:
        # a setting change can pause the camera's encoder for a moment: don't treat it as a stall
        self.video_grace = max(self.video_grace, time.monotonic() + 4)
        result = self.request(0x02, cmd_id, payload)
        self.read_settings()
        return result

    def set_parameter(self, param: int, value: int) -> dict:
        """A one-byte 02/8E parameter (field of view, stabilisation). Can restart the video encoder."""
        self.video_grace = time.monotonic() + 8
        return self.set_setting(0x8E, cam.parameter_tlv_set(param, value))

    def set_timelapse(self, interval: int | None, duration: int | None) -> dict:
        """Interval (tenths of a second) and/or duration (seconds); the other one and what the timelapse
        keeps besides the video stay as they are."""
        lapse = self.state.lapse() or {}
        interval = interval if interval is not None else lapse.get("interval") or 20
        duration = duration if duration is not None else lapse.get("duration", 0)
        return self.request(0x02, cam.LAPSE_SET, cam.timelapse_payload(lapse.get("save", 0), interval, duration))

    def set_hyperlapse(self, ratio: int) -> dict:
        return self.request(0x02, cam.LAPSE_SET, cam.hyperlapse_payload(ratio))

    # ---- Osmo Pocket: gimbal and zoom --------------------------------------------------------------
    def pocket_model(self) -> int | None:
        """Model id of the active camera when it is an Osmo Pocket (gimbal and zoom), else None."""
        model = (self.registry.get(self.target) or {}).get("model_id")
        return model if model in POCKETS else None

    def gimbal(self, action: str, value: str | None = None) -> dict:
        """recenter, flip (turn 180°), mode (follow / tilt_lock / fpv / direction_lock), speed."""
        if action in ("recenter", "flip"):
            payload = cam.GIMBAL_RECENTER if action == "recenter" else cam.GIMBAL_FLIP
            return self.request(0x04, 0x4C, payload, receiver=ADDR_GIMBAL)
        if action == "mode" and value in cam.GIMBAL_MODES:
            result: dict = {"ok": True}
            for cmd_id, payload in cam.GIMBAL_MODES[value]:
                result = self.request(0x04, cmd_id, payload, receiver=ADDR_GIMBAL)
                if not result.get("ok"):
                    break
            return result
        if action == "speed" and value in cam.GIMBAL_SPEEDS:
            return self.request(0x04, 0x50, bytes((0, 5, 1, cam.GIMBAL_SPEEDS[value])), receiver=ADDR_GIMBAL)
        return {"ok": False, "error": "unknown gimbal action"}

    def set_stick(self, tilt: float, pan: float) -> None:
        """Hold the stick at (tilt, pan); (0, 0) releases it. The page repeats this while a control is
        held; pocket_loop() streams it to the camera and releases it when the page goes quiet."""
        self._stick = (max(-1.0, min(1.0, tilt)), max(-1.0, min(1.0, pan)))
        self._stick_seen = time.monotonic()

    def zoom_to(self, factor: float) -> dict:
        return self.request(0x02, 0xB8, cam.zoom_payload(factor))

    def zoom_move(self, direction: str | None) -> dict:
        """'in' / 'out' while a zoom button is held (repeated by the page), None to stop."""
        if direction is None:
            self._zoom_moving = False
            return self.request(0x02, 0xB8, cam.ZOOM_STOP)
        self._zoom_seen = time.monotonic()
        if self._zoom_moving:
            return {"ok": True}
        self._zoom_moving = True
        return self.request(0x02, 0xB8, cam.zoom_move_payload(direction == "in"))

    def pocket_loop(self, stop: threading.Event) -> None:
        """Stream the held stick every 50 ms (one centred packet on release, then nothing: a stream of
        centred packets pauses the video), and stop a held zoom when the page stops renewing it."""
        while not stop.wait(0.05):
            link, now = self.datalink, time.monotonic()
            try:
                tilt, pan = self._stick
                held = (tilt or pan) and now - self._stick_seen < 0.5
                if held and link is not None:
                    attitude = self.state.gimbal()
                    if attitude and abs(attitude["pan"]) > cam.FLIPPED_YAW:
                        pan = -pan           # turned 180°: left and right swap
                    link.send(0x04, 0x01, cam.gimbal_stick(tilt, pan), ADDR_GIMBAL, 0x00)
                    self._stick_on = True
                elif self._stick_on:
                    self._stick_on = False
                    self._stick = (0.0, 0.0)
                    if link is not None:
                        link.send(0x04, 0x01, cam.gimbal_stick(0, 0), ADDR_GIMBAL, 0x00)
                if self._zoom_moving and now - self._zoom_seen > 0.8:
                    self.zoom_move(None)
            except OSError:
                pass
            except Exception as error:  # noqa: BLE001 - never let this thread die
                log(f"Pocket controls: {error}")

    def set_frame_rate(self, code: int) -> dict:
        """Keep the current resolution, change the frame rate."""
        answer = self.request(0x02, 0x19)
        reply = bytes.fromhex(answer.get("reply", ""))
        if not answer.get("ok") or len(reply) < 3:
            return {"ok": False, "error": "could not read the current video format"}
        return self.set_video_format(reply[1], code)

    def set_video_format(self, res: int, fps: int) -> dict:
        """Resolution + frame rate for the current capture mode (slow-mo adds its speed factor). The camera
        restarts its encoder, so the live view pauses for a few seconds: tell the session watchdog. On the Osmo
        Nano a 4:3 resolution needs the aspect switched to 4:3 first; the Osmo Pockets carry the aspect in
        the resolution code. The Osmo Action 4 refuses the aspect setting (E3) and takes every format alone, 4:3
        too, so it gets none; another camera that refuses it gets the format anyway."""
        self.video_grace = time.monotonic() + 12
        mode = self.state.capture_mode()
        model = (self.registry.get(self.target) or {}).get("model_id")
        # A 16:9 code switches the Nano's picture back by itself but leaves its aspect setting at 4:3, so
        # in Video mode set the aspect explicitly either way.
        if (mode != cam.MODE_SLOWMO and model not in POCKETS and model != cam.ACTION4
                and (res in cam.RES_4_3 or mode == cam.MODE_VIDEO)):
            aspect = self.request(0x02, 0x8E, cam.parameter_tlv_set(cam.ASPECT_TLV, 1 if res in cam.RES_4_3 else 2))
            if not aspect.get("ok") and (model == NANO or "code" not in aspect):   # not refused: no reply
                return aspect
        return self.set_setting(0x18, cam.video_format_payload(model, mode, res, fps))

    def set_white_balance(self, manual: bool, kelvin: int | None) -> dict:
        """Auto keeps the manual temperature the camera remembers; manual sets one (2000-10000 K)."""
        if kelvin is None:
            answer = self.request(0x02, 0x2D)
            reply = bytes.fromhex(answer.get("reply", ""))
            kelvin = reply[2] * 100 if answer.get("ok") and len(reply) > 2 else 5600
        mode = cam.WB_MANUAL if manual else cam.WB_AUTO
        return self.set_setting(0x2C, bytes((mode, kelvin // 100, 0, 0, 0)))

    # ---- media library -------------------------------------------------------------------------
    def _list_page(self, cursor: int) -> tuple[list[dict] | None, str | None]:
        """One 00/26 query; the answer streams back as 00/27 chunks ending with an end chunk. Returns
        (files, None), or (None, reason) when that storage doesn't answer."""
        self._media_counter = self._media_counter % 0xFFF0 + 1
        counter = self._media_counter
        with self._media_cond:
            self._media_chunks[counter] = {}
        answer = self.request(0x00, 0x26, media.list_payload(counter, cursor), timeout=3.0)
        if answer.get("code") in (0xD8, 0xD6):
            return None, "not available"
        # Without an accepted reply the list may still come (a lost reply): wait for it a little.
        deadline = time.monotonic() + (10 if answer.get("ok") else 2)
        end_at = None                          # when the end chunk came (it can come before the data)
        complete = False
        with self._media_cond:
            while time.monotonic() < deadline:
                chunks = self._media_chunks.get(counter, {})
                if end_at is None and any(kind == media.CHUNK_END for kind, _ in chunks):
                    end_at = time.monotonic()
                complete = media.page_complete(chunks, end_at is not None and time.monotonic() - end_at > 1.0)
                if complete:
                    break
                self._media_cond.wait(0.1)
            page = self._media_chunks.pop(counter, {})
        if not page and not answer.get("ok"):
            return None, answer.get("error", "no reply")
        if not complete:
            return None, "the camera did not finish sending the list"
        data = b"".join(page[k]["data"] for k in sorted(k for k in page if k[0] == media.CHUNK_DATA))
        return media.parse_manifest(data) if data else [], None

    def list_media(self) -> dict:
        """Read the whole library (internal memory, then the dock's microSD): enter media mode, page
        through it newest first, then leave media mode unless the gallery is still open."""
        snap = self.state.snapshot()
        if snap["record"] and snap["record"]["state"] != "idle":
            return {"ok": False, "error": "the camera is recording"}
        if self.datalink is None:
            return {"ok": False, "error": "not connected to the camera"}
        if not self._media_lock.acquire(blocking=False):
            return {"ok": False, "error": "the gallery is already being read"}
        try:
            self.media.update(state="listing", error=None)
            self.video_grace = time.monotonic() + 15
            if not self.state.in_media_mode() and not self.media_mode_refused:
                answer = self.request(0x02, 0x0C, cam.ENTER_MEDIA)
                entered = bool(answer.get("ok"))
                self.media_mode_refused = not entered
                if entered:
                    for _ in range(40):
                        if self.state.in_media_mode():
                            break
                        time.sleep(0.1)
                    time.sleep(1.7)                # the camera needs a moment before the first query
                else:
                    log(f"No media mode ({answer.get('error')}): listing without it")
            sd_only = (self.registry.get(self.target) or {}).get("model_id") in SD_ONLY
            files: dict[tuple[int, str], dict] = {}
            stores = {}
            partial = False                        # a store stopped answering after its newest files
            stores_to_read = (("sd", media.CURSOR_SD),) if sd_only else (("internal", media.CURSOR_INTERNAL),
                                                                          ("sd", media.CURSOR_SD))
            for store, first in stores_to_read:
                cursor = first
                for _ in range(200):
                    page, problem = self._list_page(cursor)
                    if page is None:
                        stores.setdefault(store, problem)
                        partial = partial or cursor != first
                        break
                    stores[store] = "ok"
                    if sd_only:
                        for f in page:
                            f["storage"] = 0
                    new = [f for f in page if (f["storage"], f["path"]) not in files]
                    for f in new:
                        files[(f["storage"], f["path"])] = f
                    self.media["files"] = list(files.values())   # show progress while paging
                    if len(page) < media.PAGE_SIZE or not new:
                        break
                    cursor += len(page)            # pages overlap by one file; duplicates are skipped
            result = sorted(files.values(), key=lambda f: (media.capture_time(f["name"]) or "", f["name"]), reverse=True)
            for f in result:
                f["stamp"] = media.capture_time(f["name"]) or f["date"]
            self.media.update(state="idle", files=result, stores=stores, updated=time.time(), cached=False,
                              partial=partial)
            self._save_media()
            log(f"Media library: {len(result)} files (" + ", ".join(f"{k}: {v}" for k, v in stores.items()) + ")")
            return {"ok": True, "count": len(result), "stores": stores}
        except Exception as error:  # noqa: BLE001 - shown on the page
            self.media.update(state="error", error=str(error))
            return {"ok": False, "error": str(error)}
        finally:
            if not self.media_hold and not self.downloader.active() and not self.media_mode_refused:
                self.request(0x02, 0x0C, cam.LEAVE_MEDIA)
                self.video_grace = time.monotonic() + 8
            self._media_lock.release()

    def refresh_media(self) -> dict:
        if self.media["state"] == "listing":
            return {"ok": True, "already": True}
        threading.Thread(target=self.list_media, daemon=True, name="media-list").start()
        return {"ok": True}

    def set_media_hold(self, on: bool) -> dict:
        self.media_hold_seen = time.monotonic()
        if on == self.media_hold:
            return {"ok": True}
        self.media_hold = on
        if self._media_lock.locked() or (not on and self.downloader.active()) or self.media_mode_refused:
            return {"ok": True}       # list_media handles the mode itself; downloads keep it
        self.video_grace = time.monotonic() + (10 if on else 8)
        answer = self.request(0x02, 0x0C, cam.ENTER_MEDIA if on else cam.LEAVE_MEDIA)
        self.media_mode_refused = answer.get("code") == 0xE0
        return answer

    def media_hold_loop(self, stop: threading.Event) -> None:
        """Keep media mode while the gallery is open or files are downloading (enter it again after a
        reconnect), leave it when the page stops renewing and nothing is downloading, and leave a media
        mode left over from an earlier session."""
        last_leave = 0.0
        while not stop.wait(2.0):
            try:
                if self.media_hold and time.monotonic() - self.media_hold_seen > 30:
                    log("Gallery closed: leaving media mode")
                    self.media_hold = False
                if (self.datalink is None or not self.state.connected or self._media_lock.locked()
                        or self.media_mode_refused):
                    continue
                if self.state.snapshot()["record"] is None:
                    continue
                want = self.media_hold or self.downloader.active() > 0
                in_media = self.state.in_media_mode()
                if want and not in_media:
                    # Only an accepted request earns the video watchdog's grace: if the camera is gone (flat
                    # battery, Wi-Fi off) the session must be allowed to time out and reconnect.
                    answer = self.request(0x02, 0x0C, cam.ENTER_MEDIA, quiet=True)
                    if answer.get("ok"):
                        self.video_grace = time.monotonic() + 10
                    self.media_mode_refused = answer.get("code") == 0xE0
                elif not want and in_media and time.monotonic() - last_leave > 10:
                    # (the Osmo Pocket 3 reports its own on-screen gallery the same way, and refuses this)
                    last_leave = time.monotonic()
                    self.video_grace = time.monotonic() + 8
                    self.media_mode_refused = self.request(0x02, 0x0C, cam.LEAVE_MEDIA).get("code") == 0xE0
            except Exception as error:  # noqa: BLE001 - never let this thread die
                log(f"Media mode keeper: {error}")

    def media_snapshot(self) -> dict:
        dl = self.downloader
        files = [dict(f, downloaded=dl.is_downloaded(f["name"], f["size"]),
                      res=cam.RESOLUTIONS.get(f.get("res_code"))) for f in self.media["files"]]
        return {"state": self.media["state"], "error": self.media["error"], "updated": self.media["updated"],
                "stores": self.media["stores"], "files": files, "folder": str(dl.folder),
                "cached": bool(self.media.get("cached")), "partial": bool(self.media.get("partial")),
                # on a shared network the gallery needs the camera's own Wi-Fi (thumbnails, downloads)
                "note": media.NO_FILES_ON_NETWORK if self.camera_host != CAMERA_HOST else None,
                "can_delete": self.allow_delete}

    def _save_media(self) -> None:
        if self.state_camera:
            self.cache.save(self.state_camera, self.media["updated"], self.media["stores"], self.media["files"])

    def _find(self, storage: int, path: str) -> dict | None:
        return next((f for f in self.media["files"] if f["storage"] == storage and f["path"] == path), None)

    def set_favourite(self, storage: int, path: str, on: bool) -> dict:
        """The camera's own favourite mark (the Osmo Nano keeps it with the file)."""
        if self.media.get("cached"):
            return {"ok": False, "error": "refresh the gallery first (the list is from last time)"}
        f = self._find(storage, path)
        if f is None:
            return {"ok": False, "error": "file not in the list (refresh the gallery)"}
        self._media_action += 1
        result = self.request(0x02, 0xBF, media.favourite_payload(f["handle"], self._media_action, on))
        if result.get("ok"):
            f["starred"] = on
            self._save_media()
        return result

    def delete_files(self, items: list[tuple[int, str]]) -> dict:
        """Delete files on the camera, one at a time; each request is sent once (never repeated). The page
        asks for confirmation first; --no-delete turns it off."""
        if not self.allow_delete:
            return {"ok": False, "error": "deleting is off (the app was started with --no-delete)"}
        if self.datalink is None:
            return {"ok": False, "error": "not connected to the camera"}
        if self.media.get("cached"):         # file handles from last time may point at other files now
            return {"ok": False, "error": "refresh the gallery first (the list is from last time)"}
        deleted, failed = [], []
        for storage, path in items:
            f = self._find(storage, path)
            if f is None:
                failed.append(path)
                continue
            self._media_action += 1
            result = self.request(0x00, 0x28, media.delete_payload(f["handle"], self._media_action), timeout=6)
            if result.get("ok"):
                deleted.append(path)
                self.media["files"] = [x for x in self.media["files"] if x is not f]
                log(f"Deleted on the camera: {f['name']}")
            else:
                failed.append(path)
                log(f"Not deleted: {f['name']} ({result.get('error')})")
        self._save_media()
        if failed:
            return {"ok": False, "deleted": len(deleted), "error": f"{len(failed)} not deleted (refresh to check)"}
        return {"ok": True, "deleted": len(deleted)}

    def _fetch_file(self, storage: int, path: str) -> bytes | None:
        """A small camera file (a thumbnail) over HTTP, None when it isn't there. Raises ConnectionRefusedError
        on a shared network where the camera sends no files."""
        try:
            with urllib.request.urlopen(media.http_url(storage, path, self.camera_host), timeout=6) as response:
                return response.read()
        except (urllib.error.URLError, OSError) as error:
            if media.refused(error) and self.camera_host != CAMERA_HOST:
                raise ConnectionRefusedError from None
            return None

    def thumbnail(self, storage: int, path: str) -> bytes | None:
        key = f"{storage}:{path}"
        if key in self._thumbs:
            self._thumbs.move_to_end(key)
            return self._thumbs[key]
        data = self.cache.thumb(self.state_camera, key) if self.state_camera else None
        fetched = data is None
        stem, _, ext = path.rpartition(".")
        try:
            if data is None:
                tries = [path]
                if "." + ext.upper() in media.THUMB_EXTS:   # the camera may keep it under the other extension
                    tries += [stem + other for other in media.THUMB_EXTS if other != "." + ext.upper()]
                for attempt in tries:
                    data = self._fetch_file(storage, attempt)
                    if data:
                        break
                if not data:
                    return None
            # (only while connected: with the camera away, a cached small one is shown at once)
            if self.link == "on" and 0 < _image_width(data) < THUMB_MIN and ext.upper() == "THM":
                larger = self._fetch_file(storage, stem + ".SCR")
                better = _shrink(larger) if larger else None
                if better:
                    data, fetched = better, True
        except ConnectionRefusedError:
            if data is None:
                return None
        if fetched and self.state_camera:
            self.cache.put_thumb(self.state_camera, key, data)
        self._thumbs[key] = data
        while len(self._thumbs) > 2000:
            self._thumbs.popitem(last=False)
        return data

    # ---- status for the page -------------------------------------------------------------------
    def status(self) -> dict:
        snap = self.state.snapshot()
        snap["video_age"] = round(time.monotonic() - self.last_frame, 1) if self.last_frame else None
        snap["link"] = self.link
        snap["recovery"] = self.recovery
        snap["vcam"] = self.vcam.device if self.vcam is not None and self.vcam.active else None
        pocket = self.pocket_model()
        if snap.get("capture_mode") is not None:
            snap["capture_mode_name"] = cam.mode_name(snap["capture_mode"], pocket)
        settings = snap.get("settings") or {}
        snap["pocket"] = None if pocket is None else {
            "model_id": pocket,
            "gimbal": self.state.gimbal(),
            "zoom": self.state.zoom(),
            "zoom_stops": cam.zoom_stops(pocket, settings.get("resolution_code"), snap.get("capture_mode")),
            "facing": self.state.facing_now(),
            "selfie_flip": settings.get("selfie_flip"),
        }
        snap["home"] = self.home
        tiles = [t.snapshot() for t in list(self.tiles.values())]
        snap["multiview"] = {"cameras": len(tiles),
                             "recording": sum(1 for t in tiles if (t["record"] or {}).get("state") == "recording")}
        snap["media_state"] = self.media["state"]
        snap["downloads_active"] = self.downloader.active()
        # The active camera: what we know of it, plus its advertisement (only heard while disconnected).
        known = self.registry.get(self.target) or {}
        heard = self.nearby.get(self.target or "")
        if heard and time.monotonic() - heard["t"] < 15:
            snap["camera"] = {"name": heard.get("name") or known.get("name"), "rssi": heard["rssi"],
                              "power": heard.get("power") or "unknown"}
        else:
            snap["camera"] = None
        snap["active"] = None if not self.target else {
            "address": self.target, "name": known.get("name") or (heard or {}).get("name") or self.target,
            "model": known.get("model") or (heard or {}).get("model") or "DJI Osmo",
            "tested": _tested(known or heard or {}), "known": bool(known)}
        snap["stage"] = self.stage
        snap["stage_error"] = self.stage_error
        snap["stage_note"] = self.stage_note
        return snap

    # ---- cameras -------------------------------------------------------------------------------
    def use_camera(self, address: str, name: str) -> None:
        """Connecting to a camera: start from a clean state when it isn't the one shown so far (status,
        topics, settings and gallery belong to a camera), and point downloads at its folder."""
        if self.state_camera != address:
            if self.state_camera is not None:
                self.downloader.cancel_all()
            self.state = CameraState()
            with self.frame_cond:            # the last picture belongs to the previous camera
                self.latest_frame, self.last_frame = None, 0.0
            self.media = {"state": "idle", "error": None, "updated": None, "stores": {}, "files": []}
            cached = self.cache.load(address)
            if cached:                            # what the camera had last time, until it is read again
                self.media.update(updated=cached.get("updated"), stores=cached.get("stores") or {},
                                  files=cached["files"], cached=True)
            self._thumbs.clear()
            self.media_hold = False
            self.media_mode_refused = False
            self.state_camera = address
        self.downloader.sub = "".join(ch for ch in (name or address) if ch.isalnum() or ch in "-_ ").strip() or "camera"

    def on_seen(self, entry: dict) -> None:
        old = self.nearby.get(entry["address"])
        if old and not entry.get("name"):
            entry["name"] = old.get("name", "")
        self.nearby[entry["address"]] = entry

    async def discover(self, until) -> None:
        """Listen for cameras over BLE (model + awake/asleep from their advertisement) until `until()` is
        true. Runs while no camera is connected: the scanner must stop before a BLE connection starts."""
        self.scanning = True
        try:
            await ble.scan(10 ** 6, on_found=self.on_seen, stop=until)
        except Exception as error:  # noqa: BLE001 - Bluetooth off or busy: the list just stays empty
            log(f"Bluetooth scan unavailable: {error} ({ble.UNAVAILABLE_HINT})")
            while not until():
                await asyncio.sleep(0.5)
        finally:
            self.scanning = False

    async def quick_scan(self, seconds: float = 6.0) -> None:
        """On-demand scan while a camera is connected (the 'Search again' button)."""
        if self.scanning:
            return
        self.scanning = True
        try:
            await ble.scan(seconds, on_found=self.on_seen)
        except Exception as error:  # noqa: BLE001
            log(f"Bluetooth scan unavailable: {error}")
        finally:
            self.scanning = False

    def select(self, address: str) -> dict:
        """Make `address` the active camera (a new one gets paired: the camera asks to approve "OSMO")."""
        address = address.upper()
        if address in self.tiles:            # it was shown in Multiview: one session per camera
            self.tiles[address].close()
        seen = self.nearby.get(address)
        if seen and seen.get("unsupported"):
            return {"ok": False, "error": seen["unsupported"]}
        if self.registry.get(address) is None and seen is None:
            return {"ok": False, "error": "camera not found: turn it on, bring it closer and search again"}
        self.set_stage(None)
        if self.target != address:
            self.target = address
            self.target_changed.set()
        self.want_link.set()
        return {"ok": True}

    def set_network(self, address: str, ssid: str | None, password: str | None) -> dict:
        """Reach this camera over the shared network `ssid` (the camera joins it), or over its own access
        point (None). Applies at once to the active camera."""
        address = address.upper()
        if self.registry.get(address) is None:
            return {"ok": False, "error": "unknown camera"}
        if ssid:
            if len(ssid.encode()) > 32 or (password is not None and len(password.encode()) > 63):
                return {"ok": False, "error": "network name up to 32 characters, password up to 63"}
            if password is None and self.registry.network_password(ssid) is None:
                return {"ok": False, "error": f"type the password of “{ssid}”"}
        self.registry.set_network(address, ssid, password)
        if address == self.target:
            self.set_stage(None)
            self.reconnect.set()
            self.want_link.set()             # "Save and reconnect", also after giving up (a refused network)
        return {"ok": True}

    # ---- Multiview ------------------------------------------------------------------------------
    def multiview_snapshot(self) -> dict:
        candidates = [{"address": c["address"], "name": c.get("name") or c["address"], "model": c.get("model"),
                       "network": c.get("network")}
                      for c in self.registry.public() if c["address"] != self.target and c["address"] not in self.tiles]
        model_id = (self.registry.get(self.target) or {}).get("model_id")
        return {"tiles": [t.snapshot() for t in list(self.tiles.values())], "candidates": candidates, "max": MAX_EXTRA,
                "active_modes": cam.multiview_modes(model_id)}

    def add_tile(self, address: str) -> dict:
        address = address.upper()
        known = self.registry.get(address)
        if known is None:
            return {"ok": False, "error": "unknown camera"}
        if address == self.target:
            return {"ok": False, "error": "that is the active camera"}
        if address in self.tiles:
            return {"ok": True}
        if len(self.tiles) >= MAX_EXTRA:
            return {"ok": False, "error": f"up to {MAX_EXTRA} more cameras"}
        if not known.get("network"):
            return {"ok": False, "error": "set a shared Wi-Fi network for it first (Cameras → Wi-Fi → Change)"}
        if self.loop is None:
            return {"ok": False, "error": "the app is starting"}
        tile = Tile(self, address)
        self.tiles[address] = tile
        future = asyncio.run_coroutine_threadsafe(tile.run(), self.loop)
        future.add_done_callback(lambda _f: self.tiles.pop(address) if self.tiles.get(address) is tile else None)
        log(f"Multiview: adding {tile.name}")
        return {"ok": True}

    def disconnect(self) -> dict:
        """The page's Disconnect: release every camera, the active one and the Multiview ones (each goes back
        to its own Wi-Fi)."""
        self.want_link.clear()
        self.set_stage(None)                  # stopped on purpose: no error left to show
        for tile in list(self.tiles.values()):
            tile.close()
        return {"ok": True}

    def remove_tile(self, address: str) -> dict:
        tile = self.tiles.get(address.upper())
        if tile is not None:
            tile.close()
        return {"ok": True}

    def record_all(self, start: bool) -> dict:
        """Start or stop recording on the active camera and every live extra one at the same time. Each
        result is the camera's own reply; nothing is repeated."""
        name = (self.registry.get(self.target) or {}).get("name") or "Active camera"
        jobs = [(name, lambda: self.record_video(start))] if self.datalink is not None else []
        jobs += [(t.name, lambda t=t: t.record(start)) for t in list(self.tiles.values()) if t.state == "live"]
        if not jobs:
            return {"ok": False, "error": "no camera is connected"}
        with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
            replies = list(pool.map(lambda job: (job[0], job[1]()), jobs))
        results = [{"name": n, "ok": bool(r.get("ok")), "error": r.get("error")} for n, r in replies]
        confirmed = sum(r["ok"] for r in results)
        log(f"Record all ({'start' if start else 'stop'}): {confirmed} of {len(results)} confirmed")
        return {"ok": confirmed == len(results), "confirmed": confirmed, "total": len(results), "results": results}

    def forget(self, address: str) -> None:
        address = address.upper()
        if address in self.tiles:
            self.tiles[address].close()
        self.registry.forget(address)
        self.cache.forget(address)
        if self.target == address:
            self.target = None
            self.target_changed.set()

    def set_stage(self, stage: str | None, error: str | None = None) -> None:
        self.stage, self.stage_error, self.stage_note = stage, error, None

    def set_stage_note(self, note: str | None) -> None:
        self.stage_note = note

    def cameras_snapshot(self) -> dict:
        now = time.monotonic()

        def seen_info(address: str) -> dict:
            heard = self.nearby.get(address)
            if not heard or now - heard["t"] > 20:
                return {"seen": False}
            return {"seen": True, "rssi": heard["rssi"], "power": heard.get("power")}

        known = []
        for cam_info in self.registry.public():
            info = dict(cam_info, **seen_info(cam_info["address"]))
            info["tested"] = _tested(cam_info)
            info["active"] = cam_info["address"] == self.target
            info["connected"] = info["active"] and self.link == "on"
            known.append(info)
        known_ids = {c["address"] for c in known}
        nearby = [dict(n, **seen_info(a)) for a, n in sorted(self.nearby.items(), key=lambda i: -i[1]["rssi"])
                  if a not in known_ids and (now - n["t"] <= 20 or a == self.target)]
        return {"target": self.target, "known": known, "nearby": nearby, "scanning": self.scanning,
                "stage": self.stage, "stage_error": self.stage_error, "stage_note": self.stage_note, "link": self.link,
                "networks": self.registry.network_names(), "home": self.home,
                "tested_models": [MODELS[m] for m in sorted(TESTED_MODELS)]}
