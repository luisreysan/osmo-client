"""Camera commands, setting codes and status parsers (command set 0x02 unless noted).

All of this was verified on an Osmo Nano; the full description is in docs/PROTOCOL.md.

    take photo           02/01 [01]                    (photo mode)
    timelapse start/stop 02/01 [01] / [00]             (timelapse rides take-photo; Osmo Pocket 4 / 4P: 02/02)
    record start/stop    02/02 [01] / [00]             (video, slow-mo, hyperlapse, super night)
    capture mode         02/E1 [mode]                  (see MODES)
    exposure mode        02/1E [mode, 00]              01 auto, 04 manual; read 02/1F
    ISO                  02/2A [code]                  manual only; read 02/2B
    shutter speed        02/28 [01, u16 LE, decimal, 00, 00, 00]   manual only; read 02/29
    video format         02/18 [resolution, fps, 00, 00, slow-mo factor]; read 02/19
    aspect 4:3 / 16:9    02/8E [01 01, 46 00, 01, 1|2] (parameter TLV); read [00 01 46 00]
    photo format         02/12 [size, ratio]; read 02/13 (photo mode only)
    white balance        02/2C [mode, kelvin / 100, 00, 00, 00]; read 02/2D
    exposure comp. (EV)  02/2E [0x10 + thirds of a stop]   Auto only; read cam_expo_param
    colour profile       02/42 [code]                  (see COLOR_MODES); read cam_image_effect
    sharpness / noise    02/38 [s8] / 02/44 [s8]       read cam_image_effect
    field of view        02/8E parameter 0x0009        (see FOV_LABELS)
    stabilisation        02/8E parameter 0x0008        (see EIS_LABELS)
    timelapse            02/6C [04 00 save, interval u16 LE (0.1 s), duration u32 LE (s), 00 x 7]
    hyperlapse speed     02/6C [0B 00 00, ratio u16 LE (0 = auto), 00 x 11]; both read cam_lapse_param

The commands from exposure compensation down follow the capability tables an Osmo Nano publishes; they
were not all exercised on one yet.

Osmo Pocket 3 / 4 / 4P only (OpenPocketCine's notes, not exercised with this app yet):

    gimbal (receiver 0x04)  04/4C [FE 08] recenter, [FE 09] turn 180°; modes: 04/4C [02 08] + 04/50 [00 04 01 00]
                            follow, + [.. 01] tilt locked, 04/4C [01 08] FPV, [00 08] direction lock;
                            speed 04/50 [00 05 01 <0 fast, 1 normal, 2 slow>];
                            stick 04/01 (flags 00, no reply) [tilt u16, 00 00, pan u16, 00 80 22 00], 1024 ± 550
    gimbal attitude push    04/05, 50 bytes: pan (yaw) i16 at 4, tilt = −i16 at 20 (0.1°), mode = byte 6 >> 6
    zoom (receiver 0x01)    02/B8 [0A 4E <lens u16>] to a factor (lens = 217 × factor); [01 <rate> <1 in, 0 out> 00]
                            while held, [FF 00 00 00] stop; read back from cam_lens_state (u16 at 14)

Replies start with a result code: 00 = OK, E0 = command not supported, E3 = invalid parameter (or not
allowed in the current state, e.g. ISO while exposure is on Auto), DF = not available right now.

What each capture mode offers (resolutions, frame rates, ISO, shutter speeds...) the camera publishes itself
as capability topics (see parse_capability). The fixed tables below are the Osmo Nano's, used until those
arrive.
"""

from __future__ import annotations

import re

from .models import POCKETS

# Capture modes: the value of 02/E1, also byte 57 of the 02/80 status push.
MODES = {0x05: "Photo", 0x01: "Video", 0x28: "Super Night", 0x00: "Slow-mo", 0x02: "Timelapse", 0x0A: "Hyperlapse",
         0x17: "Photo"}                  # 0x17: Photo on the Osmo Pocket 4 / 4P
PHOTO_MODES = {0x05, 0x17}
MODE_VIDEO, MODE_SLOWMO, MODE_TIMELAPSE, MODE_HYPERLAPSE, MODE_SUPER_NIGHT = 0x01, 0x00, 0x02, 0x0A, 0x28

RESULT_TEXT = {0xE0: "command not supported", 0xE3: "invalid parameter", 0xDF: "not available right now"}
RECORD_STATES = ("idle", "starting", "recording", "stopping")

