#!/usr/bin/env python3
"""Database CLI wrapper for Claude Code.

Connects to the Semaphore and ansible_logging MariaDB databases.
Provides preset queries from CLAUDE_REFERENCE_DATA.md and custom SQL.

Usage:
    python3 scripts/db_cli.py <command> [options]

Config:
    [database] section in /config/.claude/semaphore.conf
    Run 'config init --host HOST --user USER --password PASS' to set up.
"""

import argparse
import configparser
import json
import os
import sys

import pymysql
import pymysql.cursors

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "/config/.claude/semaphore.conf"
CONFIG_SECTION = "database"
DEFAULT_PORT = 3306
DEFAULT_SEMAPHORE_DB = "semaphore"
DEFAULT_LOGGING_DB = "ansible_logging"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path):
    """Load database config from INI file."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        error(f"Config file not found: {path}\n"
              "Run: python3 scripts/db_cli.py config init --host HOST "
              "--user USER --password PASS")
    cp = configparser.RawConfigParser()
    cp.read(path)
    if CONFIG_SECTION not in cp:
        error(f"Missing [{CONFIG_SECTION}] section in {path}\n"
              "Run: python3 scripts/db_cli.py config init --host HOST "
              "--user USER --password PASS")
    s = cp[CONFIG_SECTION]
    return {
        "host": os.environ.get("DB_HOST", s.get("host", "")),
        "port": int(os.environ.get("DB_PORT", s.get("port", DEFAULT_PORT))),
        "user": os.environ.get("DB_USER", s.get("user", "")),
        "password": os.environ.get("DB_PASSWORD", s.get("password", "")),
        "semaphore_db": s.get("semaphore_db", DEFAULT_SEMAPHORE_DB),
        "logging_db": s.get("logging_db", DEFAULT_LOGGING_DB),
    }


def save_config(path, host, port, user, password, semaphore_db, logging_db):
    """Append [database] section to existing config."""
    path = os.path.expanduser(path)
    cp = configparser.RawConfigParser()
    if os.path.isfile(path):
        cp.read(path)
    cp[CONFIG_SECTION] = {
        "host": host,
        "port": str(port),
        "user": user,
        "password": password,
        "semaphore_db": semaphore_db,
        "logging_db": logging_db,
    }
    with open(path, "w") as f:
        cp.write(f)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------


def get_connection(config, db_name):
    """Create a database connection."""
    try:
        return pymysql.connect(
            host=config["host"],
            port=config["port"],
            user=config["user"],
            password=config["password"],
            database=db_name,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            read_timeout=30,
        )
    except pymysql.err.OperationalError as e:
        error(f"Cannot connect to {db_name}@{config['host']}:{config['port']}",
              str(e))


def run_query(config, db_name, sql, params=None, write=False):
    """Execute a query and return results."""
    conn = get_connection(config, db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if write:
                conn.commit()
                return {"rows_affected": cur.rowcount}
            return cur.fetchall()
    except pymysql.err.ProgrammingError as e:
        error("Query error", str(e))
    except pymysql.err.OperationalError as e:
        error("Database error", str(e))
    finally:
        conn.close()


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
# Config commands
# ---------------------------------------------------------------------------


def cmd_config_init(args, config=None):
    """Set up database config."""
    test_cfg = {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "password": args.password,
        "semaphore_db": args.semaphore_db,
        "logging_db": args.logging_db,
    }
    # Test connections (best-effort, save config either way)
    results = {}
    for db in [args.semaphore_db, args.logging_db]:
        try:
            conn = pymysql.connect(
                host=test_cfg["host"], port=test_cfg["port"],
                user=test_cfg["user"], password=test_cfg["password"],
                database=db, connect_timeout=5)
            conn.close()
            results[db] = "ok"
        except Exception as e:
            results[db] = f"failed: {e}"
    save_config(args.config, args.host, args.port, args.user,
                args.password, args.semaphore_db, args.logging_db)
    print(json.dumps({"status": "ok",
                       "message": f"Database config saved to {args.config}",
                       "databases": results}))


def cmd_config_show(args, config=None):
    cfg = load_config(args.config)
    masked = cfg["password"][-4:].rjust(len(cfg["password"]), "*") if cfg["password"] else ""
    print(json.dumps({"host": cfg["host"], "port": cfg["port"],
                       "user": cfg["user"], "password": masked,
                       "semaphore_db": cfg["semaphore_db"],
                       "logging_db": cfg["logging_db"]}))


def cmd_config_test(args, config=None):
    cfg = load_config(args.config)
    results = {}
    for db in [cfg["semaphore_db"], cfg["logging_db"]]:
        conn = get_connection(cfg, db)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        results[db] = "ok"
    print(json.dumps({"status": "ok", "databases": results}))


# ---------------------------------------------------------------------------
# Custom SQL command
# ---------------------------------------------------------------------------


def cmd_query(args, config):
    """Run custom SQL."""
    db_map = {"semaphore": config["semaphore_db"],
              "logging": config["logging_db"]}
    db_name = db_map.get(args.db)
    if not db_name:
        error(f"Unknown database '{args.db}'. Use 'semaphore' or 'logging'.")

    sql = args.sql.strip()
    is_write = not sql.upper().startswith("SELECT") and not sql.upper().startswith("SHOW") \
        and not sql.upper().startswith("DESCRIBE") and not sql.upper().startswith("EXPLAIN")

    if is_write and not args.write:
        error("Write query detected. Add --write flag to confirm.",
              f"SQL: {sql[:100]}")

    result = run_query(config, db_name, sql, write=is_write)
    output(result, args)


# ---------------------------------------------------------------------------
# Preset queries: ansible_logging
# ---------------------------------------------------------------------------


def cmd_backups(args, config):
    """Recent backups."""
    sql = """SELECT hostname, application, file_name,
                    ROUND(file_size, 2) as size_mb, timestamp
             FROM backups ORDER BY timestamp DESC LIMIT %s"""
    params = [args.limit]
    if args.host:
        sql = """SELECT hostname, application, file_name,
                        ROUND(file_size, 2) as size_mb, timestamp
                 FROM backups WHERE hostname = %s
                 ORDER BY timestamp DESC LIMIT %s"""
        params = [args.host, args.limit]
    rows = run_query(config, config["logging_db"], sql, params)
    output(rows, args, ["hostname", "application", "file_name", "size_mb", "timestamp"],
           ["Host", "Application", "File", "Size MB", "Timestamp"])


def cmd_stale_backups(args, config):
    """Backups older than threshold."""
    sql = """SELECT hostname, application, backup_subtype,
                    MAX(timestamp) as last_backup,
                    TIMESTAMPDIFF(HOUR, MAX(timestamp), UTC_TIMESTAMP()) as hours_ago
             FROM backups
             WHERE file_name NOT LIKE 'FAILED_%%'
             GROUP BY hostname, application, backup_subtype
             HAVING hours_ago > %s
             ORDER BY hours_ago DESC"""
    rows = run_query(config, config["logging_db"], sql, [args.hours])
    output(rows, args, ["hostname", "application", "backup_subtype", "last_backup", "hours_ago"],
           ["Host", "Application", "Subtype", "Last Backup", "Hours Ago"])


def cmd_health(args, config):
    """Latest non-ok health checks."""
    sql = """SELECT h.hostname, h.check_name, h.check_status, h.check_value, h.timestamp
             FROM health_checks h
             INNER JOIN (
               SELECT hostname, check_name, MAX(timestamp) as max_ts
               FROM health_checks
               GROUP BY hostname, check_name
             ) latest ON h.hostname = latest.hostname
               AND h.check_name = latest.check_name
               AND h.timestamp = latest.max_ts
             WHERE h.check_status != 'ok'
             ORDER BY h.hostname, h.check_name"""
    rows = run_query(config, config["logging_db"], sql)
    output(rows, args, ["hostname", "check_name", "check_status", "check_value", "timestamp"],
           ["Host", "Check", "Status", "Value", "Timestamp"])


def cmd_restores(args, config):
    """Recent restore operations."""
    sql = """SELECT application, hostname, source_file, operation,
                    status, detail, timestamp
             FROM restores ORDER BY timestamp DESC LIMIT %s"""
    rows = run_query(config, config["logging_db"], sql, [args.limit])
    output(rows, args, ["application", "hostname", "source_file", "operation",
                        "status", "detail", "timestamp"],
           ["App", "Host", "Source", "Op", "Status", "Detail", "Timestamp"])


def cmd_updates(args, config):
    """Recent updates."""
    sql = """SELECT hostname, application, version, status, timestamp
             FROM updates ORDER BY timestamp DESC LIMIT %s"""
    rows = run_query(config, config["logging_db"], sql, [args.limit])
    output(rows, args, ["hostname", "application", "version", "status", "timestamp"],
           ["Host", "Application", "Version", "Status", "Timestamp"])


def cmd_runs(args, config):
    """Recent playbook runs."""
    sql = """SELECT playbook, hostname, run_vars, timestamp
             FROM playbook_runs ORDER BY timestamp DESC LIMIT %s"""
    rows = run_query(config, config["logging_db"], sql, [args.limit])
    output(rows, args, ["playbook", "hostname", "run_vars", "timestamp"],
           ["Playbook", "Host", "Vars", "Timestamp"])


def cmd_table_counts(args, config):
    """Row counts for all logging tables."""
    sql = """SELECT 'backups' as tbl, COUNT(*) as cnt FROM backups
             UNION ALL SELECT 'updates', COUNT(*) FROM updates
             UNION ALL SELECT 'maintenance', COUNT(*) FROM maintenance
             UNION ALL SELECT 'health_checks', COUNT(*) FROM health_checks
             UNION ALL SELECT 'restores', COUNT(*) FROM restores
             UNION ALL SELECT 'docker_sizes', COUNT(*) FROM docker_sizes
             UNION ALL SELECT 'playbook_runs', COUNT(*) FROM playbook_runs"""
    rows = run_query(config, config["logging_db"], sql)
    output(rows, args, ["tbl", "cnt"], ["Table", "Rows"])


def cmd_docker_sizes(args, config):
    """Recent docker size snapshots."""
    sql = """SELECT hostname, stack, service, size_mb, timestamp
             FROM docker_sizes ORDER BY timestamp DESC LIMIT %s"""
    rows = run_query(config, config["logging_db"], sql, [args.limit])
    output(rows, args, ["hostname", "stack", "service", "size_mb", "timestamp"],
           ["Host", "Stack", "Service", "Size MB", "Timestamp"])


# ---------------------------------------------------------------------------
# Preset queries: semaphore
# ---------------------------------------------------------------------------


def cmd_tasks(args, config):
    """Recent Semaphore tasks with template names."""
    sql = """SELECT t.name as template, th.status, th.start, th.end
             FROM task th
             JOIN project__template t ON th.template_id = t.id
             ORDER BY th.start DESC LIMIT %s"""
    rows = run_query(config, config["semaphore_db"], sql, [args.limit])
    output(rows, args, ["template", "status", "start", "end"],
           ["Template", "Status", "Start", "End"])


def cmd_failed_tasks(args, config):
    """Failed/stopped tasks since last health check."""
    sql = """SELECT t.name, th.status, th.start, th.message
             FROM task th
             JOIN project__template t ON th.template_id = t.id
             WHERE th.status IN ('error', 'stopped')
             ORDER BY th.start DESC LIMIT %s"""
    rows = run_query(config, config["semaphore_db"], sql, [args.limit])
    output(rows, args, ["name", "status", "start", "message"],
           ["Template", "Status", "Start", "Message"])


def cmd_envs(args, config):
    """List Semaphore environments."""
    if args.search:
        sql = """SELECT id, name, json FROM project__environment
                 WHERE project_id = 1 AND (name LIKE %s OR json LIKE %s)
                 ORDER BY name"""
        pattern = f"%{args.search}%"
        rows = run_query(config, config["semaphore_db"], sql, [pattern, pattern])
    else:
        sql = """SELECT id, name, json FROM project__environment
                 WHERE project_id = 1 ORDER BY name"""
        rows = run_query(config, config["semaphore_db"], sql)
    output(rows, args, ["id", "name", "json"], ["ID", "Name", "Variables"])


def cmd_semaphore_templates(args, config):
    """List all templates with env and view names."""
    sql = """SELECT t.id, t.name, t.playbook, e.name as environment, v.title as view
             FROM project__template t
             LEFT JOIN project__environment e ON t.environment_id = e.id
             LEFT JOIN project__view v ON t.view_id = v.id
             WHERE t.project_id = 1
             ORDER BY v.title, t.name"""
    rows = run_query(config, config["semaphore_db"], sql)
    output(rows, args, ["id", "name", "playbook", "environment", "view"],
           ["ID", "Name", "Playbook", "Environment", "View"])


def cmd_semaphore_schedules(args, config):
    """List all schedules with template names."""
    sql = """SELECT t.name, s.cron_format, s.name as schedule_name, s.active
             FROM project__schedule s
             JOIN project__template t ON s.template_id = t.id
             WHERE s.project_id = 1
             ORDER BY t.name"""
    rows = run_query(config, config["semaphore_db"], sql)
    output(rows, args, ["name", "cron_format", "schedule_name", "active"],
           ["Template", "Cron", "Schedule Name", "Active"])


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="db_cli.py",
        description="Database CLI for Semaphore and ansible_logging",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG,
                   help=f"Config file path (default: {DEFAULT_CONFIG})")
    p.add_argument("--format", choices=["json", "table"], default="json",
                   help="Output format (default: json)")

    sub = p.add_subparsers(dest="command", required=True)

    # --- config ---
    cfg = sub.add_parser("config", help="Manage configuration")
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)

    cfg_init = cfg_sub.add_parser("init", help="Set up database config")
    cfg_init.add_argument("--host", required=True, help="Database host")
    cfg_init.add_argument("--port", type=int, default=DEFAULT_PORT, help="Database port")
    cfg_init.add_argument("--user", required=True, help="Database user")
    cfg_init.add_argument("--password", required=True, help="Database password")
    cfg_init.add_argument("--semaphore-db", dest="semaphore_db",
                          default=DEFAULT_SEMAPHORE_DB, help="Semaphore DB name")
    cfg_init.add_argument("--logging-db", dest="logging_db",
                          default=DEFAULT_LOGGING_DB, help="Logging DB name")

    cfg_sub.add_parser("show", help="Show config")
    cfg_sub.add_parser("test", help="Test connection")

    # --- query ---
    q = sub.add_parser("query", help="Run custom SQL")
    q.add_argument("db", choices=["semaphore", "logging"], help="Target database")
    q.add_argument("sql", help="SQL query")
    q.add_argument("--write", action="store_true",
                   help="Allow INSERT/UPDATE/DELETE")

    # --- ansible_logging presets ---
    bk = sub.add_parser("backups", help="Recent backups")
    bk.add_argument("--limit", type=int, default=20, help="Row limit")
    bk.add_argument("--host", help="Filter by hostname")

    sb = sub.add_parser("stale-backups", help="Stale backups")
    sb.add_argument("--hours", type=int, default=216, help="Hours threshold (default: 216 = 9 days)")

    sub.add_parser("health", help="Latest non-ok health checks")

    rs = sub.add_parser("restores", help="Recent restores")
    rs.add_argument("--limit", type=int, default=20, help="Row limit")

    up = sub.add_parser("updates", help="Recent updates")
    up.add_argument("--limit", type=int, default=20, help="Row limit")

    rn = sub.add_parser("runs", help="Recent playbook runs")
    rn.add_argument("--limit", type=int, default=20, help="Row limit")

    sub.add_parser("table-counts", help="Row counts for logging tables")

    ds = sub.add_parser("docker-sizes", help="Docker size snapshots")
    ds.add_argument("--limit", type=int, default=20, help="Row limit")

    # --- semaphore presets ---
    tk = sub.add_parser("tasks", help="Recent Semaphore tasks with names")
    tk.add_argument("--limit", type=int, default=20, help="Row limit")

    ft = sub.add_parser("failed-tasks", help="Failed/stopped tasks")
    ft.add_argument("--limit", type=int, default=50, help="Row limit")

    ev = sub.add_parser("envs", help="List Semaphore environments")
    ev.add_argument("--search", help="Search by name or variable content")

    sub.add_parser("templates", help="All templates with env and view names")
    sub.add_parser("schedules", help="All schedules with template names")

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "config": {
        "init": cmd_config_init,
        "show": cmd_config_show,
        "test": cmd_config_test,
    },
    "query": cmd_query,
    "backups": cmd_backups,
    "stale-backups": cmd_stale_backups,
    "health": cmd_health,
    "restores": cmd_restores,
    "updates": cmd_updates,
    "runs": cmd_runs,
    "table-counts": cmd_table_counts,
    "docker-sizes": cmd_docker_sizes,
    "tasks": cmd_tasks,
    "failed-tasks": cmd_failed_tasks,
    "envs": cmd_envs,
    "templates": cmd_semaphore_templates,
    "schedules": cmd_semaphore_schedules,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if isinstance(handler, dict):
        action = getattr(args, "config_action", None)
        handler = handler.get(action)

    if not handler:
        parser.print_help()
        sys.exit(1)

    needs_config = args.command != "config"
    config = load_config(args.config) if needs_config else None

    handler(args, config)


if __name__ == "__main__":
    main()
