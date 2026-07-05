#!/usr/bin/env python3
# build_capture_message — builds the Semaphore trigger JSON payload for
# MeTube's capture Exec postprocessor (fires the instant a URL is added,
# whether via the bookmarklet or fast_check.py's dispatch).
#
# Title/uploader arrive as separate argv entries — yt-dlp's own %(field)q
# output-template conversion already shell-quotes them safely before this
# script is even invoked, so a title with spaces/quotes/etc. still arrives
# intact as one argument. This script then builds the JSON payload itself
# via json.dumps, so those same characters can't corrupt it the way
# hand-interpolating them into a JSON string literal would.
#
# Usage: build_capture_message.py <template_id> <project_id> [title] [uploader]

import json
import sys

template_id, project_id = int(sys.argv[1]), int(sys.argv[2])
title = sys.argv[3] if len(sys.argv) > 3 else ""
uploader = sys.argv[4] if len(sys.argv) > 4 else ""

message = f"{title} ({uploader})" if title else "metube capture"

print(json.dumps({"template_id": template_id, "project_id": project_id, "message": message}))