# The camera reserves a fixed amount of storage per second of video (free MB / seconds left) and per
# photo (free MB / photos left). Unlike the raw counters these ratios don't drift as the storage fills,
# so they identify the current format. Measured at 25 fps.
VIDEO_RATES = [(11.903, "4K 16:9"), (14.288, "4K 4:3"), (7.135, "2.7K 16:9"), (8.327, "2.7K 4:3"),
               (4.750, "1080p 16:9"), (5.347, "1080p 4:3")]
SLOWMO_RATES = [(12.143, "1080p · 4×"), (14.582, "1080p · 8×"), (14.527, "2.7K · 4×"), (15.537, "4K · 4×")]
PHOTO_RATES = [(1.209, "M 16:9 · 8 MP"), (1.317, "M 4:3 · 9 MP"), (2.714, "L 16:9 · 31 MP"), (2.935, "L 4:3 · 36 MP")]
RATE_TABLES = {0x01: VIDEO_RATES, 0x28: VIDEO_RATES, 0x00: SLOWMO_RATES}

# Exposure. ISO codes 03-0C are one stop apart (01/02 behave like 03, 0D like 0C).
EXPOSURE_MODES = {"auto": 0x01, "manual": 0x04}     # 02/03 (priority modes) are refused by the Nano
ISO_VALUES = {3: 100, 4: 200, 5: 400, 6: 800, 7: 1600, 8: 3200, 9: 6400, 10: 12800, 11: 25600, 12: 51200}
# The ISO codes each capture mode offers: up to 12800 in Video, 25600 in the others, Auto only in Super Night.
MODE_ISO = {0x01: list(range(3, 11)), 0x28: []}
DEFAULT_ISO = list(range(3, 12))
# Shutter speed = u16 (bit 15 set: 1/x s, otherwise whole seconds) + a decimal digit in bits 16-23
# (0x5800C = 1/12.5 s, 0x30001 = 1.3 s). The speeds the camera accepts, fastest first.
SHUTTER_SPEEDS = [0x9F40, 0x9900, 0x9388, 0x8FA0, 0x8C80, 0x89C4, 0x87D0, 0x8640, 0x84E2, 0x83E8, 0x8320,
                  0x8280, 0x81F4, 0x8190, 0x8140, 0x80FA, 0x80C8, 0x80A0, 0x807D, 0x8064, 0x8050, 0x803C,
                  0x8032, 0x8028, 0x801E, 0x8019, 0x8014, 0x800F, 0x5800C, 0x800A, 0x8008, 0x8005, 0x8004,
                  0x8003, 0x58002, 0x8002, 0x0001, 0x30001, 0x60001, 0x0002, 0x50002, 0x0003, 0x20003, 0x0004,
                  0x0005, 0x0006, 0x0008, 0x000A, 0x000F, 0x0014, 0x001E]

# Video format: 02/18 [resolution, fps, 00, 00, slow-mo factor]. Frame-rate codes were checked by
# recording and reading the files back (code 6 gives 59.94 fps). Video mode takes codes 1-6; 120 and
# 240 fps belong to Slow-mo.
FRAME_RATES = {1: 24, 2: 25, 3: 30, 4: 48, 5: 50, 6: 60, 7: 120, 8: 240, 0x0A: 100, 0x13: 200}
# Resolution codes, identified by bitrate and live view frame shape. On the Osmo Nano the 4:3 codes are
# only accepted with the aspect set to 4:3 first; a 16:9 code switches the picture back by itself. The
# square and vertical codes are the Osmo Pocket's (OpenPocketCine's notes).
RESOLUTIONS = {0x10: "4K 16:9", 0x2D: "2.7K 16:9", 0x0A: "1080p 16:9",
               0x67: "4K 4:3", 0x5F: "2.7K 4:3", 0x0C: "1080p 4:3",
               0x7D: "4K 1:1", 0x6B: "3K 1:1", 0x6A: "2160p 1:1", 0x69: "1080p 1:1",
               0x6C: "3K 9:16", 0x43: "2.7K 9:16", 0x42: "1080p 9:16"}
