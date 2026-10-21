#!/usr/bin/env python3
"""
Replace a dead PIA WireGuard tunnel on the UDM with a freshly generated config.

Runs on the liberty HOST, once per User Scripts tick (same wrapper as
pia_port_forward.py, and before it). Stateless between invocations apart from
its own JSON state file.

WHY THIS EXISTS:
PIA has no static WireGuard configs. A config is a key registered on ONE
server (PIA's addKey API), and the server can go offline or drop the key at
any time. The tunnel then silently dies: UniFi keeps showing the VPN client as
CONNECTED (WireGuard has no session to lose), and the fix used to be by hand —
generate a new config in the piawg web UI, add /32 to the Address, upload it
to the UniFi VPN client. This script does exactly that for every tunnel listed
in config.json "wg_tunnels", using PIA's own manual-connections flow
(token -> server list -> addKey), and writes the result into the VPN client's
networkconf so the UDM re-peers.

HOW A DEAD TUNNEL IS DETECTED:
By real traffic through it, never by UniFi's status. Each tunnel has a probe:
  container — curl from inside a container whose VLAN is policy-routed through
              the tunnel (SWE: transmission)
  host      — curl from the host to domains that a domain policy route sends
              through the tunnel (LAS: tracker sites)
A tunnel counts as down when EVERY probe URL fails while the host's own
control probe (plain WAN, no tunnel) succeeds, so a WAN outage, a stopped
container or one tracker being down never triggers a rotation.
FAILS_BEFORE_ROTATE consecutive down ticks trigger one.

AFTER A SWE ROTATION:
pia_port_forward.py notices the endpoint changed on its next run (the wrapper
runs it right after this) and requests a new forwarded port from the new
server, so port forwarding recovers without anything else here.

Usage:
  pia_wg_rotate.py                  # check every tunnel, rotate dead ones
  pia_wg_rotate.py --force NAME     # rotate NAME now (e.g. "PIAVPN - SWE")
  pia_wg_rotate.py --force NAME --dry-run   # build + register, do not touch UniFi
"""

import argparse
import ipaddress
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONFIG_PATH = "/mnt/user/appdata/transmission/pia_pf/config.json"
STATE_PATH = "/mnt/user/appdata/transmission/pia_pf/wg_state.json"
# Separate from pia_pf.log on purpose: maintain_health's pia_vpn_tunnel check
# counts trailing ERROR lines in that file.
LOG_PATH = "/mnt/user/appdata/transmission/pia_pf/pia_wg.log"
HOST_CA_PATH = "/mnt/user/appdata/transmission/pia_pf/ca.rsa.4096.crt"

PIA_SERVERLIST_URL = "https://serverlist.piaservers.net/vpninfo/servers/v6"
PIA_TOKEN_URL = "https://www.privateinternetaccess.com/api/client/v2/token"
# Reached over the plain WAN; proves the host itself is online before a failed
# tunnel probe is believed.
CONTROL_URL = "https://1.1.1.1/"

FAILS_BEFORE_ROTATE = 3        # ticks are 15 min apart -> ~45 min of confirmed outage
ROTATE_COOLDOWN = 3600         # never auto-rotate the same tunnel more than once an hour
SERVERLIST_SAMPLES = 4         # the server list is a rotating subset; union a few samples
MAX_SERVERS_PER_ROTATION = 3
SETTLE_SECONDS = 20            # UDM re-peers after the networkconf PUT
VERIFY_ATTEMPTS = 3
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
        pass  # logging must never be the reason a cycle fails


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
    os.chmod(STATE_PATH, 0o600)


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


def curl_ok(url, container=None):
    """True when the URL answers with any HTTP response (status code is irrelevant)."""
    cmd = ["curl", "-4", "-s", "-o", "/dev/null", "--max-time", "10", url]
    if container:
        cmd = ["docker", "exec", container] + cmd
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def container_running(name):
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                         capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == "true"


def probe(tunnel):
    """True = up, False = down, None = cannot tell (probe source unavailable)."""
    p = tunnel["probe"]
    container = p.get("container") if p["via"] == "container" else None
    if container and not container_running(container):
        return None
    return any(curl_ok(url, container) for url in p["urls"])


# ── UniFi ────────────────────────────────────────────────────────────────────

def netconf_url(cfg, tunnel):
    return f"{cfg['unifi_url']}/proxy/network/api/s/default/rest/networkconf/{tunnel['network_id']}"


def unifi_headers(cfg):
    return {"X-API-Key": cfg["unifi_api_key"], "Accept": "application/json"}


