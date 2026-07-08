#!/usr/bin/env python3
# tautulli_watched_cleanup — delete a file (and its Plex metadata) some time
# after Tautulli reports it as watched, for "disposable" library sections
# (YouTube/Twitch VOD libraries fed by yt-dlp downloads). Configured as the
# script for a Tautulli "Watched" notification agent — NOT run on a poll
# loop. Tautulli has no built-in interval/cron trigger (checked against the
# actual source of the running instance, v2.17.2 — only event-based triggers
# exist), so this is event-driven instead: on_watched fires the script the
# moment an item is marked watched, and the script itself sleeps until
# MIN_HOURS_SINCE_WATCHED has elapsed before re-checking and deleting.
#
# This is safe to do because of how Tautulli actually dispatches Script
# notifications (confirmed by reading notifiers.py in the running
# container): SCRIPTS.agent_notify() launches run_script() in its own
# throwaway thread and returns immediately — it does not block Tautulli's
# notification queue workers. So one sleeping script instance per watched
# item does not delay or block any other notification. The Script agent's
# own per-script kill-timer (default 30s) must be set to 0 (no timeout) in
# its Configuration tab, or Tautulli will kill the process before the sleep
# finishes.
#
# Caveat: the pending delay lives only in this one sleeping process. If the
# Tautulli container restarts (image update, host reboot) while a deletion is
# pending, that pending deletion is lost silently — there is no persisted
# queue. Accepted trade-off for a simple, dependency-free implementation; see
# --scan mode below for a manual way to catch anything that fell through.
#
# Two modes:
#   Event mode (used by Tautulli itself):
#     tautulli_watched_cleanup.py RATING_KEY SECTION_ID [--dry-run]
#     Tautulli's Script Arguments field should be exactly: {rating_key} {section_id}
#   Scan mode (manual testing / one-off backfill, not scheduled by anything):
#     tautulli_watched_cleanup.py --scan [--dry-run]
#     Walks every configured section right now and reports/deletes anything
#     already past the threshold, without waiting.
#
# Config is entirely environment variables — no .env file of its own. Two
# sources, both wired up via this repo's IaC rather than anything bespoke:
#   - TAUTULLI_URL / TAUTULLI_APIKEY / PLEX_URL / PLEX_TOKEN are injected
#     automatically by Tautulli itself for every Script notification
#     (confirmed in notifiers.py's run_script()) — nothing to configure.
#   - TAUTULLI_CLEANUP_SECTION_IDS / TAUTULLI_CLEANUP_MIN_HOURS /
#     TAUTULLI_CLEANUP_LOG_FILE come from the "media" stack's own
#     docker-compose .env, defined in this repo at
#     vars/definitions/container_definitions.yaml (tautulli.env /
#     tautulli.compose.environment) and rendered by templates/env.j2 —
#     the same mechanism every other container's custom env vars use.
#     For manual/CLI testing outside the container, just export these
#     seven vars yourself before running.
#
# Deliberately stdlib-only (urllib), not requests: this runs inside the
# linuxserver/tautulli container, which gets recreated on every image update
# (pip installing into it would vanish on the next pull). Nothing to install
# means nothing to lose.

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUIRED_ENV = ["TAUTULLI_URL", "TAUTULLI_APIKEY", "PLEX_URL", "PLEX_TOKEN", "TAUTULLI_CLEANUP_SECTION_IDS"]
DEFAULT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tautulli_watched_cleanup.log")

REQUEST_TIMEOUT = 30  # seconds, per HTTP call
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

_logger = logging.getLogger("tautulli_watched_cleanup")


def load_config():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.stderr.write(
            f"tautulli_watched_cleanup: missing required environment variable(s): {', '.join(missing)}\n"
        )
        sys.exit(1)

    section_ids = [s.strip() for s in os.environ["TAUTULLI_CLEANUP_SECTION_IDS"].split(",") if s.strip()]
    if not section_ids:
        sys.stderr.write("tautulli_watched_cleanup: TAUTULLI_CLEANUP_SECTION_IDS resolved to an empty list\n")
        sys.exit(1)

    return {
        "TAUTULLI_URL": os.environ["TAUTULLI_URL"].rstrip("/"),
        "TAUTULLI_API_KEY": os.environ["TAUTULLI_APIKEY"],
        "PLEX_URL": os.environ["PLEX_URL"].rstrip("/"),
        "PLEX_TOKEN": os.environ["PLEX_TOKEN"],
        "SECTION_IDS": section_ids,
        "MIN_HOURS_SINCE_WATCHED": float(os.environ.get("TAUTULLI_CLEANUP_MIN_HOURS", "6")),
        "LOG_FILE": os.environ.get("TAUTULLI_CLEANUP_LOG_FILE", DEFAULT_LOG_FILE),
    }


