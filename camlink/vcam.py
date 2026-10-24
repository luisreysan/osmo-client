"""Virtual camera output: the live view as a webcam that other programs can open.

On Linux the frames go straight to a v4l2loopback device: its format is set once (VIDIOC_S_FMT, I420),
then each frame is one write(), as ffmpeg's v4l2 output does. No library is needed, so it also runs where
pyvirtualcam has no build (Linux on ARM, a Raspberry Pi). On Windows and macOS it is the OBS Virtual
Camera, through pyvirtualcam (GPL-2.0, installed separately: see the README).

Frames are sent at 1280×720, the size of the live view; others (a 4:3 or square live view) are scaled to
fit and centred between black bars.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
from pathlib import Path

from .util import log

SIZE = (1280, 720)
_PIP = f"\"{sys.executable}\" -m pip install pyvirtualcam"
HINTS = {
    "linux": "load v4l2loopback: sudo modprobe v4l2loopback devices=1 exclusive_caps=1 "
             "card_label=\"Osmo Client\"",
    "win32": f"install OBS Studio (it does not need to be open) and pyvirtualcam: {_PIP}",
    "darwin": "install OBS Studio 30 or newer, click Start Virtual Camera in it once and allow its system "
              f"extension, and install pyvirtualcam: {_PIP}",
}

# V4L2 (linux/videodev2.h). struct v4l2_format is 208 bytes on 64-bit systems and 204 on 32-bit ones (its
# union is aligned to a pointer), and the ioctl number carries that size.
_FORMAT_SIZE = 208 if struct.calcsize("P") == 8 else 204
VIDIOC_QUERYCAP = (2 << 30) | (104 << 16) | (ord("V") << 8) | 0
VIDIOC_S_FMT = (3 << 30) | (_FORMAT_SIZE << 16) | (ord("V") << 8) | 5
_BUF_TYPE_VIDEO_OUTPUT = 2
_PIX_FMT_YU12 = int.from_bytes(b"YU12", "little")     # I420: Y, then U, then V
_FIELD_NONE = 1
_COLORSPACE_SRGB = 8
_LOOPBACK_DRIVER = b"v4l2 loopback"


def format_request(width: int, height: int) -> bytearray:
    """struct v4l2_format for VIDIOC_S_FMT: an output (written) I420 picture of width×height."""
    head = struct.pack("=I", _BUF_TYPE_VIDEO_OUTPUT) + bytes(_FORMAT_SIZE - 204)   # the union's alignment
    pix = struct.pack("=12I", width, height, _PIX_FMT_YU12, _FIELD_NONE, width, width * height * 3 // 2,
                      _COLORSPACE_SRGB, 0, 0, 0, 0, 0)
    return bytearray(head + pix + bytes(_FORMAT_SIZE - len(head) - len(pix)))


def find_loopback() -> str | None:
    """The first v4l2loopback device (/dev/videoN), or None."""
    import fcntl

    def number(path: Path) -> int:
        return int(path.name[5:]) if path.name[5:].isdigit() else 1 << 30

    for path in sorted(Path("/dev").glob("video*"), key=number):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            caps = bytearray(104)
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, caps)
            if bytes(caps[:16]).rstrip(b"\0") == _LOOPBACK_DRIVER:
                return str(path)
        except OSError:
            pass
        finally:
            os.close(fd)
    return None


class _Loopback:
    """Linux: a v4l2loopback device, written to directly."""

    def __init__(self, width: int, height: int, device: str | None) -> None:
        import fcntl

        self.device = device or find_loopback()
        if not self.device:
            raise RuntimeError("no v4l2loopback device found")
        self._fd = os.open(self.device, os.O_WRONLY)
        try:
            fcntl.ioctl(self._fd, VIDIOC_S_FMT, format_request(width, height))
        except OSError:
            os.close(self._fd)
            raise

    def send(self, data: bytes) -> None:
        os.write(self._fd, data)

    def close(self) -> None:
        os.close(self._fd)


class _Obs:
    """Windows and macOS: the OBS Virtual Camera, through pyvirtualcam."""

    def __init__(self, width: int, height: int, fps: int) -> None:
        try:
            import numpy
            import pyvirtualcam
        except ImportError as error:
            raise RuntimeError(f"pyvirtualcam is not installed ({error})") from error
        self._numpy = numpy
        self._cam = pyvirtualcam.Camera(width, height, fps, fmt=pyvirtualcam.PixelFormat.I420, backend="obs")
        self.device = self._cam.device
        self._shape = (height * 3 // 2, width)

    def send(self, data: bytes) -> None:
        self._cam.send(self._numpy.frombuffer(data, self._numpy.uint8).reshape(self._shape))

    def close(self) -> None:
        self._cam.close()


def _plane(plane, width: int, height: int) -> bytes:
    """A picture plane's bytes without the decoder's line padding."""
    data = memoryview(plane)
    if plane.line_size == width:
        return bytes(data[:width * height])
    step = plane.line_size
    return b"".join(data[row * step:row * step + width] for row in range(height))


def i420(frame, width: int, height: int) -> bytes:
    """`frame` (an av.VideoFrame) as I420 bytes at width×height: scaled to fit and, when its shape differs,
    centred between black bars instead of stretched."""
    scale = min(width / frame.width, height / frame.height)
    w, h = max(2, int(frame.width * scale) & ~1), max(2, int(frame.height * scale) & ~1)
    if frame.format.name != "yuv420p" or (frame.width, frame.height) != (w, h):
        frame = frame.reformat(width=w, height=h, format="yuv420p")
    planes = [_plane(frame.planes[0], w, h), _plane(frame.planes[1], w // 2, h // 2),
              _plane(frame.planes[2], w // 2, h // 2)]
    if (w, h) == (width, height):
        return b"".join(planes)
    out = bytearray(width * height * 3 // 2)
    out[:width * height] = b"\x10" * (width * height)                 # black: Y 16, U and V 128
    out[width * height:] = b"\x80" * (width * height // 2)
    x0, y0 = (width - w) // 4 * 2, (height - h) // 4 * 2              # even offsets, centred
    starts = (0, width * height, width * height * 5 // 4)
    for start, src, sub in zip(starts, planes, (1, 2, 2)):            # U and V: half the size each way
        stride, pw, ph, x, y = width // sub, w // sub, h // sub, x0 // sub, y0 // sub
        for row in range(ph):
            at = start + (y + row) * stride + x
            out[at:at + pw] = src[row * pw:(row + 1) * pw]
    return bytes(out)


class VirtualCamera:
    """The live view as a webcam. open() before the camera connects (opening can take seconds); send() takes
    each decoded frame, from the decoder's thread. A failure stops the output and is logged, never more."""

    def __init__(self, device: str | None = None, fps: int = 30, size: tuple[int, int] = SIZE) -> None:
        self.requested = device
        self.fps = fps
        self.width, self.height = size
        self.device: str | None = None        # once open
        self.frames = 0
        self._out = None
        self._lock = threading.Lock()         # the decoder thread sends while the main thread may close

    @property
    def active(self) -> bool:
        return self._out is not None

    def open(self) -> bool:
        system = "linux" if sys.platform.startswith("linux") else sys.platform
        log("Opening the virtual camera...")
        try:
            out = (_Loopback(self.width, self.height, self.requested) if system == "linux"
                   else _Obs(self.width, self.height, self.fps))
        except Exception as error:  # noqa: BLE001 - missing driver, device or library: the app carries on
            log(f"Virtual camera unavailable: {error}. To use it, {HINTS.get(system, HINTS['linux'])}")
            return False
        with self._lock:
            self._out, self.device = out, out.device
        log(f"Virtual camera ready: {self.device} {self.width}x{self.height}")
        return True

    def send(self, frame) -> None:
        if self._out is None:
            return
        data = i420(frame, self.width, self.height)
        with self._lock:
            if self._out is None:
                return
            try:
                self._out.send(data)
                self.frames += 1
            except Exception as error:  # noqa: BLE001 - the device went away: the page keeps working
                log(f"Virtual camera stopped: {error}")
                self._close()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        out, self._out = self._out, None
        if out is not None:
            try:
                out.close()
            except Exception:  # noqa: BLE001 - closing while shutting down
                pass