def current_endpoint_ip(cfg, tunnel):
    conf = http_json(netconf_url(cfg, tunnel), headers=unifi_headers(cfg), insecure=True)
    m = re.search(r"Endpoint\s*=\s*([0-9.]+):",
                  conf["data"][0].get("wireguard_client_configuration_file", ""))
    return m.group(1) if m else None


def lan_subnets(cfg):
    """Every non-VPN UniFi network; a tunnel address inside one would break routing to it."""
    conf = http_json(f"{cfg['unifi_url']}/proxy/network/api/s/default/rest/networkconf",
                     headers=unifi_headers(cfg), insecure=True)
    nets = []
    for n in conf["data"]:
        if n.get("purpose") != "vpn-client" and n.get("ip_subnet"):
            try:
                nets.append(ipaddress.ip_network(n["ip_subnet"], strict=False))
            except ValueError:
                pass
    return nets


def apply_to_unifi(cfg, tunnel, conf_text, peer_ip, filename):
    url = netconf_url(cfg, tunnel)
    current = http_json(url, headers=unifi_headers(cfg), insecure=True)["data"][0]
    current["wireguard_client_configuration_file"] = conf_text
    current["wireguard_client_configuration_filename"] = filename
    current["ip_subnet"] = f"{peer_ip}/32"
    http_json(url, method="PUT", headers=unifi_headers(cfg), data=current, insecure=True)


# ── PIA ──────────────────────────────────────────────────────────────────────

def get_token(cfg):
    body = urllib.parse.urlencode({"username": cfg["pia_user"], "password": cfg["pia_pass"]}).encode()
    # PIA's CDN answers 403 to urllib's default "Python-urllib" User-Agent.
    req = urllib.request.Request(PIA_TOKEN_URL, data=body, method="POST",
                                 headers={"User-Agent": "pia_wg_rotate"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["token"]


def region_servers(region_id):
    """Union of the region's WireGuard servers over several server-list samples."""
    found = {}
    for _ in range(SERVERLIST_SAMPLES):
        raw = urllib.request.urlopen(PIA_SERVERLIST_URL, timeout=20).read().decode()
        for region in json.loads(raw.split("\n", 1)[0])["regions"]:  # line 1 = payload
            if region["id"] == region_id and not region.get("offline"):
                for entry in region.get("servers", {}).get("wg", []):
                    found[entry["ip"]] = entry["cn"]
        time.sleep(1)
    return found


def wg_keypair():
    priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True).stdout.strip()
    pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True,
                         check=True).stdout.strip()
    return priv, pub


def add_key(token, ip, cn, pubkey):
    out = subprocess.run([
        "curl", "-s", "--max-time", "15", "-G",
        "--connect-to", f"{cn}::{ip}:",
        "--cacert", HOST_CA_PATH,
        "--data-urlencode", f"pt={token}",
        "--data-urlencode", f"pubkey={pubkey}",
        f"https://{cn}:1337/addKey",
    ], capture_output=True, text=True, timeout=25)
    if out.returncode != 0:
        raise RuntimeError(f"addKey curl rc={out.returncode}")
    data = json.loads(out.stdout)
    if data.get("status") != "OK":
        raise RuntimeError(f"addKey returned: {data.get('status')}")
    return data