def setup_logging(log_file):
    _logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    _logger.addHandler(file_handler)

    # Also echo to stderr so manual test runs show output immediately without
    # having to tail the log file in a second terminal.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    _logger.addHandler(stream_handler)


class TautulliError(Exception):
    pass


class RequestError(Exception):
    """Wraps any network/HTTP-level failure (unreachable host, timeout, non-2xx
    status) so callers can catch one thing regardless of the urllib exception
    shape underneath."""
    pass


def http_get_json(url, params, timeout=REQUEST_TIMEOUT):
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise RequestError(str(e)) from e


def http_delete(url, params, timeout=REQUEST_TIMEOUT):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except (urllib.error.URLError, OSError) as e:
        raise RequestError(str(e)) from e


def tautulli_request(config, cmd, **params):
    params = {"apikey": config["TAUTULLI_API_KEY"], "cmd": cmd, **params}
    body = http_get_json(f"{config['TAUTULLI_URL']}/api/v2", params)
    payload = body.get("response", {})
    if payload.get("result") != "success":
        raise TautulliError(payload.get("message") or f"Tautulli API error running {cmd}")
    return payload.get("data")


def get_item_metadata(config, rating_key):
    """One get_metadata call gives us everything needed: title, section_id,
    last_viewed_at (watched-state, for the event-mode re-check) and file
    path(s) via media_info -> parts -> file."""
    return tautulli_request(config, "get_metadata", rating_key=rating_key) or {}


def file_paths_from_metadata(data):
    paths = []
    for media in data.get("media_info", []) or []:
        for part in media.get("parts", []) or []:
            file_path = part.get("file")
            if file_path:
                paths.append(file_path)
    return paths


def delete_plex_metadata(config, rating_key):
    http_delete(
        f"{config['PLEX_URL']}/library/metadata/{rating_key}",
        {"X-Plex-Token": config["PLEX_TOKEN"]},
    )


def delete_item(config, rating_key, title, paths, dry_run):
    if not paths:
        _logger.warning("skipping '%s' (rating_key=%s): no file paths returned by Tautulli", title, rating_key)
        return

    if dry_run:
        _logger.info("[dry-run] would delete file(s) %s and Plex metadata for '%s' (rating_key=%s)",
                     paths, title, rating_key)
        return

    any_deleted = False
    for path in paths:
        try:
            os.remove(path)
            _logger.info("deleted file: %s", path)
            any_deleted = True
        except FileNotFoundError:
            _logger.warning("file already gone, continuing: %s", path)
        except OSError as e:
            _logger.error("failed to delete file %s: %s", path, e)

    try:
        delete_plex_metadata(config, rating_key)
        _logger.info("removed Plex metadata for '%s' (rating_key=%s)", title, rating_key)
    except RequestError as e:
        _logger.error("failed to remove Plex metadata for '%s' (rating_key=%s): %s", title, rating_key, e)
        return

    if any_deleted:
        _logger.info("cleanup complete for '%s' (rating_key=%s)", title, rating_key)


def event_mode(config, rating_key, section_id, dry_run):
    if section_id not in config["SECTION_IDS"]:
        _logger.debug("ignoring watched event for rating_key=%s: section %s not configured", rating_key, section_id)
        return

    min_hours = config["MIN_HOURS_SINCE_WATCHED"]
    _logger.info("watched event received: rating_key=%s section=%s — will re-check and delete in %.1fh",
                 rating_key, section_id, min_hours)

    if dry_run:
        try:
            data = get_item_metadata(config, rating_key)
        except (RequestError, TautulliError) as e:
            _logger.error("[dry-run] could not look up rating_key=%s: %s", rating_key, e)
            return
        title = data.get("title") or rating_key
        paths = file_paths_from_metadata(data)
        _logger.info("[dry-run] would sleep %.1fh, then re-check watched state and delete "
                     "file(s) %s + Plex metadata for '%s' (rating_key=%s)", min_hours, paths, title, rating_key)
        return

    time.sleep(min_hours * 3600)

    try:
        data = get_item_metadata(config, rating_key)
    except (RequestError, TautulliError) as e:
        _logger.error("could not re-check rating_key=%s after waiting, aborting deletion: %s", rating_key, e)
        return

    title = data.get("title") or rating_key
    if not data.get("last_viewed_at"):
        _logger.info("'%s' (rating_key=%s) no longer marked watched, skipping deletion", title, rating_key)
        return

    paths = file_paths_from_metadata(data)
    delete_item(config, rating_key, title, paths, dry_run=False)


