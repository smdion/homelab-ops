#!/usr/bin/env python3
# check_image_updates — proactive "a newer version is available" detector for
# version-pinned Docker containers. Run on a docker host by maintain_health.yaml.
#
# For every RUNNING container whose image tag is a comparable version (auto-derived,
# never a hardcoded list), it asks the registry for the newest STABLE tag in the same
# version family and reports whether that is newer than what is running. Containers on
# rolling tags (latest, nightly, branch tags, …) and local/un-pullable images are
# skipped — they are handled by update_systems.yaml, not this proactive check.
#
# Registry access is via skopeo run as a throwaway container (no host install, and it
# handles pagination/auth uniformly across ghcr / docker.io / lscr):
#   docker run --rm <skopeo_image> list-tags docker://<repo>
#
# Output: a single JSON object on stdout:
#   {"results": [{"container","repo","current","latest","newer","error"}, ...]}
# The fragile version parsing lives here (testable in isolation), NOT in Jinja.
#
# Usage:
#   check_image_updates.py [--skopeo-image IMG] [--exclude a,b,c] [--timeout SECS]
#   --exclude matches a container NAME or a substring of the image ref.
#   Exit code is always 0 unless argument parsing fails; per-image problems are
#   reported as {"error": ...} so one bad image never fails the health run.

import argparse
import json
import re
import subprocess
import sys

# Tags that are rolling/branch pointers, not comparable versions.
ROLLING_TAGS = {
    "latest", "stable", "edge", "develop", "dev", "devel", "main", "master",
    "nightly", "rolling", "beta", "alpha", "canary", "test", "testing",
    "insiders", "unstable", "next", "release", "current", "",
}
# Pre-release / non-stable markers that disqualify a candidate upstream tag.
PRERELEASE_RE = re.compile(
    r"(?:^|[-._])(?:rc|alpha|beta|dev|devel|nightly|snapshot|snap|pre|preview|"
    r"test|testing|canary|insiders|unstable|next|edge|m\d+|b\d+|a\d+)(?:[-._]|\d|$)",
    re.IGNORECASE,
)
# A comparable version tag: optional leading v, dotted ints, optional -suffix
# (e.g. 2026.5.3, 16-alpine, 2, v1.2.3, 16.3-alpine). The suffix must be
# non-numeric-led so "16.3" splits as version, "16-alpine" as version+suffix.
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(-[A-Za-z][\w.\-]*)?$")


def parse_version(tag):
    """Return (components:tuple[int], suffix:str) for a comparable tag, else None."""
    m = VERSION_RE.match(tag)
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2) or ""
    return nums, suffix


def split_ref(image):
    """Split a docker image reference into (repo_without_tag, tag).

    Handles optional registry[:port]/ prefix and @sha256 digests. If no tag is
    present the tag is '' (rolling — will be skipped)."""
    ref = image.split("@", 1)[0]  # drop digest
    # Separate a possible :tag from the last path segment only (registry ports
    # contain ':' but always precede a '/').
    if "/" in ref:
        head, last = ref.rsplit("/", 1)
        if ":" in last:
            name, tag = last.rsplit(":", 1)
            return f"{head}/{name}", tag
        return ref, ""
    if ":" in ref:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, ""


def is_excluded(container, image, excludes):
    for ex in excludes:
        if not ex:
            continue
        if ex == container or ex in image:
            return True
    return False


def skopeo_tags(repo, skopeo_image, timeout):
    """Return list of upstream tags for repo, or raise on failure."""
    proc = subprocess.run(
        ["docker", "run", "--rm", skopeo_image, "list-tags", f"docker://{repo}"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"skopeo exit {proc.returncode}")
    return json.loads(proc.stdout).get("Tags", [])


def newest_in_family(cur_nums, cur_suffix, tags):
    """Newest stable upstream version tag in the same family (same component count
    and same suffix) as the current pin. Returns (tag, nums) or None."""
    best = None
    for t in tags:
        if PRERELEASE_RE.search(t):
            continue
        parsed = parse_version(t)
        if not parsed:
            continue
        nums, suffix = parsed
        # Same family: identical suffix and identical number of numeric components.
        if suffix != cur_suffix or len(nums) != len(cur_nums):
            continue
        if best is None or nums > best[1]:
            best = (t, nums)
    return best


def check_container(container, image, args):
    repo, tag = split_ref(image)
    if tag.lower() in ROLLING_TAGS:
        return None  # rolling — not our concern
    parsed = parse_version(tag)
    if not parsed:
        return None  # branch/word tag (e.g. preview-OIDC) — skip
    cur_nums, cur_suffix = parsed
    result = {"container": container, "repo": repo, "current": tag,
              "latest": tag, "newer": False, "error": None}
    try:
        tags = skopeo_tags(repo, args.skopeo_image, args.timeout)
    except subprocess.TimeoutExpired:
        result["error"] = "skopeo timeout"
        return result
    except Exception as e:  # noqa: BLE001 — any registry/parse failure is non-fatal
        result["error"] = str(e)[:200]
        return result
    best = newest_in_family(cur_nums, cur_suffix, tags)
    if best:
        result["latest"] = best[0]
        result["newer"] = best[1] > cur_nums
    return result


def running_containers():
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        if "\t" in line:
            name, image = line.split("\t", 1)
            out.append((name.strip(), image.strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skopeo-image", default="quay.io/skopeo/stable")
    ap.add_argument("--exclude", default="", help="comma list of container names or image substrings")
    ap.add_argument("--timeout", type=int, default=60, help="per-image skopeo timeout (s)")
    # Test hook: check a single image ref without docker ps.
    ap.add_argument("--image", help="check one image ref (container name defaults to '_test')")
    ap.add_argument("--container", default="_test")
    args = ap.parse_args()
    excludes = [e.strip() for e in args.exclude.split(",") if e.strip()]

    if args.image:
        pairs = [(args.container, args.image)]
    else:
        pairs = running_containers()

    results = []
    for name, image in pairs:
        if is_excluded(name, image, excludes):
            continue
        r = check_container(name, image, args)
        if r is not None:
            results.append(r)
    print(json.dumps({"results": results}))


if __name__ == "__main__":
    main()
