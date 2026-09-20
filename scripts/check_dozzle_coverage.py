#!/usr/bin/env python3
"""Fail if a Docker host has no Dozzle role, or the hub is not exactly one host with the infra stack.

Every host that runs Docker stacks (`stacks:` in vm_definitions / host_definitions) or is an unRAID Docker host
(`unraid_containers:`) must declare `dozzle: { role: hub | agent | none }`. The hub (apps) generates its agent list
from these blocks (dozzle_remote_agent_env in vm_definitions.yaml), so a host without one would never appear in the
log viewer. `none` is allowed but should carry a comment saying why. See homelab-docs tasks/dozzle-iac.md.

Usage: python3 scripts/check_dozzle_coverage.py     (exit 1 with a list when something is wrong)
"""
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLES = {"hub", "agent", "none"}


def load(name, key):
    with open(os.path.join(ROOT, "vars", "definitions", name)) as f:
        data = yaml.safe_load(f)
    return data.get(key, data)


def main():
    defs = {}
    for name, key in (("vm_definitions.yaml", "vm_definitions"), ("host_definitions.yaml", "host_definitions")):
        defs.update({k: v for k, v in load(name, key).items() if isinstance(v, dict)})
    problems, hubs = [], []
    for k, v in sorted(defs.items()):
        is_docker = bool(v.get("stacks")) or bool(v.get("unraid_containers"))
        role = (v.get("dozzle") or {}).get("role")
        if is_docker and role not in ROLES:
            problems.append(f"{k}: runs Docker but has no dozzle role (add `dozzle: {{ role: agent }}`)")
        if role is not None and role not in ROLES:
            problems.append(f"{k}: dozzle.role {role!r} must be one of {sorted(ROLES)}")
        if role == "hub":
            hubs.append(k)
            if "infra" not in (v.get("stacks") or []):
                problems.append(f"{k}: is the Dozzle hub but does not run the infra stack")
    if len(hubs) != 1:
        problems.append(f"expected exactly one Dozzle hub, found {len(hubs)}: {hubs}")
    if problems:
        print("Dozzle coverage problems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    agents = [k for k, v in defs.items() if (v.get("dozzle") or {}).get("role") == "agent"]
    print(f"ok: hub={hubs[0]}, {len(agents)} agent(s): {', '.join(sorted(agents))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
