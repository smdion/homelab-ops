#!/usr/bin/env python3
# semaphore_templates_sync — make Semaphore's templates match vars/configs/semaphore_templates.yaml.
# Run on the controller by tasks/semaphore_templates_sync.yaml (playbook deploy_semaphore_templates.yaml, and
# maintain_health CHECK 34 in report mode). Design: homelab-docs semaphore/index.md ("Templates are IaC").
#
# Semaphore stores templates in its own database, so a template created or edited by hand (UI, CLI, API) is invisible to
# git and lost on a rebuild. The registry is the source of truth; this compares it with the live project:
#
#   CREATE   in the registry, not live
#   UPDATE   live but a managed field differs (playbook, view, inventory, environment, arguments, description,
#            alert/override flags, debug, limit, vault attached)
#   EXTRA    live but not in the registry — i.e. created outside IaC ("bypassed"). Reported; never deleted unless --prune
#
# Schedules are managed the same way: a template's `schedule: [{cron, name}]` in the registry is the ONLY set of
# schedules it should have (SCHED+ missing, SCHED~ wrong name/inactive, SCHED-EXTRA not in the registry).
#
#   default   report only — nothing is written
#   --apply   create and update
#   --prune   with --apply, ALSO delete EXTRA templates (refuses if the registry is suspiciously small)
#
# desired.json: {"defaults": {...}, "templates": [{"name","playbook","view","inventory","environment", ...}]}
# The API token comes from the environment (SEMAPHORE_TOKEN), never argv. Output: report on stderr, one JSON document on
# stdout. Exit 0 = in sync / applied, 2 = drift (report mode), 1 = error.

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MIN_TEMPLATES = 20  # refuse to prune when the registry is smaller than this (a bad render must not wipe Semaphore)


class Api:
    def __init__(self, base, token, project):
        self.base, self.token, self.project = base.rstrip("/"), token, project

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            f"{self.base}/api/project/{self.project}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
                return r.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:300]


def norm_args(v):
    return None if v in (None, "", "[]") else v


def norm_text(v):
    return None if v in (None, "") else v


def managed_view(live, ids):
    """The comparable form of a live template (ids -> what the registry expresses)."""
    tp = live.get("task_params") or {}
    return {
        "playbook": live.get("playbook"),
        "view_id": live.get("view_id") or None,
        "inventory_id": live.get("inventory_id") or None,
        "environment_id": live.get("environment_id") or None,
        "environment_ids": list(live.get("environment_ids") or []),  # Semaphore keeps the environment in BOTH fields
        "arguments": norm_args(live.get("arguments")),
        "description": norm_text(live.get("description")),
        "suppress_success_alerts": bool(live.get("suppress_success_alerts")),
        "allow_override_args_in_task": bool(live.get("allow_override_args_in_task")),
        "allow_override_branch_in_task": bool(live.get("allow_override_branch_in_task")),
        "allow_debug": bool(tp.get("allow_debug")),
        "limit": tp.get("limit") or None,
        "vault": any(v.get("vault_key_id") == ids["vault"] for v in live.get("vaults") or []),
    }


def wanted_view(d, defaults, ids):
    """The registry entry resolved to the same shape (names -> ids)."""
    return {
        "playbook": d["playbook"],
        "view_id": ids["views"].get(d["view"]),
        "inventory_id": ids["inventories"].get(d["inventory"]),
        "environment_id": ids["environments"].get(d["environment"]),
        "environment_ids": [ids["environments"][d["environment"]]] if d["environment"] in ids["environments"] else [],
        "arguments": norm_args(d.get("arguments")),
        "description": norm_text(d.get("description")),
        "suppress_success_alerts": bool(d.get("suppress_success_alerts")),
        "allow_override_args_in_task": bool(defaults.get("allow_override_args_in_task", True)),
        "allow_override_branch_in_task": bool(defaults.get("allow_override_branch_in_task", True)),
        "allow_debug": bool(d.get("allow_debug")),
        "limit": d.get("limit") or None,
        "vault": True,
    }


def plan(desired, defaults, live_by_name, ids):
    """Pure function: compare the registry with the live templates. Returns action lists (no I/O)."""
    creates, updates, problems = [], [], []
    seen = set()
    for d in desired:
        name = d["name"]
        seen.add(name)
        want = wanted_view(d, defaults, ids)
        for k, label in (("view_id", "view"), ("inventory_id", "inventory"), ("environment_id", "environment")):
            if want[k] is None:
                problems.append(f"{name}: unknown {label} {d[label]!r}")
        live = live_by_name.get(name)
        if live is None:
            creates.append({"name": name, "want": want})
            continue
        have = managed_view(live, ids)
        diff = {k: (have[k], want[k]) for k in want if have[k] != want[k]}
        if diff:
            updates.append({"name": name, "id": live["id"], "diff": diff, "want": want})
    extras = sorted(n for n in live_by_name if n not in seen)
    return {"creates": creates, "updates": updates, "extras": extras, "problems": problems}


