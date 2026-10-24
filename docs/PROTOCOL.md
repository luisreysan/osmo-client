# DJI Osmo over Wi-Fi: protocol notes

How OsmoCam and Osmo Client talk to a DJI Osmo camera: Bluetooth LE to find, pair and wake
it, then the camera's own Wi-Fi access point for commands, status, live video and file downloads.

This is an unofficial, independent description written for interoperability. Everything here was
verified on an **Osmo Nano**. Other Osmo models share the transport (see [Models](#12-models)) but
their commands and values may differ. Code references point to the `camlink` package in this repository.

Notation: `02/E1` means command set `0x02`, command id `0xE1`. Payloads are hex bytes; multi-byte
integers are little-endian unless noted.

## Contents

1. [Overview](#1-overview)
2. [DUML frames](#2-duml-frames)
3. [Bluetooth LE](#3-bluetooth-le)
4. [The camera's Wi-Fi access point](#4-the-cameras-wi-fi-access-point)
5. [The UDP datalink](#5-the-udp-datalink)
6. [Camera control](#6-camera-control)
7. [Settings](#7-settings)
8. [Status pushes](#8-status-pushes)
9. [Media library and downloads](#9-media-library-and-downloads)
10. [Power and sleep](#10-power-and-sleep)
11. [Limits and warnings](#11-limits-and-warnings)
12. [Models](#12-models)

---

## 1. Overview

```
computer                                   camera
 │  BLE: connect, pair ("OSMO" approve)  ──▶  │
 │  BLE: wake, ask Wi-Fi SSID + password ──▶  │   (BLE stays up: keepalive every 1 s)
 │  Wi-Fi: join the camera's access point     │   192.168.2.1, WPA2-PSK
 │  TCP 7001: one pairing frame ("poke")  ──▶ │
 │  UDP 9004: handshake, then                 │
 │     ◀── video (H.264 / H.265), status,     │
 │         replies, camera requests           │
 │     ──▶ acks every 25 ms, commands,        │
 │         answers to camera requests         │
 │  HTTP 80: file downloads (/v2)             │
```

Only one client at a time: close the DJI Mimo app on the phone (or let it disconnect) first.

## 2. DUML frames

Every message, over BLE and inside the UDP datalink, is a DUML frame (`camlink/duml.py`):

| offset | size | field |
|---|---|---|
| 0 | 1 | `0x55` start of frame |
| 1 | 2 | u16: bits 0-9 total frame length, bits 10-15 version (`1`) |
| 3 | 1 | CRC-8 of bytes 0-2 |
| 4 | 1 | sender address |
| 5 | 1 | receiver address |
| 6 | 2 | u16 sequence; a reply carries the sequence of its request |
| 8 | 1 | flags: `0x40` request expecting a reply, `0x80` reply |
| 9 | 1 | command set |
| 10 | 1 | command id |
| 11 | n | payload |
| 11+n | 2 | u16 CRC-16 of everything before it |

- CRC-8: reflected, polynomial `0x8C`, initial value `0x77`.
- CRC-16: reflected, polynomial `0x8408`, initial value `0x3692`.
- Total length = 13 + payload length.

Addresses used here (`camlink/protocol.py`):

| address | role |
|---|---|
| `0x02` | the client app (us) |
| `0x01` | camera control and status |
| `0x07` | Wi-Fi module: pairing and access point credentials (BLE) |
| `0x41` | live view transmitter |
| `0xF0` | BLE session wake / keepalive |
| `0x1C` | wakes the camera after pairing (BLE) |

## 3. Bluetooth LE

### 3.1 Advertisement

Manufacturer data, company id `0x08AA`:

```
<model id u16> 40 <6-byte device address> <state>
```

`state` is `02` while the camera is awake and `03` while it sleeps, so the sleep state can be read
passively without connecting. Model ids are listed in [section 12](#12-models). Newer adverts may
carry a product type at bytes 10-11 when bit `0x04` of byte 5 is set. Scan responses without
manufacturer data still carry the name (`OsmoNano-XXXX`, ...).

### 3.2 GATT

| characteristic | use |
|---|---|
| `0000fff4-0000-1000-8000-00805f9b34fb` | notifications; write `01 00` after subscribing |
| `0000fff5-0000-1000-8000-00805f9b34fb` | DUML frames in both directions (write without response) |

Frames can arrive split over several notifications; reassemble per characteristic.

### 3.3 Connection sequence

| step | frame (sender → receiver, seq) | payload |
|---|---|---|
| subscribe to notifications on FFF4 and FFF5, write `01 00` to FFF4 | | |
| session wake | `00/2B` 0x02 → 0xF0, seq `0x802B` | `04 00` |
| pair | `07/45` 0x02 → 0x07, seq `0x8092` | `<len><app identity><len>"osmo"` |
| ← pairing status | `07/45` reply | byte 1: `01` already paired, `02` approval needed |
| ← approval result | `07/46` (may come as a request) | `01` approved; anything else = denied or the ~20 s prompt timed out |
| wake the camera | `53/10` 0x02 → 0x1C, seq `0x8053` | `00 00 00 00` |
| keepalive, every second | `00/2B` 0x02 → 0xF0, seq `0x802B` | `01 01` |
| ask SSID | `07/07` 0x02 → 0x07, seq `0x8007` | (empty) |
| ask password | `07/0E` 0x02 → 0x07, seq `0x800E` | (empty) |
| ← SSID / password | `07/07`, `07/0E` replies | `<result> <len> <ASCII>` |

The app identity is the 32-character string the MIT-licensed
[osmosis](https://github.com/KonradIT/osmosis) project uses (`camlink/protocol.py`). The first time, the
camera shows "OSMO" and the user must approve it on the camera screen; after that, pairing answers
"already paired". If no pairing status arrives, send the pairing frame again (the apps retry at 2.5 s
and 5 s).

### 3.4 The camera's own requests

The camera also sends requests (flags `0x40`) and treats the link as lost when they go unanswered.
Over BLE, answer each one with flags `0xC0`, the same command and sequence, addresses swapped, and the
request's payload echoed back, except `00/81` ("who are you"), which gets the app record:

```
00 'A' 'P' 'P' 00×37 02 00×8 02 08 00×10        (62 bytes)
```

### 3.5 Status topics over BLE

While connected over BLE the camera may push named status topics as `00/99` frames; see
[section 8.3](#83-named-status-topics-0099).

## 4. The camera's Wi-Fi access point

- SSID and password come from `07/07` / `07/0E` over BLE. The network is WPA2-PSK (AES).
- The camera is `192.168.2.1`; the computer gets an address in `192.168.2.0/24`.
- While the computer's Wi-Fi adapter is on the camera's network it has no internet through that adapter.
  Keeping Ethernet (or a second adapter) connected keeps internet working.
- Every Osmo camera uses the same address, so files listed on one camera must not be requested from
  another. An address in `192.168.2.0/24` doesn't tell which camera's network the computer is on: switching
  cameras, compare the network's name. (Windows 11 only gives the Wi-Fi network's name to programs with
  location access; its network list, `Get-NetConnectionProfile`, names it without.)

### 4.1 Joining another network (station mode)

Instead of running its access point, the camera can join a Wi-Fi network as a station (a router, or a
computer's hotspot). The computer then keeps its own connection, and several cameras can share one
network. Over BLE, to receiver `07`:

| step | command | payload | reply |
|---|---|---|---|
| read the role | `07/39` | `00` | `00 00` access point, `00 01` station; the Osmo Nano answers `E0` (no read) |
| become a station | `07/48` | `01` | `00 00` (Osmo Nano: `00`), sometimes only after 5-6 s; wait ~10 s before joining |
| join a network | `07/47` | `<len> <SSID> <len> <password>` | `00 00` or `00 00 00` joined; `01 FF` refused for now (retry after 5 s; seen on WPA3-only networks) |
| back to its access point | `07/48` | `00` | `00` / `00 00` |

The join can take up to ~45 s to answer. On the network the camera gets an address by DHCP and serves
the same TCP 7001 poke, UDP 9004 datalink and HTTP media as on its own access point. To find it, try TCP
7001 on the local network, open a datalink with each host that answers and ask `07/07` (to receiver
`07`): the reply is the name of the camera's own access point, the same as over BLE, which tells the
cameras apart. WPA3-only networks are refused by the Osmo Nano; WPA2 (or WPA2/WPA3) works. Guest networks
that keep devices apart don't work. Follows the OpenPocketCine project's notes; verified with an Osmo Nano
and an Osmo Pocket 3 on the same 5 GHz WPA2 network, both streaming to one computer.

An identity check opens a datalink session with the host it asks, which ends any session that camera had:
never ask an address where another camera this computer uses is known to be.

## 5. The UDP datalink

`camlink/datalink.py`.

### 5.1 Poke

Open TCP `192.168.2.1:7001`, send the BLE pairing frame (`07/45`, section 3.3), wait ~0.4 s, close. The
UDP datalink then answers on port **9004**. (Some rebadged models listen on UDP 10004 and need no
poke; the apps fall back to that if the handshake goes unanswered.)

### 5.2 Link header

Every UDP packet, both directions, starts with 8 bytes:

| offset | size | field |
|---|---|---|
| 0 | 2 | u16 `0x8000 \| length` (length includes the header, 14 bits) |
| 2 | 2 | u16 session id, random, chosen by the client |
| 4 | 2 | u16 sequence |
| 6 | 1 | packet type |
| 7 | 1 | XOR of bytes 0-6 |

Packet types:

| type | direction | content |
|---|---|---|
| 0 | both | handshake |
| 1 | camera → client | link packet; u16 at offset 10 = video cursor, 18 = data cursor, 26 = extra cursor (the last data packet of the client's the camera took, 5.5) |
| 2 | camera → client | video |
| 3 | camera → client | link packet; its header sequence is the data cursor |
| 4 | client → camera | acknowledgement |
| 5 | client → camera | data: DUML messages |

DUML messages from the camera (replies, status pushes, its own requests) ride inside its non-video
packets: scan everything after the 8-byte header for DUML frames.

### 5.3 Handshake

The client picks a random session id and a base sequence (multiple of 8), then sends type-0 packets
(up to 30 tries, ~0.35 s apart) until the camera answers with a type-0 packet. Body (40 bytes):

```
<base seq u16>
64 00 64 00 c0 05 14 00 00 64 00       channel block (100, 100, 1472, 20, 0, 100)
00 01 90 01 c0 05 14 00 00 64 00       channel block (256, 400, 1472, 20, 0, 100)
14 00 64 00 c0 05 14 00 00 64 00       channel block (20, 100, 1472, 20, 0, 100)
01 01 04 01 02
```

These are the values the camera accepts; the meaning of each field is not documented.

### 5.4 Sequences and acknowledgements

- Sequences advance in steps of 8.
- Keep **two** sequences: one for link packets (handshake and acknowledgements) and one for data
  packets. After the handshake both start at `base + 8`. Data packets must not share the counter
  with acknowledgements: if they do, commands sent after the first acknowledgement are ignored.
- Every 25 ms send a type-4 acknowledgement (26-byte body): three groups of
  `<cursor u16> <cursor u16> 00 00 00 00` for the video, data and extra cursors, then `00 00`.
  - video cursor = header sequence of the last video packet; before any video arrives (and while it is
    stalled), the video cursor the camera reports at offset 10 of its type-1 packets. On the Nano that
    cursor is the session's base sequence before the first video packet; some cameras don't start
    their video until the acknowledgement carries it.
  - data / extra cursors = taken from type-1 packets (offsets 18 and 26) and type-3 packets (header
    sequence). Both start at the base sequence.

### 5.5 Data packets

A type-5 packet carries one DUML frame after a 12-byte routing header:

```
<previous data seq u16> <this data seq u16> 00 00 00 00 <counter u8> 01 00 00
```

The link header's sequence is the data sequence too. `counter` increments by one per data packet.
The DUML frame inside uses sender `0x02`, receiver `0x01` (or `0x41` for `09/A8`), flags `0x40` and its
own message sequence (the apps start at `0xA000`).

The camera takes data packets only in order: one whose previous sequence is not the last it took is
dropped, and so is every one after it. The last one it took is the u16 at offset 26 (also 24) of its
type-1 packets. So keep each data packet until that cursor names it, and send a packet still
unacknowledged after ~0.3 s again, together with the ones after it, in order. Without this, one data
packet lost on the way silences every later command (no replies; an Osmo Pocket stops its video 8-10 s
later, as it no longer hears `00/88`). Seen on a shared network with two cameras, a few times an hour.

### 5.6 Registration and live view

Right after the handshake, send:

| command | payload | purpose |
|---|---|---|
| `00/81` | `00 'A' 'P' 'P' 00×38 02 00×8 02 08 00×11` (64 bytes) | register as an app |
| `00/99` → receiver `0x28` | topic subscriptions (below; Osmo Client only) | status, capabilities |
| `02/09` | `00×10 03` | live view source: camera |
| `09/A8` → receiver `0x41` | `00 04 02 00 00 00 00 00 00 00` | start transmitting video |

Topic subscription payload:

```
02 02 00 00 <subscription id u32> 00 00 00 <name length + 6 u16> <name length u16> <name> 00 00 00 00
```

Send it to receiver `0x28`, which answers `00` (subscribed) or `01` (no such topic on this model); sent
to `0x01` it is refused (`E0`). Osmo Client subscribes to `cam_expo_param` and to the capability tables
(section 8.4), with ids from `0x6A00`. The camera keeps subscriptions until it restarts and pushes them to
whichever app is connected; subscribing to the same topic again doesn't duplicate the pushes.

**Action series** (as described by [dji-vcam](https://github.com/RB14/dji-vcam) for the Osmo Action 5
Pro; not verified with these apps yet). The video encoder (`0x28` / `0x48`) streams only while the app
keeps it alive. The apps send this to every model except the Nano, after the commands above:

| command | receiver | flags | payload | when |
|---|---|---|---|---|
| `00/81` | `0x48` | `0x80` | the 62-byte app record (5.7) | at start, then every 2 s |
| `00/88` | `0x28` | `0x40` | `17 00 46 23 7c 41 50 50 00 00 00 00 00 02` | at start, then every 1 s |
| `00/82` | `0x48` | `0x80` | `00` | at start, then every 2 s |
| `00/4F` | `0x48` | `0x40` | `01 00 <counter u8> 00 00 FF FF FF FF` (counter +1 every 400 ms) | every 200 ms |

On the Action 5 Pro, `09/A8` answers `EE` from `0x41`. The **Osmo Action 4** streams with these (seen in
a tester's packet dumps): `09/A8` answers `00` from `0x41` and `02/09` answers `D9`. Its live view is H.265
(1280×720, 25 fps) with a 12-byte picture header (5.8): a keyframe after `09/A8`, then P pictures that
refresh gradually, with the parameter sets alone about every 1.5 s; a session can start there, like the
Pocket 3's (5.8).

### 5.7 Answering the camera's requests

Over the datalink the camera keeps sending requests: `00/81` about once a second (from `0x48`), `00/74`
(from `0x88`) and named topics as requests (`00/99` from `0x28`). Answer each with flags `0x80`, sender
`0x02`, receiver = the asker, the same command and sequence:

| request | answer payload |
|---|---|
| `00/81` | the 62-byte app record (section 3.4) |
| `00/88` whose payload starts with `19` | `1a 00 00 00 00` |
| anything else | `00` |

Left unanswered, the camera treats the link as lost: a camera sitting in its Vision Dock makes the dock
show "connecting…" every few seconds.

### 5.8 Video

Type-2 packets carry H.264 or H.265 (Annex-B) after a 20-byte header (8-byte link header + 12 bytes).
Their header sequence advances by 8 per packet, so a gap means a lost packet. Byte 16 is a transport group
number, not a picture id: on the Osmo Nano a group ends after 63 packets, and a keyframe can be bigger
than that, so a picture can span two groups.

The first packet of each picture starts with a picture header:

| offset | size | content |
|---|---|---|
| 0 | 4 | `00 00 01 FF` |
| 4 | 4 | size of the access unit (u32 LE) |
| 8 | 1 | length of this header in the low bits: `90` = 16 bytes (Osmo Nano, Pocket 3), `0C` = 12 bytes (Osmo Action 4) |
| 9 | 3 or 7 | metadata (16-byte header: a timestamp at offset 12) |

The access unit follows; collect packets until that many bytes have arrived. Each picture from the Nano
also carries a private SEI: `00 00 01 06 F0 19`, 25 raw bytes, `80`. Those bytes are not escaped and can
contain `00 00 01`, so remove the whole SEI (by its fixed length) before decoding. Start decoding at the
first keyframe (an IDR picture) with its parameter sets (SPS `00 00 00 01 67`, PPS). The Osmo Nano sends one
about every second, with the parameter sets in the same picture.

**Osmo Pocket 3.** It sends a keyframe only when its stream starts: the parameter sets as a picture of
their own, then the IDR picture about 1 ms later. After that come only P pictures, with the parameter sets
repeated alone every 1.5 s, and the picture refreshes itself gradually: a lost packet heals within ~1.5 s,
so nothing needs asking. The stream keeps running after a session ends, and `09/A8` brings no keyframe
while it runs, so a new session (a reconnect, a restart) can receive the running stream without one.
Start decoding it at its parameter sets and let the decoder output pictures that are still building up
(FFmpeg: `flags output_corrupt`; by default it shows nothing until a keyframe): the moving parts show at
once and the picture is complete within a few seconds.

Without picture headers (not seen on the Nano), split pictures on the group number: a picture arrives as
a burst of packets less than 0.3 ms apart followed by ~30 ms of silence, so it can be handed to the
decoder after ~5 ms without new packets.

The Nano's live view is 1280×720 at 30 fps (960×720 when the camera records 4:3), about 6 Mbit/s,
without B-frames.

### 5.9 Replies

A reply has flags `0x80`, the command of the request and its message sequence. The first payload byte is
the result:

| code | meaning |
|---|---|
| `00` | OK |
| `E0` | command not supported |
| `E3` | invalid parameter, or not allowed in the current state |
| `DF` | not available right now |
| `D8` | storage not available (e.g. no microSD in the dock) |

Send one command at a time and wait for its reply (a mode switch takes ~0.8 s).

## 6. Camera control

All in command set `0x02`, receiver `0x01`.

| action | command | payload | notes |
|---|---|---|---|
| capture mode | `02/E1` | `<mode>` | see below; the live view pauses ~1 s |
| take photo | `02/01` | `01` | photo mode |
| record start / stop | `02/02` | `01` / `00` | video, slow-mo, hyperlapse, super night |
| timelapse start / stop | `02/01` | `01` / `00` | `02/02` answers `DF` in timelapse; stop is accepted while starting (~5 s) |

Capture modes (`02/E1` value, also byte 57 of the status push):

| value | mode |
|---|---|
| `05` | Photo |
| `01` | Video |
| `28` | Super Night |
| `00` | Slow-mo |
| `02` | Timelapse |
| `0A` | Hyperlapse |

Not usable on the Nano: `02/10` (answers `E0`); pause / resume (`02/02` `02` / `03`) answer `00` but do
nothing.

## 7. Settings

Each capture mode keeps its own exposure and format: after `02/E1`, read them again.

### 7.1 Exposure

| action | command | payload | reply |
|---|---|---|---|
| read exposure mode | `02/1F` | | `00 <mode> 00` |
| set exposure mode | `02/1E` | `<mode> 00` | `01` Auto, `04` Manual (`02`/`03` → `E3`) |
| read ISO | `02/2B` | | `00 <code>` (`00` in Auto) |
| set ISO | `02/2A` | `<code>` | Manual only (`E3` in Auto) |
| read shutter | `02/29` | | `00 01 <u16> <decimal>` (`DF` in Auto) |
| set shutter | `02/28` | `01 <u16> <decimal> 00 00 00` | Manual only |
| set exposure compensation | `02/2E` | `<10 + thirds of a stop>` | Auto only; `07` = −3 EV … `10` = 0 … `19` = +3 EV |

The exposure compensation in use, and the camera's own light meter, are in the `cam_expo_param` topic
(8.3).

ISO codes (one stop each): `03`=100, `04`=200, `05`=400, `06`=800, `07`=1600, `08`=3200, `09`=6400,
`0A`=12800, `0B`=25600, `0C`=51200. (`01`/`02` behave like `03`, `0D` like `0C`.) What a capture mode
offers is in `camcap_iso` (8.4); on the Nano: 100-12800 in Video, up to 25600 in the other modes, Auto
only in Super Night. The camera also accepts codes outside that list, but they are not offered.

Shutter speed: a u16 where bit 15 set means 1/x seconds (x = bits 0-14) and clear means whole seconds,
plus a decimal digit. Examples: `0x8019` + 0 = 1/25 s, `0x800C` + 5 = 1/12.5 s, `0x0001` + 3 = 1.3 s.
In the code the two are combined as `u16 | decimal << 16`. The speeds a capture mode offers are in
`camcap_shutter` (8.4): on the Nano 1/8000 s to 30 s in Photo and Timelapse, and down to 1/fps in the video
modes. `SHUTTER_SPEEDS` in `camlink/camera.py` is the fallback list.

### 7.2 Video format

`02/18 [resolution, fps, 00, 00, slow-mo factor]`; read with `02/19` → `00 <resolution> <fps> 00 00 <factor>`.
Changing the format restarts the camera's encoder: the live view stops for 2-5 s.

| resolution code | format |
|---|---|
| `10` | 4K 16:9 (3840×2160) |
| `2D` | 2.7K 16:9 |
| `0A` | 1080p 16:9 |
| `67` | 4K 4:3 |
| `5F` | 2.7K 4:3 |
| `0C` | 1080p 4:3 |
| `69` / `6A` / `6B` | 1080p / 2160p / 3K 1:1 (Osmo Pocket 3) |
| `42` / `43` / `6C` | 1080p / 2.7K / 3K 9:16 (Osmo Pocket 3, after the camera's own portrait lock) |

| fps code | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | `0A` |
|---|---|---|---|---|---|---|---|---|---|
| fps | 24 | 25 | 30 | 48 | 50 | 60 | 120 | 240 | 100 |

What each capture mode offers on the Nano (the camera publishes it as `camcap_video_format`, 8.4):

| mode | resolutions | fps |
|---|---|---|
| Video | all six | 24-60 (4K 4:3: up to 50) |
| Super Night | 16:9 ones | 24, 25, 30 |
| Timelapse, Hyperlapse | 16:9 ones | 25, 30 |
| Slow-mo | 4K, 2.7K, 1080p at 120 fps (factor 4); 1080p at 240 fps (factor 8) | |

**Aspect ratio.** The 4:3 codes are only accepted with the aspect set to 4:3 first, through the
parameter command `02/8E` with a one-byte TLV:

```
set: 01 01 <type u16> <length u8> <value>       type 0x0046: 1 = 4:3, 2 = 16:9
get: 00 01 <type u16>
```

Sending a 16:9 resolution switches the picture back to 16:9 but leaves the aspect setting at 4:3, so
set it to 2 explicitly in Video mode.

**Osmo Pocket 3.** It publishes no `camcap_video_format` (it answers `01`: no such topic), and the aspect
is part of the resolution code (no `02/8E` aspect write). Its choices, from the OpenPocketCine project's
survey of the camera:

| mode | resolutions | fps |
|---|---|---|
| Video | 4K, 2.7K, 1080p 16:9; 3K, 2160p, 1080p 1:1 | 24-60 |
| Low-Light (`28`) | 4K, 1080p | 24, 25, 30 |
| Timelapse, Hyperlapse | 4K, 2.7K, 1080p | 25, 30 |
| Slow-mo | 4K 100 and 120 fps, 2.7K 120 fps, 1080p 120 fps (4×); 1080p 240 fps (8×) | |

In Slow-mo the Pocket 3 takes the factor in byte 3: `02/18 [res, fps, 00, factor, 00]` (4K 100 fps:
`10 0A 00 04 00`), where the Nano takes it in byte 4. Colour profile `02/42`: `00` Normal, `3C` HLG,
`3D` D-Log M.

**Osmo Action 4** (a tester's camera reports and the camera's manual). It publishes no `camcap_*` table
either (every one answers `01`), and it refuses the `02/8E` aspect write (`E3`): `02/18` goes alone, 4:3
codes too. The slow-mo factor is in byte 4, as on the Nano. Its choices:

| mode | resolutions | fps |
|---|---|---|
| Video | 4K, 2.7K, 1080p 16:9; 4K, 2.7K 4:3 (`67`, `5F`; 1080p 4:3 `0C` refused) | 24-60; 4K and 2.7K 16:9 also 100, 120; 1080p also 100, 120, 200, 240 |
| Low-Light (`28`) | 4K, 2.7K, 1080p 16:9 | 24, 25, 30 |
| Timelapse | 4K, 2.7K, 1080p 16:9 | 25, 30 |
| Hyperlapse | 4K, 2.7K, 1080p 16:9 | 30 (25 refused) |
| Slow-mo | 4K, 2.7K, 1080p at 120 fps (factor 4); 1080p at 240 fps (factor 8) | |

Every entry was accepted and read back.

### 7.3 Photo format

`02/12 [size, ratio]`, read with `02/13` (photo mode only). Size `04` = L, `03` = M; ratio `00` = 4:3,
`01` = 16:9. L 4:3 = 36 MP, L 16:9 = 31 MP, M 4:3 = 9 MP, M 16:9 = 8 MP. Osmo Pocket 3 (and 4 / 4P):
`00 01` = 16:9, `00 03` = 1:1. The Osmo Action 4 has one size (about 10 MP) and refuses the L and M ones
(`E3`): `00 00` = 4:3, `00 01` = 16:9 (read back with `02/13`: `00 00 00`, `00 00 01`).

### 7.4 White balance

`02/2C [mode, kelvin / 100, 00, 00, 00]`, read with `02/2D`. Mode `00` = Auto, `06` = manual colour
temperature, 2000-10000 K. In Auto the camera remembers the last manual temperature.

### 7.5 Image

What each capture mode offers is in the `camcap_*` tables (8.4); the values in use are in the
`cam_image_effect` topic (8.3). Not yet checked on a camera with Osmo Client: the layouts match the
tables an Osmo Nano publishes.

| setting | command | payload | values |
|---|---|---|---|
| colour profile | `02/42` | `<code>` | `00` Normal, `3F` Normal 10-bit, `3D` D-Log M (Nano, video modes); other models also use `3C` HLG, `17` D-Log, `41` D-Log2 |
| sharpness | `02/38` | `<s8>` | Nano: −2 … +2 |
| noise reduction | `02/44` | `<s8>` | Nano: −2 … +1 |
| field of view | `02/8E` | `01 01 09 00 01 <value>` | `00` Ultra Wide, `01` Wide, `02` Standard, `05` Natural Wide (Nano: `02 01 05`) |
| stabilisation | `02/8E` | `01 01 08 00 01 <value>` | `00` Off, `01` RockSteady, `02` HorizonSteady, `03` RockSteady+, `04` HorizonBalancing |

Field of view and stabilisation are read with `02/8E [00 01 <type u16>]` → `00 00 01 <type u16> 01 <value>`.

### 7.6 Timelapse and hyperlapse

Both use `02/6C` with a 16-byte payload; the settings in use are in the `cam_lapse_param` topic (8.3).

```
timelapse:  04 00 <keep> <interval u16, 0.1 s> <duration u32, s; 0 = until stopped> 00×7
hyperlapse: 0B 00 00 <speed u16; 0 = auto> 00×11
```

`keep` is what a timelapse saves besides the video: `00` nothing, `02` JPEG, `03` RAW. The intervals
and durations a camera offers are in `camcap_photo_timer_interval` and `camcap_timelapse_duration`, the
hyperlapse speeds in `camcap_hyperlapse_ratio` (8.4). Not yet checked on a camera with Osmo Client: the
lists match the Nano's tables exactly (intervals 0.5 s to 1 h, durations 5 min to 5 h).

## 8. Status pushes

After registration the camera pushes, without being asked:

| frame | from | rate | content |
|---|---|---|---|
| `02/80` | `0x01` | ~10/s | camera status (8.1) |
| `0D/02` | `0x05` | ~1/s | battery (8.2) |
| `00/99` | `0x28` | varies | named status topics the app subscribed to (8.3) |
| `02/DC` | `0x01` | on change | storage of each store (8.5) |

### 8.1 Camera status `02/80`

| offset | size | field |
|---|---|---|
| 0 | 1 | bits 6-7: record state (0 idle, 1 starting, 2 recording, 3 stopping); bits 3-5: non-zero while taking a photo |
| 2 | 1 | bit `0x08`: writing a file |
| 3 | 1 | bit `0x40`: the app's media mode is on |
| 4 | 1 | 0 photo/timelapse, 1 video, 2 playback on the camera |
| 5 | 4 | total storage, MB |
| 9 | 4 | free storage, MB |
| 13 | 4 | photos left (0 in video modes; frames left in timelapse) |
| 17 | 4 | seconds of video left at the current format (0 in photo mode) |
| 29 | 4 | seconds recorded in the current clip |
| 57 | 1 | capture mode (section 6) |

In playback, offsets 5-20 read 0. The frame rate and exposure are not in this push.

The camera reserves a fixed amount of storage per second of video and per photo, so
`free MB / seconds left` (or `/ photos left`) identifies the current format. Measured at 25 fps:

| free ÷ seconds left | ≈ Mbit/s | format |
|---|---|---|
| 11.903 | 95 | 4K 16:9 |
| 14.288 | 114 | 4K 4:3 |
| 7.135 | 57 | 2.7K 16:9 |
| 8.327 | 67 | 2.7K 4:3 |
| 4.750 | 38 | 1080p 16:9 |
| 5.347 | 43 | 1080p 4:3 |
| 12.143 | 97 | Slow-mo 1080p 4× |
| 14.582 | 117 | Slow-mo 1080p 8× |
| 14.527 | 116 | Slow-mo 2.7K 4× |
| 15.537 | 124 | Slow-mo 4K 4× |

| free ÷ photos left | photo |
|---|---|
| 1.209 MB | M 16:9 (8 MP) |
| 1.317 MB | M 4:3 (9 MP) |
| 2.714 MB | L 16:9 (31 MP) |
| 2.935 MB | L 4:3 (36 MP) |

48/50/60 fps use a higher bitrate (4K ≈ 114 Mbit/s, 1080p ≈ 57 Mbit/s).

### 8.2 Battery `0D/02`

| offset | size | field |
|---|---|---|
| 1 | 4 | voltage, mV |
| 5 | 4 | current, mA, signed (negative = discharging) |
| 17 | 2 | temperature, 0.1 °C |
| 20 | 1 | charge, % |

### 8.3 Named status topics `00/99`

The camera publishes the named topics an app subscribed to (section 5.6: `cam_expo_param`, `cam_storage`,
`cam_video_param_v2`, ...) over the datalink, and over BLE while only BLE is connected. Subscriptions
last until the camera restarts, so a session can also get topics it didn't ask for (after the phone app,
for example). Layout:

```
02 06 00 00 <seq u16> 00 00 00 00 00 <rest length u16> <name length u16> <name> 00×6 <data length u16> <data>
```

Those that arrive as requests (flags `0x40`) must be answered (section 5.7).

`cam_expo_param` gives the exposure actually in use, also in Auto: ISO = u32 at data offset 16
(any value, e.g. 2509 in Auto), shutter = u16 at data offset 20 (same encoding as section 7.1, without
the decimal digit). Offset 6 is the exposure compensation set and offset 15 the camera's light meter,
both as `10` + thirds of a stop (the meter says how far the picture is from the exposure the camera
considers correct, which is useful in Manual).

Pushed only when they change (and once on subscribing):

| topic | data |
|---|---|
| `cam_image_effect` | offset 2: colour profile (7.5); 14: noise reduction (s8); 15: sharpness (s8) |
| `cam_lapse_param` | 0: what a timelapse keeps; 1: interval (u16, 0.1 s); 3: shortest interval (u16, 0.1 s); 5: duration (u32, s); 9: hyperlapse speed (u16) |

### 8.4 Capability tables (`camcap_*`)

The topics named `camcap_*` describe what the **current** capture mode offers. The camera sends each one
when it is subscribed and again when it changes (after a capture mode switch, for example), as requests
to answer. Data:

```
<supported u8> <table length u16> <table>          supported 0 = not available
```

Most tables are a count followed by that many entries:

| topic | table | Osmo Nano |
|---|---|---|
| `camcap_base` | count, capture modes (`02/E1` codes), then other fields | `05 01 00 0A 02 34 28` |
| `camcap_video_format` | count, then per entry: resolution, fps code, `00` | Video: 35 entries |
| `camcap_iso` | `00`, count, ISO codes (`00` = Auto) | Video: Auto, 100-12800 |
| `camcap_shutter` | `00`, n, n bytes, group count; per group: key, slowest speed (u16 + decimal), count, speeds (u16 + decimal each) | Video at 25 fps: 1/8000-1/25 s |
| `camcap_photo_size` | count, then per entry: size, ratio, `00`, `00` (section 7.3) | Photo: M and L, 4:3 and 16:9 |
| `camcap_wb` | minimum K / 100, `00`, maximum K / 100, `00`, 4 bytes, count, white balance modes | 2000-10000 K; `00`, `06` |
| `camcap_exposure_mode` | `00`, count, exposure modes (`02/1E`) | `01`, `04` |

| `camcap_color_mode`, `camcap_fov`, `camcap_eis` | count, values (section 7.5) | Video: `00 3F 3D`; `02 01 05`; `00 01` |
| `camcap_sharpness`, `camcap_denoise` | count, signed values | −2 … +2; −2 … +1 |
| `camcap_photo_timer_interval` | 4 bytes, count, then per entry: kind, seconds (u16), milliseconds (u16) | Timelapse: 19 intervals, 0.5 s to 1 h |
| `camcap_timelapse_duration` | count, durations (u32, s; 0 = until stopped) | 0, 5 min … 5 h |
| `camcap_hyperlapse_ratio` | count, speeds (0 = auto) | Auto, 2, 5, 10, 15, 30 |

Osmo Client uses these and falls back to the Nano's tables (section 7) for the first six until they
arrive; the settings of sections 7.5 and 7.6 are only offered when the camera lists them. A mode
without a setting sends its table empty (count 0: `camcap_eis` in Photo). Others the Nano publishes (count and
values unless noted): `camcap_capture_aspect_type`, `camcap_iso_auto_max`, `camcap_countdown` (u32 ms),
`camcap_loop_video_duration` (u16 s), `camcap_slowmotion_ratio` (pairs of base fps code and factor). Its
mode `34` is not handled by the apps yet; it lists two more resolution codes, `42` and `43`.

### 8.5 Storage `02/DC`

| offset | size | field |
|---|---|---|
| 6 | 4 | microSD total, MB |
| 10 | 4 | microSD free, MB |
| 24 | 4 | internal memory total, MB (payloads of 32 bytes or more) |
| 28 | 4 | internal memory free, MB |

In playback the microSD fields can read 0: keep the last non-zero values.

## 9. Media library and downloads

`camlink/media.py`. The listing follows the media protocol documented by
[osmosis](https://github.com/KonradIT/osmosis).

1. **Media mode**: `02/0C [01 01 00 01]`. Status byte 3 gets bit `0x40` and the live view stops. Leave
   with `02/0C [01 01 00 00]`. Wait ~1.7 s after entering before the first listing. Without the live
   view the camera serves files about 6× faster (~45 MB/s instead of ~8 MB/s) and runs cooler.
2. **List a page**: `00/26`
   ```
   4a 00 2a 10 <counter u32> 00 00 <cursor u32> <page size u16> 0d 01 00 ff×8 00 01 00 00 00 00 00 00 00 00 00
   ```
   `counter` identifies the request. `cursor` `0x40000001` = newest file in internal memory,
   `0x00000001` = newest on the dock's microSD (`D8` when there is none). Page size up to `0x2D`.
   Next page: `cursor + number of records` (pages overlap by one record).
3. **Answer**: the reply is `00`, then `00/27` chunks arrive. Each starts with a 10-byte sub-header
   `4a <type> <length u16> <counter u16> <seq u16> 00 00` (type `04` start, `01` data, `03` end).
   Concatenating the data chunks in `seq` order gives the manifest. The length counts the sub-header and
   uses bits 0-11; bit `0x1000` marks the **last data chunk**. The end chunk can arrive before the data
   (an Osmo Pocket 3 sends it first, then again every 0.2 s), so the list is complete when the last data
   chunk and every one before it are there, not at the first end chunk.
4. **Manifest**: u32 record count, then per record a block anchored on a `19 06` tag at offset T:

   | offset | field |
   |---|---|
   | T-18 | u32 time: FAT date << 16 \| FAT time (can be an hour off; the file name carries the local time) |
   | T-14 | u32 size, bytes |
   | T-10 | u32 handle (bit 30 set = internal memory) |
   | T-6 | u16 duration, s |
   | T-3 | resolution code (video; section 7.2) |
   | T-2 | type: `03` video, `00` photo |
   | T-1 | `FF` / `FE` |
   | T+8 | favourite: `01` (on the Osmo Nano; on other layouts this byte can be a length) |

   followed by TLVs: `0d <len> <file name>`, `1a <len> 00 00 00 01 <path without extension>`,
   `1a <len> 00 00 00 02 <thumbnail path>`. A video's frame rate is a u32 numerator followed by a u32
   denominator of 1000 or 1001 within its record (60000/1001 = 59.94 fps). Some layouts mark a favourite
   with `01` right after `1b 0a 00 00 00 02 02 01 14 02 15 03` instead.
5. **Download**: the camera runs an HTTP server on port 80:
   `GET http://192.168.2.1/v2?storage=<1 internal | 0 dock microSD>&path=<path>.<EXT>`. Range requests
   work (206), so interrupted downloads can resume.
   - Thumbnail: the thumbnail path + `.THM` (JPEG, 256×144 or 256×192).
   - Low-resolution preview of a video: the video path with `.LRF` instead of `.MP4` (H.264 720p MP4,
     plays in a browser).
6. **Favourite** (kept by the camera with the file): `02/BF [01 01 <handle u32> <counter u32> 00 <0|1> 00 00 00]`,
   `counter` = any number that changes with each request.
7. **Delete**: `00/28 [01 <handle u32> <counter u32> 00 01 00 00 00 01 01 00 00]`, reply `00 00` = deleted.
   Never send it twice for the same file. Osmo Client asks for confirmation first (`--no-delete` hides it).

Items 6 and 7 follow the OpenPocketCine project's notes (the delete layout is the one osmosis documents).
Both verified on an Osmo Nano: the favourite mark shows on the camera, and a deleted file is gone from the
next listing.

**Osmo Pocket 3.** It answers the media mode request with `E0`, yet lists its files all the same, page
after page (70 files tried, in pages of 45). It has one microSD card and no internal memory: download everything with `storage=0`, even
when a handle has bit 30 set (it answers `D6` for the internal-memory cursor). Its records put the file
name at the end, after the path TLVs (the Nano puts it before them): a path belongs to the file whose name
it ends with. They may carry other bytes than `FF` / `FE` before the `19 06` tag and another type byte for
photos (tell them by the extension). Thumbnails: `.THM` is only 90×90 (about 1 KB); `.SCR` is a
720-pixel JPEG (about 350 KB), which Osmo Client shrinks to 512 pixels for the gallery. On a shared network
(4.1) it
lists its files but refuses connections to its file server: thumbnails and downloads need its own access
point.

## 10. Power and sleep

- **Wake**: the normal BLE connection (section 3.3) wakes a sleeping camera; it keeps advertising while
  asleep.
- **Sleep / power off**: no command is known for the Nano. Releasing BLE and Wi-Fi lets the camera's
  idle timer put it to sleep by itself.

## 11. Limits and warnings

- One client at a time; close the phone app first.
- Many format changes in a row heat the camera (each restarts the encoder). Around 67 °C it stops
  charging and may refuse Wi-Fi until it cools down.
- Do not sweep unknown commands or values: sweeping capture-mode values has been reported to freeze a
  Nano until its battery was disconnected.

## 12. Models

Model ids in the BLE advertisement (from [osmosis](https://github.com/KonradIT/osmosis)). The Osmo Nano
and the Osmo Action 4 are tested with both apps; the Osmo Pocket 3 with Osmo Client.

| id | model | notes |
|---|---|---|
| `0x0010` | Osmo Action 2 | |
| `0x0012` | Osmo Action 3 | |
| `0x0014` | Osmo Action 4 | tested with Osmo Client and OsmoCam: H.265 live view (1280×720, 25 fps), 12-byte picture header (5.8); formats (7.2) |
| `0x0015` | Osmo Action 5 Pro | |
| `0x0017` | Osmo 360 | WPA3 Wi-Fi and 360° video: not supported |
| `0x0018` | Osmo Action 6 | |
| `0x0019` | Osmo Nano | tested |
| `0x0020` | Osmo Pocket 3 | tested with Osmo Client: H.264 live view; gimbal, zoom, formats (7.2), gallery (9) |
| `0x0021` | Osmo Pocket 4 | H.265 live view; gimbal, zoom (12.1) |
| `0x0022` | Osmo Pocket 4P | H.265 live view; gimbal, zoom (12.1); BLE name `OsmoPocket4P-…` |

### 12.1 Osmo Pocket 3 / 4 / 4P

Follows the OpenPocketCine project's notes, which verified these on the cameras. Tested with a real Osmo
Pocket 3 since Osmo Client 1.6; the Pocket 4 and 4P are not exercised with it yet.

**Session** (same handshake, poke, picture header and acknowledgements as the Nano):

1. Number the first command from the camera's first 34-byte status packet (type 1): its u16 at offset 8,
   plus 8. Starting from the handshake alone gave slow or failed starts.
2. `00/81` → `0x48`, flags `0x80`, the 62-byte app record; `00/88` → `0x28` (`17 00 46 23 7C 'APP' 00×5
   02`); `03/DA [05 FF FF FF FF]` → `0x03` (the gimbal learns an app is attached); the topic subscriptions.
3. `02/68 [08]` → `0x01`, then `09/A8` (same payload as the Nano's) → **`0x08`**. No `02/09`.
4. `00/88` again every second: the video stops 8-10 s after the last one (after the last data packet the
   camera took: see 5.5).

A pairing reply `00 06` means "try again in a moment". On 5.8 GHz the access point can take about a
minute to appear.

**Live view:** the Pocket 3 sends H.264; the Pocket 4 and 4P send **H.265** (1280×720, ~25 fps) in the
same picture header. Tell them apart by the parameter sets of the first keyframe: H.265 VPS/SPS/PPS start
`40 01` / `42 01` / `44 01`, H.264 SPS/PPS `67` / `68` (an H.265 `28 01` keyframe is not an H.264 PPS).

**Photo mode** is `0x17` on the Pocket 4 / 4P (`0x05` on the Pocket 3 and the Nano). A **timelapse**
starts and stops with record (`02/02`) on the Pocket 4 / 4P, with take-photo (`02/01`) on the Pocket 3 and
the Nano. **Slow-mo** formats carry the speed factor in byte 3 on every Pocket (7.2); the Pocket 4P adds
4K 200 fps (fps code `13`, factor 4) and 240 fps (factor 8), and publishes its own `camcap_video_format`.

**Gimbal**, command set `0x04` → `0x04` (flags `0x40`, acknowledged `00`):

| action | command | payload |
|---|---|---|
| recenter | `04/4C` | `FE 08` |
| turn 180° (front / selfie) | `04/4C` | `FE 09` |
| follow / tilt locked | `04/4C` + `04/50` | `02 08` + `00 04 01 00` / `00 04 01 01` |
| FPV / direction lock | `04/4C` | `01 08` / `00 08` |
| speed | `04/50` | `00 05 01 <00 fast, 01 normal, 02 slow>` |
| stick | `04/01`, flags `0x00` (no reply) | `<tilt u16> 00 00 <pan u16> 00 80 22 00` |

Stick axes: 1024 = centre, ± 550 (values beyond are ignored), tilt up and pan right positive. Send it
about every 50 ms while held, then **one** centred packet: a stream of centred packets pauses the video.
Past ±165° of pan (turned 180°) left and right swap. The attitude comes as `04/05` (50 bytes): pan i16 at
4, tilt = −i16 at 20 (0.1°), mode = byte 6 >> 6 (0 direction lock, 1 FPV, 2 follow).

**Zoom**, `02/B8` → `0x01`: `0A 4E <lens u16>` goes to a factor (lens = 217 × factor: 1× 217, 3× 651,
12× 2604); `01 <rate 48-4E> <01 in, 00 out> 00` while held, `FF 00 00 00` to stop. Read back from the
`cam_lens_state` topic (u16 at 14). Stops: Pocket 3 and 4 1×/2×/4× (Pocket 3: up to 2× at 4K, 3× at 2.7K,
4× at 1080p), Pocket 4P 1×/3×/6×/12×; Slow-mo, Timelapse and Super Night stay at 1× (4P: 1×/3×).
The Pocket 4P refuses to zoom in D-Log2.
