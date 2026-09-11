#!/usr/bin/env python3
"""
Maintain PIA's forwarded port and propagate it to UniFi and Transmission.

Runs on the liberty HOST (not inside a container), once per User Scripts tick.
Stateless between invocations apart from its own JSON state file.

WHY THE SPLIT EXECUTION MODEL:
PIA's port-forward API (getSignature/bindPort) only answers requests that
physically traverse the PIA tunnel. Transmission's VLAN is what the router
policy-routes through PIA, so those three calls are proxied via
`docker exec transmission curl ...` to borrow its network namespace. The
reverse is also true and is why the whole script cannot simply live inside
that container: everything on that VLAN egresses through the tunnel, so the
container cannot reach the UniFi controller on the LAN at all. The host can
reach UniFi and Transmission's RPC directly, so those calls are made here.
Transmission's own process/entrypoint is never modified.

WHY THE PAYLOAD IS CACHED:
Each getSignature call issues a BRAND NEW port. Re-requesting it every cycle
would rotate the port every 15 minutes and guarantee the firewall/client are
perpetually stale. The payload+signature pair is therefore persisted and only
re-requested when it is missing, near expiry, or the tunnel moved to a
different PIA server. Steady-state cycles only call bindPort.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64decode
from datetime import datetime, timezone

CONFIG_PATH = "/mnt/user/appdata/transmission/pia_pf/config.json"
STATE_PATH = "/mnt/user/appdata/transmission/pia_pf/state.json"
LOG_PATH = "/mnt/user/appdata/transmission/pia_pf/pia_pf.log"
# Path to PIA's CA as seen from INSIDE the container (/config is the bind mount
# for /mnt/user/appdata/transmission), so curl there can verify their
# self-signed port-forward endpoint properly instead of running insecure.
CONTAINER_CA_PATH = "/config/pia_pf/ca.rsa.4096.crt"

CONTAINER = "transmission"
PIA_SERVERLIST_URL = "https://serverlist.piaservers.net/vpninfo/servers/v6"
PIA_TOKEN_URL = "https://www.privateinternetaccess.com/api/client/v2/token"

# PIA drops the bind if it is not refreshed inside ~15 min, so this script is
# expected to run at least that often. These thresholds decide when the cached
# credentials are refreshed EARLY, so a cycle never races an expiry boundary.
TOKEN_RENEW_MARGIN = 2 * 3600          # token lives 24h; renew with 2h to spare
PAYLOAD_RENEW_MARGIN = 3 * 86400       # payload lives ~60d; renew with 3d to spare
LOG_MAX_BYTES = 512 * 1024


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # logging must never be the reason a refresh cycle fails


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)
    os.chmod(STATE_PATH, 0o600)  # holds a PIA auth token and bind signature


def http_json(url, method="GET", headers=None, data=None, insecure=False):
    import ssl
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else {}


def in_tunnel_curl(args, timeout=25):
    """Run curl inside the Transmission container to borrow the PIA tunnel."""
    cmd = ["docker", "exec", CONTAINER, "curl", "-s", "--max-time", "15"] + args
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"curl in container failed rc={out.returncode}: {out.stderr.strip()}")
    return out.stdout


def current_gateway_ip(cfg):
    """Read the endpoint the router's WireGuard profile is currently peered with."""
    netconf = http_json(
        f"{cfg['unifi_url']}/proxy/network/api/s/default/rest/networkconf/{cfg['pia_network_id']}",
        headers={"X-API-Key": cfg["unifi_api_key"], "Accept": "application/json"},
        insecure=True,
    )
    conf_file = netconf["data"][0].get("wireguard_client_configuration_file", "")
    m = re.search(r"Endpoint\s*=\s*([0-9.]+):", conf_file)
    if not m:
        raise RuntimeError("could not parse Endpoint from the UniFi WireGuard profile")
    return m.group(1)


def resolve_cn(gateway_ip, attempts=4):
    """
    Map a gateway IP to the certificate CN getSignature/bindPort must be
    addressed as, and confirm its region offers port forwarding.

    PIA's server list returns a ROTATING SUBSET of their fleet, so a given IP
    is frequently absent from any single response even while it is perfectly
    valid. Retrying re-rolls that sample; the caller caches the result so this
    lookup only happens when the tunnel actually moves to a new server.
    """
    last_seen_regions = 0
    for attempt in range(attempts):
        raw = urllib.request.urlopen(PIA_SERVERLIST_URL, timeout=20).read().decode()
        servers = json.loads(raw.split("\n", 1)[0])  # payload is line 1; signature follows
        last_seen_regions = len(servers.get("regions", []))
        for region in servers["regions"]:
            for entry in region.get("servers", {}).get("wg", []):
                if entry.get("ip") == gateway_ip:
                    if not region.get("port_forward"):
                        raise RuntimeError(
                            f"region '{region['name']}' does not support port forwarding"
                        )
                    return entry["cn"], region["name"]
        if attempt < attempts - 1:
            time.sleep(2)
    raise RuntimeError(
        f"gateway {gateway_ip} absent from {attempts} server-list samples "
        f"({last_seen_regions} regions in the last one)"
    )