def plan_schedules(desired, live_schedules, template_ids):
    """Pure function: compare each template's registry schedules with the live ones.

    Returns {"creates": [{template, cron, name}], "updates": [{id, template, cron, diff}],
             "extras": [{id, template, cron}]}. `template_ids` maps a live template name to its id (templates that do not
    exist yet have no id, so all of their schedules are creates)."""
    by_tid = {}
    for sch in live_schedules:
        by_tid.setdefault(sch["template_id"], []).append(sch)
    id_to_name = {v: k for k, v in template_ids.items()}
    creates, updates, extras = [], [], []
    for d in desired:
        want = d.get("schedule") or []
        tid = template_ids.get(d["name"])
        live = by_tid.get(tid, []) if tid else []
        for w in want:
            match = next((x for x in live if x["cron_format"] == w["cron"]), None)
            if match is None:
                creates.append({"template": d["name"], "cron": w["cron"], "name": w.get("name") or w["cron"]})
                continue
            diff = {}
            if w.get("name") and match.get("name") != w["name"]:
                diff["name"] = (match.get("name"), w["name"])
            if not match.get("active"):
                diff["active"] = (False, True)
            if diff:
                updates.append({"id": match["id"], "template": d["name"], "cron": w["cron"], "diff": diff, "live": match})
        wanted_crons = {w["cron"] for w in want}
        extras += [{"id": x["id"], "template": d["name"], "cron": x["cron_format"]} for x in live
                   if x["cron_format"] not in wanted_crons]
    # schedules of templates that are not in the registry at all disappear with the extra template (reported there)
    return {"creates": creates, "updates": updates, "extras": extras}