RES_4_3 = {0x67, 0x5F, 0x0C}
ASPECT_TLV = 0x46                       # 02/8E parameter TLV: 1 = 4:3, 2 = 16:9
# What each capture mode accepts (everything else answers E3).
MODE_FORMATS = {
    0x01: {"res": [0x10, 0x2D, 0x0A, 0x67, 0x5F, 0x0C], "fps": [1, 2, 3, 4, 5, 6]},    # Video
    0x28: {"res": [0x10, 0x2D, 0x0A], "fps": [1, 2, 3]},                               # Super Night
    0x02: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},                                  # Timelapse
    0x0A: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},                                  # Hyperlapse
}
LIMITED_FPS = {0x67: [1, 2, 3, 4, 5]}   # 4K 4:3 tops out at 50 fps
# Slow-mo: (resolution, fps code, speed factor); the factor goes in the last byte of 02/18.
SLOWMO_FORMATS = [(0x10, 7, 4), (0x2D, 7, 4), (0x0A, 7, 4), (0x0A, 8, 8)]
# Photo format: 02/12 [size, ratio]. Size 0 is the Osmo Pocket's only one.
PHOTO_SIZE_CODES = {4: "L", 3: "M", 0: ""}
PHOTO_RATIOS = {0: "4:3", 1: "16:9", 3: "1:1"}
PHOTO_MP = {(4, 0): 36, (4, 1): 31, (3, 0): 9, (3, 1): 8}
NANO_PHOTO_FORMATS = [(4, 0), (4, 1), (3, 0), (3, 1)]
# The Osmo Pocket 3 publishes no format tables (camcap_video_format is a topic it doesn't have). Its
# choices, as its own app offers them (a survey in OpenPocketCine's notes): the aspect is part of the
# resolution code (no 4:3; 1:1 up to 3K; 9:16 only follows the camera's own portrait lock), Low-Light
# (0x28) offers 1080p / 4K at 24-30 fps, and Slow-mo carries its speed factor in byte 3 of 02/18.
POCKET3 = 0x0020
POCKET4S = {0x0021, 0x0022}               # Osmo Pocket 4 / 4P
POCKET3_FORMATS = {
    MODE_VIDEO: {"res": [0x10, 0x2D, 0x0A, 0x6B, 0x6A, 0x69], "fps": [1, 2, 3, 4, 5, 6]},
    MODE_SUPER_NIGHT: {"res": [0x10, 0x0A], "fps": [1, 2, 3]},
    MODE_TIMELAPSE: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},
    MODE_HYPERLAPSE: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},
}
POCKET3_SLOWMO = [(0x10, 0x0A, 4), (0x10, 7, 4), (0x2D, 7, 4), (0x0A, 7, 4), (0x0A, 8, 8)]
POCKET3_PHOTO_FORMATS = [(0, 1), (0, 3)]
POCKET3_COLOR_MODES = [0x00, 0x3C, 0x3D]       # Normal, HLG, D-Log M (Video and Slow-mo)
# The Osmo Action 4 publishes no format tables either. Its choices, from its manual, each one checked by a
# tester's camera reports: 4:3 at 4K and 2.7K only (the Nano's codes, no aspect write), Video up to 120 fps
# at 4K and 2.7K 16:9 and 240 fps at 1080p, Hyperlapse at 30 fps only, and one photo size (about 10 MP) in
# 4:3 or 16:9. Slow-mo is the Nano's. "fps_by_res" overrides a mode's frame
# rates for one resolution.
ACTION4 = 0x0014
_ACTION4_FAST = [1, 2, 3, 4, 5, 6, 0x0A, 7]
ACTION4_FORMATS = {
    MODE_VIDEO: {"res": [0x10, 0x2D, 0x0A, 0x67, 0x5F], "fps": [1, 2, 3, 4, 5, 6],
                 "fps_by_res": {0x10: _ACTION4_FAST, 0x2D: _ACTION4_FAST, 0x0A: [*_ACTION4_FAST, 0x13, 8],
                                0x67: [1, 2, 3, 4, 5, 6]}},
    MODE_SUPER_NIGHT: {"res": [0x10, 0x2D, 0x0A], "fps": [1, 2, 3]},
    MODE_TIMELAPSE: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},
    MODE_HYPERLAPSE: {"res": [0x10, 0x2D, 0x0A], "fps": [3]},
}
ACTION4_PHOTO_FORMATS = [(0, 0), (0, 1)]
# White balance: 02/2C [mode, kelvin / 100, 00, 00, 00]; mode 0 = auto, 6 = manual colour temperature.
WB_AUTO, WB_MANUAL = 0x00, 0x06
WB_KELVIN = range(2000, 10001, 100)
# Exposure compensation: 02/2E [EV_ZERO + thirds of a stop], -3 to +3 EV (Auto exposure only).
EV_ZERO = 0x10
EV_THIRDS = range(-9, 10)
# Colour profile: 02/42 [code]. The codes differ between models; these are the ones seen so far.
COLOR_MODES = {0x00: "Normal", 0x3F: "Normal 10-bit", 0x3D: "D-Log M", 0x3C: "HLG", 0x17: "D-Log", 0x41: "D-Log2"}
LOG_COLOR_MODES = {0x3D, 0x17, 0x41}
SHARPNESS_SET, DENOISE_SET = 0x38, 0x44
# 02/8E parameters (one byte each).
PARAM_EIS, PARAM_FOV = 0x0008, 0x0009
FOV_LABELS = {0x00: "Ultra Wide", 0x01: "Wide", 0x02: "Standard", 0x05: "Natural Wide"}
EIS_LABELS = {0x00: "Off", 0x01: "RockSteady", 0x02: "HorizonSteady", 0x03: "RockSteady+", 0x04: "HorizonBalancing"}
# Timelapse and hyperlapse settings share 02/6C; LAPSE_SAVE is what a timelapse keeps besides the video.
LAPSE_SET = 0x6C
LAPSE_SAVE = {0x00: "Video", 0x02: "JPEG + video", 0x03: "RAW + video"}

