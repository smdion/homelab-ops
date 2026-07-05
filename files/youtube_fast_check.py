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
#   2. Enumerate each channel via yt-dlp itself (--flat-playlist --print),
#      using PROBE_CONFIG (rendered from the real download_default/on_demand
#      vars — see templates/youtube_fast_probe.conf.j2). This means
#      --playlist-end/--dateafter/cookies/extractor-args/reject-title/live-filter
#      are all applied by yt-dlp natively — nothing here reimplements them, so
#      this can never drift from what the actual download would consider.
#   3. A video counts as "new" if it's in none of: the shared yt-dlp download
#      archive (already downloaded, by any profile), the pending queue (already
#      queued here), the dispatched set (already sent to MeTube, awaiting the
#      archive to confirm completion), or that channel's frozen first-pass
#      baseline (see 6). Reusing the existing archive instead of keeping a
#      second, ever-growing per-video "seen" list avoids duplicating state
#      that already exists.
#   4. New videos enter a pending queue. Due items get a quality check (yt-dlp -F,
#      same PROBE_CONFIG); if the target height is available, or the item has
#      aged past MAX_AGE_SECONDS, dispatch it; otherwise reschedule per
#      BACKOFF_SECONDS.
#   5. Dispatch = POST to MeTube's own /add endpoint (same call the bookmarklet
#      makes) — MeTube's existing capture config (skip_download + writedesktoplink
#      + Exec-fires-Semaphore) does everything downstream unmodified. The video
#      moves from pending to the dispatched set until it shows up in the archive.
#   6. A channel's very first pass freezes its current enumeration as a baseline
#      and queues nothing, so opting a channel in doesn't trigger a backlog
#      download (download_default's scan is the backstop for anything older —
#      or was, for YouTube, before this replaced that role). The archive only
#      records what was actually downloaded, not a channel's pre-existing
#      back-catalog — without this baseline, those old, never-downloaded videos
#      would look "new" on the very next check. Written once per channel and
#      never appended to again: bounded by (channel count x playlist-end), not
#      by time or upload frequency like a naive "seen" list would be.
#
# Usage (inside the metube container):
#   python3 youtube_fast_check.py [--dry-run] [--channel-list PATH] [--state-dir PATH]

import argparse
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import urllib.request

CHANNEL_LIST_DEFAULT = "/configs/default/channel_list"
ARCHIVE_DEFAULT = "/configs/default/downloaded"  # yt-dlp --download-archive, shared
                                                   # across every profile (channels,
                                                   # on_demand, and this fast path)
STATE_DIR_DEFAULT = "/configs/youtube_fast"
# Rendered by deploy_youtube_fast_check.yaml from the real download_default +
# download_on_demand vars (templates/youtube_fast_probe.conf.j2) — kept out of
# this script entirely so nothing here can drift from those rules; re-deploy
# to pick up changes.
PROBE_CONFIG_DEFAULT = "/configs/youtube_fast/probe.conf"
METUBE_ADD_URL = "http://localhost:8081/add"

MIN_HEIGHT = 1080
BACKOFF_SECONDS = [900, 1800, 3600, 7200, 14400, 28800]  # 15m,30m,1h,2h,4h,8h
MAX_AGE_SECONDS = 86400  # 24h — dispatch best-available rather than wait forever

# Persistent history, capped so it never grows unbounded: youtube_fast_check.log
# (current) plus up to LOG_BACKUP_COUNT rotated-out copies (.1, .2, ...) once the
# current file passes LOG_MAX_BYTES. Separate from User Scripts' own log.txt,
# which only ever holds the most recent single run.
LOG_MAX_BYTES = 1_000_000  # 1MB per file — at typical activity levels (~150-250
                            # lines/day idle-cadence), this alone covers 1-2 months
                            # before ever rotating, well past the visible tail window
LOG_BACKUP_COUNT = 3       # ~4MB ceiling total

_logger = logging.getLogger("youtube_fast_check")


def setup_logging(state_dir):
    _logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [youtube_fast_check] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(state_dir, "youtube_fast_check.log"),
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    _logger.addHandler(file_handler)

    # Keep stderr output too — this is what User Scripts' own per-run log.txt
    # captures, so the "last run" view in its GUI still works unchanged.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(logging.Formatter("[youtube_fast_check] %(message)s"))
    _logger.addHandler(stream_handler)


def log(msg):
    _logger.info(msg)


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


def read_download_archive(archive_path):
    """Return the set of YouTube video IDs yt-dlp has already downloaded."""
    ids = set()
    try:
        with open(archive_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] == "youtube":
                    ids.add(parts[1])
    except FileNotFoundError:
        pass
    return ids


def _probe_args(probe_config):
    """--config-location for probe.conf if it's been rendered, else nothing —
    a missing config degrades to an un-filtered probe rather than crashing."""
    return ["--config-location", probe_config] if os.path.isfile(probe_config) else []


