#!/usr/bin/env python3
# beszel_sync — make the Beszel hub's systems, alerts and notification target match the definitions.
# Run on the controller (needs the hub API, which the Semaphore host can reach over WireGuard) by
# tasks/beszel_sync.yaml. See homelab-docs tasks/beszel-iac.md for the design.
#
# The hub keeps its system list in its own database, so hardware renames and rebuilds never reach it. This
# reconciles it from a "desired" list rendered from vm_definitions/host_definitions:
#
#   desired.json  [{"key","name","address","port","alerts": {"CPU": {"value":90,"min":10}, ...} | null}, ...]
#
# Matching is by ADDRESS only (the identity that survives renames — a hub entry called "HomeOne" that points at
# the unRAID box IS the unRAID box). Names are never used to match: the hub's old "AMP" was really the Synology,
# and adopting it for the real AMP would attach the wrong history. A re-addressed host therefore shows up as a
# create plus an orphan, which the lifecycle hooks (register/deregister) handle explicitly.
#
#   default          report only — nothing is written
#   --apply          create missing systems, fix drifted name/host/port, create/update alerts, set the webhook
#   --prune          ALSO delete hub systems that match no definition. Never deletes a system that is currently
#                    "up" unless --prune-up is also given, and aborts if the desired list looks too small.
#
# Credentials come from the environment (never argv): BESZEL_USER, BESZEL_PASSWORD; optional BESZEL_WEBHOOK
# (a shoutrrr URL) is added to the hub's notification targets. Output: a human report on stderr and one JSON
# document on stdout. Exit 0 = in sync (or fully applied), 2 = drift found (report mode), 1 = error.

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

MIN_DESIRED = 3  # refuse to prune when the desired list is smaller than this (a bad render must not wipe the hub)


