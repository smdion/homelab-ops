#!/usr/bin/env python3
"""Extract extra-var documentation from playbook header comments.

Parses the comment block at the top of each playbook YAML file and extracts
required/optional extra vars along with usage examples.

Usage:
  python3 scripts/extract_extra_vars.py                      # table output
  python3 scripts/extract_extra_vars.py --format json         # JSON output
  python3 scripts/extract_extra_vars.py --format semaphore    # Semaphore template audit
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Playbooks that are not runnable (requirements files, etc.)
SKIP_FILES = {"requirements.yaml"}


def parse_header(filepath: Path) -> dict:
    """Parse the YAML comment header block to extract extra vars info."""
    lines = filepath.read_text().splitlines()
    header_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            continue
        if stripped.startswith("#"):
            header_lines.append(stripped.lstrip("# ").rstrip())
        elif stripped == "":
            header_lines.append("")
        else:
            break  # end of comment block

    text = "\n".join(header_lines)

    result = {
        "playbook": filepath.name,
        "description": "",
        "required_vars": [],
        "optional_vars": [],
        "usage_examples": [],
        "deprecated": False,
    }

    # Check for deprecation
    if "DEPRECATED" in text.upper():
        result["deprecated"] = True

    # Extract description (first non-empty line)
    for line in header_lines:
        if line.strip():
            result["description"] = line.strip()
            break

    # Extract required extra vars
    required_section = False
    optional_section = False
    usage_section = False

    for line in header_lines:
        lower = line.lower().strip()

        # Detect section headers
        if "required extra var" in lower or "required:" in lower:
            required_section = True
            optional_section = False
            usage_section = False
            continue
        elif "optional extra var" in lower or "optional:" in lower:
            required_section = False
            optional_section = True
            usage_section = False
            continue
        elif "usage:" in lower:
            required_section = False
            optional_section = False
            usage_section = True
            continue
        elif re.match(r"^(vault|vm lifecycle|extra var)", lower):
            # Detect other section types
            if "extra var" in lower:
                optional_section = True
                required_section = False
                usage_section = False
                continue
            else:
                required_section = False
                optional_section = False
                usage_section = False
                continue

        # Parse vars from current section
        if required_section or optional_section:
            # Match patterns like: var_name  — description
            # or: var_name=value  — description
            # or: var_name (with no description)
            var_match = re.match(
                r"^\s*(\w[\w=|/]*(?:\s*\|\s*\w+)*)"
                r"\s*[—–-]?\s*(.*)",
                line,
            )
            if var_match and not line.startswith("Do NOT"):
                var_name = var_match.group(1).strip()
                var_desc = var_match.group(2).strip()
                # Skip lines that are clearly not variable definitions
                if (
                    var_name
                    and not var_name[0].isupper()
                    and len(var_name) < 40
                    and "=" not in var_name.split("=")[0]
                    or "=" in var_name
                ):
                    entry = {"name": var_name, "description": var_desc}
                    if required_section:
                        result["required_vars"].append(entry)
                    else:
                        result["optional_vars"].append(entry)

        # Parse usage examples
        if usage_section:
            if "ansible-playbook" in line:
                result["usage_examples"].append(line.strip())

    # Also scan for -e vars in usage examples
    for example in result["usage_examples"]:
        for match in re.finditer(r"-e\s+(\w+=\S+)", example):
            var_expr = match.group(1)
            var_name = var_expr.split("=")[0]
            # Check if already captured
            all_names = [v["name"].split("=")[0] for v in
                         result["required_vars"] + result["optional_vars"]]
            if var_name not in all_names:
                result["optional_vars"].append({
                    "name": var_expr,
                    "description": "(from usage example)",
                })

    return result


def format_table(playbooks: list) -> str:
    """Format as a readable table."""
    lines = []
    lines.append(f"{'Playbook':<35} {'Required':<35} {'Optional':<50}")
    lines.append("─" * 120)

    for pb in playbooks:
        if pb["deprecated"]:
            lines.append(f"{pb['playbook']:<35} DEPRECATED")
            continue

        req = ", ".join(v["name"] for v in pb["required_vars"]) or "(none)"
        opt = ", ".join(v["name"] for v in pb["optional_vars"]) or "(none)"
        lines.append(f"{pb['playbook']:<35} {req:<35} {opt:<50}")

    return "\n".join(lines)


def format_semaphore_audit(playbooks: list) -> str:
    """Show which playbooks have extra vars that should be pre-filled."""
    lines = []
    lines.append("Semaphore Template Pre-fill Audit")
    lines.append("=" * 60)
    lines.append("")

    for pb in playbooks:
        if pb["deprecated"]:
            continue

        has_required = bool(pb["required_vars"])
        has_safety = any(
            "confirm" in v["name"] for v in
            pb["required_vars"] + pb["optional_vars"]
        )
        has_variants = len(pb["usage_examples"]) > 1

        if has_required or has_safety or has_variants:
            lines.append(f"  {pb['playbook']}")
            if pb["required_vars"]:
                lines.append(
                    f"    Required: {', '.join(v['name'] for v in pb['required_vars'])}"
                )
            if has_safety:
                lines.append("    Safety gate: confirm=yes")
            if has_variants:
                lines.append(f"    Variants ({len(pb['usage_examples'])}):")
                for ex in pb["usage_examples"]:
                    # Extract just the -e parts
                    evars = re.findall(r"-e\s+\S+", ex)
                    if evars:
                        lines.append(f"      {' '.join(evars)}")
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Extract extra-var documentation from playbook headers"
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "semaphore"],
        default="table",
        help="Output format",
    )
    parser.add_argument(
        "--playbook",
        help="Single playbook to extract (default: all)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    if args.playbook:
        files = [root / args.playbook]
    else:
        files = sorted(root.glob("*.yaml"))
        files = [f for f in files if f.name not in SKIP_FILES]

    playbooks = []
    for f in files:
        if f.is_file():
            try:
                info = parse_header(f)
                playbooks.append(info)
            except Exception as e:
                print(f"Warning: failed to parse {f.name}: {e}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps(playbooks, indent=2))
    elif args.format == "semaphore":
        print(format_semaphore_audit(playbooks))
    else:
        print(format_table(playbooks))


if __name__ == "__main__":
    main()