# ---- Osmo Pocket: gimbal and zoom
GIMBAL_RECENTER = bytes.fromhex("fe08")
GIMBAL_FLIP = bytes.fromhex("fe09")
# Each mode is one or two commands: (command id, payload)
GIMBAL_MODES = {
    "follow": [(0x4C, bytes.fromhex("0208")), (0x50, bytes.fromhex("00040100"))],
    "tilt_lock": [(0x4C, bytes.fromhex("0208")), (0x50, bytes.fromhex("00040101"))],
    "fpv": [(0x4C, bytes.fromhex("0108"))],
    "direction_lock": [(0x4C, bytes.fromhex("0008"))],
}
GIMBAL_MODE_NAMES = {0: "direction_lock", 1: "fpv", 2: "follow"}   # attitude byte 6 >> 6
GIMBAL_SPEEDS = {"fast": 0, "normal": 1, "slow": 2}
STICK_CENTER, STICK_RANGE = 1024, 550    # the camera ignores values beyond ± 550
FLIPPED_YAW = 165.0                      # past this pan angle the camera faces you: pan is mirrored
# Zoom: lens position = 217 × factor. Stops each model offers; on the Pocket 3 the top one depends on
# the resolution (4K 2×, 2.7K 3×, 1080p 4×); Slow-mo, Timelapse and Super Night stay at 1×.
# Osmo Pocket 3: which way the camera faces (04/27 push, about 10 a second: byte 2 bit 0x40 = towards the
# user, after "turn 180°") and its Selfie Flip setting (02/8E parameter 0x0038, set on the camera only).
PARAM_SELFIE_FLIP = 0x0038
ZOOM_UNIT = 217
ZOOM_STOPS = {0x0020: [1, 2, 4], 0x0021: [1, 2, 4], 0x0022: [1, 3, 6, 12]}
POCKET3_ZOOM_MAX = {0x0A: 4, 0x0C: 4, 0x42: 4, 0x69: 4, 0x2D: 3, 0x43: 3, 0x5F: 3, 0x6A: 3,
                    0x10: 2, 0x67: 2, 0x7D: 2, 0x6B: 2, 0x6C: 2}
ZOOM_RATE = 0x4A
ZOOM_STOP = bytes.fromhex("ff000000")

# Media mode (gallery): the live view stops and files can be listed and downloaded.
ENTER_MEDIA = bytes.fromhex("01010001")
LEAVE_MEDIA = bytes.fromhex("01010000")

_TOPIC_NAME = re.compile(rb"[a-z][a-z0-9_]{2,63}")


def parameter_tlv_set(tlv_type: int, value: int) -> bytes:
    """02/8E payload setting one 1-byte parameter: [01 01][type u16 LE][length 1][value]."""
    return bytes((1, 1)) + tlv_type.to_bytes(2, "little") + bytes((1, value))


def parameter_tlv_get(tlv_type: int) -> bytes:
    """02/8E payload reading one parameter: [00 01][type u16 LE]."""
    return bytes((0, 1)) + tlv_type.to_bytes(2, "little")


def parameter_value(reply: bytes, tlv_type: int) -> int | None:
    """Value from a 02/8E read reply: [00][00 01][type u16 LE][length 1][value]."""
    if len(reply) >= 7 and reply[:3] == b"\x00\x00\x01" and int.from_bytes(reply[3:5], "little") == tlv_type:
        return reply[6]
    return None


def signed_byte(value: int) -> int:
    return value - 256 if value > 127 else value


def timelapse_payload(save: int, interval_tenths: int, duration_s: int) -> bytes:
    return bytes((0x04, 0, save)) + interval_tenths.to_bytes(2, "little") + duration_s.to_bytes(4, "little") + bytes(7)


def hyperlapse_payload(ratio: int) -> bytes:
    return bytes((0x0B, 0, 0)) + ratio.to_bytes(2, "little") + bytes(11)