def get_token(cfg):
    resp = in_tunnel_curl([
        "-u", f"{cfg['pia_user']}:{cfg['pia_pass']}",
        "--data-urlencode", f"username={cfg['pia_user']}",
        "--data-urlencode", f"password={cfg['pia_pass']}",
        PIA_TOKEN_URL,
    ])
    token = json.loads(resp)["token"]
    return token, int(time.time()) + 24 * 3600


def get_signature(token, gateway_ip, cn):
    resp = in_tunnel_curl([
        "-G", "--connect-to", f"{cn}::{gateway_ip}:",
        "--cacert", CONTAINER_CA_PATH,
        "--data-urlencode", f"token={token}",
        f"https://{cn}:19999/getSignature",
    ])
    data = json.loads(resp)
    if data.get("status") != "OK":
        raise RuntimeError(f"getSignature returned: {data}")
    inner = json.loads(b64decode(data["payload"]))
    expires = int(
        datetime.fromisoformat(
            re.sub(r"\.\d+Z$", "+00:00", inner["expires_at"])
        ).replace(tzinfo=timezone.utc).timestamp()
    )
    return data["payload"], data["signature"], int(inner["port"]), expires


def bind_port(payload, signature, gateway_ip, cn):
    resp = in_tunnel_curl([
        "-G", "--connect-to", f"{cn}::{gateway_ip}:",
        "--cacert", CONTAINER_CA_PATH,
        "--data-urlencode", f"payload={payload}",
        "--data-urlencode", f"signature={signature}",
        f"https://{cn}:19999/bindPort",
    ])
    data = json.loads(resp)
    if data.get("status") != "OK":
        raise RuntimeError(f"bindPort returned: {data}")
    return data.get("message", "")


def update_unifi_port(cfg, port):
    base = f"{cfg['unifi_url']}/proxy/network/api/s/default/rest/firewallgroup/{cfg['unifi_port_group_id']}"
    hdrs = {"X-API-Key": cfg["unifi_api_key"], "Accept": "application/json"}
    current = http_json(base, headers=hdrs, insecure=True)["data"][0]
    if current.get("group_members") == [str(port)]:
        return False
    current["group_members"] = [str(port)]
    http_json(base, method="PUT", headers=hdrs, data=current, insecure=True)
    return True


def update_transmission_port(cfg, port):
    url = cfg["transmission_rpc_url"]
    try:
        http_json(url, method="POST", data={"method": "session-get"})
        sid = ""
    except urllib.error.HTTPError as exc:
        sid = exc.headers.get("X-Transmission-Session-Id", "")
    hdrs = {"X-Transmission-Session-Id": sid}
    cur = http_json(url, method="POST", headers=hdrs, data={"method": "session-get"})
    if cur["arguments"].get("peer-port") == port:
        return False
    http_json(url, method="POST", headers=hdrs,
              data={"method": "session-set", "arguments": {"peer-port": port}})
    return True


def main():
    cfg = read_json(CONFIG_PATH)
    if not cfg:
        log(f"FATAL: missing or unreadable config at {CONFIG_PATH}")
        return 1
    state = read_json(STATE_PATH, {}) or {}
    now = int(time.time())

    try:
        gateway_ip = current_gateway_ip(cfg)
        if state.get("gateway_ip") == gateway_ip and state.get("gateway_cn"):
            cn, region = state["gateway_cn"], state.get("gateway_region", "cached")
        else:
            # Tunnel moved (or first run). The previous bind belongs to the old
            # server and is worthless here, so resolve the new server and force
            # a fresh signature.
            previous = state.get("gateway_ip")
            cn, region = resolve_cn(gateway_ip)
            if previous and previous != gateway_ip:
                log(f"gateway moved {previous} -> {gateway_ip} ({region}); re-requesting")
            else:
                log(f"resolved gateway {gateway_ip} ({region})")
            state.pop("payload", None)
            state["gateway_ip"] = gateway_ip
            state["gateway_cn"] = cn
            state["gateway_region"] = region
    except Exception as exc:
        log(f"FATAL: gateway discovery failed: {exc}")
        return 1

    try:
        if not state.get("token") or state.get("token_expires", 0) - now < TOKEN_RENEW_MARGIN:
            state["token"], state["token_expires"] = get_token(cfg)
            log("obtained a new PIA auth token")

        if not state.get("payload") or state.get("payload_expires", 0) - now < PAYLOAD_RENEW_MARGIN:
            (state["payload"], state["signature"],
             state["port"], state["payload_expires"]) = get_signature(
                state["token"], gateway_ip, cn)
            expiry = datetime.fromtimestamp(state["payload_expires"]).strftime("%Y-%m-%d")
            log(f"new forwarded port {state['port']} from {region} (expires {expiry})")

        msg = bind_port(state["payload"], state["signature"], gateway_ip, cn)
        log(f"bind refreshed: port {state['port']} ({msg})")
    except Exception as exc:
        log(f"ERROR: PIA refresh failed: {exc}")
        write_state(state)
        return 1

    port = state["port"]
    changed = []
    try:
        if update_unifi_port(cfg, port):
            changed.append("unifi")
        if update_transmission_port(cfg, port):
            changed.append("transmission")
    except Exception as exc:
        log(f"ERROR: propagating port {port} failed: {exc}")
        write_state(state)
        return 1

    state["applied_port"] = port
    write_state(state)
    if changed:
        log(f"port {port} propagated to: {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
