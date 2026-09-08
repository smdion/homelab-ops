#!/usr/bin/env python3
"""Sync Alton Brown's "Cooks Food" YouTube series into Sonarr.

No Usenet/torrent indexer will ever have this content -- it's YouTube-exclusive.
This mirrors it into Sonarr's library directly, scoped to a single playlist
(not the general ~50-channel fast_check.py archive pipeline), with Sonarr's
own `hasFile` field as the sole incremental checkpoint (no separate archive/
state file to drift out of sync -- an episode is "needed" only if Sonarr
itself doesn't already have a file for it, checked fresh every run).

Runs on the liberty HOST (not inside a container, deployed by
deploy_alton_brown_sync.yaml): shells into the metube container for the
actual yt-dlp download (reuses its proven cookies/extractor-args config,
avoiding the YouTube bot-check issue plain yt-dlp hits), then moves the
finished file directly via the host's own /mnt/remotes mount into Sonarr's
library, then triggers a Sonarr rescan so it's picked up immediately.

Secrets and environment-specific paths are NOT hardcoded here -- this file
is committed to a public repo. deploy_alton_brown_sync.yaml renders a 0600
config file from vault at deploy time (same pattern deploy_fast_check.yaml
uses for its Discord webhook); this script only ever reads that rendered
file, never a literal secret.
"""
import configparser
import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

CONFIG_PATH = Path("/mnt/user/appdata/scripts/alton_brown_sync.conf")

_cfg = configparser.ConfigParser()
_cfg.read(CONFIG_PATH)
_c = _cfg["config"]

SONARR_URL = _c.get("sonarr_url", "http://localhost:8989/sonarr")
SONARR_KEY = _c["sonarr_key"]
SERIES_ID = _c.getint("series_id")
SERIES_FOLDER = _c.get("series_folder")
PLAYLIST_URL = _c.get("playlist_url")
COOKIES_PATH = _c.get("cookies_path", "/configs/default/www.youtube.com_cookies.txt")
STAGING_HOST_DIR = Path(_c.get("staging_host_dir", "/mnt/user/temp/alton_brown_sync"))
STAGING_CONTAINER_DIR = _c.get("staging_container_dir", "/tempvideo/alton_brown_sync")
TV_ROOT = Path(_c.get("tv_root")) / SERIES_FOLDER
DISCORD_WEBHOOK = _c.get("discord_webhook", "")

# Specials that don't live in the numbered playlist and have no episode
# number to auto-match -- add by hand the rare time one shows up.
MANUAL_OVERRIDES = json.loads(_c.get("manual_overrides", "[]"))

EPISODE_TITLE_RE = re.compile(r"Episode\s+(\d+)", re.IGNORECASE)


def sonarr_get(path):
    req = urllib.request.Request(f"{SONARR_URL}/api/v3/{path}", headers={"X-Api-Key": SONARR_KEY})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def sonarr_post(path, body):
    req = urllib.request.Request(
        f"{SONARR_URL}/api/v3/{path}", data=json.dumps(body).encode(),
        headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"}, method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def discord_notify(msg):
    if not DISCORD_WEBHOOK:
        return
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 alton-brown-sync"},
            data=json.dumps({"content": msg}).encode(),
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"WARNING: discord notify failed: {e}")


def list_playlist_videos():
    out = subprocess.run(
        ["sudo", "docker", "exec", "metube", "yt-dlp", "--flat-playlist", "-J", PLAYLIST_URL],
        capture_output=True, text=True, timeout=120,
    )
    data = json.loads(out.stdout)
    videos = []
    for entry in data.get("entries", []):
        title = entry.get("title", "")
        m = EPISODE_TITLE_RE.search(title)
        if not m:
            continue
        videos.append({"episode": int(m.group(1)), "url": f"https://www.youtube.com/watch?v={entry['id']}", "title": title})
    return videos