def ev_label(thirds: int) -> str:
    """-4 -> '−1.3', 3 -> '+1.0', 0 -> '0'."""
    if not thirds:
        return "0"
    whole, rest = divmod(abs(thirds), 3)
    return ("+" if thirds > 0 else "−") + f"{whole}.{(0, 3, 7)[rest]}"


def color_label(code: int) -> str:
    return COLOR_MODES.get(code, f"Profile 0x{code:02x}")


def fov_label(code: int) -> str:
    return FOV_LABELS.get(code, f"FOV {code}")


def eis_label(code: int) -> str:
    return EIS_LABELS.get(code, f"Mode {code}")


def gimbal_stick(tilt: float, pan: float) -> bytes:
    """04/01 payload for stick deflections in -1..1 (tilt up and pan right positive)."""
    axis = lambda v: STICK_CENTER + round(max(-1.0, min(1.0, v)) * STICK_RANGE)  # noqa: E731
    return axis(tilt).to_bytes(2, "little") + bytes(2) + axis(pan).to_bytes(2, "little") + bytes.fromhex("00802200")


def parse_attitude(p: bytes) -> dict | None:
    """04/05 gimbal attitude push (50 bytes): pan and tilt in degrees, and the gimbal mode."""
    if len(p) != 50:
        return None
    i16 = lambda at: int.from_bytes(p[at:at + 2], "little", signed=True)  # noqa: E731
    return {"pan": i16(4) / 10, "tilt": -i16(20) / 10, "mode": GIMBAL_MODE_NAMES.get(p[6] >> 6)}


def zoom_payload(factor: float) -> bytes:
    return bytes.fromhex("0a4e") + round(ZOOM_UNIT * factor).to_bytes(2, "little")


def zoom_move_payload(zoom_in: bool) -> bytes:
    return bytes((0x01, ZOOM_RATE, 1 if zoom_in else 0, 0))


def zoom_from_lens_state(data: bytes) -> float | None:
    """Zoom factor from the cam_lens_state topic (lens position, u16 at 14)."""
    if len(data) < 16:
        return None
    lens = int.from_bytes(data[14:16], "little")
    return round(lens / ZOOM_UNIT, 1) if 100 <= lens <= 3000 else None


def zoom_stops(model_id: int | None, resolution: int | None, capture_mode: int | None) -> list[int]:
    stops = ZOOM_STOPS.get(model_id or 0, [])
    if not stops:
        return []
    if capture_mode in (MODE_SLOWMO, MODE_TIMELAPSE, MODE_SUPER_NIGHT):
        return [1, 3] if model_id == 0x0022 else [1]
    if model_id == 0x0020 and resolution in POCKET3_ZOOM_MAX:
        top = POCKET3_ZOOM_MAX[resolution]
        return [s for s in stops if s < top] + [top]
    return stops


def shutter_payload(raw: int) -> bytes:
    return b"\x01" + (raw & 0xFFFF).to_bytes(2, "little") + bytes(((raw >> 16) & 0xFF, 0, 0, 0))


def shutter_text(raw: int) -> str | None:
    whole, decimal = raw & 0x7FFF, (raw >> 16) & 0xFF
    number = f"{whole}.{decimal}" if decimal else str(whole)
    if raw & 0x8000 and whole:
        return f"1/{number}"
    return f'{number}"' if whole else None


def photo_label(size: int, ratio: int) -> str:
    label = f"{PHOTO_SIZE_CODES.get(size, f'size {size}')} {PHOTO_RATIOS.get(ratio, f'ratio {ratio}')}".strip()
    return label + (f" · {PHOTO_MP[size, ratio]} MP" if (size, ratio) in PHOTO_MP else "")


def slowmo_label(res: int, fps: int) -> str:
    """As the cameras name them: 100 and 120 fps are 4× slower, 200 and 240 fps 8×."""
    rate = FRAME_RATES.get(fps, 0)
    return f"{RESOLUTIONS.get(res, hex(res)).split()[0]} · {rate} fps · {8 if rate >= 200 else 4}×"


def builtin_tables(model_id: int | None) -> dict:
    """What to offer when the camera publishes no table of its own: the Osmo Pocket 3's or the Osmo
    Action 4's, else the Osmo Nano's."""
    if model_id == POCKET3:
        return {"formats": POCKET3_FORMATS, "slowmo": POCKET3_SLOWMO, "photo": POCKET3_PHOTO_FORMATS,
                "colors": POCKET3_COLOR_MODES}
    if model_id == ACTION4:
        return {"formats": ACTION4_FORMATS, "slowmo": SLOWMO_FORMATS, "photo": ACTION4_PHOTO_FORMATS,
                "colors": []}
    return {"formats": MODE_FORMATS, "slowmo": SLOWMO_FORMATS, "photo": NANO_PHOTO_FORMATS, "colors": []}


