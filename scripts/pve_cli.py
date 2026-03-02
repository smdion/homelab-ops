#!/usr/bin/env python3
"""Proxmox VE API CLI wrapper for Claude Code.

Connects to the Proxmox VE REST API via token authentication.
Provides commands for listing VMs, checking node/cluster status,
and managing VM power state.

Usage:
    python3 scripts/pve_cli.py <command> [subcommand] [options]

Config:
    [proxmox] section in /config/.claude/semaphore.conf
    Run 'config init --host HOST --user USER --token-id ID --token-secret SECRET' to set up.
"""

import argparse
import configparser
import json
import os
import sys

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "/config/.claude/semaphore.conf"
CONFIG_SECTION = "proxmox"
DEFAULT_PORT = 8006
DEFAULT_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path):
    """Load proxmox config from INI file."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        error(f"Config file not found: {path}\n"
              "Run: python3 scripts/pve_cli.py config init --host HOST "
              "--user USER --token-id ID --token-secret SECRET")
    cp = configparser.RawConfigParser()
    cp.read(path)
    if CONFIG_SECTION not in cp:
        error(f"Missing [{CONFIG_SECTION}] section in {path}\n"
              "Run: python3 scripts/pve_cli.py config init --host HOST "
              "--user USER --token-id ID --token-secret SECRET")
    s = cp[CONFIG_SECTION]
    host = os.environ.get("PVE_HOST", s.get("host", ""))
    port = int(os.environ.get("PVE_PORT", s.get("port", DEFAULT_PORT)))
    user = s.get("user", "")
    token_id = s.get("token_id", "")
    token_secret = s.get("token_secret", "")

    if not host or not token_id or not token_secret:
        error("Config missing host, token_id, or token_secret.")

    nodes = {}
    nodes_str = s.get("nodes", "")
    if nodes_str:
        for entry in nodes_str.split(","):
            entry = entry.strip()
            if ":" in entry:
                name, ip = entry.split(":", 1)
                nodes[name.strip()] = ip.strip()

    return {
        "host": host,
        "port": port,
        "user": user,
        "token_id": token_id,
        "token_secret": token_secret,
        "nodes": nodes,
        "base_url": f"https://{host}:{port}",
    }


def save_config(path, host, port, user, token_id, token_secret, nodes_str):
    """Write [proxmox] section to existing config."""
    path = os.path.expanduser(path)
    cp = configparser.RawConfigParser()
    if os.path.isfile(path):
        cp.read(path)
    cp[CONFIG_SECTION] = {
        "host": host,
        "port": str(port),
        "user": user,
        "token_id": token_id,
        "token_secret": token_secret,
        "nodes": nodes_str,
    }
    with open(path, "w") as f:
        cp.write(f)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def error(msg, detail=None):
    print(f"Error: {msg}", file=sys.stderr)
    if detail:
        print(f"  {detail}", file=sys.stderr)
    sys.exit(1)


def format_table(rows, columns=None, headers=None):
    if not rows:
        return "(no results)"
    if columns is None:
        columns = list(rows[0].keys())
    if headers is None:
        headers = columns
    widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        vals = [str(row.get(c, "") if row.get(c) is not None else "") for c in columns]
        for i, v in enumerate(vals):
            if len(v) > 80:
                vals[i] = v[:77] + "..."
            widths[i] = max(widths[i], len(vals[i]))
        str_rows.append(vals)
    sep = "  "
    lines = [sep.join(h.ljust(w) for h, w in zip(headers, widths))]
    lines.append(sep.join("\u2500" * w for w in widths))
    for vals in str_rows:
        lines.append(sep.join(v.ljust(w) for v, w in zip(vals, widths)))
    return "\n".join(lines)


def output(data, args, columns=None, headers=None):
    fmt = getattr(args, "format", "json")
    if fmt == "table" and isinstance(data, list) and data:
        print(format_table(data, columns, headers))
    else:
        print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def api_request(method, path, config, params=None, data=None,
                timeout=DEFAULT_TIMEOUT):
    """Core HTTP method for PVE API."""
    url = config["base_url"] + path
    auth_value = f"PVEAPIToken={config['user']}!{config['token_id']}={config['token_secret']}"
    headers = {
        "Authorization": auth_value,
        "Accept": "application/json",
    }
    try:
        resp = requests.request(
            method, url, headers=headers, params=params, json=data,
            verify=False, timeout=timeout,
        )
    except requests.ConnectionError:
        error(f"Cannot connect to {config['base_url']}", "Check host and network.")
    except requests.Timeout:
        error(f"Request timed out after {timeout}s")

    if resp.status_code == 401:
        error("Authentication failed", "Check API token credentials.")
    if resp.status_code == 403:
        error("Permission denied")
    if resp.status_code == 404:
        error(f"Not found: {method} {path}")
    if resp.status_code >= 500:
        error(f"Server error ({resp.status_code})", resp.text)
    if resp.status_code >= 400:
        error(f"HTTP {resp.status_code}", resp.text)

    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def api_get(path, config, params=None, timeout=DEFAULT_TIMEOUT):
    return api_request("GET", path, config, params=params, timeout=timeout)


def api_post(path, config, data=None, timeout=DEFAULT_TIMEOUT):
    return api_request("POST", path, config, data=data, timeout=timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_vm_node(config, vmid):
    """Find which node a VMID lives on via cluster resources."""
    result = api_get("/api2/json/cluster/resources", config,
                     params={"type": "vm"})
    data = result.get("data", [])
    for vm in data:
        if vm.get("vmid") == vmid:
            return vm.get("node")
    error(f"VMID {vmid} not found on any node")


def format_uptime(seconds):
    """Format seconds into human-readable uptime."""
    if not seconds:
        return ""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_bytes(b):
    """Format bytes into human-readable size."""
    if not b:
        return ""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}PB"


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


def cmd_config_init(args, config=None):
    """Set up proxmox config."""
    test_cfg = {
        "base_url": f"https://{args.host}:{args.port}",
        "user": args.user,
        "token_id": args.token_id,
        "token_secret": args.token_secret,
    }
    try:
        result = api_get("/api2/json/version", test_cfg)
        version = result.get("data", {}).get("version", "unknown")
    except SystemExit:
        version = "connection failed"

    nodes_str = args.nodes or ""
    save_config(args.config, args.host, args.port, args.user,
                args.token_id, args.token_secret, nodes_str)
    print(json.dumps({"status": "ok",
                       "message": f"Proxmox config saved to {args.config}",
                       "pve_version": version}))


def cmd_config_show(args, config=None):
    cfg = load_config(args.config)
    masked = cfg["token_secret"][-4:].rjust(len(cfg["token_secret"]), "*")
    print(json.dumps({"host": cfg["host"], "port": cfg["port"],
                       "user": cfg["user"], "token_id": cfg["token_id"],
                       "token_secret": masked,
                       "nodes": cfg["nodes"]}))


def cmd_config_test(args, config=None):
    cfg = load_config(args.config)
    result = api_get("/api2/json/version", cfg)
    version = result.get("data", {}).get("version", "unknown")
    print(json.dumps({"status": "ok", "pve_version": version,
                       "host": cfg["host"]}))


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------


def cmd_ping(args, config):
    result = api_get("/api2/json/version", config)
    data = result.get("data", {})
    print(json.dumps({"status": "ok", "version": data.get("version"),
                       "release": data.get("release")}))


# ---------------------------------------------------------------------------
# Node commands
# ---------------------------------------------------------------------------


def cmd_node_list(args, config):
    """List cluster nodes with status."""
    result = api_get("/api2/json/nodes", config)
    data = result.get("data", [])
    rows = []
    for n in sorted(data, key=lambda x: x.get("node", "")):
        rows.append({
            "node": n.get("node", ""),
            "status": n.get("status", ""),
            "uptime": format_uptime(n.get("uptime", 0)),
            "cpu": f"{n.get('cpu', 0) * 100:.1f}%",
            "mem_used": format_bytes(n.get("mem", 0)),
            "mem_total": format_bytes(n.get("maxmem", 0)),
            "ip": config["nodes"].get(n.get("node", ""), ""),
        })
    output(rows, args,
           ["node", "status", "uptime", "cpu", "mem_used", "mem_total", "ip"],
           ["Node", "Status", "Uptime", "CPU%", "Mem Used", "Mem Total", "IP"])


def cmd_node_status(args, config):
    """Get detailed node status."""
    result = api_get(f"/api2/json/nodes/{args.node}/status", config)
    data = result.get("data", {})
    output(data, args)


# ---------------------------------------------------------------------------
# VM commands
# ---------------------------------------------------------------------------


def cmd_vm_list(args, config):
    """List VMs — qm list equivalent."""
    if args.node:
        nodes_to_query = [args.node]
    else:
        result = api_get("/api2/json/nodes", config)
        nodes_to_query = [n["node"] for n in result.get("data", [])
                          if n.get("status") == "online"]

    rows = []
    for node in sorted(nodes_to_query):
        result = api_get(f"/api2/json/nodes/{node}/qemu", config)
        for vm in result.get("data", []):
            rows.append({
                "vmid": vm.get("vmid", ""),
                "name": vm.get("name", ""),
                "status": vm.get("status", ""),
                "node": node,
                "cpus": vm.get("cpus", ""),
                "mem": format_bytes(vm.get("maxmem", 0)),
                "disk": format_bytes(vm.get("maxdisk", 0)),
                "uptime": format_uptime(vm.get("uptime", 0)),
            })
    rows.sort(key=lambda x: (x["node"], x["vmid"]))
    output(rows, args,
           ["vmid", "name", "status", "node", "cpus", "mem", "disk", "uptime"],
           ["VMID", "Name", "Status", "Node", "CPUs", "Memory", "Disk", "Uptime"])


def cmd_vm_status(args, config):
    """Get VM status details."""
    node = args.node or find_vm_node(config, args.vmid)
    result = api_get(
        f"/api2/json/nodes/{node}/qemu/{args.vmid}/status/current", config)
    data = result.get("data", {})
    output(data, args)


def cmd_vm_start(args, config):
    """Start a VM."""
    node = args.node or find_vm_node(config, args.vmid)
    result = api_post(
        f"/api2/json/nodes/{node}/qemu/{args.vmid}/status/start", config)
    print(json.dumps({"status": "ok", "vmid": args.vmid, "node": node,
                       "task": result.get("data", "")}))


def cmd_vm_stop(args, config):
    """Stop a VM (hard stop)."""
    node = args.node or find_vm_node(config, args.vmid)
    result = api_post(
        f"/api2/json/nodes/{node}/qemu/{args.vmid}/status/stop", config)
    print(json.dumps({"status": "ok", "vmid": args.vmid, "node": node,
                       "task": result.get("data", "")}))


def cmd_vm_shutdown(args, config):
    """Gracefully shutdown a VM."""
    node = args.node or find_vm_node(config, args.vmid)
    result = api_post(
        f"/api2/json/nodes/{node}/qemu/{args.vmid}/status/shutdown", config)
    print(json.dumps({"status": "ok", "vmid": args.vmid, "node": node,
                       "task": result.get("data", "")}))


# ---------------------------------------------------------------------------
# Cluster commands
# ---------------------------------------------------------------------------


def cmd_cluster_status(args, config):
    """Get cluster status."""
    result = api_get("/api2/json/cluster/status", config)
    data = result.get("data", [])
    output(data, args)


def cmd_cluster_resources(args, config):
    """List cluster resources."""
    params = {}
    if args.type:
        params["type"] = args.type
    result = api_get("/api2/json/cluster/resources", config, params=params)
    data = result.get("data", [])

    if args.type == "vm":
        rows = []
        for r in sorted(data, key=lambda x: (x.get("node", ""), x.get("vmid", 0))):
            rows.append({
                "vmid": r.get("vmid", ""),
                "name": r.get("name", ""),
                "status": r.get("status", ""),
                "node": r.get("node", ""),
                "type": r.get("type", ""),
                "cpu": f"{r.get('cpu', 0) * 100:.1f}%",
                "mem": format_bytes(r.get("mem", 0)),
                "maxmem": format_bytes(r.get("maxmem", 0)),
            })
        output(rows, args,
               ["vmid", "name", "status", "node", "type", "cpu", "mem", "maxmem"],
               ["VMID", "Name", "Status", "Node", "Type", "CPU%", "Mem", "MaxMem"])
    else:
        output(data, args)


# ---------------------------------------------------------------------------
# Storage commands
# ---------------------------------------------------------------------------


def cmd_storage_list(args, config):
    """List storage on a node or all nodes."""
    if args.node:
        result = api_get(f"/api2/json/nodes/{args.node}/storage", config)
        data = result.get("data", [])
    else:
        result = api_get("/api2/json/cluster/resources", config,
                         params={"type": "storage"})
        data = result.get("data", [])

    rows = []
    for s in sorted(data, key=lambda x: (x.get("node", ""), x.get("storage", ""))):
        rows.append({
            "node": s.get("node", ""),
            "storage": s.get("storage", ""),
            "type": s.get("type", s.get("plugintype", "")),
            "status": s.get("status", ""),
            "used": format_bytes(s.get("used", s.get("disk", 0))),
            "total": format_bytes(s.get("total", s.get("maxdisk", 0))),
        })
    output(rows, args,
           ["node", "storage", "type", "status", "used", "total"],
           ["Node", "Storage", "Type", "Status", "Used", "Total"])


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="pve_cli.py",
        description="Proxmox VE API CLI wrapper",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG,
                   help=f"Config file path (default: {DEFAULT_CONFIG})")
    p.add_argument("--format", choices=["json", "table"], default="json",
                   help="Output format (default: json)")

    sub = p.add_subparsers(dest="command", required=True)

    # --- ping ---
    sub.add_parser("ping", help="Check PVE API connectivity")

    # --- config ---
    cfg = sub.add_parser("config", help="Manage configuration")
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)

    cfg_init = cfg_sub.add_parser("init", help="Set up proxmox config")
    cfg_init.add_argument("--host", required=True, help="PVE host/VIP")
    cfg_init.add_argument("--port", type=int, default=DEFAULT_PORT,
                          help="API port (default: 8006)")
    cfg_init.add_argument("--user", required=True, help="PVE user (e.g. root@pam)")
    cfg_init.add_argument("--token-id", dest="token_id", required=True,
                          help="API token ID")
    cfg_init.add_argument("--token-secret", dest="token_secret", required=True,
                          help="API token secret")
    cfg_init.add_argument("--nodes", help="Node map: name1:ip1,name2:ip2,...")

    cfg_sub.add_parser("show", help="Show config")
    cfg_sub.add_parser("test", help="Test connection")

    # --- node ---
    node = sub.add_parser("node", help="Node operations")
    node_sub = node.add_subparsers(dest="node_action", required=True)

    node_sub.add_parser("list", help="List cluster nodes")

    node_status = node_sub.add_parser("status", help="Node status details")
    node_status.add_argument("node", help="Node name")

    # --- vm ---
    vm = sub.add_parser("vm", help="VM operations")
    vm_sub = vm.add_subparsers(dest="vm_action", required=True)

    vm_list = vm_sub.add_parser("list", help="List VMs (qm list)")
    vm_list.add_argument("--node", help="Filter by node")

    vm_status = vm_sub.add_parser("status", help="VM status")
    vm_status.add_argument("vmid", type=int, help="VM ID")
    vm_status.add_argument("--node", help="Node (auto-detected if omitted)")

    vm_start = vm_sub.add_parser("start", help="Start a VM")
    vm_start.add_argument("vmid", type=int, help="VM ID")
    vm_start.add_argument("--node", help="Node (auto-detected if omitted)")

    vm_stop = vm_sub.add_parser("stop", help="Stop a VM (hard)")
    vm_stop.add_argument("vmid", type=int, help="VM ID")
    vm_stop.add_argument("--node", help="Node (auto-detected if omitted)")

    vm_shutdown = vm_sub.add_parser("shutdown", help="Graceful shutdown")
    vm_shutdown.add_argument("vmid", type=int, help="VM ID")
    vm_shutdown.add_argument("--node", help="Node (auto-detected if omitted)")

    # --- cluster ---
    cluster = sub.add_parser("cluster", help="Cluster operations")
    cluster_sub = cluster.add_subparsers(dest="cluster_action", required=True)

    cluster_sub.add_parser("status", help="Cluster status")

    cluster_res = cluster_sub.add_parser("resources", help="Cluster resources")
    cluster_res.add_argument("--type", choices=["vm", "storage", "node"],
                             help="Filter by resource type")

    # --- storage ---
    storage = sub.add_parser("storage", help="Storage operations")
    storage_sub = storage.add_subparsers(dest="storage_action", required=True)

    storage_list = storage_sub.add_parser("list", help="List storage")
    storage_list.add_argument("--node", help="Filter by node")

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "ping": cmd_ping,
    "config": {
        "init": cmd_config_init,
        "show": cmd_config_show,
        "test": cmd_config_test,
    },
    "node": {
        "list": cmd_node_list,
        "status": cmd_node_status,
    },
    "vm": {
        "list": cmd_vm_list,
        "status": cmd_vm_status,
        "start": cmd_vm_start,
        "stop": cmd_vm_stop,
        "shutdown": cmd_vm_shutdown,
    },
    "cluster": {
        "status": cmd_cluster_status,
        "resources": cmd_cluster_resources,
    },
    "storage": {
        "list": cmd_storage_list,
    },
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if isinstance(handler, dict):
        action_key = f"{args.command}_action"
        action = getattr(args, action_key, None)
        handler = handler.get(action)

    if not handler:
        parser.print_help()
        sys.exit(1)

    needs_config = args.command != "config"
    config = load_config(args.config) if needs_config else None

    handler(args, config)


if __name__ == "__main__":
    main()
