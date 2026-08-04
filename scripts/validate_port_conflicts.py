#!/usr/bin/env python3
"""Detect host-port conflicts across stacks assigned to the same VM role.

Loads container_definitions.yaml, stack_definitions.yaml, and vm_definitions.yaml
(+ host_definitions.yaml) to build a role -> stacks -> containers -> ports mapping,
then reports any host-port collisions within a role.

Containers with Docker Compose profiles are tracked separately — two containers
with non-overlapping profiles never conflict even if they bind the same host port.

Usage:
  python3 scripts/validate_port_conflicts.py                  # human-readable table
  python3 scripts/validate_port_conflicts.py --format json    # JSON output
  python3 scripts/validate_port_conflicts.py --format quiet   # exit code only

Exit codes:
  0 — no conflicts
  1 — conflicts found
  2 — load/parse error
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# YAML loading — handle Jinja2 {{ }} expressions as literal strings
# ---------------------------------------------------------------------------

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# Jinja2 expressions inside YAML values break the parser.  Replace them with
# a placeholder before parsing so the structural data (ports, profiles, stacks)
# is accessible.  We only need the literal strings for port mappings and list
# membership — Jinja2 expressions in those fields would be unusual but we
# handle them gracefully by skipping unresolvable values.
_JINJA_RE = re.compile(r"\{\{.*?\}\}")
_JINJA_PLACEHOLDER = "__JINJA__"


def _load_yaml_with_jinja(path: Path) -> dict:
    """Load a YAML file, neutralising Jinja2 template expressions."""
    raw = path.read_text()
    sanitised = _JINJA_RE.sub(_JINJA_PLACEHOLDER, raw)
    return yaml.safe_load(sanitised) or {}


# ---------------------------------------------------------------------------
# Port parsing
# ---------------------------------------------------------------------------

def _parse_host_port(mapping: str) -> tuple[str, str] | None:
    """Parse a compose port mapping and return (host_port, protocol).

    Accepted formats:
      "host:container"          -> (host, "tcp")
      "host:container/udp"      -> (host, "udp")
      "host:container/tcp"      -> (host, "tcp")

    Returns None if the mapping contains unresolved Jinja2 placeholders or
    is otherwise unparseable.
    """
    if _JINJA_PLACEHOLDER in str(mapping):
        return None

    mapping = str(mapping).strip()

    # Split off protocol suffix
    protocol = "tcp"
    if "/" in mapping:
        mapping, protocol = mapping.rsplit("/", 1)

    parts = mapping.split(":")
    if len(parts) < 2:
        return None

    host_port = parts[0]
    return (host_port, protocol.lower())


# ---------------------------------------------------------------------------
# Role -> stack -> container collection
# ---------------------------------------------------------------------------

def _build_role_stacks(vm_defs: dict, host_defs: dict) -> dict[str, list[str]]:
    """Derive stack_roles: role name -> ordered stack list."""
    roles = {}
    for name, spec in {**vm_defs, **host_defs}.items():
        stacks = spec.get("stacks")
        if stacks:
            roles[name] = list(stacks)
    return roles


def _collect_port_bindings(container_defs: dict) -> dict[str, list[dict]]:
    """Build stack -> list of {container, host_port, protocol, profiles}.

    Only containers with compose.ports are included.
    """
    stack_ports: dict[str, list[dict]] = {}
    for cname, cspec in container_defs.items():
        stack = cspec.get("stack")
        compose = cspec.get("compose")
        if not stack or not compose:
            continue

        ports = compose.get("ports", [])
        profiles = compose.get("profiles", [])
        network_mode = compose.get("network_mode", "")

        # Containers with network_mode: "service:X" share the network
        # namespace of another container — their ports are actually bound
        # on the parent's network.  We still track them because the host
        # port is consumed on the same host, but we note the parent so
        # the conflict message is clear.
        for port_str in ports:
            parsed = _parse_host_port(port_str)
            if parsed is None:
                continue
            host_port, proto = parsed
            stack_ports.setdefault(stack, []).append({
                "container": cname,
                "host_port": host_port,
                "protocol": proto,
                "profiles": list(profiles),
                "network_mode": network_mode,
            })

    return stack_ports


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _is_active(profiles: list[str], active_profiles: list[str] | None) -> bool:
    """Return True if a binding's profiles are active for a given role.

    - No profiles ([]) means the service always runs -> always active.
    - active_profiles is None when the stack has no role_profiles entry for
      this role — conservatively assume active (preserves prior behaviour).
    - Otherwise the binding is active only if it shares a profile with the
      role's active set.
    """
    if not profiles:
        return True
    if active_profiles is None:
        return True
    return bool(set(profiles) & set(active_profiles))


def detect_conflicts(
    container_defs: dict,
    vm_defs: dict,
    host_defs: dict,
    stack_defs: dict | None = None,
) -> list[dict]:
    """Return a list of conflict records.

    Each record: {
        "role": str,
        "host_port": str,
        "protocol": str,
        "containers": [{"name": str, "stack": str, "profiles": list}, ...]
    }
    """
    stack_defs = stack_defs or {}
    role_stacks = _build_role_stacks(vm_defs, host_defs)
    stack_ports = _collect_port_bindings(container_defs)

    conflicts = []
    for role, stacks in sorted(role_stacks.items()):
        # Collect all port bindings for this role, dropping bindings whose
        # profile isn't active for this role (per stack_definitions.role_profiles)
        # Key: (host_port, protocol) -> list of binding dicts
        port_map: dict[tuple[str, str], list[dict]] = {}
        for stack in stacks:
            active_profiles = stack_defs.get(stack, {}).get("role_profiles", {}).get(role)
            for binding in stack_ports.get(stack, []):
                if not _is_active(binding["profiles"], active_profiles):
                    continue
                key = (binding["host_port"], binding["protocol"])
                entry = {
                    "name": binding["container"],
                    "stack": stack,
                    "profiles": binding["profiles"],
                }
                port_map.setdefault(key, []).append(entry)

        # Any port with 2+ bindings that are simultaneously active is a conflict.
        for (host_port, proto), bindings in sorted(port_map.items()):
            if len(bindings) < 2:
                continue

            conflicts.append({
                "role": role,
                "host_port": host_port,
                "protocol": proto,
                "containers": bindings,
            })

    return conflicts


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_table(conflicts: list[dict]) -> str:
    """Human-readable conflict report."""
    if not conflicts:
        return "No port conflicts detected."

    lines = []
    lines.append(f"CONFLICTS FOUND: {len(conflicts)}")
    lines.append("")
    for c in conflicts:
        port_label = f"{c['host_port']}/{c['protocol']}"
        lines.append(f"  Role: {c['role']}  Port: {port_label}")
        for ct in c["containers"]:
            prof = f"  profiles={ct['profiles']}" if ct["profiles"] else ""
            lines.append(f"    - {ct['name']} (stack: {ct['stack']}{prof})")
        lines.append("")
    return "\n".join(lines)


def _format_json(conflicts: list[dict]) -> str:
    return json.dumps(conflicts, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect host-port conflicts across stacks on the same VM role."
    )
    parser.add_argument(
        "--format", choices=["table", "json", "quiet"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--definitions-dir", type=Path, default=None,
        help="Path to vars/definitions/ directory (auto-detected if omitted)",
    )
    args = parser.parse_args()

    # Locate definitions directory
    if args.definitions_dir:
        defs_dir = args.definitions_dir
    else:
        # Walk up from script location to find vars/definitions/
        script_dir = Path(__file__).resolve().parent
        repo_root = script_dir.parent
        defs_dir = repo_root / "vars" / "definitions"

    required_files = {
        "container": defs_dir / "container_definitions.yaml",
        "stack": defs_dir / "stack_definitions.yaml",
        "vm": defs_dir / "vm_definitions.yaml",
    }
    optional_files = {
        "host": defs_dir / "host_definitions.yaml",
    }

    for label, path in required_files.items():
        if not path.is_file():
            print(f"ERROR: {label} definitions not found: {path}", file=sys.stderr)
            return 2

    try:
        container_defs = _load_yaml_with_jinja(required_files["container"]).get(
            "container_definitions", {}
        )
        vm_defs = _load_yaml_with_jinja(required_files["vm"]).get(
            "vm_definitions", {}
        )
        stack_defs = _load_yaml_with_jinja(required_files["stack"]).get(
            "stack_definitions", {}
        )

        host_path = optional_files["host"]
        host_defs = {}
        if host_path.is_file():
            host_defs = _load_yaml_with_jinja(host_path).get(
                "host_definitions", {}
            )
    except Exception as exc:
        print(f"ERROR: Failed to load definitions: {exc}", file=sys.stderr)
        return 2

    conflicts = detect_conflicts(container_defs, vm_defs, host_defs, stack_defs)

    if args.format == "table":
        print(_format_table(conflicts))
    elif args.format == "json":
        print(_format_json(conflicts))
    # quiet: no output

    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
