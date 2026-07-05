#!/usr/bin/env python3
# youtube_fast_check — background discovery + quality-gated dispatch for YouTube
# channels in channel_list, run inside the metube container via `docker exec` from
# a User Scripts cron entry on the unRAID host (liberty). No persistent process,
# no Ansible/Semaphore polling — each invocation is a single, stateless-between-runs
# pass; all state lives in JSON files under STATE_DIR so a crashed run just means
# the next cron tick picks up where the files left off.
#
# What each pass does:
#   1. Parse channel_list for YouTube entries (same file, same manual-add workflow
#      as the scheduled channel scan — nothing about that file changes).
#   2. Resolve + cache channel_id per URL (yt-dlp already extracts this; no API key
#      / quota needed). Only unresolved entries do this work.
#   3. Fetch each channel's public Atom feed (no auth) and diff against seen_ids to
#      find videos not seen before.
#   4. New videos enter a pending queue. Due items get a quality check (yt-dlp -F);
#      if the target height is available, or the item has aged past MAX_AGE_SECONDS,
#      dispatch it; otherwise reschedule per BACKOFF_SECONDS.
#   5. Dispatch = POST to MeTube's own /add endpoint (same call the bookmarklet
#      makes) — MeTube's existing capture config (skip_download + writedesktoplink
#      + Exec-fires-Semaphore) does everything downstream unmodified.
#
# Usage (inside the metube container):
#   python3 youtube_fast_check.py [--dry-run] [--channel-list PATH] [--state-dir PATH]

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

CHANNEL_LIST_DEFAULT = "/configs/default/channel_list"
STATE_DIR_DEFAULT = "/configs/youtube_fast"
METUBE_ADD_URL = "http://localhost:8081/add"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"

