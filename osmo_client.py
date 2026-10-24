#!/usr/bin/env python3
"""Osmo Client: control a DJI Osmo camera from the computer over Wi-Fi (unofficial).

Opens a local page (http://127.0.0.1:8091) with the live view, the camera's capture modes, a shutter
(photo or record, depending on the mode), exposure and format settings, the full camera status, and a
gallery to preview the camera's files and download them to the computer.

How it connects: Bluetooth LE to find, pair and wake the camera (the first time it asks to approve
"OSMO" on its screen) and to read the password of its Wi-Fi access point; then the computer's Wi-Fi joins
that access point and everything else runs over it. Keep internet on another adapter (Ethernet) while
connected. "Disconnect" on the page, or Ctrl+C here, puts the computer's Wi-Fi back on its own network.
Or the camera joins a Wi-Fi network the computer is on (a router, or the computer's hotspot) and the
computer finds it there: set it per camera on the page (Cameras).

    python osmo_client.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from camlink import ble, camera as cam, datalink, models, wifi
from camlink.protocol import CAMERA_HOST
from camlink.registry import DEFAULT_TRANSPORT, other_transport
from camlink.util import fine_timer, log
from camlink.vcam import VirtualCamera
from camlink.video import VideoDecoder
from client import report
from client.app import App
from client.server import start_server
from client.station import back_to_own_wifi, reach
from client.watchdog import FeedWatchdog, start_wait


class Changed(Exception):
    """A connection step was given up: the page disconnected, picked another camera or changed its Wi-Fi."""


async def run(args: argparse.Namespace) -> None:
    app = App()
    app.loop = asyncio.get_running_loop()
    if sys.platform != "win32":      # `kill` stops like Ctrl+C (on Linux, BlueZ would keep the camera's link)
        app.loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    if args.downloads:
        app.downloader.base = Path(args.downloads)
    stop = threading.Event()
    decoder = VideoDecoder(app.on_frame, stop)
    decoder.start()
    threading.Thread(target=app.settings_loop, args=(stop,), daemon=True, name="settings").start()
    threading.Thread(target=app.media_hold_loop, args=(stop,), daemon=True, name="media-hold").start()
    threading.Thread(target=app.pocket_loop, args=(stop,), daemon=True, name="pocket-controls").start()
    try:
        start_server(app, args.port)
    except OSError as error:
        log(f"Port {args.port} is already in use ({error.strerror or error}): Osmo Client is probably running "
            f"already (http://127.0.0.1:{args.port}/). Close it first, or start this one with --port")
        stop.set()
        return
    if getattr(args, "report", False):
        report.start(app, decoder, stop)
    if getattr(args, "vcam", False):  # opened before the camera connects: opening can take seconds
        vcam = VirtualCamera(args.vcam_device)
        if await asyncio.to_thread(vcam.open):
            app.vcam = vcam
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    home = args.home or wifi.current_network(args.interface)
    if home.lower().startswith(models.NAME_PREFIXES):
        home = ""                    # started while still on a camera's network: nothing to return to
    app.home = home
    # Which camera: --address, else the one used last; with none linked yet the page lists the cameras
    # around and the user picks one.
    last = app.registry.last_used()
    app.target = args.address.upper() if args.address else (last or {}).get("address")
    app.allow_delete = not args.no_delete
    if app.target:                   # its gallery as it was last time, before the camera is reached
        app.use_camera(app.target, (app.registry.get(app.target) or {}).get("name") or app.target)
    if args.off:
        app.want_link.clear()
    link: ble.BleLink | None = None
    session: datalink.Datalink | None = None
    task: asyncio.Future | None = None
    joined = False                   # the Wi-Fi was moved to the camera (so disconnecting restores it)
    station = False                  # the camera was asked to join a shared network in this BLE session
    quiet = 0                        # sessions in a row that got no video (each waits longer for it)

    def wanted() -> bool:
        return app.want_link.is_set() and app.target is not None

    def changed() -> bool:
        return not wanted() or app.target_changed.is_set() or app.reconnect.is_set() or stop.is_set()

    async def unless_changed(step, given_up: threading.Event):
        """Run a connection step, but give it up as soon as changed(): a camera joining a network can take
        minutes, and switching it back to its own Wi-Fi must not wait for that. `given_up` is set then, for
        the step's threads (a network search), which cancelling does not stop."""
        task = asyncio.ensure_future(step)
        try:
            while not task.done():
                if changed():
                    raise Changed
                await asyncio.wait({task}, timeout=0.25)
            return task.result()
        finally:
            if not task.done():
                given_up.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def pause(seconds: float) -> None:
        """Sleep, but wake early if the page disconnects, picks another camera, or we quit."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and wanted() and not app.target_changed.is_set() and not stop.is_set():
            await asyncio.sleep(0.25)

    async def own_wifi() -> None:
        nonlocal station
        await back_to_own_wifi(link, app.registry, station)
        station = False

    async def joined_by_hand(camera: ble.BleLink, why: str) -> None:
        """This computer can't move its Wi-Fi by itself (Linux without NetworkManager): wait until the user
        joins the camera's network by hand. The Bluetooth link stays up meanwhile, which keeps the camera's
        Wi-Fi awake: no pairing again every few seconds."""
        app.set_stage("wifi")
        app.set_stage_note(f"{why}. Waiting for this computer to join “{camera.ssid}”…")
        log(f"{why}. Waiting for this computer to join {camera.ssid}")
        while not wifi.on_camera_network(camera.ssid, args.interface):
            if camera.disconnected.is_set():
                raise RuntimeError("The camera dropped the Bluetooth link")
            await asyncio.sleep(1)
        log(f"On the camera network ({wifi.local_ip_towards(CAMERA_HOST)})")

    async def reach_on_network(target: str, network: str, port: int, given_up: threading.Event) -> str:
        """Station mode: the computer stays on its own network and the camera joins `network`."""
        nonlocal joined, station
        if joined:                   # coming from the camera's own access point
            await asyncio.to_thread(wifi.restore, home, args.interface)
            joined = False
        try:
            return await reach(link, app.registry, target, network, port, app.set_stage, pause,
                               lambda: not given_up.is_set() and not changed(),
                               lambda: {t.host for t in list(app.tiles.values()) if t.host}, note=app.set_stage_note)
        finally:
            # put it back on its own Wi-Fi later only if it was asked to join (or found on the network)
            station = bool((app.registry.get(target) or {}).get("in_station"))

    async def release() -> None:
        nonlocal link, joined
        if link is not None:
            await own_wifi()
            with contextlib.suppress(Exception):
                await link.close()
            link = None
        if joined:
            await asyncio.to_thread(wifi.restore, home, args.interface)
            joined = False

    try:
        while not stop.is_set():
            app.target_changed.clear()
            if not wanted():
                quiet = 0
                # Disconnected, or no camera chosen yet: release the camera (BLE + Wi-Fi) so the phone app
                # or the dock can use it, put the computer back on its own network, and listen for cameras
                # around (that is also how the page shows whether the camera is asleep).
                app.link = "off" if app.target else "select"
                if app.stage != "error":
                    app.set_stage(None)
                if link is not None or joined:
                    log("Disconnected: camera released")
                await release()
                await app.discover(lambda: wanted() or stop.is_set())
                continue
            target = app.target
            known = app.registry.get(target)
            app.link = "connecting"
            if target in app.tiles:          # it was shown in Multiview: let that session end (and let go of it)
                app.tiles[target].close()
                for _ in range(120):
                    if target not in app.tiles:
                        break
                    await asyncio.sleep(0.25)
            network = (known or {}).get("network")
            try:
                # Pair again whenever BLE dropped (camera asleep, off, restarted) or another camera was
                # picked: pairing is also what wakes the camera's Wi-Fi.
                if link is None or link.disconnected.is_set() or link.address != target:
                    if link is not None and link.address != target:
                        await release()  # another camera picked: the previous one goes back to its own Wi-Fi
                    elif link is not None:
                        await link.close()
                    link = ble.BleLink()
                    link.on_topic = lambda payload: app.state.update_topic(payload)  # app.state is replaced per camera
                    link.on_stage = app.set_stage
                    await link.connect(target, args.approve_timeout)
                    seen = app.nearby.get(target)
                    if seen and (link.advert is None or link.advert.get("model_id") is None):
                        link.advert = seen     # the advert caught while connecting had no model in it
                    known = app.registry.remember(link)
                if app.target != target or not wanted():
                    continue
                app.use_camera(target, (known or {}).get("name") or link.name)
                app.reconnect.clear()    # the camera's Wi-Fi setting is read here: only later changes restart
                known = app.registry.get(target) or known
                transport = (known or {}).get("transport") or dict(DEFAULT_TRANSPORT)
                network = (known or {}).get("network")
                if network:
                    given_up = threading.Event()
                    host = await unless_changed(
                        reach_on_network(target, network, transport.get("port", datalink.DATALINK_PORT), given_up),
                        given_up)
                else:
                    await own_wifi()     # it was on a shared network until now
                    host = CAMERA_HOST
                    app.set_stage("wifi")
                    joined = True        # set first: a failed join may already have left the home network
                    # an Osmo Pocket on 5.8 GHz can take about a minute to bring its access point up
                    timeout = max(args.join_timeout, 90) if (known or {}).get("model_id") in models.POCKETS else args.join_timeout
                    try:
                        await asyncio.to_thread(wifi.join, link.ssid, link.password, timeout, args.interface)
                    except wifi.JoinByHand as error:
                        joined = False   # the user moves the computer's Wi-Fi, and moves it back
                        await unless_changed(joined_by_hand(link, str(error)), threading.Event())
                app.camera_host = app.downloader.host = host
                if transport.get("poke", True):
                    await asyncio.to_thread(datalink.poke, host)
            except Changed:
                log("Stopped trying: " + ("the camera's Wi-Fi setting changed" if app.reconnect.is_set()
                                          else "disconnected, or another camera picked"))
                continue                 # the next round reads the new setting (and puts the camera back)
            except Exception as error:  # noqa: BLE001 - keep trying until the camera is back (Ctrl+C quits)
                app.set_stage("error", str(error))
                if link is not None:
                    await own_wifi()     # never leave it on a network it could not be reached on
                    with contextlib.suppress(Exception):
                        await link.close()
                link = None
                if isinstance(error, (wifi.NoWifi, ble.JoinRefused)):
                    # Trying again can't help. No Wi-Fi on the computer: the camera can join a network the
                    # computer is on instead (Ethernet to the router is fine). The camera refused the shared
                    # network: the message says what to check.
                    if isinstance(error, wifi.NoWifi):
                        app.set_stage("error", f"{error}. Have the camera join a network this computer is on "
                                               "instead: Cameras, then Wi-Fi → Change")
                    log(f"Could not connect: {error}")
                    app.want_link.clear()
                    continue
                if app.registry.get(target) is None:
                    # A new camera that did not pair (prompt not approved, out of range...): don't keep
                    # asking it to approve; wait for the user to try again from the page.
                    log(f"Could not link the camera ({error})")
                    app.want_link.clear()
                    continue
                log(f"Camera not reachable ({error}); retrying in 5 s")
                await pause(5)
                continue
            if not wanted() or app.target != target:
                continue                 # disconnected or another camera picked while connecting
            app.set_stage("datalink")
            session = datalink.Datalink(port=transport.get("port", datalink.DATALINK_PORT), host=host,
                                        on_video=decoder.feed, on_message=app.on_message,
                                        liveview=models.liveview_mode((known or {}).get("model_id")),
                                        topics=cam.SUBSCRIBED_TOPICS)
            decoder.reset()
            app.datalink = session
            app.state.connected = True
            task = asyncio.ensure_future(asyncio.to_thread(session.run))
            started = reported = time.monotonic()
            decoded, errors = decoder.frames, decoder.errors
            watchdog: FeedWatchdog | None = None   # from the live view request (a handshake can take seconds)
            while not task.done() and not stop.is_set():
                await asyncio.sleep(0.3)
                now = time.monotonic()
                if datalink.DUMP_DIR and now - reported >= 10:
                    reported = now
                    log(f"Diagnostics (active camera): decoded {decoder.frames - decoded}, decode errors "
                        f"{decoder.errors - errors}; {session.pictures_summary()}")
                if (session.last_video or app.state.last_status > started) and app.link != "on":
                    app.link = "on"
                    app.set_stage("on")
                if app.last_frame > now - 1:
                    app.recovery = None
                if not wanted() or app.target != target or app.reconnect.is_set():
                    app.reconnect.clear()
                    session.stop()
                elif app.state.in_playback():
                    if watchdog:
                        watchdog.started = now   # the gallery pauses the live view; the link is fine
                elif session.ready.is_set() and not session.stopped.is_set():
                    if watchdog is None:
                        watchdog = FeedWatchdog(args.stall, now, start_wait(quiet))
                    step = watchdog.check(now, session.last_video, app.last_frame, app.video_grace,
                                          session.frames.truncated if session.frames else 0)
                    if step == "keyframe":
                        log("Live view: no picture or a broken one, asked the camera for a fresh keyframe")
                        app.recovery = "Asking the camera for a fresh picture…"
                        with contextlib.suppress(OSError):
                            session.request_keyframe()
                    elif step == "restart":
                        waited = "" if session.last_video else f" in {now - watchdog.started:.0f} s"
                        log(f"No video{waited}, restarting the session ({session.summary()}; decoded "
                            f"{decoder.frames - decoded}, decode errors {decoder.errors - errors})")
                        app.recovery = "Restarting the video link…"
                        session.stop()
            app.datalink = None
            app.state.connected = False
            session.stop()
            quiet = 0 if session.last_video else quiet + 1
            try:
                await task
            except RuntimeError as error:
                if "handshake" in str(error) and not network:
                    # Not every model listens where the Osmo Nano does (some rebadges use UDP 10004 without
                    # the TCP poke): try the other transport next time and remember it.
                    other = other_transport(transport)
                    log(f"{error}; next try: UDP {other['port']}" + (" with" if other["poke"] else " without")
                        + " the TCP poke")
                    app.registry.set_transport(target, other)
                    app.set_stage("error", str(error))
                else:
                    log(f"Session ended: {error}")
            except Exception:  # noqa: BLE001
                pass
            if app.target != target:
                quiet = 0
                await release()          # another camera picked: drop this one before connecting
            if not stop.is_set():
                await pause(1)
    except asyncio.CancelledError:
        log("Stopping...")
        raise
    finally:
        # End the live view session first: its thread would otherwise keep Python from exiting.
        app.datalink = None
        if session is not None:
            session.stop()
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=2)
        for tile in list(app.tiles.values()):  # Multiview cameras go back to their own Wi-Fi
            tile.close()
        for _ in range(80):                  # up to 20 s: a handshake in progress, then the Wi-Fi switch
            if not app.tiles:
                break
            await asyncio.sleep(0.25)
        stop.set()
        if joined:
            await asyncio.to_thread(wifi.restore, home, args.interface)
        if link is not None:
            await own_wifi()
            with contextlib.suppress(Exception):
                await link.close()
        if app.vcam is not None:
            app.vcam.close()
        log("Stopped.")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8091, help="port of the local page (default 8091)")
    parser.add_argument("--address", help="Bluetooth address of the camera, as the page lists it (default: the "
                                          "one used last)")
    parser.add_argument("--off", action="store_true", help="start disconnected (connect from the page)")
    parser.add_argument("--report", action="store_true",
                        help="camera report: once connected, check what the camera answers to each of the app's "
                             "settings (about ten minutes; everything is put back) and save a file to send to "
                             "the author (see the README)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page in the browser")
    parser.add_argument("--vcam", action="store_true",
                        help="also send the live view to a virtual camera, for other programs to use as a webcam "
                             "(a v4l2loopback device on Linux, the OBS Virtual Camera on Windows and macOS)")
    parser.add_argument("--vcam-device", help="v4l2loopback device for --vcam, e.g. /dev/video40 (default: the "
                                              "first one)")
    parser.add_argument("--downloads", help="folder for files copied from the camera (default: DJI Osmo in "
                                            "your Videos folder, Movies on macOS)")
    parser.add_argument("--interface", help="Wi-Fi adapter to use: 'Wi-Fi 2' on Windows, en0 on macOS, wlan0 "
                                            "on Linux (default: the first one)")
    parser.add_argument("--home", help="Wi-Fi network to return to (default: the current one)")
    parser.add_argument("--stall", type=float, default=3.0, help="restart the session after N s without video")
    parser.add_argument("--no-delete", action="store_true", help="don't offer to delete files on the camera")
    parser.add_argument("--allow-delete", action="store_true", help=argparse.SUPPRESS)   # 1.3.0: deleting was opt-in
    parser.add_argument("--join-timeout", type=float, default=45)
    parser.add_argument("--approve-timeout", type=float, default=90)
    try:
        with fine_timer():
            asyncio.run(run(parser.parse_args()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
