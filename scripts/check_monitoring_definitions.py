#!/usr/bin/env python3
"""Fail if a host's monitoring declarations (Beszel + Dozzle) are missing or contradict what its stacks deploy.

Beszel (`beszel: { agent: container | binary | none }`) and Dozzle (`dozzle: { role: hub | agent | none }`) are driven by the
definitions: the hub lists / registers exactly what they declare, so a host that forgot its block would silently never
appear in either. Rules:

  Beszel
    * every host/VM definition declares `beszel.agent` (container | binary | none) — `none` should carry a comment why;
    * a host that runs the `infra` or `vpn` stack gets a `beszel-agent` container from that stack regardless, so it must
      declare `agent: container` (declaring `none` would stop registration and the firewall rule but leave the container);
    * `agent: binary` is for hosts WITHOUT those stacks.
  Dozzle
    * every host that runs Docker (`stacks:` or `unraid_containers:`) declares a `dozzle.role`;
    * exactly one hub, and it runs the infra stack.

Usage: python3 scripts/check_monitoring_definitions.py     (exit 1 with a list when something is wrong)
See homelab-docs tasks/beszel-iac.md and tasks/dozzle-iac.md.
"""
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BESZEL = {"container", "binary", "none"}
DOZZLE = {"hub", "agent", "none"}
AGENT_STACKS = {"infra", "vpn"}  # stacks that always deploy a beszel-agent container


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
        stacks = set(v.get("stacks") or [])
        agent = (v.get("beszel") or {}).get("agent")
        if agent not in BESZEL:
            problems.append(f"{k}: no `beszel: {{ agent: container|binary|none }}` block")
        elif stacks & AGENT_STACKS and agent != "container":
            problems.append(f"{k}: runs {sorted(stacks & AGENT_STACKS)} (which deploys a beszel-agent container) but declares "
                            f"beszel.agent={agent!r}; use `container`")
        elif agent == "binary" and stacks & AGENT_STACKS:
            problems.append(f"{k}: agent=binary on a host whose stacks already deploy the container agent")
        is_docker = bool(stacks) or bool(v.get("unraid_containers"))
        role = (v.get("dozzle") or {}).get("role")
        if is_docker and role not in DOZZLE:
            problems.append(f"{k}: runs Docker but has no dozzle role (add `dozzle: {{ role: agent }}`)")
        if role is not None and role not in DOZZLE:
            problems.append(f"{k}: dozzle.role {role!r} must be one of {sorted(DOZZLE)}")
        if role == "hub":
            hubs.append(k)
            if "infra" not in stacks:
                problems.append(f"{k}: is the Dozzle hub but does not run the infra stack")
    if len(hubs) != 1:
        problems.append(f"expected exactly one Dozzle hub, found {len(hubs)}: {hubs}")
    if problems:
        print("Monitoring declaration problems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    agents = sorted(k for k, v in defs.items() if (v.get("dozzle") or {}).get("role") == "agent")
    bz = {a: sorted(k for k, v in defs.items() if (v.get("beszel") or {}).get("agent") == a) for a in sorted(BESZEL)}
    print(f"ok: {len(defs)} definitions | beszel: " + ", ".join(f"{len(v)} {a}" for a, v in bz.items())
          + f" | dozzle: hub={hubs[0]}, {len(agents)} agent(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
