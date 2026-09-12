#!/usr/bin/env python3
# fast_check — background discovery + quality-gated dispatch for channels in
# channel_list, run inside the metube container via `docker exec` from a User
# Scripts cron entry on the unRAID host (liberty). No persistent process, no
# Ansible/Semaphore polling — each invocation is a single, stateless-between-runs
# pass; all state lives in JSON files under STATE_DIR so a crashed run just means
# the next cron tick picks up where the files left off.
#
# Platform-pluggable: --platform selects which channel_list section to read and
# which archive extractor label counts as "already downloaded" (see PLATFORMS
# below). Enumeration and quality-checking themselves are NOT platform-specific
# code paths — yt-dlp handles site detection from the URL itself; what varies
# per platform is only the rendered --probe-config (different cookies/
# extractor-args per site) and state paths, both supplied by the caller
# (deploy_fast_check.yaml's per-platform wrapper script), not hardcoded here.
#
# What each pass does:
#   1. Parse channel_list for the selected platform's section (same file, same
#      manual-add workflow as the old scheduled channel scan — nothing about
#      that file changes).
#   2. Enumerate each channel via yt-dlp itself (--flat-playlist --print),
#      using PROBE_CONFIG (rendered per-platform from the real download config
#      vars — see templates/fast_check_probe_<platform>.conf.j2). This means
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
#   6. Reconcile the dispatched set each pass: an entry that shows up in the
#      archive is cleared (download done); one that has sat there past
#      DISPATCH_TIMEOUT_SECONDS without reaching the archive is treated as a
#      failed/interrupted download and re-queued (bounded by MAX_REDISPATCH,
#      then a one-shot Discord alert). This is the only retry path — "Download
#      — Videos" itself carries no schedule/backstop.
#   7. A channel's very first pass freezes its current enumeration as a baseline
#      and queues nothing, so opting a channel in doesn't trigger a backlog
#      download (the old scheduled scan is the backstop for anything older, for
#      whichever platforms still rely on it). The archive only records what was
#      actually downloaded, not a channel's pre-existing back-catalog — without
#      this baseline, those old, never-downloaded videos would look "new" on
#      the very next check. Written once per channel and never appended to
#      again: bounded by (channel count x playlist-end), not by time or upload
#      frequency like a naive "seen" list would be.
#
# Usage (inside the metube container):
#   python3 fast_check.py --platform youtube [--dry-run] [--state-dir PATH] ...

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

# platform -> (channel_list section marker, archive extractor label)
# verify_duration: run the post-archive duration-sanity check below (see
# DURATION_MISMATCH_THRESHOLD) — scoped to platforms where a live-capture can
# exit cleanly without actually finishing (Twitch: a dropped CDN token or
# network blip can make yt-dlp's HLS poller conclude "the stream ended" and
# write a normal archive entry for a fraction of the real VOD). YouTube VODs
# aren't live-captured the same way here, so left off for now.
PLATFORMS = {
    "youtube": {"section": "youtube", "archive_label": "youtube", "verify_duration": False},
    "twitch": {"section": "twitch", "archive_label": "twitchvod", "verify_duration": True},
}

CHANNEL_LIST_DEFAULT = "/configs/default/channel_list"
ARCHIVE_DEFAULT = "/configs/default/downloaded"  # yt-dlp --download-archive, shared
                                                   # across every profile and platform
METUBE_ADD_URL = "http://localhost:8081/add"
DOWNLOADS_ROOT = "/downloads"  # matches the metube container's own /downloads mount

# An archive entry only proves yt-dlp's process exited cleanly, not that it
# captured the whole thing (see PLATFORMS.verify_duration comment above). Once
# the source is no longer live, compare the real on-disk file's duration
# against the source's own now-final duration; anything below this fraction is
# treated the same as a failed dispatch (re-queued, bounded by MAX_REDISPATCH).
# 0.85 leaves headroom for normal minor discrepancies (yt-dlp/platform duration
# rounding, a few seconds of stream start-up lag) without masking a real
# multi-hour truncation like the one that motivated this check.
DURATION_MISMATCH_THRESHOLD = 0.85

MIN_HEIGHT = 1080
BACKOFF_SECONDS = [900, 1800, 3600, 7200, 14400, 28800]  # 15m,30m,1h,2h,4h,8h
MAX_AGE_SECONDS = 86400  # 24h — dispatch best-available rather than wait forever

# Reconciliation for a dispatch that never completed downstream. A dispatched
# video is normally cleared from the dispatched set only by showing up in the
# shared download archive. If the real download (Semaphore "Download — Videos"
# -> detached yt-dlp) fails or is killed mid-run — a fragment abort on an
# expired Twitch CDN token, the metube container being recreated, the Semaphore
# API being unreachable when the wrapper tries to re-trigger the sweep — nothing
# else ever retries it or surfaces it, because "Download — Videos" carries no
# schedule/backstop by design. So: if an entry has sat in the dispatched set
# this long without reaching the archive, treat the download as failed and
# re-queue it (fresh quality check -> fresh /add -> fresh CDN token). Bounded:
# after MAX_REDISPATCH attempts, give up, fire one Discord alert, and leave it
# in the dispatched set (visible, not retried forever).
DISPATCH_TIMEOUT_SECONDS = 8 * 3600  # a real ~35GB 1080p60 VOD download finishes
                                     # well inside this (archive-file mtimes: ~1h),
                                     # with headroom for a few serially-queued runs