def slowmo_factor(model_id: int | None, res: int, fps: int) -> int:
    """The factor byte 02/18 carries in Slow-mo: 8 at 240 fps, else 4 (the Osmo Pocket 4P's 200 fps takes 4
    too, although the camera calls it 8×)."""
    return next((x for r, f, x in builtin_tables(model_id)["slowmo"] if (r, f) == (res, fps)),
                8 if FRAME_RATES.get(fps, 120) >= 240 else 4)


def video_format_payload(model_id: int | None, mode: int | None, res: int, fps: int) -> bytes:
    """02/18 payload. Slow-mo adds its speed factor: in the last byte on the Osmo Nano, in byte 3 on the
    Osmo Pockets (seen on the Pocket 3 and the Pocket 4P)."""
    if mode != MODE_SLOWMO:
        return bytes((res, fps, 0, 0, 0))
    factor = slowmo_factor(model_id, res, fps)
    return bytes((res, fps, 0, factor, 0)) if model_id in POCKETS else bytes((res, fps, 0, 0, factor))


def record_command(model_id: int | None, mode: int | None) -> int:
    """Record start / stop: 02/02, except in Timelapse, which rides take-photo (02/01) on the Osmo Nano and
    the Osmo Pocket 3; the Osmo Pocket 4 / 4P record a timelapse like a video."""
    return 0x01 if mode == MODE_TIMELAPSE and model_id not in POCKET4S else 0x02


def mode_name(mode: int, model_id: int | None = None) -> str:
    if mode == MODE_SUPER_NIGHT and model_id == POCKET3:
        return "Low-Light"
    return MODES.get(mode, f"0x{mode:02x}")


def multiview_modes(model_id: int | None) -> list[dict]:
    """The capture modes Multiview offers for a camera (its own Photo code)."""
    photo = 0x17 if model_id in POCKET4S else 0x05
    return [{"code": m, "name": mode_name(m, model_id)}
            for m in (MODE_VIDEO, MODE_SLOWMO, MODE_TIMELAPSE, MODE_HYPERLAPSE, MODE_SUPER_NIGHT, photo)]


def match_rate(value: float, table: list[tuple[float, str]]) -> str | None:
    """Closest label within 2 %; both labels when the table can't tell two apart."""
    hits = sorted((abs(value - ref) / ref, label) for ref, label in table)
    hits = [h for h in hits if h[0] < 0.02]
    if not hits:
        return None
    if len(hits) > 1 and hits[1][0] - hits[0][0] < 0.006:
        return f"{hits[0][1]} / {hits[1][1]}"
    return hits[0][1]


def parse_status(p: bytes) -> dict | None:
    """02/80 status push (about 10 per second), little-endian fields."""
    if len(p) < 58:
        return None
    u32 = lambda offset: int.from_bytes(p[offset:offset + 4], "little")  # noqa: E731
    return {
        "record_state": (p[0] >> 6) & 3,  # index into RECORD_STATES
        "photo_busy": bool(p[0] & 0x38),  # set while a photo is being taken
        "storing": bool(p[2] & 0x08),     # writing a file
        # Byte 4: 0 photo/timelapse, 1 video, 2 playback on the camera. The app's media mode (02/0C)
        # sets bit 0x40 of byte 3 instead.
        "playback": p[4] == 2 or bool(p[3] & 0x40),
        "media_mode": bool(p[3] & 0x40),
        "total_mb": u32(5),
        "free_mb": u32(9),
        "shots_left": u32(13),
        "time_left": u32(17),             # seconds of video that still fit
        # seconds recorded in the current clip (u16: the Osmo Pocket 3 keeps 02 in byte 32)
        "elapsed": int.from_bytes(p[29:31], "little"),
        "capture_mode": p[57],            # MODES
    }


def parse_battery(p: bytes) -> dict | None:
    """0D/02 battery push (about once a second)."""
    if len(p) < 21:
        return None
    return {
        "percent": p[20],
        "voltage": int.from_bytes(p[1:5], "little"),                # mV
        "current": int.from_bytes(p[5:9], "little", signed=True),   # mA, negative = discharging
        "temp": int.from_bytes(p[17:19], "little") / 10,            # °C
    }


def parse_topic(p: bytes) -> tuple[str, bytes] | None:
    """Named status topic push (00/99): a header, u16 name length, the name, 6 bytes, u16 data length,
    the data. The name length is searched for rather than assumed at a fixed offset."""
    for i in range(11, min(len(p) - 2, 24)):
        n = int.from_bytes(p[i:i + 2], "little")
        if 3 <= n <= 64 and _TOPIC_NAME.fullmatch(p[i + 2:i + 2 + n]):
            j = i + 2 + n
            if j + 8 > len(p):
                return None
            size = int.from_bytes(p[j + 6:j + 8], "little")
            return p[i + 2:j].decode(), p[j + 8:j + 8 + size]
    return None


