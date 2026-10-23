#!/usr/bin/env python3
# beszel_quiet — open or close a one-time Beszel quiet window for the systems a playbook is about to take down on
# purpose (a reboot), so planned downtime does not page. Run on the controller by tasks/beszel_quiet.yaml.
#
# Beszel DROPS notifications inside a quiet window (it does not queue them), so windows are kept short and tied to
# one run: `open` creates a window from now to now + --minutes for each system; `close` deletes the windows this
# script opened for those systems. A run that dies between the two leaves the window to expire on its own.
#
#   beszel_quiet.py --hub-url URL --action open  --minutes 30 --address 10.10.10.10 [--address ...]
#   beszel_quiet.py --hub-url URL --action close              --address 10.10.10.10 [--address ...]
#
# Systems are matched by ADDRESS (the same identity beszel_sync.py uses). Only one-time windows owned by the sync
# user are ever deleted, and only those still open; daily windows and anyone else's are left alone.
# Credentials come from the environment: BESZEL_USER, BESZEL_PASSWORD. Exit 0 = done (an unknown address is
# reported, not fatal: quiet windows are best effort), 1 = hub error.

import argparse
import datetime
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beszel_sync import Hub  # noqa: E402  (same directory; reuse the API client)


def pb_time(t):
    """PocketBase date format (UTC)."""
    return t.strftime("%Y-%m-%d %H:%M:%S.000Z")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-url", required=True)
    ap.add_argument("--action", choices=["open", "close"], required=True)
    ap.add_argument("--address", action="append", required=True)
    ap.add_argument("--minutes", type=int, default=30)
    args = ap.parse_args()

    hub = Hub(args.hub_url)
    try:
        uid = hub.login(os.environ["BESZEL_USER"], os.environ["BESZEL_PASSWORD"])
        systems = hub.list("systems")
    except (KeyError, RuntimeError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    now = datetime.datetime.now(datetime.timezone.utc)
    by_addr = {s["host"]: s for s in systems}
    errors = 0
    for addr in args.address:
        s = by_addr.get(addr)
        if s is None:
            print(f"  SKIP   {addr}: no Beszel system with this address", file=sys.stderr)
            continue
        if args.action == "open":
            body = {"user": uid, "system": s["id"], "type": "one-time",
                    "start": pb_time(now), "end": pb_time(now + datetime.timedelta(minutes=args.minutes))}
            st, res = hub.call("POST", "/api/collections/quiet_hours/records", body)
            ok = st == 200
            print(f"  {'QUIET' if ok else 'ERROR'}  {s['name']}: {args.minutes} min"
                  + ("" if ok else f" (HTTP {st})"), file=sys.stderr)
        else:
            flt = urllib.parse.quote(f"system='{s['id']}' && user='{uid}' && type='one-time' && end>'{pb_time(now)}'")
            st, res = hub.call("GET", f"/api/collections/quiet_hours/records?perPage=50&filter={flt}")
            items = res.get("items", []) if st == 200 and isinstance(res, dict) else []
            ok = st == 200
            for q in items:
                dst, _ = hub.call("DELETE", f"/api/collections/quiet_hours/records/{q['id']}")
                ok = ok and dst == 204
            print(f"  {'OPEN' if ok else 'ERROR'}  {s['name']}: closed {len(items)} window(s)"
                  + ("" if ok else f" (HTTP {st})"), file=sys.stderr)
        errors += 0 if ok else 1
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