MAX_REDISPATCH = 3

# Persistent history, capped so it never grows unbounded: fast_check.log
# (current) plus up to LOG_BACKUP_COUNT rotated-out copies (.1, .2, ...) once the
# current file passes LOG_MAX_BYTES. Separate from User Scripts' own log.txt,
# which only ever holds the most recent single run.
LOG_MAX_BYTES = 1_000_000  # 1MB per file — at typical activity levels (~150-250
                            # lines/day idle-cadence), this alone covers 1-2 months
                            # before ever rotating, well past the visible tail window
LOG_BACKUP_COUNT = 3       # ~4MB ceiling total

_logger = logging.getLogger("fast_check")


def setup_logging(state_dir, platform):
    _logger.setLevel(logging.INFO)
    fmt = logging.Formatter(f"%(asctime)s [fast_check:{platform}] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(state_dir, "fast_check.log"),
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    _logger.addHandler(file_handler)

    # Keep stderr output too — this is what User Scripts' own per-run log.txt
    # captures, so the "last run" view in its GUI still works unchanged.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(logging.Formatter(f"[fast_check:{platform}] %(message)s"))
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


def parse_channel_urls(channel_list_path, section):
    """Return the list of channel URLs listed under the given channel_list
    section marker (e.g. '#YouTube', '#Twitch' -> section='youtube'/'twitch')."""
    urls = []
    current_section = None
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
            current_section = stripped.lstrip("#").strip().lower()
            continue
        if current_section == section and stripped.startswith("http"):
            # Strip inline trailing "#nickname" annotations (e.g. "<url> #foo") —
            # only a leading "#" starts a section marker, per the check above.
            url = stripped.split("#", 1)[0].strip()
            urls.append(url)
    return urls


def read_download_archive(archive_path, extractor_label):
    """Return the set of video IDs yt-dlp has already downloaded for this
    platform's extractor (archive lines are "<extractor> <id>")."""
    ids = set()
    try:
        with open(archive_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] == extractor_label:
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
    Site detection is yt-dlp's own, from the URL — no platform-specific code
    path needed here. Returns list of (video_id, video_url)."""
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


def get_source_duration(url, probe_config, timeout=30):
    """Return (duration_seconds, is_live) for the source video right now. Used
    only after an archive entry already exists, to find out whether the
    source has actually finished (is_live False/None) and, if so, how long it
    really is — the only point at which "duration" is a meaningful number for
    something that was live when we dispatched it."""
    proc = subprocess.run(
        ["yt-dlp", *_probe_args(probe_config), "--skip-download", "-j", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "yt-dlp -j failed").strip().splitlines()[-1:] or "yt-dlp -j failed")
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    return info.get("duration"), bool(info.get("is_live"))


def find_downloaded_file(video_id):
    """Locate the actual file MeTube wrote for this video_id. MeTube's own
    /history 'filename' field can lag the real on-disk name (folder/minute-
    suffix can shift slightly for a live capture between dispatch and finish —
    seen in practice), so search by the " - v<id>.<ext>" suffix MeTube always
    appends instead of trusting that field verbatim."""
    suffix = f" - v{video_id}."
    for root, _dirs, files in os.walk(DOWNLOADS_ROOT):
        for name in files:
            if suffix in name:
                return os.path.join(root, name)
    return None


def get_file_duration(path, timeout=30):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError((proc.stderr or "ffprobe failed").strip().splitlines()[-1:] or "ffprobe failed")
    return float(proc.stdout.strip())


def duration_check(video_id, url, probe_config):
    """Compare the real on-disk file against the source's final duration, once
    the source has one to compare against. Returns one of:
      "ok"          — captured length is within DURATION_MISMATCH_THRESHOLD
      "truncated"   — source has finished and the file falls well short of it
      "unresolved"  — source is still live, or a lookup failed; can't judge
                      yet, so the caller should leave this entry in place for
                      a later pass rather than either clearing or failing it
                      (an entry stuck "unresolved" still eventually hits the
                      existing DISPATCH_TIMEOUT_SECONDS bound below, so this
                      can't hang forever)
    """
    try:
        source_duration, is_live = get_source_duration(url, probe_config)
    except Exception as e:  # noqa: BLE001
        log(f"duration-check: source lookup failed for {url}: {e}")
        return "unresolved"
    if is_live or not source_duration:
        return "unresolved"  # source hasn't concluded yet — nothing final to compare
    path = find_downloaded_file(video_id)
    if not path:
        log(f"duration-check: no on-disk file found for {video_id} ({url})")
        return "unresolved"
    try:
        file_duration = get_file_duration(path)
    except Exception as e:  # noqa: BLE001
        log(f"duration-check: ffprobe failed for {path}: {e}")
        return "unresolved"
    ratio = file_duration / source_duration
    if ratio < DURATION_MISMATCH_THRESHOLD:
        log(f"WARNING: {url} captured {int(file_duration)}s of {int(source_duration)}s "
            f"source ({ratio:.0%}) — treating as truncated")
        return "truncated"
    return "ok"


def alert_discord(message, webhook_url):
    """Best-effort Discord alert for a give-up condition. No-op when no webhook
    is configured (local runs, dry runs). Never raises — a failed alert must not
    break the reconciliation pass."""
    if not webhook_url:
        return
    try:
        payload = json.dumps({
            "username": "fast_check",
            "embeds": [{"description": message, "color": 0xE67E22}],
        }).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001
        log(f"discord alert failed: {e}")


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
    ap.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    ap.add_argument("--channel-list", default=CHANNEL_LIST_DEFAULT)
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--state-dir", default=None, help="defaults to /configs/<platform>_fast")
    ap.add_argument("--probe-config", default=None, help="defaults to <state-dir>/probe.conf")
    ap.add_argument("--dry-run", action="store_true", help="log actions, never call MeTube's /add")
    ap.add_argument("--alert-webhook-file", default=None,
                    help="file containing a Discord webhook URL for give-up alerts; "
                         "defaults to <state-dir>/alert_webhook if present")
    args = ap.parse_args()

    platform_cfg = PLATFORMS[args.platform]
    if args.state_dir is None:
        args.state_dir = f"/configs/{args.platform}_fast"
    if args.probe_config is None:
        args.probe_config = os.path.join(args.state_dir, "probe.conf")
    if args.alert_webhook_file is None:
        args.alert_webhook_file = os.path.join(args.state_dir, "alert_webhook")
    alert_webhook = ""
    if os.path.isfile(args.alert_webhook_file):
        with open(args.alert_webhook_file) as f:
            alert_webhook = f.read().strip()

    os.makedirs(args.state_dir, exist_ok=True)
    setup_logging(args.state_dir, args.platform)
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

    archive_ids = read_download_archive(args.archive, platform_cfg["archive_label"])

    # Self-prune + reconcile. Once a dispatched video shows up in the archive the
    # real download completed — stop tracking it, UNLESS this platform verifies
    # duration (see PLATFORMS.verify_duration) and the on-disk file falls well
    # short of the source's real (now-final) duration, meaning yt-dlp's process
    # exited cleanly without actually capturing the whole thing. Either that, or
    # sitting here past DISPATCH_TIMEOUT_SECONDS without ever reaching the
    # archive, means the real download failed or was killed mid-run: re-queue it
    # so it goes through a fresh quality check -> /add -> download with a fresh
    # CDN token. After MAX_REDISPATCH attempts, give up — fire one Discord alert
    # and leave the entry here (visible, no longer retried).
    for video_id in list(dispatched.keys()):
        entry = dispatched[video_id]
        failure_reason = None
        past_timeout = now - entry["dispatched_at"] >= DISPATCH_TIMEOUT_SECONDS
        if video_id in archive_ids:
            if not platform_cfg["verify_duration"]:
                del dispatched[video_id]
                continue
            result = duration_check(video_id, entry["url"], args.probe_config)
            if result == "ok":
                del dispatched[video_id]
                continue
            if result == "unresolved":
                if not past_timeout:
                    continue  # can't judge yet — leave in place for a later pass
                failure_reason = "in archive but never resolved (still live, or repeated lookup failures)"
            else:
                failure_reason = "captured file is far shorter than the source — suspected truncated capture"
        elif past_timeout:
            failure_reason = "dispatched but never reached the archive"
        else:
            continue

        redispatch_count = entry.get("redispatch_count", 0)
        stale_h = int((now - entry["dispatched_at"]) / 3600)
        if redispatch_count < MAX_REDISPATCH:
            log(f"WARNING: {entry['url']} {failure_reason} ({stale_h}h) — "
                f"re-queueing (re-dispatch {redispatch_count + 1}/{MAX_REDISPATCH})")
            pending[video_id] = {
                "url": entry["url"], "next_check_at": now,
                "attempts": 0, "first_seen_at": now,
                "redispatch_count": redispatch_count + 1,
            }
            del dispatched[video_id]
        elif not entry.get("alerted"):
            log(f"ERROR: {entry['url']} still not downloaded after {MAX_REDISPATCH} "
                f"re-dispatches ({stale_h}h) — giving up; re-add it manually if still wanted")
            alert_discord(
                f"fast_check [{args.platform}]: **{entry['url']}** failed to download "
                f"after {MAX_REDISPATCH} re-dispatches ({stale_h}h stale, {failure_reason}). "
                f"Manual re-add needed.",
                alert_webhook,
            )
            entry["alerted"] = True

    urls = parse_channel_urls(args.channel_list, platform_cfg["section"])
    log(f"{len(urls)} {args.platform} channel(s) in {args.channel_list}")

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
            dispatched[video_id] = {
                "url": item["url"], "dispatched_at": now,
                "redispatch_count": item.get("redispatch_count", 0),
            }
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