def is_eligible(item, min_hours, now):
    play_count = item.get("play_count") or 0
    last_played = item.get("last_played")
    if play_count <= 0 or not last_played:
        return False, "not yet marked watched"
    hours_since = (now - int(last_played)) / 3600
    if hours_since < min_hours:
        return False, f"watched {hours_since:.1f}h ago, below {min_hours}h threshold"
    return True, f"watched {hours_since:.1f}h ago"


def get_section_items(config, section_id):
    """Page through get_library_media_info for one section. refresh=1 asks
    Tautulli to pull live watched-state from Plex first, rather than serving
    its own (potentially stale) cached table."""
    items = []
    start = 0
    length = 500
    while True:
        data = tautulli_request(
            config, "get_library_media_info",
            section_id=section_id, refresh=1,
            start=start, length=length,
        )
        rows = (data or {}).get("data", [])
        items.extend(rows)
        total = (data or {}).get("recordsFiltered", len(items))
        start += len(rows)
        if not rows or start >= total:
            break
    return items


def scan_mode(config, dry_run):
    """Manual testing / backfill: walk every configured section right now
    and act on anything already past the threshold. Not scheduled by
    anything — event_mode (driven by Tautulli's on_watched trigger) is the
    real mechanism; this is a hand-run catch-up tool."""
    now = time.time()
    total_items = 0
    total_eligible = 0
    for section_id in config["SECTION_IDS"]:
        try:
            items = get_section_items(config, section_id)
        except (RequestError, TautulliError) as e:
            _logger.error("could not fetch items for section %s, skipping it: %s", section_id, e)
            continue

        _logger.info("%d item(s) in section %s", len(items), section_id)
        total_items += len(items)

        for item in items:
            eligible, reason = is_eligible(item, config["MIN_HOURS_SINCE_WATCHED"], now)
            title = item.get("title") or item.get("rating_key")
            rating_key = item.get("rating_key")
            if not eligible:
                _logger.debug("skipping '%s' (section %s): %s", title, section_id, reason)
                continue
            total_eligible += 1
            _logger.info("eligible for deletion: '%s' (section %s, %s)", title, section_id, reason)
            try:
                data = get_item_metadata(config, rating_key)
                paths = file_paths_from_metadata(data)
            except (RequestError, TautulliError) as e:
                _logger.error("skipping '%s' (rating_key=%s): failed to look up file path: %s", title, rating_key, e)
                continue
            delete_item(config, rating_key, title, paths, dry_run)

    _logger.info("=== scan complete: %d eligible item(s) out of %d across %d section(s) ===",
                 total_eligible, total_items, len(config["SECTION_IDS"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rating_key", nargs="?", help="Plex rating_key of the watched item (event mode)")
    ap.add_argument("section_id", nargs="?", help="Tautulli section_id the item belongs to (event mode)")
    ap.add_argument("--scan", action="store_true",
                     help="check every configured section right now instead of a single item (manual testing/backfill)")
    ap.add_argument("--dry-run", action="store_true", help="log actions without waiting or deleting anything")
    args = ap.parse_args()

    if not args.scan and not (args.rating_key and args.section_id):
        ap.error("either --scan, or both RATING_KEY and SECTION_ID, are required")

    config = load_config()
    setup_logging(config["LOG_FILE"])

    if args.scan:
        _logger.info("=== scan start (dry-run=%s, sections=%s, min_hours=%s) ===",
                     args.dry_run, ",".join(config["SECTION_IDS"]), config["MIN_HOURS_SINCE_WATCHED"])
        scan_mode(config, args.dry_run)
    else:
        event_mode(config, args.rating_key, args.section_id, args.dry_run)


if __name__ == "__main__":
    main()
