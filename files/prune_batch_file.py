#!/usr/bin/env python3
# prune_batch_file — keep download_videos.yaml's yt-dlp --batch-file from growing
# without bound. MeTube's capture flow only ever appends URLs to it; nothing
# removed them, so it accreted every URL ever queued (~470 lines) and every
# download run re-walked the whole list. Archived URLs skip fast, but URLs that
# never succeed (members-only / deleted / geo-blocked videos) are never archived
# and so get a full re-extraction attempt on every single run, forever.
#
# Run inside the metube container during the completion sweep (yt-dlp idle), when
# it is safe to rewrite the file and the archive has just grown. Two passes:
#   1. drop any URL whose video id is already in the download archive — a
#      guaranteed no-op to re-download, so removing it changes nothing.
#   2. cap what remains to the last MAX_KEEP lines (append order = oldest first).
#      Anything older has had MAX_KEEP+ runs to succeed; if it still has not, it
#      is a permanent failure, not a pending retry. Recent transient failures
#      stay and still retry next run.
#
# A URL wrongly dropped here is not lost data: channel content is re-dispatched
# by fast_check.py's reconciler, and a bookmarklet add can simply be re-added.
#
# Usage: prune_batch_file.py <batch_file> <archive_file> [max_keep]

import os
import re
import sys

MAX_KEEP_DEFAULT = 300

batch_file = sys.argv[1]
archive_file = sys.argv[2]
max_keep = int(sys.argv[3]) if len(sys.argv) > 3 else MAX_KEEP_DEFAULT

# yt-dlp archive lines are "<extractor> <id>", e.g. "youtube dQw4w9WgXcQ" or
# "twitchvod v2861387113". Keep both the raw id and a leading-"v"-stripped form
# so a twitch "/videos/<n>" URL (id "<n>") matches its "v<n>" archive entry.
archive_ids = set()
try:
    with open(archive_file) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2:
                vid = parts[1]
                archive_ids.add(vid)
                if vid.startswith("v") and vid[1:].isdigit():
                    archive_ids.add(vid[1:])
except FileNotFoundError:
    pass

# Pull a plausible video id out of a watch/permalink URL. Covers the shapes this
# pipeline actually sees (YouTube watch?v=, youtu.be/, Twitch /videos/<n>); an
# unrecognised URL yields no id and is always kept.
_ID_PATTERNS = [
    re.compile(r"[?&]v=([A-Za-z0-9_-]{6,})"),      # youtube watch?v=
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})"),  # youtu.be short
    re.compile(r"/videos/(\d+)"),                    # twitch vod
    re.compile(r"/shorts/([A-Za-z0-9_-]{6,})"),    # youtube shorts
]


def url_id(url):
    for pat in _ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


try:
    with open(batch_file) as f:
        lines = [ln.rstrip("\n") for ln in f]
except FileNotFoundError:
    print("no batch file")
    sys.exit(0)

before = len(lines)
kept = []
archived_pruned = 0
blanks = 0
for ln in lines:
    s = ln.strip()
    if not s or not s.startswith("http"):
        blanks += 1  # drop blanks / comments while we're here
        continue
    vid = url_id(s)
    if vid is not None and vid in archive_ids:
        archived_pruned += 1  # already downloaded — re-listing it is pointless
        continue
    kept.append(s)
capped = 0
if len(kept) > max_keep:
    capped = len(kept) - max_keep
    kept = kept[-max_keep:]

if archived_pruned == 0 and capped == 0 and blanks == 0:
    print("nothing to prune")
    sys.exit(0)

tmp = batch_file + ".tmp"
with open(tmp, "w") as f:
    f.write("\n".join(kept) + ("\n" if kept else ""))
os.replace(tmp, batch_file)
print(f"pruned {archived_pruned} archived + {capped} over-cap + {blanks} blank; {before} -> {len(kept)}")