def exposure_from_topic(data: bytes) -> dict | None:
    """From the 'cam_expo_param' topic: ISO and shutter in use (real values, also in Auto), the exposure
    compensation set (thirds of a stop, [6]) and the camera's own meter reading ([15], thirds of a stop
    from what it considers correct; useful in Manual)."""
    if len(data) < 22:
        return None
    thirds = lambda value: value - EV_ZERO if 0 < value < 2 * EV_ZERO else None  # noqa: E731
    return {"iso": int.from_bytes(data[16:20], "little") or None,
            "shutter": shutter_text(int.from_bytes(data[20:22], "little")),
            "ev": thirds(data[6]),
            "meter": thirds(data[15])}


def image_from_topic(data: bytes) -> dict | None:
    """From the 'cam_image_effect' topic: colour profile [2], noise reduction [14], sharpness [15]."""
    if len(data) < 3:
        return None
    image = {"color": data[2], "log": data[2] in LOG_COLOR_MODES}
    if len(data) > 15:
        image.update(denoise=signed_byte(data[14]), sharpness=signed_byte(data[15]))
    return image


def lapse_from_topic(data: bytes) -> dict | None:
    """From the 'cam_lapse_param' topic: what a timelapse keeps besides the video [0], interval [1-2] and
    shortest interval [3-4] in tenths of a second, duration in seconds [5-8] (0 = until stopped),
    hyperlapse speed [9-10] (0 = auto)."""
    if len(data) < 11:
        return None
    u16 = lambda at: int.from_bytes(data[at:at + 2], "little")  # noqa: E731
    return {"save": data[0], "interval": u16(1), "min_interval": u16(3),
            "duration": int.from_bytes(data[5:9], "little"), "ratio": u16(9)}


def parse_storage(p: bytes) -> dict | None:
    """02/DC storage push: microSD total / free (MB) at [6] / [10], internal memory at [24] / [28]."""
    if len(p) < 14:
        return None
    u32 = lambda offset: int.from_bytes(p[offset:offset + 4], "little")  # noqa: E731
    stores = {"sd": {"total_mb": u32(6), "free_mb": u32(10)}}
    if len(p) >= 32:
        stores["internal"] = {"total_mb": u32(24), "free_mb": u32(28)}
    return stores


# Capability topics: what the current capture mode offers. The camera sends each one once when subscribed
# and again when it changes (after a capture mode switch, for example). Layout: [0] supported (0 = no),
# [1-2] u16 LE length, then the table, mostly a count followed by that many entries.
CAPABILITY_TOPICS = ("camcap_base", "camcap_video_format", "camcap_iso", "camcap_shutter",
                     "camcap_photo_size", "camcap_wb", "camcap_color_mode", "camcap_fov", "camcap_eis",
                     "camcap_sharpness", "camcap_denoise", "camcap_photo_timer_interval",
                     "camcap_timelapse_duration", "camcap_hyperlapse_ratio")
SUBSCRIBED_TOPICS = ("cam_expo_param", "cam_image_effect", "cam_lapse_param", "cam_lens_state") + CAPABILITY_TOPICS


def _entries(table: bytes, width: int, at: int = 0) -> list[bytes] | None:
    """The `count` entries of `width` bytes that follow the count byte at `at`."""
    if at >= len(table):
        return None
    end = at + 1 + table[at] * width
    if end > len(table):
        return None
    return [table[i:i + width] for i in range(at + 1, end, width)]


def _shutter_table(table: bytes) -> list[int] | None:
    """[x][n][n bytes][group count], then per group: [key][slowest: u16 + decimal][count][count speeds of
    u16 + decimal] (the raw speed format of 02/28). Speeds of every group, in order."""
    if len(table) < 2 or 2 + table[1] >= len(table):
        return None
    at = 2 + table[1]
    speeds: list[int] = []
    group = at + 1
    for _ in range(table[at]):
        entries = _entries(table, 3, group + 4)
        if entries is None:
            return None
        speeds += [raw for raw in (int.from_bytes(e[:2], "little") | e[2] << 16 for e in entries) if raw not in speeds]
        group += 5 + 3 * len(entries)
    return speeds