def list_channel_videos(url, probe_config, timeout=60):
    """Enumerate a channel's videos via yt-dlp itself (--flat-playlist), so
    --playlist-end/--dateafter/etc from probe_config apply exactly as they
    would for the real download — nothing here re-decides scope on its own.
    Returns list of (video_id, video_url)."""
    proc = subprocess.run(
        ["yt-dlp", *_probe_args(probe_config), "--flat-playlist", "--skip-download",
         "--print", "%(id)s %(webpage_url)s", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "yt-dlp enumeration failed").strip().splitlines()[-1:] or "yt-dlp enumeration failed")
    out = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


HEIGHT_RE = re.compile(r"^\s*\S+\s+\S+\s+(\d+)x(\d+)", re.MULTILINE)


def max_available_height(url, probe_config, timeout=30):
    proc = subprocess.run(
        ["yt-dlp", *_probe_args(probe_config), "--skip-download", "-F", url],
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
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--state-dir", default=STATE_DIR_DEFAULT)
    ap.add_argument("--probe-config", default=PROBE_CONFIG_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="log actions, never call MeTube's /add")
    args = ap.parse_args()

    os.makedirs(args.state_dir, exist_ok=True)
    setup_logging(args.state_dir)
    seeded_channels_path = os.path.join(args.state_dir, "seeded_channels.json")
    pending_path = os.path.join(args.state_dir, "pending.json")
    dispatched_path = os.path.join(args.state_dir, "dispatched.json")

    # channel_url -> [video_id, ...]: the channel's enumeration snapshot at
    # first-pass time, frozen forever after that one write — NOT appended to on
    # later runs. See module docstring point 6 for why this exists.
    seeded_channels = load_json(seeded_channels_path, {})
    pending = load_json(pending_path, {})               # video_id -> {url, next_check_at, attempts, first_seen_at}
    dispatched = load_json(dispatched_path, {})         # video_id -> {url, dispatched_at} — awaiting archive

    now = time.time()
    seeded_dirty = False

    archive_ids = read_download_archive(args.archive)

    # Self-prune: once a dispatched video shows up in the archive, the real
    # download completed — stop tracking it. Anything still here after a long
    # time is a stuck/failed download, which the archive will eventually absorb
    # or which stays visible in this (small, in-flight-only) set for inspection.
    for video_id in list(dispatched.keys()):
        if video_id in archive_ids:
            del dispatched[video_id]

    urls = parse_youtube_channels(args.channel_list)
    log(f"{len(urls)} YouTube channel(s) in {args.channel_list}")

    new_count = 0
    for url in urls:
        try:
            entries = list_channel_videos(url, args.probe_config)
        except Exception as e:  # noqa: BLE001 — one bad channel must not stop the run
            log(f"enumeration failed for {url}: {e}")
            continue
        # First time we've ever checked this channel: freeze its current
        # enumeration as the baseline and queue nothing, so opting a channel in
        # doesn't trigger a backlog download.
        if url not in seeded_channels:
            seeded_channels[url] = [video_id for video_id, _ in entries]
            seeded_dirty = True
            continue
        baseline = seeded_channels.get(url, [])
        for video_id, video_url in entries:
            if (video_id in archive_ids or video_id in pending
                    or video_id in dispatched or video_id in baseline):
                continue
            pending[video_id] = {
                "url": video_url, "next_check_at": now,
                "attempts": 0, "first_seen_at": now,
            }
            new_count += 1
    if new_count:
        log(f"{new_count} new video(s) queued for quality check")

    dispatched_count, rescheduled = 0, 0
    for video_id in list(pending.keys()):
        item = pending[video_id]
        if item["next_check_at"] > now:
            continue
        age = now - item["first_seen_at"]
        try:
            height = max_available_height(item["url"], args.probe_config)
        except Exception as e:  # noqa: BLE001
            log(f"quality check failed for {item['url']}: {e}")
            height = 0
        ready = height >= MIN_HEIGHT or age >= MAX_AGE_SECONDS
        if ready:
            try:
                dispatch_to_metube(item["url"], args.dry_run)
                dispatched_count += 1
                log(f"dispatched {item['url']} (height={height}, age={int(age)}s)")
            except Exception as e:  # noqa: BLE001
                log(f"dispatch failed for {item['url']}: {e} (will retry next pass)")
                continue
            dispatched[video_id] = {"url": item["url"], "dispatched_at": now}
            del pending[video_id]
        else:
            attempts = item["attempts"]
            delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
            item["attempts"] = attempts + 1
            item["next_check_at"] = now + delay
            rescheduled += 1
    if dispatched_count or rescheduled:
        log(f"dispatched={dispatched_count} rescheduled={rescheduled} still-pending={len(pending)}")

    if seeded_dirty:
        atomic_write_json(seeded_channels_path, seeded_channels)
    atomic_write_json(pending_path, pending)
    atomic_write_json(dispatched_path, dispatched)


if __name__ == "__main__":
    main()
