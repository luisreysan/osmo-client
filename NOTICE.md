# Notices

## Trademarks

DJI, Osmo, Osmo Nano and DJI Mimo are trademarks of SZ DJI Technology Co., Ltd. They are used here only
to say which cameras this software works with. This project is independent and is not affiliated with,
endorsed by or sponsored by DJI.

## Prior work

- **osmosis** by Konrad Iturbe — <https://github.com/KonradIT/osmosis> — MIT License. The camera model
  ids, the app identity used when pairing, and the description of the media listing protocol follow
  that project's documentation. Its license:

  > MIT License
  >
  > Copyright (c) 2026 Konrad Iturbe
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
  > associated documentation files (the "Software"), to deal in the Software without restriction,
  > including without limitation the rights to use, copy, modify, merge, publish, distribute,
  > sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
  > furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
  > NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
  > NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
  > OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
  > CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

- **dji-vcam** by razboy — <https://github.com/RB14/dji-vcam> — MIT License. How the Action series'
  live view is started and kept alive (registration with the video encoder, the trigger and the 200 ms
  heartbeat) and the video cursor of the status packets follow that project's protocol notes. No code
  from it is included here. Its license: MIT License, Copyright (c) 2026 razboy, with the same
  permission notice and disclaimer as quoted above for osmosis.

- **osmo_nano_webcam** by Matis-ard — <https://github.com/Matis-ard/osmo_nano_webcam> — showed how
  the Osmo Nano's live view can be received over Wi-Fi. No code from it is included here.

- **OpenPocketCine** and **OpenCine** by Erik Sutton and contributors —
  <https://github.com/erik-sutton95/OpenPocketCine> — Apache License 2.0 (Copyright 2026 Erik Sutton and
  OpenPocketCine contributors). The live view's picture header and private SEI, the exposure
  compensation, image, timelapse and storage commands, and the monitoring tools' algorithms and constants
  (false color bands, zebra and focus peaking thresholds, scope layouts, the .cube LUT layout), the Osmo
  Pocket support (session, H.265 live view, gimbal and zoom commands, the Osmo Pocket 3's formats and
  gallery) and Multiview's approach (each extra camera on a shared network, a shared record command
  confirmed per camera) follow that project's code and documentation. No code from it is included here.

## Dependencies

Installed with pip from `requirements.txt` the first time the app runs; none of them are included in
this repository.

| package | license |
|---|---|
| [bleak](https://github.com/hbldh/bleak) | MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause; its wheels include FFmpeg libraries under their own (L)GPL licenses |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU |
| [pyvirtualcam](https://github.com/letmaik/pyvirtualcam) | GPL-2.0; optional, not in `requirements.txt`: only for `--vcam` on Windows and macOS, installed separately by the user |

Osmo Client's own code is MIT. With `--vcam` on Windows and macOS it uses pyvirtualcam (GPL-2.0) as a
separately installed library to reach the OBS Virtual Camera; if you build a single bundled executable that
includes pyvirtualcam, that bundle as a whole must be distributed under the terms of the GPL-2.0. On Linux
`--vcam` writes to a v4l2loopback device (GPL-2.0, installed separately by the user) without any library.
