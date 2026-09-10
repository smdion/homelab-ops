#!/usr/bin/env python3
# plex_analyze_new_downloads — after a yt-dlp download completes, tell Plex to
# scan the relevant library section and force a full "analyze" pass on the new
# item(s) immediately, rather than waiting for Plex's own nightly maintenance
# task or (worse) doing the analysis on-the-fly the first time someone presses
# play. Confirmed via a real incident (2026-09-09): a video watched within
# hours of download transcoded on its first playback and direct-played cleanly
# on the second, minutes later — its Plex `updatedAt` timestamp matched the
# first playback to the minute, meaning Plex was still finishing analysis of
# a never-before-played file exactly as the transcode decision was made.
#
# Deliberately stdlib-only (urllib), matching tautulli_watched_cleanup.py's
# convention in this repo, and for the same reason: this runs inside the
# metube container, which gets recreated on every image update.
#
# Usage:
#   plex_analyze_new_downloads.py MANIFEST_JSON PLEX_URL PLEX_TOKEN SECTION_MAP
#     MANIFEST_JSON — JSON array of downloaded-video dicts (same shape as the
#                     per-video Discord notification loop already uses), each
#                     needs at least "extractor" and "id".
#     SECTION_MAP   — JSON object mapping extractor -> Plex library section id,
#                     e.g. '{"Youtube": "6", "TwitchVod": "3"}'
#
# Best-effort throughout: any failure here must never break the download
# pipeline's own sweep/notify flow, so every step logs and continues rather
# than raising past main().

import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_TIMEOUT = 30
POLL_ATTEMPTS = 10
POLL_DELAY_SECONDS = 3
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plex_analyze_new_downloads.log")

_logger = logging.getLogger("plex_analyze_new_downloads")


def setup_logging():
    _logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    file_handler.setFormatter(fmt)
    _logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    _logger.addHandler(stream_handler)


def plex_request(method, plex_url, path, token, params=None):
    params = dict(params or {})
    params["X-Plex-Token"] = token
    query = urllib.parse.urlencode(params)
    url = f"{plex_url}{path}?{query}"
    req = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def refresh_section(plex_url, token, section_id):
    plex_request("GET", plex_url, f"/library/sections/{section_id}/refresh", token)


def find_rating_key_by_video_id(plex_url, token, section_id, video_id):
    """yt-dlp's output template embeds " - {id}." in every filename, so an
    exact substring match on the file path is a reliable, unambiguous way to
    find the just-imported item without needing Plex's own metadata IDs."""
    body = plex_request("GET", plex_url, f"/library/sections/{section_id}/all", token,
                         params={"sort": "addedAt:desc", "X-Plex-Container-Size": "50"})
    data = json.loads(body)
    for item in data.get("MediaContainer", {}).get("Metadata", []):
        for media in item.get("Media", []):
            for part in media.get("Part", []):
                if f" - {video_id}." in (part.get("file") or ""):
                    return item.get("ratingKey")
    return None


def analyze_item(plex_url, token, rating_key):
    plex_request("PUT", plex_url, f"/library/metadata/{rating_key}/analyze", token)


def process_video(plex_url, token, section_id, video):
    video_id = video.get("id")
    title = video.get("title") or video_id
    if not video_id:
        _logger.warning("skipping entry with no 'id' field: %s", video)
        return

    for attempt in range(1, POLL_ATTEMPTS + 1):
        try:
            rating_key = find_rating_key_by_video_id(plex_url, token, section_id, video_id)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            _logger.error("lookup failed for '%s' (id=%s) on attempt %d: %s", title, video_id, attempt, e)
            return
        if rating_key:
            try:
                analyze_item(plex_url, token, rating_key)
                _logger.info("analyzed '%s' (id=%s, section=%s, ratingKey=%s) on attempt %d",
                             title, video_id, section_id, rating_key, attempt)
            except (urllib.error.URLError, OSError) as e:
                _logger.error("analyze call failed for '%s' (ratingKey=%s): %s", title, rating_key, e)
            return
        time.sleep(POLL_DELAY_SECONDS)

    _logger.warning("gave up waiting for '%s' (id=%s, section=%s) to appear after %d attempts",
                     title, video_id, section_id, POLL_ATTEMPTS)


def main():
    if len(sys.argv) != 5:
        sys.stderr.write("usage: plex_analyze_new_downloads.py MANIFEST_JSON PLEX_URL PLEX_TOKEN SECTION_MAP\n")
        sys.exit(1)

    setup_logging()
    manifest_json, plex_url, token, section_map_json = sys.argv[1:5]
    plex_url = plex_url.rstrip("/")

    try:
        videos = json.loads(manifest_json)
    except json.JSONDecodeError as e:
        _logger.error("could not parse manifest JSON: %s", e)
        sys.exit(0)  # best-effort — don't fail the pipeline over this

    try:
        section_map = json.loads(section_map_json)
    except json.JSONDecodeError as e:
        _logger.error("could not parse section map JSON: %s", e)
        sys.exit(0)

    if not videos:
        _logger.info("no videos in manifest, nothing to do")
        return

    refreshed_sections = set()
    for video in videos:
        extractor = video.get("extractor")
        section_id = section_map.get(extractor)
        if not section_id:
            _logger.warning("no configured Plex section for extractor '%s', skipping '%s'",
                             extractor, video.get("title"))
            continue
        if section_id not in refreshed_sections:
            try:
                refresh_section(plex_url, token, section_id)
                _logger.info("triggered refresh for section %s (extractor=%s)", section_id, extractor)
            except (urllib.error.URLError, OSError) as e:
                _logger.error("section refresh failed for section %s: %s", section_id, e)
            refreshed_sections.add(section_id)

    # Give the scan a moment to start picking up files before the first poll.
    time.sleep(POLL_DELAY_SECONDS)

    for video in videos:
        extractor = video.get("extractor")
        section_id = section_map.get(extractor)
        if section_id:
            process_video(plex_url, token, section_id, video)


if __name__ == "__main__":
    main()