class Hub:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.token = None

    def call(self, method, path, body=None, timeout=20):
        req = urllib.request.Request(
            self.url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **({"Authorization": self.token} if self.token else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode()
                return r.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, text[:200]

    def login(self, user, password):
        # generous timeout: the hub sends a "login alert" email on every login and waits ~15s for its SMTP relay
        status, res = self.call("POST", "/api/collections/users/auth-with-password",
                                {"identity": user, "password": password}, timeout=90)
        if status != 200 or not isinstance(res, dict) or "token" not in res:
            raise RuntimeError(f"hub login failed (HTTP {status})")
        self.token = res["token"]
        return res["record"]["id"]

    def list(self, collection):
        status, res = self.call("GET", f"/api/collections/{collection}/records?perPage=500")
        if status != 200:
            raise RuntimeError(f"listing {collection} failed (HTTP {status})")
        return res["items"]


def plan(desired, systems, alerts, defaults):
    """Pure function: compare desired against the hub. Returns the action lists (no I/O)."""
    by_host = {}
    for s in systems:
        by_host.setdefault(s["host"], []).append(s)
    used, matched = set(), []  # matched: (desired, hub system | None)
    for d in desired:
        cand = [s for s in by_host.get(d["address"], []) if s["id"] not in used]
        hub = cand[0] if cand else None  # address only: a name match must NOT adopt an unrelated old entry
        if hub:
            used.add(hub["id"])
        matched.append((d, hub))

    creates, updates, orphans = [], [], []
    for d, hub in matched:
        if hub is None:
            creates.append(d)
        else:
            diff = {k: (hub[k], want) for k, want in (("name", d["name"]), ("host", d["address"]),
                                                       ("port", str(d["port"]))) if str(hub[k]) != want}
            if diff:
                updates.append({"desired": d, "hub": hub, "diff": diff})
    for s in systems:
        if s["id"] not in used:
            orphans.append(s)

    alert_index = {(a["system"], a["name"]): a for a in alerts}
    alert_creates, alert_updates = [], []
    for d, hub in matched:
        want = d.get("alerts")
        if want is None:
            want = defaults
        for name, spec in (want or {}).items():
            if not spec:
                continue
            existing = alert_index.get((hub["id"], name)) if hub else None
            if existing is None:
                alert_creates.append({"system_name": d["name"], "system_id": hub["id"] if hub else None,
                                      "name": name, **spec})
            elif (existing.get("value"), existing.get("min")) != (spec["value"], spec["min"]):
                alert_updates.append({"system_name": d["name"], "id": existing["id"], "name": name,
                                      "from": [existing.get("value"), existing.get("min")],
                                      "to": [spec["value"], spec["min"]]})
    return {"creates": creates, "updates": updates, "orphans": orphans,
            "alert_creates": alert_creates, "alert_updates": alert_updates}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-url", required=True)
    ap.add_argument("--desired", required=True)
    ap.add_argument("--defaults", default="{}", help="JSON of default alerts, e.g. '{\"CPU\":{\"value\":90,\"min\":10}}'")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--prune-up", action="store_true", help="allow pruning a system the hub currently reports up")
    args = ap.parse_args()

    desired = json.load(open(args.desired))
    defaults = json.loads(args.defaults)
    hub = Hub(args.hub_url)
    try:
        uid = hub.login(os.environ["BESZEL_USER"], os.environ["BESZEL_PASSWORD"])
        systems, alerts = hub.list("systems"), hub.list("alerts")
        settings = hub.list("user_settings")[0]
    except (KeyError, RuntimeError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    p = plan(desired, systems, alerts, defaults)
    webhook = os.environ.get("BESZEL_WEBHOOK", "")
    hooks = settings["settings"].get("webhooks", [])
    webhook_missing = bool(webhook) and webhook not in hooks

    def say(msg):
        print(msg, file=sys.stderr)

    say(f"hub: {len(systems)} system(s), {len(alerts)} alert(s); desired: {len(desired)} system(s)")
    for d in p["creates"]:
        say(f"  CREATE  {d['name']:14} {d['address']}")
    for u in p["updates"]:
        say(f"  UPDATE  {u['hub']['name']:14} " + ", ".join(f"{k}: {a} -> {b}" for k, (a, b) in u["diff"].items()))
    for s in p["orphans"]:
        say(f"  ORPHAN  {s['name']:14} {s['host']:16} status={s['status']} (matches no definition)")
    for a in p["alert_creates"]:
        say(f"  ALERT+  {a['system_name']:14} {a['name']} value={a['value']} min={a['min']}")
    for a in p["alert_updates"]:
        say(f"  ALERT~  {a['system_name']:14} {a['name']} {a['from']} -> {a['to']}")
    if webhook_missing:
        say("  WEBHOOK notification target missing")

    drift = bool(p["creates"] or p["updates"] or p["orphans"] or p["alert_creates"] or p["alert_updates"]
                 or webhook_missing)
    result = {"drift": drift, "applied": False, "pruned": [], "errors": [],
              "counts": {k: len(v) for k, v in p.items()}, "webhook_missing": webhook_missing}

    if args.apply:
        if args.prune and len(desired) < MIN_DESIRED:
            say(f"REFUSING to prune: only {len(desired)} desired system(s) (minimum {MIN_DESIRED}) — bad render?")
            args.prune = False
            result["errors"].append("prune refused: desired list too small")
        # 1. prune first, so a dead entry never blocks a rename onto its name
        if args.prune:
            for s in p["orphans"]:
                if s["status"] == "up" and not args.prune_up:
                    say(f"  SKIP prune {s['name']}: reported up (use --prune-up)")
                    continue
                st, _ = hub.call("DELETE", f"/api/collections/systems/records/{s['id']}")
                (result["pruned"] if st == 204 else result["errors"]).append(
                    s["name"] if st == 204 else f"delete {s['name']}: HTTP {st}")
        # 2. renames/moves, then creates
        pending = list(p["updates"])
        for _ in range(3):  # a rename can collide with a name another update is about to free; retry in passes
            failed = []
            for u in pending:
                body = {"name": u["desired"]["name"], "host": u["desired"]["address"],
                        "port": str(u["desired"]["port"])}
                st, res = hub.call("PATCH", f"/api/collections/systems/records/{u['hub']['id']}", body)
                if st != 200:
                    failed.append((u, f"HTTP {st} {res}"))
            pending = [u for u, _ in failed]
            if not pending:
                break
        for u, why in failed if pending else []:
            result["errors"].append(f"update {u['hub']['name']}: {why}")
        created = {}
        for d in p["creates"]:
            st, res = hub.call("POST", "/api/collections/systems/records",
                               {"name": d["name"], "host": d["address"], "port": str(d["port"]), "users": [uid]})
            if st == 200:
                created[d["name"]] = res["id"]
            else:
                result["errors"].append(f"create {d['name']}: HTTP {st} {res}")
        # 3. alerts
        for a in p["alert_creates"]:
            sid = a["system_id"] or created.get(a["system_name"])
            if not sid:
                continue
            st, res = hub.call("POST", "/api/collections/alerts/records",
                               {"system": sid, "user": uid, "name": a["name"], "value": a["value"], "min": a["min"]})
            if st != 200:
                result["errors"].append(f"alert {a['system_name']}/{a['name']}: HTTP {st} {res}")
        for a in p["alert_updates"]:
            st, res = hub.call("PATCH", f"/api/collections/alerts/records/{a['id']}",
                               {"value": a["to"][0], "min": a["to"][1]})
            if st != 200:
                result["errors"].append(f"alert update {a['system_name']}/{a['name']}: HTTP {st} {res}")
        # 4. notification target (read-modify-write the whole settings object; never touches other keys)
        if webhook_missing:
            new = dict(settings["settings"])
            new["webhooks"] = hooks + [webhook]
            st, res = hub.call("PATCH", f"/api/collections/user_settings/records/{settings['id']}", {"settings": new})
            if st != 200:
                result["errors"].append(f"webhook: HTTP {st}")
        result["applied"] = True
        if result["errors"]:
            for e in result["errors"]:
                say(f"  ERROR {e}")
    elif drift:
        say("report only — nothing written (re-run with --apply; --prune also deletes orphans)")

    print(json.dumps(result))
    if result["errors"]:
        return 1
    return 0 if (args.apply or not drift) else 2


if __name__ == "__main__":
    sys.exit(main())