MIN_HEIGHT = 1080
BACKOFF_SECONDS = [900, 1800, 3600, 7200, 14400, 28800]  # 15m,30m,1h,2h,4h,8h
MAX_AGE_SECONDS = 86400  # 24h — dispatch best-available rather than wait forever

ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def log(msg):
    print(f"[youtube_fast_check] {msg}", file=sys.stderr)


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def parse_youtube_channels(channel_list_path):
    """Return the list of channel URLs listed under the '#YouTube' section."""
    urls = []
    section = None
    try:
        with open(channel_list_path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        log(f"channel list not found: {channel_list_path}")
        return urls
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip().lower()
            continue
        if section == "youtube" and stripped.startswith("http"):
            # Strip inline trailing "#nickname" annotations (e.g. "<url> #foo") —
            # only a leading "#" starts a section marker, per the check above.
            url = stripped.split("#", 1)[0].strip()
            urls.append(url)
    return urls


def resolve_channel_id(url, timeout=30):
    proc = subprocess.run(
        ["yt-dlp", "--skip-download", "--playlist-items", "1", "--print", "channel_id", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "yt-dlp failed").strip().splitlines()[-1:] or "yt-dlp failed")
    channel_id = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    if not channel_id:
        raise RuntimeError("no channel_id in yt-dlp output")
    return channel_id


def fetch_feed_video_ids(channel_id, timeout=20):
    """Return list of (video_id, url) from the channel's public Atom feed."""
    req = urllib.request.Request(
        FEED_URL.format(channel_id),
        headers={"User-Agent": "homelab-ops youtube_fast_check"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    root = ET.fromstring(body)
    out = []
    for entry in root.findall("a:entry", ATOM_NS):
        vid_el = entry.find("yt:videoId", ATOM_NS)
        link_el = entry.find("a:link", ATOM_NS)
        if vid_el is None or vid_el.text is None:
            continue
        video_id = vid_el.text.strip()
        url = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={video_id}"
        out.append((video_id, url))
    return out


HEIGHT_RE = re.compile(r"^\s*\S+\s+\S+\s+(\d+)x(\d+)", re.MULTILINE)


def max_available_height(url, timeout=30):
    proc = subprocess.run(
        ["yt-dlp", "--skip-download", "-F", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "yt-dlp -F failed").strip().splitlines()[-1:] or "yt-dlp -F failed")
    heights = [int(h) for (_, h) in HEIGHT_RE.findall(proc.stdout)]
    return max(heights) if heights else 0


def dispatch_to_metube(url, dry_run):
    if dry_run:
        log(f"[dry-run] would POST /add for {url}")
        return
    payload = json.dumps({"url": url, "quality": "best"}).encode()
    req = urllib.request.Request(
        METUBE_ADD_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel-list", default=CHANNEL_LIST_DEFAULT)
    ap.add_argument("--state-dir", default=STATE_DIR_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="log actions, never call MeTube's /add")
    args = ap.parse_args()

    os.makedirs(args.state_dir, exist_ok=True)
    channel_ids_path = os.path.join(args.state_dir, "channel_ids.json")
    seen_ids_path = os.path.join(args.state_dir, "seen_ids.json")
    pending_path = os.path.join(args.state_dir, "pending.json")

    channel_ids = load_json(channel_ids_path, {})   # url -> channel_id
    seen_ids = load_json(seen_ids_path, {})         # channel_id -> [video_id, ...]
    pending = load_json(pending_path, {})           # video_id -> {url, next_check_at, attempts, first_seen_at}

    now = time.time()
    channel_ids_dirty = False
    seen_ids_dirty = False

    urls = parse_youtube_channels(args.channel_list)
    log(f"{len(urls)} YouTube channel(s) in {args.channel_list}")

    for url in urls:
        if url in channel_ids:
            continue
        try:
            channel_ids[url] = resolve_channel_id(url)
            channel_ids_dirty = True
            log(f"resolved channel_id for {url} -> {channel_ids[url]}")
        except Exception as e:  # noqa: BLE001 — one bad channel must not stop the run
            log(f"failed to resolve channel_id for {url}: {e}")

    new_count = 0
    for url, channel_id in channel_ids.items():
        seen = set(seen_ids.get(channel_id, []))
        try:
            entries = fetch_feed_video_ids(channel_id)
        except Exception as e:  # noqa: BLE001
            log(f"feed fetch failed for {url} ({channel_id}): {e}")
            continue
        first_pass = channel_id not in seen_ids
        for video_id, video_url in entries:
            if video_id in seen:
                continue
            seen.add(video_id)
            seen_ids_dirty = True
            # First time we've ever checked this channel: seed seen_ids without
            # queuing anything, so opting a channel in doesn't trigger a backlog
            # download (download_default's scheduled scan is the backstop for that).
            if not first_pass and video_id not in pending:
                pending[video_id] = {
                    "url": video_url, "next_check_at": now,
                    "attempts": 0, "first_seen_at": now,
                }
                new_count += 1
        seen_ids[channel_id] = list(seen)
    if new_count:
        log(f"{new_count} new video(s) queued for quality check")

    dispatched, rescheduled = 0, 0
    for video_id in list(pending.keys()):
        item = pending[video_id]
        if item["next_check_at"] > now:
            continue
        age = now - item["first_seen_at"]
        try:
            height = max_available_height(item["url"])
        except Exception as e:  # noqa: BLE001
            log(f"quality check failed for {item['url']}: {e}")
            height = 0
        ready = height >= MIN_HEIGHT or age >= MAX_AGE_SECONDS
        if ready:
            try:
                dispatch_to_metube(item["url"], args.dry_run)
                dispatched += 1
                log(f"dispatched {item['url']} (height={height}, age={int(age)}s)")
            except Exception as e:  # noqa: BLE001
                log(f"dispatch failed for {item['url']}: {e} (will retry next pass)")
                continue
            del pending[video_id]
        else:
            attempts = item["attempts"]
            delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
            item["attempts"] = attempts + 1
            item["next_check_at"] = now + delay
            rescheduled += 1
    if dispatched or rescheduled:
        log(f"dispatched={dispatched} rescheduled={rescheduled} still-pending={len(pending)}")

    if channel_ids_dirty:
        atomic_write_json(channel_ids_path, channel_ids)
    if seen_ids_dirty:
        atomic_write_json(seen_ids_path, seen_ids)
    atomic_write_json(pending_path, pending)


if __name__ == "__main__":
    main()