def parse_capability(name: str, data: bytes):
    """Decode a capability topic; None when it's not one we use, the camera marks it unsupported, or it
    doesn't parse.
        camcap_base          capture modes (02/E1 codes), then other fields
        camcap_video_format  (resolution, fps code) pairs for 02/18
        camcap_iso           ISO codes for 02/2A (Auto left out)
        camcap_shutter       raw shutter speeds for 02/28
        camcap_photo_size    (size, ratio) pairs for 02/12
        camcap_wb            colour temperature range (K) for 02/2C
        camcap_color_mode    colour profile codes for 02/42
        camcap_fov / _eis    field of view / stabilisation values for 02/8E
        camcap_sharpness     sharpness levels for 02/38 (signed)
        camcap_denoise       noise reduction levels for 02/44 (signed)
        camcap_photo_timer_interval  timelapse intervals, in tenths of a second
        camcap_timelapse_duration    timelapse durations in seconds (0 = until stopped)
        camcap_hyperlapse_ratio      hyperlapse speeds (0 = auto)"""
    if len(data) < 4 or not data[0]:
        return None
    size = int.from_bytes(data[1:3], "little")
    table = data[3:3 + size]
    if len(table) < size:
        return None
    if name in ("camcap_base", "camcap_color_mode", "camcap_fov", "camcap_eis", "camcap_hyperlapse_ratio"):
        entries = _entries(table, 1)
        return [e[0] for e in entries] if entries is not None else None
    if name in ("camcap_sharpness", "camcap_denoise"):
        entries = _entries(table, 1)
        return [signed_byte(e[0]) for e in entries] if entries is not None else None
    if name == "camcap_timelapse_duration":
        entries = _entries(table, 4)
        return [int.from_bytes(e, "little") for e in entries] if entries is not None else None
    if name == "camcap_photo_timer_interval":     # 4 header bytes, count, then [kind][seconds u16][ms u16]
        entries = _entries(table, 5, 4)
        if entries is None:
            return None
        return [int.from_bytes(e[1:3], "little") * 10 + int.from_bytes(e[3:5], "little") // 100 for e in entries]
    if name == "camcap_video_format":
        entries = _entries(table, 3)
        return [(e[0], e[1]) for e in entries] if entries is not None else None
    if name == "camcap_iso":                     # a flag byte, then the codes (0 = Auto)
        entries = _entries(table, 1, 1)
        return [e[0] for e in entries if e[0]] if entries is not None else None
    if name == "camcap_shutter":
        return _shutter_table(table)
    if name == "camcap_photo_size":
        entries = _entries(table, 4)
        return [(e[0], e[1]) for e in entries] if entries is not None else None
    if name == "camcap_wb":                      # [min / 100] 00 [max / 100] 00 ...
        if len(table) < 4 or not 10 <= table[0] < table[2]:
            return None
        return (table[0] * 100, table[2] * 100)
    return None


def parse_settings(mode: bytes, iso: bytes, shutter: bytes, video: bytes, photo: bytes = b"",
                   wb: bytes = b"") -> dict | None:
    """Replies to 02/1F, 02/2B, 02/29, 02/19, 02/13 and 02/2D (each one starts with its result code).
    In Auto the camera reports ISO 0 and refuses the shutter read."""
    if len(mode) < 2 or mode[0] or len(iso) < 2 or iso[0]:
        return None
    raw = int.from_bytes(shutter[2:4], "little") | shutter[4] << 16 if len(shutter) >= 5 and not shutter[0] else None
    ok_video = len(video) >= 3 and not video[0]
    ok_photo = len(photo) >= 3 and not photo[0]
    ok_wb = len(wb) >= 3 and not wb[0]
    wb_mode = None
    if ok_wb:
        wb_mode = "auto" if wb[1] == WB_AUTO else "manual" if wb[1] == WB_MANUAL else f"0x{wb[1]:02x}"
    return {
        "mode": next((k for k, v in EXPOSURE_MODES.items() if v == mode[1]), f"0x{mode[1]:02x}"),
        "iso_code": iso[1] or None,
        "iso": ISO_VALUES.get(iso[1]),
        "shutter_raw": raw,
        "shutter": shutter_text(raw) if raw else None,
        "resolution_code": video[1] if ok_video else None,
        "resolution": RESOLUTIONS.get(video[1]) if ok_video else None,
        "fps_code": video[2] if ok_video else None,
        "fps": FRAME_RATES.get(video[2]) if ok_video else None,
        "speed": video[5] if ok_video and len(video) > 5 else None,
        "photo_size_code": photo[1] if ok_photo else None,
        "photo_ratio_code": photo[2] if ok_photo else None,
        "photo": photo_label(photo[1], photo[2]) if ok_photo else None,
        "wb": wb_mode,
        "wb_kelvin": wb[2] * 100 if ok_wb else None,
    }
