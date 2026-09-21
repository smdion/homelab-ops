#!/usr/bin/env python3
"""Fail if a build/deploy/retire/restore playbook is missing the monitoring hook (Beszel + Dozzle).

Every playbook that creates, changes, retires or restores a host must end with the "Sync monitoring" play
(tasks/monitoring_hook.yaml), so monitoring is registered and cleaned up as part of the same run. See
homelab-docs tasks/beszel-iac.md (Phase 2b). A new build_*/deploy_* playbook is covered automatically by the
glob below; anything else that touches hosts goes in EXTRA. Opting out needs a reason in EXEMPT.

Usage: python3 scripts/check_monitoring_hooks.py     (exit 1 and a list when something is missing)
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = "tasks/monitoring_hook.yaml"
GLOBS = ["build_*.yaml", "deploy_*.yaml"]
EXTRA = ["bootstrap_amp", "apply_role", "dr_rebuild", "reip_vmid", "retire_vm", "cleanup_test_vms", "purge_ceph",
         "restore_hosts", "restore_app", "restore_databases", "restore_amp"]
EXEMPT = {
    "deploy_beszel_monitoring": "it IS the Beszel sync (tasks/beszel_sync.yaml); hooking it would run the sync twice",
    "deploy_dozzle_hub": "it IS the Dozzle sync (tasks/dozzle_sync.yaml); hooking it would run the sync twice",
}


def main():
    names = {os.path.basename(p)[:-5] for g in GLOBS for p in glob.glob(os.path.join(ROOT, g))}
    names |= {n for n in EXTRA if os.path.exists(os.path.join(ROOT, f"{n}.yaml"))}
    missing = []
    for n in sorted(names - set(EXEMPT)):
        with open(os.path.join(ROOT, f"{n}.yaml")) as f:
            if HOOK not in f.read():
                missing.append(n)
    if not os.path.exists(os.path.join(ROOT, HOOK)):
        print(f"missing {HOOK}")
        return 1
    if missing:
        print("playbooks without the Beszel monitoring hook (append the 'Sync monitoring' play):")
        for n in missing:
            print(f"  {n}.yaml")
        return 1
    print(f"ok: {len(names) - len(EXEMPT)} playbook(s) hooked, {len(EXEMPT)} exempt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
