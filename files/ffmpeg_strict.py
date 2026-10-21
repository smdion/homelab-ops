#!/usr/bin/env python3
# ffmpeg_strict — the ffmpeg-downloader equivalent of yt-dlp's
# --abort-on-unavailable-fragments. Twitch VODs go through `--downloader m3u8:ffmpeg`
# (fMP4 init-segment order, see templates/metube.conf.j2), and ffmpeg's HLS demuxer
# never fails on a lost segment: it logs a warning, skips it, and exits 0. yt-dlp
# then archives a file with a hole in it and never retries.
#
# Deployed under both names, `ffmpeg` and `ffprobe`, in a directory passed to
# yt-dlp's --ffmpeg-location (it looks up both there). As ffprobe it simply execs
# the real binary. As ffmpeg it runs the real binary with stderr piped through,
# forwards every byte unchanged, and when a line matches a FATAL pattern it stops
# ffmpeg and exits 1 — yt-dlp reports "ffmpeg exited with code 1", the video is NOT
# archived, and the next run retries it with a fresh token.
#
# Patterns are the libavformat 8.1 messages for data that ffmpeg gave up on (all
# WARNING/ERROR level, so the downloader's -loglevel must stay at warning or lower):
#   hls.c  "Segment N of playlist N failed too many times, skipping" (open retries
#          exhausted — -seg_max_retry), "Failed to open an initialization section",
#          "Failed to reload playlist"
#   http.c "Stream ends prematurely" (connection closed mid-segment)
# Not catchable here: a read error mid-segment other than an early close (timeout,
# reset) ends that segment silently in hls.c with nothing logged. That is why the
# downloader deliberately sets no -rw_timeout — a hang is left to the
# download_videos.yaml stall watchdog, which kills it as a failure instead.
#
# The real ffmpeg is the first one on PATH outside this script's own directory.
# PR_SET_PDEATHSIG ties it to this wrapper, so a wrapper killed by yt-dlp or the
# watchdog never leaves an orphaned ffmpeg behind.

import ctypes
import os
import re
import shutil
import signal
import subprocess
import sys

FATAL = re.compile(
    rb"failed too many times, skipping"
    rb"|Failed to open an initialization section"
    rb"|Failed to reload playlist"
    rb"|Stream ends prematurely"
)

PR_SET_PDEATHSIG = 1


def real_binary(name):
    here = os.path.dirname(os.path.realpath(__file__))
    path = os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and os.path.realpath(d) != here
    )
    found = shutil.which(name, path=path)
    if not found:
        sys.exit(f"ffmpeg_strict: no real {name} on PATH")
    return found


def die_with_parent():
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


def main():
    name = os.path.basename(sys.argv[0])
    real = real_binary("ffprobe" if name == "ffprobe" else "ffmpeg")
    if name == "ffprobe":
        os.execv(real, [real] + sys.argv[1:])

    proc = subprocess.Popen([real] + sys.argv[1:], stderr=subprocess.PIPE,
                            preexec_fn=die_with_parent)
    out = sys.stderr.buffer
    pending = b""
    fatal = None
    while True:
        chunk = proc.stderr.read1(65536)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        pending += chunk
        *lines, pending = re.split(rb"[\r\n]", pending)
        for line in lines:
            if FATAL.search(line):
                fatal = line
                break
        if fatal:
            break

    if fatal is None:
        sys.exit(proc.wait())

    out.write(b"ffmpeg_strict: aborting, ffmpeg dropped data: " + fatal.strip() + b"\n")
    out.flush()
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    sys.exit(1)


if __name__ == "__main__":
    main()
