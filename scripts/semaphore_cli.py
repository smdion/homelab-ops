#!/usr/bin/env python3
"""Semaphore API CLI wrapper for Claude Code.

Usage:
    python3 scripts/semaphore_cli.py <command> [subcommand] [options]

Config:
    /config/.claude/semaphore.conf (INI format, persistent CephFS storage)
    Run 'config init --url URL --token TOKEN' to set up.
"""

import argparse
import configparser
import json
import os
import sys
import time

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "/config/.claude/semaphore.conf"
DEFAULT_PROJECT_ID = 1
DEFAULT_TIMEOUT = 30
LONG_TIMEOUT = 120
DEFAULT_POLL_INTERVAL = 5
TERMINAL_STATUSES = {"success", "error", "stopped", "rejected"}

VIEW_MAP = {
    "Backup": 2, "Update": 3, "Maintain": 4, "Download": 5,
    "Verify": 6, "Restore": 7, "Rollback": 7, "Test": 7,
    "Deploy": 8, "Build": 8, "Apply": 8, "DR": 8,
    "Setup": 9, "Manage": 9,
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path):
    """Load INI config. Returns dict with 'url' and 'token'."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        error(f"Config file not found: {path}\n"
              "Run: python3 scripts/semaphore_cli.py config init --url URL --token TOKEN")
    cp = configparser.RawConfigParser()
    cp.read(path)
    if "semaphore" not in cp:
        error(f"Missing [semaphore] section in {path}")
    section = cp["semaphore"]
    url = os.environ.get("SEMAPHORE_URL", section.get("url", "")).rstrip("/")
    token = os.environ.get("SEMAPHORE_TOKEN", section.get("token", ""))
    if not url or not token:
        error("Config missing 'url' or 'token'. Re-run config init.")
    return {"url": url, "token": token}


def save_config(path, url, token):
    """Write config to INI file."""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cp = configparser.RawConfigParser()
    cp["semaphore"] = {"url": url.rstrip("/"), "token": token}
    with open(path, "w") as f:
        cp.write(f)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def error(msg, detail=None):
    """Print error to stderr and exit."""
    print(f"Error: {msg}", file=sys.stderr)
    if detail:
        print(f"  {detail}", file=sys.stderr)
    sys.exit(1)


def format_table(rows, columns, headers=None):
    """Format list of dicts as aligned table."""
    if not rows:
        return "(no results)"
    headers = headers or columns
    widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        vals = [str(row.get(c, "")) for c in columns]
        for i, v in enumerate(vals):
            if len(v) > 60:
                vals[i] = v[:57] + "..."
            widths[i] = max(widths[i], len(vals[i]))
        str_rows.append(vals)
    sep = "  "
    lines = [sep.join(h.ljust(w) for h, w in zip(headers, widths))]
    lines.append(sep.join("─" * w for w in widths))
    for vals in str_rows:
        lines.append(sep.join(v.ljust(w) for v, w in zip(vals, widths)))
    return "\n".join(lines)


def output(data, args, columns=None, headers=None):
    """Print data in requested format."""
    fmt = getattr(args, "format", "json")
    if fmt == "table" and isinstance(data, list) and columns:
        print(format_table(data, columns, headers))
    else:
        print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def api_request(method, path, config, json_data=None, params=None,
                raw=False, timeout=DEFAULT_TIMEOUT):
    """Core HTTP method for Semaphore API."""
    url = config["url"] + path
    headers = {
        "Authorization": f"Bearer {config['token']}",
        "Accept": "application/json",
    }
    try:
        resp = requests.request(
            method, url, headers=headers, json=json_data,
            params=params, verify=False, timeout=timeout,
        )
    except requests.ConnectionError:
        error(f"Cannot connect to {config['url']}", "Check URL and network.")
    except requests.Timeout:
        error(f"Request timed out after {timeout}s")

    if resp.status_code == 401:
        error("Authentication failed", "Check API token.")
    if resp.status_code == 403:
        error("Permission denied")
    if resp.status_code == 404:
        error(f"Not found: {method} {path}")
    if resp.status_code == 409:
        error("Conflict", resp.text)
    if resp.status_code == 422:
        error("Validation error", resp.text)
    if resp.status_code >= 500:
        error(f"Server error ({resp.status_code})", resp.text)
    if resp.status_code >= 400:
        error(f"HTTP {resp.status_code}", resp.text)

    if raw:
        return resp.text
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def api_get(path, config, params=None, raw=False, timeout=DEFAULT_TIMEOUT):
    return api_request("GET", path, config, params=params, raw=raw,
                       timeout=timeout)


def api_post(path, config, data=None, timeout=DEFAULT_TIMEOUT):
    return api_request("POST", path, config, json_data=data, timeout=timeout)


def api_put(path, config, data=None, timeout=DEFAULT_TIMEOUT):
    return api_request("PUT", path, config, json_data=data, timeout=timeout)


def api_delete(path, config, timeout=DEFAULT_TIMEOUT):
    return api_request("DELETE", path, config, timeout=timeout)


# ---------------------------------------------------------------------------
# Phase 1 commands: config
# ---------------------------------------------------------------------------


def cmd_config_init(args, config=None):
    """Write config file, validate with ping."""
    url = args.url.rstrip("/")
    token = args.token
    test_cfg = {"url": url, "token": token}
    try:
        result = api_get("/api/ping", test_cfg, raw=True)
    except SystemExit:
        error("Could not connect to Semaphore. Check URL and token.")
    save_config(args.config, url, token)
    print(json.dumps({"status": "ok", "message": f"Config saved to {args.config}",
                       "ping": result.strip()}))


def cmd_config_show(args, config=None):
    """Display config with masked token."""
    cfg = load_config(args.config)
    masked = cfg["token"][-4:].rjust(len(cfg["token"]), "*") if cfg["token"] else ""
    print(json.dumps({"url": cfg["url"], "token": masked,
                       "config_path": args.config}))


def cmd_config_test(args, config=None):
    """Ping Semaphore with current config."""
    cfg = load_config(args.config)
    result = api_get("/api/ping", cfg, raw=True)
    print(json.dumps({"status": "ok", "ping": result.strip(), "url": cfg["url"]}))


# ---------------------------------------------------------------------------
# Phase 1 commands: ping
# ---------------------------------------------------------------------------


def cmd_ping(args, config):
    """Health check."""
    result = api_get("/api/ping", config, raw=True)
    print(json.dumps({"status": "ok", "ping": result.strip()}))


# ---------------------------------------------------------------------------
# Phase 1 commands: task
# ---------------------------------------------------------------------------


def cmd_task_run(args, config):
    """Run a template and optionally wait for completion."""
    base = f"/api/project/{args.project}/tasks"
    payload = {"template_id": args.template_id}
    if args.extra_args:
        payload["arguments"] = args.extra_args
    if args.message:
        payload["message"] = args.message
    if args.debug:
        payload["debug"] = True
    if args.dry_run:
        payload["dry_run"] = True
    if args.diff:
        payload["diff"] = True
    if args.limit:
        payload["limit"] = args.limit

    task = api_post(base, config, payload, timeout=LONG_TIMEOUT)

    if not (args.wait or args.tail):
        output(task, args)
        return

    task_id = task["id"]
    last_len = 0
    poll = args.poll

    while True:
        time.sleep(poll)
        task = api_get(f"{base}/{task_id}", config)

        if args.tail:
            raw = api_get(f"{base}/{task_id}/raw_output", config, raw=True)
            if raw and len(raw) > last_len:
                sys.stderr.write(raw[last_len:])
                sys.stderr.flush()
                last_len = len(raw)

        status = task.get("status", "")
        if status in TERMINAL_STATUSES:
            output(task, args)
            sys.exit(0 if status == "success" else 2)


def cmd_task_list(args, config):
    """List recent tasks."""
    base = f"/api/project/{args.project}/tasks"
    params = {}
    if args.count:
        params["count"] = args.count
    # The /tasks/last endpoint returns the most recent tasks
    tasks = api_get(f"{base}/last", config, params=params)
    if not isinstance(tasks, list):
        tasks = []

    # Client-side filters
    if args.template:
        tasks = [t for t in tasks if t.get("template_id") == args.template]
    if args.status:
        tasks = [t for t in tasks if t.get("status") == args.status]

    columns = ["id", "status", "template_id", "start", "end", "message"]
    headers = ["ID", "Status", "Template", "Start", "End", "Message"]
    output(tasks, args, columns=columns, headers=headers)


def cmd_task_status(args, config):
    """Get task details."""
    base = f"/api/project/{args.project}/tasks/{args.task_id}"
    task = api_get(base, config)
    output(task, args)


def cmd_task_output(args, config):
    """Get structured task output."""
    base = f"/api/project/{args.project}/tasks/{args.task_id}/output"
    data = api_get(base, config)
    output(data, args)


def cmd_task_log(args, config):
    """Get raw task log."""
    base = f"/api/project/{args.project}/tasks/{args.task_id}/raw_output"
    raw = api_get(base, config, raw=True)
    print(raw)


def cmd_task_stop(args, config):
    """Stop a running task."""
    base = f"/api/project/{args.project}/tasks/{args.task_id}/stop"
    api_post(base, config)
    print(json.dumps({"status": "ok", "task_id": args.task_id,
                       "message": "Stop requested"}))


# ---------------------------------------------------------------------------
# Phase 2 commands: template, schedule, env, inventory, view
# ---------------------------------------------------------------------------


def cmd_template_list(args, config):
    """List templates."""
    base = f"/api/project/{args.project}/templates"
    templates = api_get(base, config)
    if not isinstance(templates, list):
        templates = []
    if args.view:
        templates = [t for t in templates if t.get("view_id") == args.view]
    if args.search:
        q = args.search.lower()
        templates = [t for t in templates if q in t.get("name", "").lower()]
    templates.sort(key=lambda t: t.get("name", ""))
    columns = ["id", "name", "playbook", "view_id", "inventory_id", "environment_id"]
    headers = ["ID", "Name", "Playbook", "View", "Inventory", "Env"]
    output(templates, args, columns=columns, headers=headers)


def cmd_template_get(args, config):
    """Get template details."""
    base = f"/api/project/{args.project}/templates/{args.template_id}"
    data = api_get(base, config)
    output(data, args)


def cmd_schedule_list(args, config):
    """List schedules."""
    base = f"/api/project/{args.project}/schedules"
    schedules = api_get(base, config)
    if not isinstance(schedules, list):
        schedules = []
    if args.template:
        schedules = [s for s in schedules if s.get("template_id") == args.template]
    columns = ["id", "template_id", "name", "cron_format", "active"]
    headers = ["ID", "Template", "Name", "Cron", "Active"]
    output(schedules, args, columns=columns, headers=headers)


def cmd_schedule_get(args, config):
    """Get schedule details."""
    base = f"/api/project/{args.project}/schedules/{args.schedule_id}"
    data = api_get(base, config)
    output(data, args)


def cmd_env_list(args, config):
    """List environments."""
    base = f"/api/project/{args.project}/environment"
    envs = api_get(base, config)
    if not isinstance(envs, list):
        envs = []
    envs.sort(key=lambda e: e.get("name", ""))
    columns = ["id", "name", "json"]
    headers = ["ID", "Name", "Variables"]
    output(envs, args, columns=columns, headers=headers)


def cmd_env_get(args, config):
    """Get environment details."""
    base = f"/api/project/{args.project}/environment/{args.env_id}"
    data = api_get(base, config)
    output(data, args)


def cmd_inventory_list(args, config):
    """List inventories."""
    base = f"/api/project/{args.project}/inventory"
    invs = api_get(base, config)
    if not isinstance(invs, list):
        invs = []
    invs.sort(key=lambda i: i.get("name", ""))
    columns = ["id", "name", "type"]
    headers = ["ID", "Name", "Type"]
    output(invs, args, columns=columns, headers=headers)


def cmd_view_list(args, config):
    """List views."""
    base = f"/api/project/{args.project}/views"
    views = api_get(base, config)
    if not isinstance(views, list):
        views = []
    views.sort(key=lambda v: v.get("position", 0))
    columns = ["id", "title", "position"]
    headers = ["ID", "Title", "Position"]
    output(views, args, columns=columns, headers=headers)


# ---------------------------------------------------------------------------
# Phase 3 commands: create/update/delete
# ---------------------------------------------------------------------------


def _validate_template_name(name, view_id):
    """Return list of warnings for template naming conventions."""
    warnings = []
    if " \u2014 " not in name:
        warnings.append("Name should follow 'Verb \u2014 Target [Subtype]' (missing em-dash)")
    else:
        verb = name.split(" \u2014 ")[0].strip()
        expected = VIEW_MAP.get(verb)
        if expected and expected != view_id:
            warnings.append(f"Verb '{verb}' maps to view {expected}, got {view_id}")
    return warnings


def cmd_template_create(args, config):
    """Create a template."""
    base = f"/api/project/{args.project}/templates"
    payload = {
        "project_id": args.project,
        "name": args.name,
        "playbook": args.playbook,
        "inventory_id": args.inventory_id,
        "repository_id": args.repository_id,
        "environment_id": args.environment_id,
        "view_id": args.view_id,
        "allow_override_args_in_task": True,
        "allow_override_branch_in_task": True,
        "type": "",
        "app": "ansible",
    }
    if args.arguments:
        payload["arguments"] = args.arguments
    if args.description:
        payload["description"] = args.description

    warnings = _validate_template_name(args.name, args.view_id)
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)

    result = api_post(base, config, payload)
    output(result, args)


def cmd_template_update(args, config):
    """Update a template."""
    base = f"/api/project/{args.project}/templates/{args.template_id}"
    current = api_get(base, config)
    if args.name is not None:
        current["name"] = args.name
    if args.playbook is not None:
        current["playbook"] = args.playbook
    if args.inventory_id is not None:
        current["inventory_id"] = args.inventory_id
    if args.environment_id is not None:
        current["environment_id"] = args.environment_id
    if args.view_id is not None:
        current["view_id"] = args.view_id
    if args.arguments is not None:
        current["arguments"] = args.arguments
    if args.description is not None:
        current["description"] = args.description

    warnings = _validate_template_name(current["name"], current.get("view_id", 0))
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)

    api_put(base, config, current)
    updated = api_get(base, config)
    output(updated, args)


def cmd_template_delete(args, config):
    """Delete a template (requires --confirm)."""
    if not args.confirm:
        base = f"/api/project/{args.project}/templates/{args.template_id}"
        t = api_get(base, config)
        error("Delete requires --confirm flag",
              f"Target: Template {args.template_id} \"{t.get('name', '?')}\"")
    base = f"/api/project/{args.project}/templates/{args.template_id}"
    api_delete(base, config)
    print(json.dumps({"status": "ok", "deleted": "template",
                       "id": args.template_id}))


def cmd_schedule_create(args, config):
    """Create a schedule."""
    base = f"/api/project/{args.project}/schedules"
    payload = {
        "project_id": args.project,
        "template_id": args.template_id,
        "cron_format": args.cron,
        "name": args.name,
        "active": not args.inactive,
    }
    result = api_post(base, config, payload)
    output(result, args)


def cmd_schedule_update(args, config):
    """Update a schedule."""
    base = f"/api/project/{args.project}/schedules/{args.schedule_id}"
    current = api_get(base, config)
    if args.cron is not None:
        current["cron_format"] = args.cron
    if args.name is not None:
        current["name"] = args.name
    if args.active:
        current["active"] = True
    if args.inactive:
        current["active"] = False
    api_put(base, config, current)
    updated = api_get(base, config)
    output(updated, args)


def cmd_schedule_delete(args, config):
    """Delete a schedule (requires --confirm)."""
    if not args.confirm:
        base = f"/api/project/{args.project}/schedules/{args.schedule_id}"
        s = api_get(base, config)
        error("Delete requires --confirm flag",
              f"Target: Schedule {args.schedule_id} \"{s.get('name', '?')}\"")
    base = f"/api/project/{args.project}/schedules/{args.schedule_id}"
    api_delete(base, config)
    print(json.dumps({"status": "ok", "deleted": "schedule",
                       "id": args.schedule_id}))


def cmd_env_create(args, config):
    """Create an environment."""
    base = f"/api/project/{args.project}/environment"
    payload = {
        "project_id": args.project,
        "name": args.name,
        "json": args.json_vars or "{}",
        "env": "{}",
    }
    result = api_post(base, config, payload)
    output(result, args)


def cmd_env_update(args, config):
    """Update an environment."""
    base = f"/api/project/{args.project}/environment/{args.env_id}"
    current = api_get(base, config)
    if args.name is not None:
        current["name"] = args.name
    if args.json_vars is not None:
        current["json"] = args.json_vars
    api_put(base, config, current)
    updated = api_get(base, config)
    output(updated, args)


def cmd_env_delete(args, config):
    """Delete an environment (requires --confirm)."""
    if not args.confirm:
        base = f"/api/project/{args.project}/environment/{args.env_id}"
        e = api_get(base, config)
        error("Delete requires --confirm flag",
              f"Target: Environment {args.env_id} \"{e.get('name', '?')}\"")
    base = f"/api/project/{args.project}/environment/{args.env_id}"
    api_delete(base, config)
    print(json.dumps({"status": "ok", "deleted": "environment",
                       "id": args.env_id}))


# ---------------------------------------------------------------------------
# Phase 4 commands: integrations, backup
# ---------------------------------------------------------------------------


def cmd_backup(args, config):
    """Export full project backup."""
    base = f"/api/project/{args.project}/backup"
    data = api_get(base, config, timeout=LONG_TIMEOUT)
    output(data, args)


def cmd_integration_list(args, config):
    """List integrations."""
    base = f"/api/project/{args.project}/integrations"
    data = api_get(base, config)
    if not isinstance(data, list):
        data = []
    columns = ["id", "name", "template_id"]
    headers = ["ID", "Name", "Template"]
    output(data, args, columns=columns, headers=headers)


def cmd_integration_get(args, config):
    """Get integration details."""
    base = f"/api/project/{args.project}/integrations/{args.integration_id}"
    data = api_get(base, config)
    output(data, args)


def cmd_integration_create(args, config):
    """Create an integration."""
    base = f"/api/project/{args.project}/integrations"
    payload = {
        "project_id": args.project,
        "name": args.name,
        "template_id": args.template_id,
    }
    if args.auth_method:
        payload["auth_method"] = args.auth_method
    if args.auth_secret:
        payload["auth_secret"] = {"secret": args.auth_secret}
    if args.auth_header:
        payload["auth_header"] = args.auth_header
    result = api_post(base, config, payload)
    output(result, args)


def cmd_integration_delete(args, config):
    """Delete an integration (requires --confirm)."""
    if not args.confirm:
        base = f"/api/project/{args.project}/integrations/{args.integration_id}"
        i = api_get(base, config)
        error("Delete requires --confirm flag",
              f"Target: Integration {args.integration_id} \"{i.get('name', '?')}\"")
    base = f"/api/project/{args.project}/integrations/{args.integration_id}"
    api_delete(base, config)
    print(json.dumps({"status": "ok", "deleted": "integration",
                       "id": args.integration_id}))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="semaphore_cli.py",
        description="Semaphore API CLI wrapper",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG,
                   help=f"Config file path (default: {DEFAULT_CONFIG})")
    p.add_argument("--format", choices=["json", "table"], default="json",
                   help="Output format (default: json)")
    p.add_argument("--project", type=int, default=DEFAULT_PROJECT_ID,
                   help=f"Project ID (default: {DEFAULT_PROJECT_ID})")

    sub = p.add_subparsers(dest="command", required=True)

    # --- ping ---
    sub.add_parser("ping", help="Health check")

    # --- backup ---
    sub.add_parser("backup", help="Full project backup")

    # --- config ---
    cfg = sub.add_parser("config", help="Manage configuration")
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)

    cfg_init = cfg_sub.add_parser("init", help="Set up config")
    cfg_init.add_argument("--url", required=True, help="Semaphore URL")
    cfg_init.add_argument("--token", required=True, help="API token")

    cfg_sub.add_parser("show", help="Show config")
    cfg_sub.add_parser("test", help="Test connection")

    # --- task ---
    task = sub.add_parser("task", help="Task operations")
    task_sub = task.add_subparsers(dest="task_action", required=True)

    task_run = task_sub.add_parser("run", help="Run a template")
    task_run.add_argument("template_id", type=int, help="Template ID to run")
    task_run.add_argument("--extra-args", dest="extra_args",
                          help="Extra CLI arguments as JSON array")
    task_run.add_argument("--message", help="Task message")
    task_run.add_argument("--debug", action="store_true", help="Debug mode")
    task_run.add_argument("--dry-run", dest="dry_run", action="store_true",
                          help="Check mode")
    task_run.add_argument("--diff", action="store_true", help="Diff mode")
    task_run.add_argument("--limit", help="Limit pattern")
    task_run.add_argument("--wait", action="store_true",
                          help="Wait for completion")
    task_run.add_argument("--tail", action="store_true",
                          help="Stream output (implies --wait)")
    task_run.add_argument("--poll", type=int, default=DEFAULT_POLL_INTERVAL,
                          help=f"Poll interval seconds (default: {DEFAULT_POLL_INTERVAL})")

    task_list = task_sub.add_parser("list", help="List recent tasks")
    task_list.add_argument("--count", type=int, default=20,
                           help="Number of tasks (default: 20)")
    task_list.add_argument("--template", type=int, help="Filter by template ID")
    task_list.add_argument("--status", help="Filter by status")

    task_status = task_sub.add_parser("status", help="Task details")
    task_status.add_argument("task_id", type=int, help="Task ID")

    task_output = task_sub.add_parser("output", help="Structured task output")
    task_output.add_argument("task_id", type=int, help="Task ID")

    task_log = task_sub.add_parser("log", help="Raw task log")
    task_log.add_argument("task_id", type=int, help="Task ID")

    task_stop = task_sub.add_parser("stop", help="Stop a task")
    task_stop.add_argument("task_id", type=int, help="Task ID")

    # --- template ---
    tpl = sub.add_parser("template", help="Template operations")
    tpl_sub = tpl.add_subparsers(dest="template_action", required=True)

    tpl_list = tpl_sub.add_parser("list", help="List templates")
    tpl_list.add_argument("--view", type=int, help="Filter by view ID")
    tpl_list.add_argument("--search", help="Filter by name substring")

    tpl_get = tpl_sub.add_parser("get", help="Get template details")
    tpl_get.add_argument("template_id", type=int, help="Template ID")

    tpl_create = tpl_sub.add_parser("create", help="Create template")
    tpl_create.add_argument("--name", required=True, help="Template name")
    tpl_create.add_argument("--playbook", required=True, help="Playbook file")
    tpl_create.add_argument("--inventory-id", dest="inventory_id", type=int,
                            default=3, help="Inventory ID (default: 3)")
    tpl_create.add_argument("--repository-id", dest="repository_id", type=int,
                            default=1, help="Repository ID (default: 1)")
    tpl_create.add_argument("--environment-id", dest="environment_id",
                            type=int, required=True, help="Environment ID")
    tpl_create.add_argument("--view-id", dest="view_id", type=int,
                            required=True, help="View ID (2-9)")
    tpl_create.add_argument("--arguments", help="Default CLI arguments")
    tpl_create.add_argument("--description", help="Description")

    tpl_update = tpl_sub.add_parser("update", help="Update template")
    tpl_update.add_argument("template_id", type=int, help="Template ID")
    tpl_update.add_argument("--name", help="New name")
    tpl_update.add_argument("--playbook", help="New playbook")
    tpl_update.add_argument("--inventory-id", dest="inventory_id", type=int)
    tpl_update.add_argument("--environment-id", dest="environment_id", type=int)
    tpl_update.add_argument("--view-id", dest="view_id", type=int)
    tpl_update.add_argument("--arguments", help="New arguments")
    tpl_update.add_argument("--description", help="New description")

    tpl_delete = tpl_sub.add_parser("delete", help="Delete template")
    tpl_delete.add_argument("template_id", type=int, help="Template ID")
    tpl_delete.add_argument("--confirm", action="store_true",
                            help="Confirm deletion")

    # --- schedule ---
    sched = sub.add_parser("schedule", help="Schedule operations")
    sched_sub = sched.add_subparsers(dest="schedule_action", required=True)

    sched_list = sched_sub.add_parser("list", help="List schedules")
    sched_list.add_argument("--template", type=int,
                            help="Filter by template ID")

    sched_get = sched_sub.add_parser("get", help="Get schedule details")
    sched_get.add_argument("schedule_id", type=int, help="Schedule ID")

    sched_create = sched_sub.add_parser("create", help="Create schedule")
    sched_create.add_argument("--template-id", dest="template_id", type=int,
                              required=True, help="Template ID")
    sched_create.add_argument("--cron", required=True,
                              help="Cron expression")
    sched_create.add_argument("--name", required=True, help="Schedule name")
    sched_create.add_argument("--inactive", action="store_true",
                              help="Create as inactive")

    sched_update = sched_sub.add_parser("update", help="Update schedule")
    sched_update.add_argument("schedule_id", type=int, help="Schedule ID")
    sched_update.add_argument("--cron", help="New cron expression")
    sched_update.add_argument("--name", help="New name")
    sched_update.add_argument("--active", action="store_true",
                              help="Enable schedule")
    sched_update.add_argument("--inactive", action="store_true",
                              help="Disable schedule")

    sched_delete = sched_sub.add_parser("delete", help="Delete schedule")
    sched_delete.add_argument("schedule_id", type=int, help="Schedule ID")
    sched_delete.add_argument("--confirm", action="store_true",
                              help="Confirm deletion")

    # --- env ---
    env = sub.add_parser("env", help="Environment operations")
    env_sub = env.add_subparsers(dest="env_action", required=True)

    env_sub.add_parser("list", help="List environments")

    env_get = env_sub.add_parser("get", help="Get environment details")
    env_get.add_argument("env_id", type=int, help="Environment ID")

    env_create = env_sub.add_parser("create", help="Create environment")
    env_create.add_argument("--name", required=True, help="Environment name")
    env_create.add_argument("--json", dest="json_vars",
                            help="Variable group JSON")

    env_update = env_sub.add_parser("update", help="Update environment")
    env_update.add_argument("env_id", type=int, help="Environment ID")
    env_update.add_argument("--name", help="New name")
    env_update.add_argument("--json", dest="json_vars",
                            help="New variable group JSON")

    env_delete = env_sub.add_parser("delete", help="Delete environment")
    env_delete.add_argument("env_id", type=int, help="Environment ID")
    env_delete.add_argument("--confirm", action="store_true",
                            help="Confirm deletion")

    # --- inventory ---
    inv = sub.add_parser("inventory", help="Inventory operations")
    inv_sub = inv.add_subparsers(dest="inventory_action", required=True)
    inv_sub.add_parser("list", help="List inventories")

    # --- view ---
    view = sub.add_parser("view", help="View operations")
    view_sub = view.add_subparsers(dest="view_action", required=True)
    view_sub.add_parser("list", help="List views")

    # --- integration ---
    integ = sub.add_parser("integration", help="Integration/webhook operations")
    integ_sub = integ.add_subparsers(dest="integration_action", required=True)

    integ_sub.add_parser("list", help="List integrations")

    integ_get = integ_sub.add_parser("get", help="Get integration details")
    integ_get.add_argument("integration_id", type=int, help="Integration ID")

    integ_create = integ_sub.add_parser("create", help="Create integration")
    integ_create.add_argument("--name", required=True, help="Integration name")
    integ_create.add_argument("--template-id", dest="template_id", type=int,
                              required=True, help="Template ID")
    integ_create.add_argument("--auth-method", dest="auth_method",
                              help="Auth method (none, github, token, hmac)")
    integ_create.add_argument("--auth-secret", dest="auth_secret",
                              help="Auth secret/token")
    integ_create.add_argument("--auth-header", dest="auth_header",
                              help="Auth header name")

    integ_delete = integ_sub.add_parser("delete", help="Delete integration")
    integ_delete.add_argument("integration_id", type=int,
                              help="Integration ID")
    integ_delete.add_argument("--confirm", action="store_true",
                              help="Confirm deletion")

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Command dispatch table
COMMANDS = {
    "ping": cmd_ping,
    "backup": cmd_backup,
    "config": {
        "init": cmd_config_init,
        "show": cmd_config_show,
        "test": cmd_config_test,
    },
    "task": {
        "run": cmd_task_run,
        "list": cmd_task_list,
        "status": cmd_task_status,
        "output": cmd_task_output,
        "log": cmd_task_log,
        "stop": cmd_task_stop,
    },
    "template": {
        "list": cmd_template_list,
        "get": cmd_template_get,
        "create": cmd_template_create,
        "update": cmd_template_update,
        "delete": cmd_template_delete,
    },
    "schedule": {
        "list": cmd_schedule_list,
        "get": cmd_schedule_get,
        "create": cmd_schedule_create,
        "update": cmd_schedule_update,
        "delete": cmd_schedule_delete,
    },
    "env": {
        "list": cmd_env_list,
        "get": cmd_env_get,
        "create": cmd_env_create,
        "update": cmd_env_update,
        "delete": cmd_env_delete,
    },
    "inventory": {
        "list": cmd_inventory_list,
    },
    "view": {
        "list": cmd_view_list,
    },
    "integration": {
        "list": cmd_integration_list,
        "get": cmd_integration_get,
        "create": cmd_integration_create,
        "delete": cmd_integration_delete,
    },
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Resolve command handler
    handler = COMMANDS.get(args.command)
    if isinstance(handler, dict):
        action_key = f"{args.command}_action"
        action = getattr(args, action_key, None)
        handler = handler.get(action)

    if not handler:
        parser.print_help()
        sys.exit(1)

    # Config commands don't need a loaded config
    needs_config = args.command != "config"
    config = load_config(args.config) if needs_config else None

    # --tail implies --wait
    if hasattr(args, "tail") and args.tail:
        args.wait = True

    handler(args, config)


if __name__ == "__main__":
    main()
