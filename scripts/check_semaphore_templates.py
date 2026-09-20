#!/usr/bin/env python3
"""Fail if a playbook has no entry in the Semaphore template registry, or the registry is inconsistent.

Semaphore templates are IaC: vars/configs/semaphore_templates.yaml is the single source (see the header of that file and
deploy_semaphore_templates.yaml). Every playbook with a `# Category:` header must appear in it as some template's
`playbook:`, unless it is listed in EXEMPT with a reason. Also checks the registry itself: unique names, known views,
`Verb — Target` naming.

Usage: python3 scripts/check_semaphore_templates.py     (exit 1 with a list when something is wrong)
"""
import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWS = {"Backups", "Updates", "Maintenance", "Downloads", "Verify", "Restore", "Deploy", "Setup"}
EXEMPT = {
    "test_restore.yaml": "deprecated — replaced by build_ubuntu.yaml -e role= (see its header)",
}


def main():
    with open(os.path.join(ROOT, "vars", "configs", "semaphore_templates.yaml")) as f:
        reg = yaml.safe_load(f)
    templates = reg["semaphore_templates"]
    problems, names = [], set()
    for t in templates:
        n = t["name"]
        if n in names:
            problems.append(f"duplicate template name: {n}")
        names.add(n)
        if t["view"] not in VIEWS:
            problems.append(f"{n}: unknown view {t['view']!r} (allowed: {', '.join(sorted(VIEWS))})")
        if " — " not in n:
            problems.append(f"{n}: name should be 'Verb — Target [Subtype]' (em-dash)")
        if not os.path.exists(os.path.join(ROOT, t["playbook"])):
            problems.append(f"{n}: playbook {t['playbook']} does not exist")
    covered = {t["playbook"] for t in templates}
    for path in sorted(glob.glob(os.path.join(ROOT, "*.yaml"))):
        pb = os.path.basename(path)
        with open(path) as f:
            head = f.read(600)
        if "# Category:" in head and pb not in covered and pb not in EXEMPT:
            problems.append(f"{pb}: no template in the registry (add one, or exempt it with a reason in this script)")
    if problems:
        print("Semaphore template registry problems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"ok: {len(templates)} template(s), every playbook covered ({len(EXEMPT)} exempt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
