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
SKIP_FILES = {"requirements.yaml", "inventory.yaml", "inventory.example.yaml",
               "playbook.example.yaml"}


def _all_var_names(result: dict) -> set:
    """Return set of base var names (without =value) already captured."""
    names = set()
    for v in result["required_vars"] + result["optional_vars"]:
        names.add(v["name"].split("=")[0])
    return names


# Section header patterns that signal optional extra vars
_OPTIONAL_HEADERS = re.compile(
    r"^(optional(\s+extra\s+var\w*)?|scope\s+selectors?|options?|extra\s+var\w*|modes?)"
    r"\s*(\(.*\))?\s*:?\s*$",
    re.IGNORECASE,
)

# Section header patterns that signal required extra vars
_REQUIRED_HEADERS = re.compile(
    r"^required(\s+extra\s+var\w*)?\s*:", re.IGNORECASE
)

# Section headers for vault/internal vars (NOT extra vars — skip these)
_SKIP_HEADERS = re.compile(
    r"^required\s+(variables?|vault\s+var)", re.IGNORECASE
)

# Inline -e var pattern: `-e var=value`
_INLINE_EVAR = re.compile(r"-e\s+(\w+=\S+)")

# Safety-gate mention: "requires confirm=yes" or "safety-gated: requires -e confirm=yes"
_SAFETY_GATE = re.compile(
    r"(?:safety[- ]gated|requires)\s*[:.]?\s*(?:-e\s+)?confirm=yes",
    re.IGNORECASE,
)


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
        "category": "",
        "required_vars": [],
        "optional_vars": [],
        "usage_examples": [],
        "deprecated": False,
    }

    # Extract category from header comment (e.g. "Category: Backup")
    for line in header_lines:
        cat_match = re.match(r"^Category:\s*(.+)", line.strip())
        if cat_match:
            result["category"] = cat_match.group(1).strip()
            break

    # Check for deprecation
    if "DEPRECATED" in text.upper():
        result["deprecated"] = True

    # Extract description (first non-empty line)
    for line in header_lines:
        if line.strip():
            result["description"] = line.strip()
            break

    # --- Multi-pass extraction ---

    required_section = False
    optional_section = False
    usage_section = False
    skip_section = False
    # Track which line indices are in skip/non-var sections (for fallback filtering)
    _skip_line_indices = set()

    for line_idx, line in enumerate(header_lines):
        lower = line.lower().strip()

        # Blank lines reset section state (sections are contiguous blocks)
        if not lower:
            required_section = optional_section = usage_section = False
            continue

        # Detect section headers -------------------------------------------------
        if _SKIP_HEADERS.match(lower):
            # Vault/internal vars section — skip until next section
            required_section = optional_section = usage_section = False
            skip_section = True
            continue

        if _REQUIRED_HEADERS.match(lower) or lower == "required:":
            required_section = True
            optional_section = usage_section = skip_section = False
            continue

        if _OPTIONAL_HEADERS.match(lower):
            optional_section = True
            required_section = usage_section = skip_section = False
            continue

        if "usage:" in lower:
            usage_section = True
            required_section = optional_section = skip_section = False
            continue

        # Other section-like headers reset parsing
        if re.match(r"^(vault|vm lifecycle|semaphore|interactive|after|prerequisite)\b", lower):
            required_section = optional_section = usage_section = False
            skip_section = True
            continue

        if skip_section:
            _skip_line_indices.add(line_idx)
            continue

        # Parse vars from structured sections ------------------------------------
        if required_section or optional_section:
            # Strip leading `-e ` prefix (some headers use it inline)
            clean = re.sub(r"^\s*-e\s+", "", line)

            # Match: var_name — description  OR  var_name=value — description
            # Accepts: word chars, =, |, /, <, >, - (for date placeholders like YYYY-MM-DD)
            var_match = re.match(
                r"^\s*(\w[\w=|/<>-]*(?:\s*\|\s*\w+)*)"
                r"\s+[—–-]\s+(.*)",
                clean,
            )
            if not var_match:
                # Try looser match: var_name with no description
                var_match = re.match(
                    r"^\s*(\w[\w=|/<>-]*(?:\s*\|\s*\w+)*)\s*$",
                    clean,
                )
                if var_match:
                    # Fake the second group
                    class _M:
                        def group(self, n):
                            if n == 1:
                                return var_match.group(1)
                            return ""
                    var_match = _M()
            if var_match and not line.lstrip().startswith("Do NOT"):
                var_name = var_match.group(1).strip()
                var_desc = var_match.group(2).strip()
                # Skip continuation lines and common English words
                _skip_words = {
                    "requires", "default", "override", "example",
                    "note", "see", "the", "this", "that", "when",
                    "if", "for", "and", "or", "not", "all", "any",
                    "run", "set", "use",
                }
                if (
                    var_name
                    and len(var_name) < 80
                    and var_name.lower() not in _skip_words
                    and (
                        not var_name[0].isupper()
                        or "=" in var_name
                    )
                ):
                    entry = {"name": var_name, "description": var_desc}
                    if required_section:
                        result["required_vars"].append(entry)
                    else:
                        result["optional_vars"].append(entry)

        # Parse usage examples ---------------------------------------------------
        if usage_section:
            if "ansible-playbook" in line:
                result["usage_examples"].append(line.strip())

    # --- Fallback: scan entire header for `-e var=value` patterns ---------------
    for line_idx, line in enumerate(header_lines):
        if line_idx in _skip_line_indices:
            continue
        matches = list(_INLINE_EVAR.finditer(line))
        for i, m in enumerate(matches):
            var_expr = m.group(1)
            var_base = var_expr.split("=")[0]
            if var_base not in _all_var_names(result):
                # Description is text between this -e and the next (or end)
                if i + 1 < len(matches):
                    after = line[m.end():matches[i + 1].start()]
                else:
                    after = line[m.end():]
                desc = ""
                desc_match = re.match(r"\s*[—–-]+\s*(.*)", after)
                if desc_match:
                    desc = desc_match.group(1).strip()
                # Strip trailing comment/shell chars
                var_expr = re.sub(r"[\\#]+$", "", var_expr).rstrip()
                result["optional_vars"].append({
                    "name": var_expr,
                    "description": desc or "(from header)",
                })

    # --- Fallback: detect safety-gate mentions ----------------------------------
    if "confirm" not in _all_var_names(result):
        if _SAFETY_GATE.search(text):
            result["optional_vars"].append({
                "name": "confirm=yes",
                "description": "safety gate",
            })

    # --- Extract vars from usage examples ---------------------------------------
    for example in result["usage_examples"]:
        for match in re.finditer(r"-e\s+(\w+=\S+)", example):
            var_expr = match.group(1)
            var_name = var_expr.split("=")[0]
            # Strip trailing shell chars (backslash, comment markers)
            var_expr = re.sub(r"[\\#]+$", "", var_expr).rstrip()
            if var_name not in _all_var_names(result):
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