def body_from(want, live=None):
    """Request body for create/update from the resolved wanted state (keeps every unmanaged field of `live`)."""
    b = dict(live or {"app": "ansible", "repository_id": 1, "type": ""})
    b.update({
        "playbook": want["playbook"], "view_id": want["view_id"], "inventory_id": want["inventory_id"],
        "environment_id": want["environment_id"], "environment_ids": want["environment_ids"],
        "arguments": want["arguments"], "description": want["description"] or "",
        "suppress_success_alerts": want["suppress_success_alerts"],
        "allow_override_args_in_task": want["allow_override_args_in_task"],
        "allow_override_branch_in_task": want["allow_override_branch_in_task"],
    })
    tp = dict(b.get("task_params") or {})
    for key, val in (("limit", want["limit"]), ("allow_debug", want["allow_debug"] or None)):
        if val:
            tp[key] = val
        else:
            tp.pop(key, None)
    b["task_params"] = tp
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--desired", required=True)
    ap.add_argument("--project", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    say = lambda m: print(m, file=sys.stderr)
    reg = json.load(open(args.desired))
    desired, defaults = reg["templates"], reg.get("defaults", {})
    api = Api(args.url, os.environ.get("SEMAPHORE_TOKEN", ""), args.project)
    try:
        def get(path):
            st, res = api.call("GET", path)
            if st != 200:
                raise RuntimeError(f"GET {path} -> HTTP {st}")
            return res
        listing = get("/templates")
        with ThreadPoolExecutor(8) as ex:
            live_full = list(ex.map(lambda t: get(f"/templates/{t['id']}"), listing))
        live_sched = get("/schedules")
        ids = {
            "views": {v["title"]: v["id"] for v in get("/views")},
            "inventories": {i["name"]: i["id"] for i in get("/inventory")},
            "environments": {e["name"]: e["id"] for e in get("/environment")},
            "vault": next((k["id"] for k in get("/keys") if k["name"] == defaults.get("vault_key", "ansible-vault")), None),
        }
    except (RuntimeError, OSError) as e:
        say(f"ERROR: {e}")
        return 1
    if ids["vault"] is None:
        say(f"ERROR: vault key {defaults.get('vault_key')!r} not found in Semaphore")
        return 1

    names = [t["name"] for t in live_full]
    dups = sorted({n for n in names if names.count(n) > 1})
    live_by_name = {t["name"]: t for t in live_full}
    p = plan(desired, defaults, live_by_name, ids)
    sp = plan_schedules(desired, live_sched, {n: t["id"] for n, t in live_by_name.items()})

    say(f"semaphore: {len(live_full)} live template(s), {len(desired)} in the registry")
    for c in p["creates"]:
        say(f"  CREATE  {c['name']}")
    for u in p["updates"]:
        say(f"  UPDATE  {u['name']}: " + "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in u["diff"].items()))
    for n in p["extras"]:
        say(f"  EXTRA   {n}  (live, not in the registry — created outside IaC)")
    for c in sp["creates"]:
        say(f"  SCHED+  {c['template']} @ {c['cron']} ({c['name']})")
    for u in sp["updates"]:
        say(f"  SCHED~  {u['template']} @ {u['cron']}: " + "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in u["diff"].items()))
    for x in sp["extras"]:
        say(f"  SCHED-EXTRA  {x['template']} @ {x['cron']}  (live schedule not in the registry)")
    for n in dups:
        say(f"  DUPLICATE live name: {n}")
    for pr in p["problems"]:
        say(f"  PROBLEM {pr}")

    drift = bool(p["creates"] or p["updates"] or p["extras"] or dups or p["problems"]
                 or sp["creates"] or sp["updates"] or sp["extras"])
    result = {"drift": drift, "applied": False, "errors": [], "deleted": [],
              "counts": {**{k: len(v) for k, v in p.items()}, **{f"sched_{k}": len(v) for k, v in sp.items()}},
              "details": {"missing": [c["name"] for c in p["creates"]],
                          "changed": [f"{u['name']} ({', '.join(u['diff'])})" for u in p["updates"]],
                          "extra": p["extras"], "duplicates": dups, "problems": p["problems"],
                          "schedules_missing": [f"{c['template']} @ {c['cron']}" for c in sp["creates"]],
                          "schedules_changed": [f"{u['template']} @ {u['cron']} ({', '.join(u['diff'])})" for u in sp["updates"]],
                          "schedules_extra": [f"{x['template']} @ {x['cron']}" for x in sp["extras"]]}}

    if args.apply and p["problems"]:
        say("REFUSING to apply: the registry references unknown views/inventories/environments")
        result["errors"].append("registry has unresolved references")
    elif args.apply:
        for c in p["creates"]:
            st, res = api.call("POST", "/templates", body_from(c["want"]) | {"name": c["name"]})
            if st not in (200, 201) or not isinstance(res, dict):
                result["errors"].append(f"create {c['name']}: HTTP {st} {res}")
                continue
            full = get(f"/templates/{res['id']}")
            st, res2 = api.call("PUT", f"/templates/{res['id']}",
                                body_from(c["want"], full) | {"vaults": [{"vault_key_id": ids["vault"], "type": "password"}]})
            if st not in (200, 204):
                result["errors"].append(f"create {c['name']} (attach vault): HTTP {st} {res2}")
        for u in p["updates"]:
            live = live_by_name[u["name"]]
            body = body_from(u["want"], live)
            body["vaults"] = [{"vault_key_id": ids["vault"], "type": "password"}]
            st, res = api.call("PUT", f"/templates/{u['id']}", body)
            if st not in (200, 204):
                result["errors"].append(f"update {u['name']}: HTTP {st} {res}")
        # schedules (templates now exist; resolve ids fresh so newly created templates are included)
        tids = {t["name"]: t["id"] for t in get("/templates")}
        for c in sp["creates"]:
            tid = tids.get(c["template"])
            if not tid:
                result["errors"].append(f"schedule {c['template']} @ {c['cron']}: template not found")
                continue
            st, res = api.call("POST", "/schedules", {"project_id": args.project, "template_id": tid,
                                                      "cron_format": c["cron"], "name": c["name"], "active": True,
                                                      "type": "", "delete_after_run": False})
            if st not in (200, 201):
                result["errors"].append(f"schedule {c['template']} @ {c['cron']}: HTTP {st} {res}")
        for u in sp["updates"]:
            body = dict(u["live"])
            body.update({"name": u["diff"].get("name", (None, u["live"].get("name")))[1], "active": True})
            st, res = api.call("PUT", f"/schedules/{u['id']}", body)
            if st not in (200, 204):
                result["errors"].append(f"schedule update {u['template']} @ {u['cron']}: HTTP {st} {res}")
        if args.prune:
            for x in sp["extras"]:
                st, res = api.call("DELETE", f"/schedules/{x['id']}")
                if st not in (200, 204):
                    result["errors"].append(f"schedule delete {x['template']} @ {x['cron']}: HTTP {st} {res}")
        if args.prune:
            if len(desired) < MIN_TEMPLATES:
                say(f"REFUSING to prune: only {len(desired)} template(s) in the registry (minimum {MIN_TEMPLATES})")
                result["errors"].append("prune refused: registry too small")
            else:
                for n in p["extras"]:
                    st, res = api.call("DELETE", f"/templates/{live_by_name[n]['id']}")
                    (result["deleted"] if st in (200, 204) else result["errors"]).append(
                        n if st in (200, 204) else f"delete {n}: HTTP {st} {res}")
        result["applied"] = True
        for e in result["errors"]:
            say(f"  ERROR {e}")
    elif drift:
        say("report only — nothing written (re-run with --apply; --prune also deletes EXTRA templates)")

    print(json.dumps(result))
    if result["errors"]:
        return 1
    return 0 if (args.apply or not drift) else 2


if __name__ == "__main__":
    sys.exit(main())
