"""camlink: talk to DJI Osmo cameras from Windows, macOS or Linux over Bluetooth LE and Wi-Fi.

Unofficial and independent; not affiliated with DJI. The protocol is described in docs/PROTOCOL.md.

    protocol   addresses, identities and fixed payloads shared by the other modules
    duml       message framing (CRCs, encoder, stream reader)
    models     which camera is advertising, from its BLE advertisement
    ble        find, pair, wake the Wi-Fi, read the access point credentials, join other networks
    wifi       join the camera's access point and return to the previous network (one backend per system)
    datalink   DUML commands and the live view over UDP
    nal        start codes and parameter sets: H.264 or H.265
    video      live view decoding, H.264 or H.265 (PyAV)
    registry   cameras linked to this computer
    camera     camera commands, setting codes and status parsers
    media      the camera's media library: listing and HTTP downloads
    lan        finding a camera that joined a shared Wi-Fi network (station mode)
    vcam       the live view as a webcam (v4l2loopback on Linux, the OBS Virtual Camera elsewhere)
"""

__version__ = "1.7.0"
