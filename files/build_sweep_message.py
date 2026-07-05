#!/usr/bin/env python3
# build_sweep_message — builds the Semaphore trigger JSON payload for the
# post-download sweep self-retrigger in download_videos.yaml, including the
# most recently downloaded video's title/uploader when the manifest has one.
# Run inside the metube container by the detached download wrapper, after
# yt-dlp finishes (manifest entries are written as each video completes, via
# metube.conf.j2's --print-to-file, so they're already there by this point).
#
# Usage: build_sweep_message.py <manifest_path> <template_id> <project_id>

import json
import sys

manifest_path, template_id, project_id = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

message = "post-download sweep"
try:
    with open(manifest_path) as f:
        lines = [line for line in f if line.strip()]
    if lines:
        last = json.loads(lines[-1])
        title = (last.get("title") or "")[:80]
        uploader = last.get("uploader") or ""
        if len(lines) > 1:
            message = f"{len(lines)} videos, latest: {title} ({uploader})"
        else:
            message = f"{title} ({uploader})"
except Exception:  # noqa: BLE001 — a malformed/missing manifest must never
    pass            # block the trigger; fall back to the generic message.

print(json.dumps({"template_id": template_id, "project_id": project_id, "message": message}))
