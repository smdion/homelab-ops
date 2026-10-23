#!/usr/bin/env python3
# container_watch — decide which containers that SHOULD be running are not, and what is new or recovered since the
# last run. Pure logic, run on the controller by maintain_containers.yaml (which does the probing and the DB I/O).
#
# Input (JSON file, --input):
#   {"hosts": {"<host>": {"skip": "maintenance"|"unreachable"|"",
#                         "ps": [{"Names": "...", "State": "running"|"exited"|...}, ...],      # docker ps -a
#                         "restart": {"<container>": "always"|"unless-stopped"|"no"|...},      # restart policies
#                         "stacks": {"<stack>": <docker compose config --format json> | null},  # compose hosts
#                         "declared": ["<container>", ...]}},                                   # unRAID unraid_containers
#    "state": [{"hostname","container","problem","seen_count","notified"}, ...],                # container_watch rows
#    "exclude": ["<host>/<container>" or "<container>", ...], "confirm_runs": 2}
#
# What SHOULD run on a host:
#   compose host  every service of every stack whose restart policy is always / unless-stopped. `docker compose config`
#                 already drops services whose profile is not active on that host (e.g. hub-only containers).
#   unRAID host   the declared web-UI containers, plus any existing container with restart always / unless-stopped.
# A problem is: missing (no such container), or present but not running (exited / created / restarting / paused / dead).
#
# Debounce: a problem must be seen on `confirm_runs` consecutive runs before it is reported, so a deploy that recreates a
# container mid-probe never alerts. Skipped hosts (maintenance flag, unreachable) keep their rows untouched.
#
# Output (stdout JSON): {"new": [...], "recovered": [...], "ongoing": [...], "state": [rows for the checked hosts],
#                        "checked": [hosts], "skipped": {host: reason}, "counts": {host: {expected, running}}}

import argparse
import json
import sys

RESTARTING = ("always", "unless-stopped")


def expected_containers(host):
    """Names that should be running on one host (lower-cased name -> display name)."""
    want = {}
    for stack, cfg in (host.get("stacks") or {}).items():
        if not isinstance(cfg, dict):
            continue
        project = cfg.get("name") or stack
        for svc, spec in (cfg.get("services") or {}).items():
            if (spec.get("restart") or "no") not in RESTARTING:
                continue
            name = spec.get("container_name") or f"{project}-{svc}-1"
            want[name.lower()] = name
    for name in host.get("declared") or []:
        want[name.lower()] = name
    for name, policy in (host.get("restart") or {}).items():
        if policy in RESTARTING:
            want.setdefault(name.lower(), name)
    return want


def problems_for(host):
    """{lower name: (display name, problem)} for one checked host."""
    present = {}
    for c in host.get("ps") or []:
        for n in (c.get("Names") or "").split(","):
            if n:
                present[n.lower()] = (c.get("State") or "unknown").lower()
    out = {}
    for low, name in expected_containers(host).items():
        st = present.get(low)
        if st is None:
            out[low] = (name, "missing")
        elif st != "running":
            out[low] = (name, st)
    return out


def evaluate(data):
    confirm = max(1, int(data.get("confirm_runs", 2)))
    exclude = {e.lower() for e in data.get("exclude") or []}
    prev = {(r["hostname"], r["container"].lower()): r for r in data.get("state") or []}
    new, recovered, ongoing, state, checked, skipped, counts = [], [], [], [], [], {}, {}

    for hname, host in sorted((data.get("hosts") or {}).items()):
        if host.get("skip"):
            skipped[hname] = host["skip"]
            continue
        checked.append(hname)
        counts[hname] = {"expected": len(expected_containers(host)),
                         "running": sum(1 for c in host.get("ps") or [] if (c.get("State") or "") == "running")}
        probs = {low: v for low, v in problems_for(host).items()
                 if low not in exclude and f"{hname.lower()}/{low}" not in exclude}
        for low, (name, problem) in sorted(probs.items()):
            old = prev.get((hname, low))
            count = (int(old["seen_count"]) + 1) if old else 1
            notified = bool(old and int(old.get("notified") or 0))
            item = {"hostname": hname, "container": name, "problem": problem}
            if count >= confirm and not notified:
                new.append(item)
                notified = True
            elif notified:
                ongoing.append(item)
            state.append({**item, "seen_count": count, "notified": int(notified)})
        for (ph, low), old in prev.items():
            if ph == hname and low not in probs and int(old.get("notified") or 0):
                recovered.append({"hostname": ph, "container": old["container"], "problem": old["problem"]})
    return {"new": new, "recovered": recovered, "ongoing": ongoing, "state": state,
            "checked": checked, "skipped": skipped, "counts": counts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    args = ap.parse_args()
    with open(args.input) as f:
        data = json.load(f)
    print(json.dumps(evaluate(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
