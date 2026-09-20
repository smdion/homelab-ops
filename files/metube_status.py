#!/usr/bin/env python3
# metube_status — publishes the download tracker's state to MQTT for Home Assistant,
# from INSIDE the metube container (stdlib only: the image has no paho).
#
# Topics (retained), under the profile's topic prefix from metube_mqtt.json:
#   <topic>/state   ON | OFF
#   <topic>/info    {"profile","status","since","title","source","uploader","url"}
# HA's sensor reads /info as JSON attributes, so extra keys appear with no HA change.
# The idle payload is mirrored in download_videos.yaml's dispatcher publish — keep in sync.
#
# Modes (one writer per moment: the download process itself):
#   begin                                       detached wrapper started -> ON, no item yet
#   item <extractor> <title> <uploader> <url>   yt-dlp `--exec before_dl:` — a video is about
#                                               to download -> ON + what it is
#   end                                         wrapper exited -> OFF + cleared
#   reconcile                                   publish the truth (PID marker alive or not);
#                                               idle clears the item, running only re-asserts
#                                               ON so it never wipes the current title. Run
#                                               every 10 min so a container killed mid-download
#                                               (backups stop containers) cannot leave a stale
#                                               "downloading" behind.
#
# NEVER fails a download: hard 3 s network cap, every error swallowed, always exit 0. yt-dlp
# aborts an item when an --exec command exits non-zero, so the caller also appends `|| true`.
# Troubleshooting: set METUBE_STATUS_DEBUG=1 to let the underlying error surface instead.

import json
import os
import socket
import struct
import sys
import time

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metube_mqtt.json")
TIMEOUT = 3
SOURCES = {"Youtube": "YouTube", "TwitchVod": "Twitch", "TwitchStream": "Twitch", "TwitchClips": "Twitch"}


def _utf(text):
    raw = text.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def _remaining_length(n):
    out = bytearray()
    while True:
        digit, n = n % 128, n // 128
        out.append(digit | 0x80 if n else digit)
        if not n:
            return bytes(out)


def publish(cfg, messages):
    """One short MQTT 3.1.1 session: CONNECT (user/pass) -> retained QoS0 PUBLISHes -> DISCONNECT."""
    connect_body = (
        _utf("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)  # level 4, user+pass+clean session
        + _utf(f"metube-status-{os.getpid()}") + _utf(cfg["user"]) + _utf(cfg["password"])
    )
    packets = [b"\x10" + _remaining_length(len(connect_body)) + connect_body]
    for topic, payload in messages:
        body = _utf(topic) + payload.encode("utf-8")
        packets.append(b"\x31" + _remaining_length(len(body)) + body)  # PUBLISH, retain, QoS 0
    with socket.create_connection((cfg["host"], int(cfg["port"])), TIMEOUT) as sock:
        sock.settimeout(TIMEOUT)
        sock.sendall(packets[0])
        connack = sock.recv(4)
        if len(connack) < 4 or connack[0] != 0x20 or connack[3] != 0:
            raise RuntimeError("broker refused the connection")
        for packet in packets[1:]:
            sock.sendall(packet)
        sock.sendall(b"\xe0\x00")  # DISCONNECT


def _clean(text, limit):
    text = "".join(ch for ch in str(text) if ch.isprintable()).strip()
    return text[:limit]


def _info(cfg, status, since="", title="", source="", uploader="", url=""):
    return json.dumps({
        "profile": cfg["profile"], "status": status, "since": since,
        "title": title, "source": source, "uploader": uploader, "url": url,
    })


def _since(cfg):
    try:
        return str(int(os.stat(cfg["state_file"]).st_mtime))  # wrapper start, same as the guard
    except OSError:
        return str(int(time.time()))


def _running(cfg):
    try:
        with open(cfg["state_file"]) as f:
            pid = f.read().strip()
        return bool(pid) and os.path.isdir(f"/proc/{pid}")
    except OSError:
        return False


def main(argv):
    cfg = json.load(open(CONFIG))
    state_topic, info_topic = f"{cfg['topic']}/state", f"{cfg['topic']}/info"
    mode = argv[1] if len(argv) > 1 else ""

    if mode == "begin":
        messages = [(state_topic, "ON"), (info_topic, _info(cfg, "downloading", _since(cfg)))]
    elif mode == "item" and len(argv) >= 6:
        extractor, title, uploader, url = argv[2:6]
        messages = [(state_topic, "ON"), (info_topic, _info(
            cfg, "downloading", _since(cfg), _clean(title, 300),
            SOURCES.get(extractor, _clean(extractor, 40)), _clean(uploader, 100), _clean(url, 500)))]
    elif mode == "end" or (mode == "reconcile" and not _running(cfg)):
        messages = [(state_topic, "OFF"), (info_topic, _info(cfg, "idle"))]
    elif mode == "reconcile":
        messages = [(state_topic, "ON")]
    else:
        return
    publish(cfg, messages)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:  # noqa: BLE001 — a tracker hiccup must never affect a download
        if os.environ.get("METUBE_STATUS_DEBUG"):  # troubleshooting only: surface the error
            raise
    sys.exit(0)