def build_conf(priv, reg, region_id):
    # Same shape as the piawg generator's output, with the /32 the UDM needs
    # already on the Address.
    return (
        "[Interface]\n"
        f"Address = {reg['peer_ip']}/32\n"
        f"PrivateKey = {priv}\n"
        f"DNS = {','.join(reg['dns_servers'])}\n"
        "\n"
        f"# PIA-{region_id} (generated by pia_wg_rotate.py)\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {reg['server_key']}\n"
        f"Endpoint = {reg['server_ip']}:{reg['server_port']}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )


# ── Notifications ────────────────────────────────────────────────────────────

def notify(cfg, title, description, color, fields):
    if not cfg.get("discord_webhook_id"):
        return
    url = f"https://discord.com/api/webhooks/{cfg['discord_webhook_id']}/{cfg['discord_webhook_token']}"
    embed = {
        "title": title, "description": description, "color": color,
        "author": {"name": cfg.get("host_name", "liberty")},
        "fields": [{"name": k, "value": v, "inline": True} for k, v in fields],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        req = urllib.request.Request(url, data=json.dumps({"username": "Ansible Bot", "embeds": [embed]}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "pia_wg_rotate"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:
        log(f"WARNING: Discord notification failed: {exc}")


# ── Rotation ─────────────────────────────────────────────────────────────────

def rotate(cfg, tunnel, reason, dry_run=False):
    """Try servers until one carries traffic. Returns the server IP that worked, or None."""
    name, region_id = tunnel["name"], tunnel["region"]
    current_ip = current_endpoint_ip(cfg, tunnel)
    servers = region_servers(region_id)
    if not servers:
        raise RuntimeError(f"no online WireGuard servers listed for region '{region_id}'")
    # Other servers first; the current one last, since PIA also drops idle keys
    # from servers that are otherwise healthy (a fresh key on it can be the fix).
    order = [ip for ip in servers if ip != current_ip]
    random.shuffle(order)
    if current_ip in servers:
        order.append(current_ip)

    token = get_token(cfg)
    lans = lan_subnets(cfg)
    for ip in order[:MAX_SERVERS_PER_ROTATION]:
        cn = servers[ip]
        try:
            priv, pub = wg_keypair()
            reg = add_key(token, ip, cn, pub)
        except Exception as exc:
            log(f"{name}: {ip} ({cn}) addKey failed: {exc}")
            continue
        clash = next((n for n in lans if ipaddress.ip_address(reg["peer_ip"]) in n), None)
        if clash:
            log(f"{name}: {ip} assigned {reg['peer_ip']}, inside LAN {clash}; skipping")
            continue
        conf = build_conf(priv, reg, region_id)
        if dry_run:
            log(f"{name}: DRY RUN — would apply {reg['server_ip']} ({cn}), peer {reg['peer_ip']}/32")
            print(re.sub(r"(PrivateKey = ).*", r"\1<redacted>", conf))
            return reg["server_ip"]
        apply_to_unifi(cfg, tunnel, conf, reg["peer_ip"], f"PIA-{region_id}-{cn}.conf")
        log(f"{name}: applied {reg['server_ip']} ({cn}), peer {reg['peer_ip']}/32; verifying")
        for _ in range(VERIFY_ATTEMPTS):
            time.sleep(SETTLE_SECONDS)
            if probe(tunnel):
                log(f"{name}: tunnel carries traffic via {reg['server_ip']}")
                notify(cfg, f"PIA VPN — {name}", f"Rotated — {reason}", 0x2ECC71,
                       [("Old server", current_ip or "unknown"), ("New server", f"{reg['server_ip']} ({cn})")])
                return reg["server_ip"]
        log(f"{name}: {reg['server_ip']} applied but the probe still fails; trying the next server")
    notify(cfg, f"PIA VPN — {name}", f"Failed — rotation found no working server ({reason})", 0xE74C3C,
           [("Region", region_id), ("Tried", ", ".join(order[:MAX_SERVERS_PER_ROTATION]))])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", metavar="NAME", help="rotate this tunnel now")
    ap.add_argument("--dry-run", action="store_true", help="with --force: register a key but leave UniFi alone")
    args = ap.parse_args()

    cfg = read_json(CONFIG_PATH)
    if not cfg or not cfg.get("wg_tunnels"):
        log(f"FATAL: missing config or wg_tunnels in {CONFIG_PATH}")
        return 1
    state = read_json(STATE_PATH, {}) or {}
    now = int(time.time())
    rc = 0

    if args.force:
        tunnel = next((t for t in cfg["wg_tunnels"] if t["name"] == args.force), None)
        if not tunnel:
            log(f"FATAL: no tunnel named '{args.force}'")
            return 1
        try:
            ok = rotate(cfg, tunnel, "forced", dry_run=args.dry_run)
        except Exception as exc:
            log(f"ERROR: {tunnel['name']}: rotation failed: {exc}")
            return 1
        if not args.dry_run:
            state[tunnel["name"]] = {"fails": 0, "last_rotation": now}
            write_state(state)
        return 0 if ok else 1

    if not curl_ok(CONTROL_URL):
        log("host WAN control probe failed; skipping tunnel checks this tick")
        return 0

    for tunnel in cfg["wg_tunnels"]:
        name = tunnel["name"]
        st = state.setdefault(name, {"fails": 0})
        up = probe(tunnel)
        if up is None:
            continue  # probe source unavailable; neither a failure nor a recovery
        if up:
            if st.get("fails"):
                log(f"{name}: tunnel healthy again after {st['fails']} failed probe(s)")
            st["fails"] = 0
            continue
        st["fails"] = st.get("fails", 0) + 1
        log(f"{name}: probe failed ({st['fails']}/{FAILS_BEFORE_ROTATE})")
        if st["fails"] < FAILS_BEFORE_ROTATE:
            continue
        if now - st.get("last_rotation", 0) < ROTATE_COOLDOWN:
            log(f"{name}: rotated less than {ROTATE_COOLDOWN // 60} min ago; waiting")
            continue
        st["last_rotation"] = now
        try:
            if rotate(cfg, tunnel, f"{st['fails']} consecutive failed probes"):
                st["fails"] = 0
            else:
                rc = 1
        except Exception as exc:
            log(f"ERROR: {name}: rotation failed: {exc}")
            rc = 1

    write_state(state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