def build_needed(sonarr_episodes, playlist_videos):
    by_season_ep = {(e["seasonNumber"], e["episodeNumber"]): e for e in sonarr_episodes}
    needed = []

    for v in playlist_videos:
        key = (1, v["episode"])
        ep = by_season_ep.get(key)
        if ep and not ep.get("hasFile"):
            needed.append({"url": v["url"], "sonarr_episode": ep})

    for o in MANUAL_OVERRIDES:
        key = (o["season"], o["episode"])
        ep = by_season_ep.get(key)
        if ep and not ep.get("hasFile"):
            needed.append({"url": o["url"], "sonarr_episode": ep})

    return needed


def ensure_staging_dir():
    # Created via docker exec (not host-side Path.mkdir) so it's owned by
    # metube's own container user (99:100) from the start -- metube runs
    # non-root and can't write into a directory the host's ssh user created.
    subprocess.run(
        ["sudo", "docker", "exec", "metube", "mkdir", "-p", STAGING_CONTAINER_DIR],
        check=True, capture_output=True, text=True, timeout=10,
    )


def download(url, dest_container_dir):
    cmd = [
        "sudo", "docker", "exec", "metube", "yt-dlp",
        "-i", "-q",
        "--cookies", COOKIES_PATH,
        "--extractor-args", "youtube:player-client=default,-tv,web_safari,web_embedded",
        "--merge-output-format", "mp4",
        "-o", f"{dest_container_dir}/%(id)s.%(ext)s",
        "--print", "after_move:filepath",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {url}: {result.stderr[-500:]}")
    filepath = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return filepath


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "-", name)


def main():
    ensure_staging_dir()

    playlist_videos = list_playlist_videos()
    print(f"{len(playlist_videos)} numbered episodes found in playlist")

    sonarr_episodes = sonarr_get(f"episode?seriesId={SERIES_ID}")
    needed = build_needed(sonarr_episodes, playlist_videos)
    print(f"{len(needed)} episode(s) needed (missing in Sonarr)")

    if not needed:
        return

    fixed = []
    for item in needed:
        ep = item["sonarr_episode"]
        season, epnum, title = ep["seasonNumber"], ep["episodeNumber"], ep.get("title", "Episode")
        print(f"Downloading S{season:02d}E{epnum:02d} - {title} ...")
        try:
            container_path = download(item["url"], STAGING_CONTAINER_DIR)
        except Exception as e:
            print(f"  FAILED: {e}")
            discord_notify(f"⚠️ [alton-brown-sync] Failed to download S{season:02d}E{epnum:02d} - {title}: {e}")
            continue

        host_source = STAGING_HOST_DIR / Path(container_path).name
        if not host_source.exists():
            print(f"  FAILED: expected file not found at {host_source}")
            discord_notify(f"⚠️ [alton-brown-sync] Downloaded S{season:02d}E{epnum:02d} but couldn't find the output file")
            continue

        season_dir = TV_ROOT / f"Season {season:02d}"
        season_dir.mkdir(parents=True, exist_ok=True)
        ext = host_source.suffix
        final_name = f"{SERIES_FOLDER} - S{season:02d}E{epnum:02d} - {sanitize(title)}{ext}"
        final_path = season_dir / final_name

        # STAGING_HOST_DIR (/mnt/user/temp) and TV_ROOT (/mnt/remotes/...) are
        # different filesystems -- os.rename()/Path.rename() cannot cross
        # that boundary ("Invalid cross-device link"). shutil.move() falls
        # back to copy+delete automatically when rename() fails this way.
        shutil.move(str(host_source), str(final_path))
        print(f"  placed: {final_path}")
        fixed.append((season, epnum, title))

    if fixed:
        sonarr_post("command", {"name": "RescanSeries", "seriesId": SERIES_ID})
        lines = "\n".join(f"- S{s:02d}E{e:02d} - {t}" for s, e, t in fixed)
        discord_notify(f"\U0001f4fa [alton-brown-sync] Added {len(fixed)} episode(s):\n{lines}")
        print(f"Triggered Sonarr rescan. {len(fixed)} episode(s) added.")


if __name__ == "__main__":
    main()
