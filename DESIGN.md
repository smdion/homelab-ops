# Ansible Home Lab — Design & Architecture

This document describes the **current state** of the project — how it works today, not how it
got here. It covers philosophy, file layout, Semaphore setup, playbook patterns, database
architecture, and vault configuration. No historical changes or references to previous designs
are included. Its purpose is to give a future maintainer (or future self) enough context to
make changes confidently.

## Table of Contents

- [Philosophy](#philosophy)
  - [Roadmap](#roadmap)
- [File Structure](#file-structure)
- [Semaphore Setup](#semaphore-setup)
- [Playbook Patterns](#playbook-patterns)
  - [Error handling](#error-handling)
  - [Coding conventions](#coding-conventions)
  - [Check mode support](#check-mode-dry-run-support)
  - [Pre-task validations](#pre-task-validations)
  - [Triple alerting](#triple-alerting-discord-push--grafana-pull--uptime-kuma-dead-mans-switch)
  - [Health check state management](#health-check-state-management)
  - [Version detection pattern](#version-detection-pattern-update_systemsyaml)
- [Database Architecture](#database-architecture)
  - [Timezone convention](#timezone-convention)
  - [Table schemas](#backups-table)
  - [Naming scheme](#naming-scheme)
  - [Categorization values](#categorization-values)
- [Vault / Secrets](#vault--secrets)
- [Making Common Changes](#making-common-changes)
  - [Grafana dashboard](#grafana-dashboard)
- [Security Hardening](#security-hardening)

---

## Philosophy

**Ansible playbooks are the single source of truth for configuration.** Semaphore is an
orchestration UI — it handles scheduling, SSH credentials, and vault decryption — but it does
not own any configuration values. Every config variable lives in a version-controlled `vars/*.yaml`
file. Every secret lives in the encrypted vault.

**Semaphore variable groups are routing-only.** Each variable group JSON contains only
`hosts_variable` (and sometimes `config_file`). No config values, no secrets, no overrides.
If a variable is not in `vars/` or the vault, it does not exist. The `blank` variable group
(`{}`) is a placeholder required by the Semaphore UI for templates that need no routing.

**The database is a log, not a controller.** The MariaDB `ansible_logging` database stores backup
and update records for visibility and history. Ansible writes to it — nothing reads from it to
make decisions. No triggers, no stored functions, no computed columns. All categorization
(type, subtype) and hostname normalization happen in Ansible before the INSERT.

**Shared tasks over inline duplication.** Logic that appears in more than one playbook belongs in
`tasks/`. Before adding inline task blocks, check whether the operation is already handled by a
shared file — and if the same block is being copy-pasted to a second playbook, extract it first.
When reviewing or modifying playbooks, look for opportunities to move repeated logic into `tasks/`.

**Vars files are the only per-deployment configuration layer.** All deployment-specific values
live in `vars/*.yaml` files, the encrypted vault, and the Ansible inventory. Playbooks contain
no hardcoded hostnames, container names, database names, URLs, filesystem paths, or network
topology. To adapt this project for a different homelab: create your own `vars/` files following
`vars/example.yaml`, populate the inventory with your hosts, and encrypt your secrets. The
playbooks, shared tasks, SQL schema, and Grafana dashboard work unchanged.

### What goes where

| Location | What belongs there | Examples |
|----------|-------------------|----------|
| **`vars/*.yaml`** | All deployment-specific configuration — anything that would change on a different homelab | Hostnames, URLs, filesystem paths, container names, thresholds, API endpoints, retention periods, application paths |
| **`vars/secrets.yaml`** (vault) | Credentials, API keys, domain suffixes, IP addresses | `discord_webhook_token`, `logging_db_password`, `domain_ext` |
| **`group_vars/all.yaml`** | Shared defaults that apply to all hosts | `ansible_remote_tmp`, `backup_base_dir`, `backup_url` template |
| **Inventory** | Host definitions, group membership, SSH/connection settings | `[ubuntu]`, `[pve]`, `[docker_stacks]`, host FQDNs |
| **Semaphore variable groups** | Routing-only — `hosts_variable` and `config_file` | `{"hosts_variable": "pve:pbs"}` |
| **Docker container labels** (`homelab.*`) | Static per-container config metadata discovered at runtime by tasks | `homelab.backup.paths`, `homelab.health_timeout`, `homelab.test.skip_health` |
| **MariaDB (`ansible_logging`)** | Dynamic operational data — time-series records that accumulate per run | Backup records, update history, health checks, restore results, playbook runs |
| **Play-level `vars:`** | Only derived/computed values and operation metadata | `config_file: "{{ hosts_variable }}"`, `maintenance_name: "Docker"`, `maintenance_type: "Servers"` |

**Play-level `vars:` rules:**

- **Derived values** — Jinja2 expressions that compute a value from vars-file variables are fine:
  `amp_versions_dir: "{{ amp_home }}/.ampdata/Versions/Mainline"` (computes from `amp_home` in vars)
- **Operation metadata** — names, types, subtypes, and descriptions that describe what the
  playbook *does* are fine: `maintenance_name: "AMP"`, `maintenance_subtype: "Prune"`. These
  are operation labels, not deployment configuration — they don't change per homelab.
- **Never in `vars:`** — URLs (even cosmetic ones like icon URLs), filesystem paths, container
  names, hostnames, thresholds, or any value that a different deployment might need to change.
  These always go in `vars/*.yaml` files.

**Quick test:** If you changed homelabs and had to edit a value, it must be in a `vars/` file.
If the value describes the operation itself (not the environment), it can stay in the playbook.

**Prefer Docker labels over vars files for per-container metadata.**

When a configuration value is specific to a single Docker container or stack — backup paths,
health check timeouts, test-mode skip flags — put it in a `homelab.*` label on the container
rather than in a vars file. Labels are co-located with what they describe, survive stack
redeployment automatically, and let tasks discover config at runtime without a vars-file edit
for every new stack. Vars files are for deployment-wide configuration (hostnames, API endpoints,
credentials, thresholds that apply across the whole homelab). If you find yourself adding a
per-stack or per-container entry to a vars file, consider a container label instead.

**Docker labels are not a substitute for MariaDB.** Labels hold static metadata; they cannot
represent time-series records. Anything that accumulates per run (backup records, health check
history, restore results, playbook runs) belongs in the database.

## File Structure

```
├── ansible.cfg                     # Ansible settings: disable retry files, YAML stdout callback
├── inventory.yaml                  # Semaphore inventory — committed, vault-encrypted (not in .gitignore)
├── inventory.example.yaml          # Template inventory with example hosts and group structure
│
├── group_vars/
│   ├── all.yaml                    # Shared defaults: ansible_remote_tmp, ansible_python_interpreter, backup_base_dir/tmp_dir/tmp_file/dest_path/url, backup_type/update_type ("Servers"), is_postgres/is_mariadb/is_influxdb (false)
│   ├── pikvm.yaml                  # PiKVM override: ansible_remote_tmp → /tmp/.ansible/tmp (RO filesystem; /tmp is tmpfs)
│   └── tantiveiv.yaml              # tantive-iv override: docker_mem_limit → "10g" (12GB RAM vs 4GB default)
│
├── vars/
│   ├── secrets.yaml                # AES256-encrypted vault — ALL secrets (incl. domain config, docker_* keys, pve_* keys)
│   ├── secrets.yaml.example         # Template with all vault keys documented (copy → encrypt)
│   ├── example.yaml                # Template for creating new platform vars files
│   ├── semaphore_check.yaml         # Health thresholds (26 checks), controller_fqdn, semaphore_db_name, semaphore_url/semaphore_ext_url, display_timezone, retention_days, appliance_check_hosts
│   ├── proxmox.yaml                 # Proxmox PVE + PBS
│   ├── pikvm.yaml                   # PiKVM KVM — backup/update config; see group_vars/pikvm.yaml for connection override
│   ├── unifi_network.yaml           # Unifi Network — backup, gateway paths (unifi_state_file), unifi_backup_retention, maintenance_url
│   ├── unifi_protect.yaml           # Unifi Protect — backup, API paths (unifi_protect_api_backup_path, unifi_protect_temp_file)
│   ├── amp.yaml                     # AMP — backup/update + maintenance config (amp_user, amp_home, amp_versions_keep)
│   ├── docker_stacks.yaml           # Docker Compose — backup/update, stack_assignments, app_info (pre-deploy restore map), docker_* defaults
│   ├── docker_run.yaml              # Docker run / unRAID — backup/update, backup/update exclude lists, app_restore mapping
│   ├── ubuntu_os.yaml               # Ubuntu OS updates
│   ├── unraid_os.yaml               # unRAID OS backup
│   ├── synology.yaml                # Synology NAS sync
│   ├── vm_definitions.yaml          # Proxmox VM provisioning — VMID/IP/resource definitions for build_ubuntu.yaml
│   ├── db_primary_postgres.yaml     # Primary host Postgres DB backup + db_container_deps for restore
│   ├── db_primary_mariadb.yaml      # Primary host MariaDB backup + db_container_deps for restore
│   ├── db_primary_influxdb.yaml     # Primary host InfluxDB backup + db_container_deps for restore
│   ├── db_secondary_postgres.yaml   # Secondary host Postgres DB backup + db_container_deps for restore
│   ├── download_default.yaml       # yt-dlp download profile: default (scheduled channel downloads)
│   └── download_on_demand.yaml    # yt-dlp download profile: on_demand (bookmarklet-triggered; verbose, allows live streams)
│
├── tasks/
│   ├── notify.yaml                  # Shared notification task (Discord + Apprise)
│   ├── log_mariadb.yaml             # Shared MariaDB logging task (backups, updates, maintenance tables)
│   ├── log_run_context.yaml         # Shared MariaDB logging task (playbook_runs table — one row per playbook invocation)
│   ├── log_restore.yaml             # Shared MariaDB logging task (restores table — restore operations only)
│   ├── log_health_check.yaml        # Shared MariaDB logging task (health_checks table — single row; currently unused)
│   ├── log_health_checks_batch.yaml # Shared MariaDB logging task (health_checks table — multi-row batch INSERT)
│   ├── assert_config_file.yaml      # Shared pre-task: assert config_file is set
│   ├── assert_disk_space.yaml       # Shared pre-task: assert sufficient disk space
│   ├── assert_db_connectivity.yaml  # Shared pre-task: assert MariaDB logging DB is reachable
│   ├── backup_single_stack.yaml     # Per-stack backup loop body (stop stack, archive appdata, verify, fetch, restart, record result)
│   ├── backup_single_db.yaml        # Per-DB backup loop body (dump, verify integrity, fetch to controller, record result)
│   ├── db_dump.yaml                 # Dump a single DB from a Docker container — PostgreSQL/MariaDB/InfluxDB engine abstraction
│   ├── db_restore.yaml              # Restore a single DB from backup — verify (temp DB) or production overwrite, all engines
│   ├── db_count.yaml                # Count tables/measurements in a database — PostgreSQL/MariaDB/InfluxDB
│   ├── db_drop_temp.yaml            # Drop a database and clean up container temp files — all engines
│   ├── deploy_single_stack.yaml     # Per-stack deploy loop body (mkdir, template .env, copy compose, validate, up)
│   ├── provision_vm.yaml            # Provision VM on Proxmox via cloud-init template clone + API config
│   ├── bootstrap_vm.yaml            # Bootstrap Ubuntu VM: apt, Docker, SSH hardening, UFW, NFS
│   ├── ssh_hardening.yaml           # Passwordless sudo + SSH config hardening + service restart
│   ├── docker_stop.yaml             # Stop Docker containers/stacks (selective, stack, or unRAID mode)
│   ├── docker_start.yaml            # Start Docker containers/stacks (selective, stack, or unRAID mode)
│   ├── restore_appdata.yaml         # Restore appdata archive (staging inspect or inplace mode)
│   ├── restore_app_step.yaml        # Per-app restore loop body (stop stack, restore DB+appdata, restart, health check; OOM detection)
│   ├── verify_app_http.yaml         # Per-app HTTP endpoint verification (used by restore_app.yaml and test_backup_restore.yaml)
│   ├── backup_single_amp_instance.yaml  # Per-AMP-instance backup loop body (stop→archive→verify→fetch→start)
│   ├── restore_single_amp_instance.yaml # Per-AMP-instance restore loop body (stop→remove→extract→start); mirrors backup_single_amp_instance
│   └── verify_docker_health.yaml    # Poll Docker container health until all healthy or timeout
│
├── templates/
│   └── metube.conf.j2              # Jinja2 template for yt-dlp config — rendered per profile from vars/download_<name>.yaml
│
├── backup_hosts.yaml               # Config/Appdata backups (Proxmox, PiKVM, Unifi, AMP, Docker, unRAID); integrity verification; DB dir exclusion
├── backup_databases.yaml           # Database backups (Postgres + MariaDB dumps, InfluxDB portable backup); integrity verification
├── backup_offline.yaml             # unRAID → Synology offline sync (WOL + rsync); shutdown verification; logs both successful and failed syncs; hosts via hosts_variable
├── verify_backups.yaml             # On-demand backup verification — DB backups restored to temp DB; config archives integrity-checked and staged
├── restore_databases.yaml          # Database restore from backup dumps — safety-gated; supports single-DB restore on shared instances
├── restore_hosts.yaml              # Config/appdata restore — staging (inspect) or inplace; selective app + coordinated cross-host DB restore
├── rollback_docker.yaml            # Docker container rollback — revert to previous image versions; safety-gated; per-stack or per-service targeting
├── update_systems.yaml             # OS, application, and Docker container updates (Proxmox, PiKVM, AMP, Ubuntu, Docker); PVE cluster quorum pre-check; rollback snapshot; unRAID update_container script
├── maintain_amp.yaml               # AMP game server maintenance (versions, dumps, prune, journal)
├── maintain_semaphore.yaml         # Delete stopped/error + old download tasks from Semaphore DB + prune ansible_logging retention (runs on localhost)
├── maintain_logging_db.yaml        # Purge failed/warning records from ansible_logging (failed updates, maintenance, zero-size backups, warning/critical health checks) — runs on localhost
├── maintain_docker.yaml            # Prune unused Docker images across all Docker hosts
├── maintain_cache.yaml             # Drop Linux page cache on Ubuntu and unRAID hosts
├── maintain_unifi.yaml             # Restart Unifi Network service
├── maintain_health.yaml            # Scheduled health monitoring — 26 checks across all SSH hosts + DB/API; Uptime Kuma dead man's switch
├── maintain_pve.yaml               # Idempotent Proxmox node config (keepalived VIP, ansible user, SSH hardening); stale snapshot check (>14d alert); PBS task error check (last 2d via proxmox-backup-manager); Discord + MariaDB logging
├── download_videos.yaml            # MeTube yt-dlp downloads — per-video Discord notifications + temp file cleanup; parameterized on config_file; hosts via hosts_variable
├── setup_ansible_user.yaml         # One-time utility: create ansible user on PVE/PBS/unRAID hosts (SSH key from vault, ansible_remote_tmp dir, validation assertions)
├── setup_pve_vip.yaml              # One-time VIP setup: install and configure keepalived on PVE nodes; verifies VIP reachable on port 22
├── deploy_stacks.yaml             # Deploy Docker stacks from Git — templates .env from vault, copies compose, starts stacks
├── build_ubuntu.yaml              # Provision Ubuntu VMs on Proxmox via API — cloud-init, Docker install, SSH config
├── restore_amp.yaml               # AMP instance restore — stop instance(s), replace data dir from archive, restart; per-instance or all; requires confirm=yes
├── restore_app.yaml               # Production single-app restore — stop stack, restore DB(s) + appdata inplace, restart, health check; requires confirm=yes
├── test_restore.yaml              # Automated restore testing — provision disposable VM, deploy stacks, health check, revert
├── test_backup_restore.yaml          # Test all app_info apps on disposable VM — per-app DB+appdata restore, OOM auto-recovery, Discord summary, revert
├── deploy_grafana.yaml            # Deploy Grafana dashboard + Ansible-Logging datasource via API (localhost — no SSH)
│
├── files/
│   └── get_push_epoch.sh           # Helper script for Docker image age checks (deployed to remote hosts by update_systems.yaml)
│
├── sql/
│   └── init.sql                    # Database schema — run `mysql -u root -p < sql/init.sql` to create all tables
│
├── grafana/
│   └── grafana.json                # Grafana dashboard — Backup, Updates & Health Monitoring
│
├── stacks/                         # Docker stack definitions — one subdirectory per functional group
│   ├── infra/                      # Infrastructure: dockerproxy, beszel-agent, dozzle
│   ├── databases/                  # Data tier: mariadb, postgres, adminer
│   ├── auth/                       # Authentication: authentik, swag, crowdsec
│   ├── monitoring/                 # Observability: grafana, victoriametrics
│   ├── dev/                        # Development: code-server, netbootxyz
│   ├── media/                      # Media: jellyseerr, tautulli
│   ├── apps/                       # Applications: homepage, firefox, homebox, etc.
│   ├── nfs/                        # NFS server
│   └── vpn/                        # VPN: wireguard, npm, beszel, dozzle-agent
│   # Each stack contains:
│   #   docker-compose.yaml         — plain YAML, committed to Git
│   #   env.j2                      — Jinja2 template, rendered to .env at deploy time from vault
│
├── requirements.txt                # Python pip dependencies (PyMySQL, proxmoxer, requests)
├── requirements.yaml               # Ansible Galaxy collection dependencies (community.docker, community.general, community.mysql)
├── CONTRIBUTING.md                 # Contribution guide: code style, testing, PR expectations
```

### Key files explained

**`group_vars/all.yaml`** — Shared Ansible defaults applied to all hosts. Loaded automatically by
Ansible when the inventory is in the same directory. Contains:
- `ansible_remote_tmp: ~/.ansible/tmp` — uses home directory instead of `/tmp` to avoid
  world-readable temporary files that could leak module arguments
- `ansible_python_interpreter: auto_silent` — suppresses Python discovery warnings
- `backup_base_dir`, `backup_tmp_dir`, `backup_tmp_file`, `backup_dest_path`, `backup_url` —
  centralized backup path defaults; `backup_base_dir` is the controller's backup root directory
  (used in `backup_dest_path` and the disk space pre-task assertion). `backup_tmp_dir` defaults
  to `"/backup"` — override in `vars/*.yaml` only for hosts that use a different staging path
  (e.g., `"/tmp/backup"` for AMP/Unifi Protect, `"/mnt/user/Backup/ansibletemp/"` for unRAID).
  `backup_url` overrides in `unifi_network.yaml`, `synology.yaml`, database vars, and `ubuntu_os.yaml`
- `backup_type: "Servers"` — default category for Grafana filtering; override to `"Appliances"`
  in vars files for purpose-built gear (proxmox, pikvm, unifi_network, unifi_protect)
- `update_type: "Servers"` — same convention as `backup_type` for the updates table
- `is_postgres: false`, `is_mariadb: false`, `is_influxdb: false` — database engine flags; all
  default false so each `db_*.yaml` vars file only needs to set the one flag that is `true`

**`ansible.cfg`** — Ansible configuration: disables `.retry` files and sets `stdout_callback: yaml`
for human-readable output.

**`vars/secrets.yaml`** — AES256-encrypted vault. Contains Discord webhook credentials
(`discord_webhook_id`, `discord_webhook_token`), MariaDB logging credentials (`logging_db_*`),
API keys (`semaphore_api_token`, `unvr_api_key`), domain suffixes for hostname normalization
(`domain_local`, `domain_ext`), and the ansible user SSH public key
(`ansible_user_ssh_pubkey`). Edit with `ansible-vault edit vars/secrets.yaml`.
**Never commit the decrypted file.**

**`vars/secrets.yaml.example`** — Template listing all expected vault keys with descriptions and
example values. Copy to `vars/secrets.yaml` and encrypt with `ansible-vault encrypt`. Optional
keys (MeTube webhook, UNVR API key, Synology credentials, SSH pubkey) are commented out.

**`vars/example.yaml`** — Template for creating new platform vars files. Documents all backup and
update variables with comments explaining each field. Copy to `vars/<platform>.yaml` when adding
a new platform.

**`sql/init.sql`** — Standalone database schema file. Creates the `ansible_logging` database and
all six tables (`backups`, `updates`, `maintenance`, `restores`, `health_checks`, `health_check_state`).
Run once with `mysql -u root -p < sql/init.sql`. Uses `CREATE TABLE IF NOT EXISTS` so re-running
is safe.

**`tasks/notify.yaml`** — Shared notification task (Discord + optional Apprise). Called via
`include_tasks` with `vars:` block providing required vars (`discord_title`, `discord_color`,
`discord_fields`) and optional vars. **All notification channels are optional** — each fires only
if its credentials are present in the vault; unconfigured channels are silently skipped.

**Apprise support** — set `apprise_urls` (space-separated Apprise service URLs; requires
`pip install apprise` on the controller) and/or `apprise_api_url` + `apprise_api_key` (self-hosted
[Apprise API](https://github.com/caronc/apprise-api) Docker container). Both can be active
simultaneously alongside Discord. Title and body are derived from the same `discord_name`,
`discord_operation`, and `discord_status` vars — no extra vars needed in callers.

Discord embed dict is built dynamically — optional fields are only included when set, so Discord
embeds stay clean. Optional vars and their usage:

- `discord_description` — embed body text (all playbooks on failure; `download_videos` per-video)
- `discord_url` — clickable link on title (backup/update/maintenance playbooks)
- `discord_footer` — dict with `text` and optional `icon_url` keys (`download_videos` per-video)
- `discord_timestamp` — ISO timestamp (defaults to `ansible_date_time.iso8601`)
- `discord_author` — override author name; defaults to `inventory_hostname`. Playbooks on
  `localhost` pass `discord_author: "{{ controller_fqdn }}"` (`maintain_health`)
- `discord_author_icon` — override author icon; defaults to Semaphore PNG (`download_videos` per-video uses platform icon)
- `discord_author_url` — clickable link on author name (`download_videos` per-video links to channel URL)
- `discord_username` — override webhook bot name; defaults to "Ansible Bot" (`download_videos` per-video uses "{uploader} - MeTube Bot")
- `discord_avatar` — override webhook bot avatar; defaults to Semaphore PNG (`download_videos` per-video uses MeTube icon)
- `discord_thumbnail` — override small thumbnail; defaults to unRAID notify PNG
- `discord_image` — large embed image (`download_videos` per-video uses video thumbnail)

Webhook credentials (`discord_webhook_id`, `discord_webhook_token`) are inherited from the
play-level `vars_files: vars/secrets.yaml`. Playbooks that need a different webhook (e.g.,
`download_videos` routes per-video notifications to a separate MeTube channel) override these
via `vars:` on the `include_tasks` call using `discord_download_webhook_id`/`discord_download_webhook_token`.

**`templates/metube.conf.j2`** — Jinja2 template for yt-dlp configuration, rendered per download
profile from `vars/{{ config_file }}.yaml`. Profile-specific settings (quality, paths, batch
file, filters) come from the vars file; common options (ignore errors, embed metadata,
sponsorblock) and the `--print-to-file` JSONL metadata export are in the template. Two vars
control conditional rendering:
- `ytdlp_quiet` — includes `-q` when true; `download_on_demand` sets `false` for verbose logs
- `ytdlp_filter_live` — includes `--match-filter !is_live` when true; `download_on_demand`
  sets `false` to allow live stream downloads

Deployed to the host via `ansible.builtin.template` to `/mnt/user/appdata/youtube-dl/<config_name>/`.

**`vars/download_default.yaml`** — yt-dlp download profile for scheduled channel downloads.
Loaded via `vars_files: vars/{{ config_file }}.yaml`. Contains `config_name` (profile identifier
for container paths), quality, format, paths, output template, batch file, archive, rate limit,
filters, and extractor args.

**`vars/download_on_demand.yaml`** — yt-dlp download profile for bookmarklet-triggered downloads.
Shares `config_name: default` with `download_default.yaml` (same config directory and download
archive) but overrides `ytdlp_batch_file` to read from the bookmarklet feeder file, and sets
`ytdlp_quiet: false` and `ytdlp_filter_live: false` for verbose output and live stream support.

Each Semaphore template uses its own `config_file` to load the correct profile:
- **Default** environment: `{"hosts_variable": "<download_host>", "config_file": "download_default"}`
- **On Demand** environment: `{"hosts_variable": "<download_host>", "config_file": "download_on_demand"}`

Both profiles share the same download archive (`/configs/default/downloaded`) so videos downloaded
by either template are not re-downloaded by the other.

yt-dlp partial failures (rc=1) are expected — members-only, geo-restricted, or unavailable videos
cause a non-zero exit but the `-i` flag continues past them. The playbook uses
`failed_when: ytdlp_result.rc >= 2` so only config/option errors (rc=2) trigger the rescue block.
The JSONL manifest is parsed with `select('match', '^\{.*\}$') | map('from_json')` — the regex
pre-filter skips any lines that aren't complete JSON objects (yt-dlp warnings, truncated lines
from descriptions containing literal newlines, etc.) so a single malformed entry doesn't crash
the entire parse.

**`verify_backups.yaml`** — On-demand backup verification. Tests DB backups by restoring to a temp
database on the same container (create `_restore_test_<dbname>` → restore → count tables → drop).
Tests config archives by verifying gzip integrity and extracting to a staging directory. For
`docker_stacks` hosts, verifies per-stack archives. For `amp` hosts, verifies per-instance
archives (integrity check + extract + file count). Reuses existing `vars/db_*.yaml` and
`vars/*.yaml` via standard `hosts_variable`/`config_file` routing. Logs results to `restores`
table with `operation: verify`. Semaphore templates: `Verify — AMP [Backup]` (id=77).

**`restore_databases.yaml`** — Database restore from backup dumps. Supports restoring a specific
database on a shared instance (e.g., just `nextcloud` on shared MariaDB without touching `semaphore`
or `ansible_logging`). Safety-gated with `confirm=yes` assertion. Creates pre-restore safety
backup by default. Stops only **same-host** dependent containers via `db_container_deps` mapping
(from `vars/db_*.yaml`) — cross-host app containers have empty deps and must be stopped manually or
via `restore_hosts.yaml -e with_databases=yes`. Supports `restore_db` (single DB) and
`restore_date` (specific backup date) parameters.

**`restore_hosts.yaml`** — Config/appdata restore from backup archives. Two modes: `staging` (extract
to `<backup_tmp_dir>/restore_staging/` for inspection) and `inplace` (extract to actual paths, requires
`confirm=yes`). Supports selective app restore via `-e restore_app=sonarr` (convention-based:
app name maps to subdirectory under `src_raw_files[0]`). Supports coordinated DB+appdata restore via
`-e with_databases=yes` — loads DB vars into a `_db_vars` namespace (avoiding collision with
play-level Docker vars) and uses `delegate_to: db_host` to restore databases on the correct host
(handles cross-host scenarios like appdata on one host + DB on another). Multi-container
apps handled via `app_info[restore_app]` in `vars/docker_stacks.yaml` + runtime config from container labels. `serial: 1`
matches `backup_hosts.yaml`. Requires `--limit <hostname>` when using `restore_app` on multi-host
groups.

**`deploy_grafana.yaml`** — Deploys the Grafana dashboard and `Ansible-Logging` MySQL datasource
via the Grafana API. Runs from localhost (no SSH) — requires `grafana_url` and
`grafana_service_account_token` in vault. The datasource is created only if missing (HTTP 404
check); never overwrites existing configuration. Before importing, the playbook syncs threshold
values from Ansible vars into the dashboard JSON:
- **Stale Backups** — SQL query (`> N hours`), panel color thresholds (yellow/red), and title
  all derive from `health_backup_stale_days` (default 10, converted to hours internally).
  Yellow threshold is set to `stale_days * 24 - 48` (2-day warning before red).
- **Stale Updates** — SQL query (`INTERVAL N DAY`) and title derive from
  `health_update_stale_days` (default 14).

The raw `grafana/grafana.json` keeps baseline values — the playbook replaces them at deploy time.
Change the Ansible var, re-deploy, and the Grafana panels update automatically.

**`deploy_stacks.yaml`** — Deploys Docker stacks from Git to target hosts. Each stack lives in
`stacks/<name>/` with a `docker-compose.yaml` and an `env.j2` Jinja2 template. The playbook
creates `/opt/stacks/<name>/` on the target, renders `.env` from vault secrets (mode `0600`),
copies the compose file, validates with `docker compose config`, and starts with
`community.docker.docker_compose_v2` (removes orphans). Stacks deploy in `stack_assignments`
order — databases before auth, etc. — ensuring dependency readiness. The `databases` stack
includes a port-wait step (3306, 5432) before proceeding. Pre-tasks assert the host has a
`stack_assignments` entry, sufficient disk space, and DB connectivity. Supports single-stack
deploy via `-e deploy_stack=<name>`, render-only mode via `-e validate_only=yes`, and debug
output via `-e debug_no_log=yes`. Runs `serial: 1` to avoid parallel deploy issues.

**`build_ubuntu.yaml`** — Two-play playbook for Proxmox VM lifecycle management via cloud-init
template cloning. Supports four `vm_state` values: `present` (default), `absent`, `snapshot`, and
`revert`. **Play 1** (localhost) manages the VM via the Proxmox API. On **create**, it first
ensures a cloud-init template exists on Ceph shared storage (one-time per cluster — SSHes to the
PVE node via `delegate_to`, downloads the Ubuntu Noble cloud image, and converts it to a Proxmox
template with `qm` commands). It then clones the template to the target node using cross-node
cloning (`node` = template source, `target` = destination — works because Ceph storage is shared),
configures cloud-init (user, SSH key from vault, static IP, DNS), resizes the disk via the
Proxmox REST API, starts the VM, and waits for SSH. **Destroy** stops and removes the VM.
**Snapshot** creates a named disk-only snapshot via the Proxmox REST API (no RAM state — fast and
lightweight on Ceph). **Revert** stops the VM, rolls back to a named snapshot, restarts, and waits
for SSH — ideal for iterative testing (e.g., snapshot after bootstrap, test restores, revert,
repeat). Snapshot and revert require `-e snapshot_name=<name>`. VM definitions (VMID, IP, cores,
memory, disk, target node) come from `vars/vm_definitions.yaml`, selected by `-e vm_name=<key>`.
`pve_api_host` should be the cluster VIP (set by `setup_pve_vip.yaml`) — provisioning uses the
cluster resources API to resolve which node the VM actually landed on, so node assignment is
VIP-safe regardless of which PVE node is currently MASTER. `tasks/provision_vm.yaml` (shared with
`test_restore.yaml`) is idempotent: it checks the cluster for an existing VMID before cloning, so
re-running after a partial failure resumes from where it left off rather than failing on a duplicate
VMID. **Play 2** (the new VM, added
to in-memory `build_target` group — only runs on create) bootstraps Ubuntu: waits for cloud-init
to finish and apt locks to release, runs dist-upgrade, installs Docker and base packages, enables
Docker service, adds user to docker/sudo groups, configures passwordless sudo, hardens SSH
(matching `setup_ansible_user.yaml` — prohibit root password, disable password auth, allow ssh-rsa
for Guacamole), and configures UFW (default deny + allow SSH). Deploy stacks separately via
`deploy_stacks.yaml` after provisioning.

**`restore_amp.yaml`** — AMP game server instance restore. Safety-gated with `confirm=yes`. Stops
each instance, removes the existing data directory, extracts the latest archive from the
controller, and restarts instances that were running before. Supports restoring all instances
(default) or a single one via `-e amp_instance_filter=<instance>`. Accepts `-e restore_target=<host>`.
Play 1 (localhost) discovers and validates backup archives; Play 2 (the AMP host) performs the
restore. Per-instance results logged to the `restores` table; partial success supported (some
instances succeed, others fail). Uses `tasks/restore_single_amp_instance.yaml` (loop var: `_amp_instance`).
Semaphore template: `Restore — AMP [Instance]` (id=76). Required extra vars: `-e restore_target=<host> -e confirm=yes`.

**`maintain_amp.yaml`** — AMP game server maintenance. Runs on `amp` hosts. Cleans up old AMP version binaries (keeps `amp_versions_keep` newest under `Versions/Mainline/`), rotates `instances.json` `.bak` files (same keep count), removes crash core dump files (`core*`) from all instance directories, purges instance log files older than 30 days, prunes unused Docker images (non-dangling), and vacuums journal logs older than `amp_journal_max_age`. Stale `.tar.gz` files in `/tmp/backup` older than 1 day are also removed. Uses `block`/`rescue`/`always` — sends Discord alert on failure; logs to `maintenance` table (`type: Servers`, `subtype: Maintenance`) regardless. Supports `--tags versions` to run only the version-prune step; `-e amp_versions_keep=<n>` overrides the keep count. Semaphore template: `Maintain — AMP [Cleanup]` (id=38). Scheduled weekly.

**`restore_app.yaml`** — Production single-app restore. Safety-gated with `confirm=yes`. Stops
the target stack, restores DB(s) + appdata inplace, restarts the stack, runs HTTP health checks,
and logs to the `restores` table (`restore_subtype: Appdata`). Accepts `-e restore_app=<app>` and
`-e restore_target=<host>`. Uses `tasks/restore_app_step.yaml` for per-app logic. Sends Discord
notification on success or failure.

**`test_backup_restore.yaml`** — Automated all-app restore test on a disposable VM. Provisions a
test VM (or reuses one with `-e provision=false`), deploys all stacks, restores each `app_info`
app in sequence (DB + appdata inplace), runs HTTP health checks, and summarizes results via
Discord. Includes OOM auto-recovery: if a restore OOM-kills the VM, saves partial results to
localhost, doubles RAM via PVE API, reboots, and retries the OOM-failed apps. Reverts the VM to
a pre-restore snapshot when done. Uses `tasks/restore_app_step.yaml` (loop var: `_test_app`).
Logs to the `maintenance` table (type: `Servers`, subtype: `Test Backup Restore`).

**`rollback_docker.yaml`** — Reverts Docker containers to their previous image versions using
the snapshot saved by `update_systems.yaml`. Two rollback paths: **fast** (old image still on
disk — `docker tag` re-tag, no network needed) and **slow** (image pruned by
`maintain_docker.yaml` — pulls old version tag from registry). Safety-gated with
`confirm=yes`. Without it, shows snapshot info and exits (dry-run). Supports three
scopes: all containers (default), per-stack via `-e rollback_stack=<name>`, or per-service via
`-e rollback_service=<name>`. Docker Compose
hosts (`docker_stacks`) only — for unRAID `docker_run` hosts, see manual rollback guidance
below. Uses `tasks/log_restore.yaml` with `operation: rollback` (per-service). Discord
notification uses yellow (16776960) to distinguish from green/red.

**`update_systems.yaml` — rollback snapshot:** Before pulling new Docker Compose images, the
update playbook captures a `.rollback_snapshot.json` per stack in `/opt/stacks/<stack>/` with
the timestamp, image name, full image ID (`sha256:...`), and version label for every target
service. Uses the same 3-tier label detection as the update comparison. Each update overwrites
the previous snapshot — only the last pre-update state is kept. The snapshot files are included
in regular `/opt` appdata backups automatically.

**Per-stack backup architecture:** Docker stacks hosts use per-stack backup archives instead of
a monolithic `/opt` tar.gz. Each stack is stopped individually, archived, and restarted — minimizing
downtime. Backup paths are discovered at runtime from `homelab.backup.paths` labels on containers
(stopped containers retain labels); `/opt/stacks/{name}/` (compose + .env) is always appended
automatically. Database data directories are excluded because they have dedicated SQL dump jobs.
Non-docker_stacks hosts (proxmox, pikvm, unraid, unifi) still use monolithic archives with
`backup_exclude_dirs | default([])` for hosts that define exclusions.

**Per-instance AMP backup architecture:** AMP hosts use per-instance backup archives, mirroring the per-stack pattern used for Docker hosts. `backup_hosts.yaml` discovers instance names from `{{ amp_home }}/.ampdata/instances/` (one subdirectory per instance), saves the `instances.json` registry and an instance inventory JSON to the controller, then loops over instances via `tasks/backup_single_amp_instance.yaml`. Supports single-instance targeting with `-e amp_instance_filter=<instance>`. The `Backups/` and `Versions/` subdirectories are excluded from each instance archive to keep archive sizes manageable (these directories hold AMP's own internal backups, not the game world data). Results are accumulated in `_amp_backup_results`; a non-empty failed list sets `backup_failed: true` for Discord/DB reporting.

**`tasks/backup_single_amp_instance.yaml`** — Per-instance AMP backup loop body called by `backup_hosts.yaml` (loop var: `_amp_instance`). Stops the instance via the AMP management script, creates a `.tar.gz` of the instance data directory (excluding `Backups/` and `Versions/`), verifies integrity with `gunzip -t`, fetches the archive to the controller, then restarts the instance. The `always:` block restarts the instance regardless of outcome — instances are always brought back up even on backup failure. Results appended to `_amp_backup_results`.

**`tasks/restore_single_amp_instance.yaml`** — Per-instance AMP restore loop body called by `restore_amp.yaml` (loop var: `_amp_instance`). Stops the instance, removes the existing data directory, extracts the latest archive from the controller, and restarts the instance if it was running before the restore. Logs per-instance result to the `restores` table. Mirrors the backup task's structure — stop, act, restart in `always:`.

**`tasks/backup_single_stack.yaml`** — Per-stack backup loop body called by `backup_hosts.yaml`
(loop var: `_backup_stack`). Stops the named stack via `docker_stop.yaml`, discovers backup paths
from `homelab.backup.paths` container labels, appends `/opt/stacks/<stack>/`, filters to paths
that exist (via `stat`), creates a `.tar.gz` archive, verifies integrity with `gunzip -t`, fetches
to the controller, and records success/failure in `_stack_backup_results`. The `always:` block
restarts the stack and deletes the temp archive regardless of outcome — containers are always
brought back up even when the backup fails.

**`tasks/backup_single_db.yaml`** — Per-database backup loop body called by `backup_databases.yaml`
(loop var: `_current_db`). Delegates to `db_dump.yaml`, verifies integrity (`gzip -t` for SQL
dumps, `tar tzf` for InfluxDB), fetches the dump to the controller, and appends a success/failure
record to `combined_results`. Inherits engine flags (`is_postgres`, `is_mariadb`, `is_influxdb`),
credentials, and paths from the caller's scope.

**`tasks/db_dump.yaml`** — Single-database dump engine abstraction. Accepts `_db_name`,
`_db_container`, `_db_username`, `_db_password`, `_db_dest_file`, and engine flags
(`_db_is_postgres`, `_db_is_mariadb`, `_db_is_influxdb`). For PostgreSQL: `pg_dump | gzip`.
For MariaDB: `mysqldump | gzip` (password via `MYSQL_PWD` env var, never on the command line).
For InfluxDB: `influxd backup -portable` → `docker cp` → `tar czf`. Used by `backup_single_db.yaml`
and `verify_backups.yaml` (for the restore-test flow).

**`tasks/db_restore.yaml`** — Single-database restore engine abstraction. Accepts the same engine
flags plus `_db_source_file` and optional `_db_target_name` (when set, creates the target DB first
rather than overwriting the source — used by `verify_backups.yaml` to restore to a temp DB without
touching production). For PostgreSQL: `gunzip -cf | psql`. For MariaDB: `gunzip -cf | mysql`.
For InfluxDB: `tar xzf` → `docker cp` → `influxd restore -portable`. Used by both
`verify_backups.yaml` (temp restore) and `restore_databases.yaml` (production restore).

**`tasks/db_count.yaml`** — Count tables or measurements in a database for verification.
For PostgreSQL: queries `information_schema.tables WHERE table_schema = 'public'`. For MariaDB:
queries `information_schema.tables WHERE table_schema = '<db>'`. For InfluxDB: `SHOW MEASUREMENTS`
piped through `wc -l`. Accumulates results in `_db_count_results` dict (keyed by `_db_name`).
Used by `verify_backups.yaml` to confirm a restored backup is non-empty.

**`tasks/db_drop_temp.yaml`** — Drop a database and clean up container temp files. For
PostgreSQL: `DROP DATABASE IF EXISTS` on the `postgres` maintenance DB. For MariaDB:
`DROP DATABASE IF EXISTS`. For InfluxDB: `DROP DATABASE` + container temp directory cleanup.
Accepts optional `_db_influx_source_name` for InfluxDB temp path cleanup. Used by
`verify_backups.yaml` after counting tables in a temp restore DB.

**`tasks/log_restore.yaml`** — Shared MariaDB logging for the `restores` table. Uses
`community.mysql.mysql_query` with parameterized queries. Separate from `log_mariadb.yaml` because
the `restores` table has a different schema (`source_file`, `operation`, `detail` instead of
`file_name`/`file_size` or `type`/`subtype`). Vars: `log_hostname`, `restore_application`,
`restore_source_file`, `restore_type`, `restore_subtype`, `restore_operation`, `restore_status`,
`restore_detail`.

**`tasks/log_mariadb.yaml`** — Shared MariaDB logging for the three operational tables (`backups`,
`updates`, `maintenance`). Receives `log_hostname` and passes it directly to the INSERT — no
transformation applied. Uses `community.mysql.mysql_query` with parameterized queries
(`%s` + `positional_args`) to INSERT via PyMySQL on localhost. The `log_table` var selects which
table, query, and args to use. For updates, `log_status` defaults to `'success'` if omitted
(backwards-compatible). All DB tasks set `ansible_python_interpreter: "{{ ansible_playbook_python }}"`
to use the Ansible venv Python (where PyMySQL is installed) instead of the system Python discovered
by `auto_silent`.

**`tasks/log_health_check.yaml`** — Shared MariaDB logging for the `health_checks` table. Uses
`community.mysql.mysql_query` with parameterized queries. Separate from `log_mariadb.yaml` because
the table schema is different (check_name, check_status, check_value, check_detail instead of
type/subtype/file_name/version). Called by `maintain_health.yaml` once per check per host per run.
Vars: `log_hostname`, `hc_check_name`, `hc_status`, `hc_value`, `hc_detail`.

**`tasks/assert_config_file.yaml`** — Pre-task assertion that `config_file` is defined and
non-empty. Catches misconfigured Semaphore variable groups before any work starts.

**`tasks/assert_disk_space.yaml`** — Shared pre-task assertion that a given filesystem path has
sufficient free space. Called via `include_tasks` with `vars:` block providing `assert_disk_path`
and `assert_disk_min_gb`. Uses `df --output=avail` + `ansible.builtin.assert`. Has
`check_mode: false` on the df task so it runs during `--check`.

**`tasks/assert_db_connectivity.yaml`** — Shared pre-task assertion that the MariaDB logging
database is reachable. Runs `SELECT 1` via `community.mysql.mysql_query`. Inherits `logging_db_*`
vars from playbook scope. Has `check_mode: false` so it validates connectivity during `--check`.
Used by all 18 operational playbooks that log to MariaDB (every playbook except `download_videos.yaml`
and `setup_ansible_user.yaml`).

**`maintain_health.yaml` — check notes:**
- **Host groups:** Play 2 and Play 3 target hosts defined by `health_check_groups` in
  `vars/semaphore_check.yaml`. Adding a new group to that list is all that's needed to include
  it in health monitoring — no hardcoded host patterns in the playbook itself.
- **State management:** Last check timestamp stored in `health_check_state` table (single-row
  MariaDB table), not in a file. Survives container restarts. Read at Play 1 start, written at
  Play 3 end via `INSERT ... ON DUPLICATE KEY UPDATE`.
- **Timestamp comparison:** Play 1 Check 1 (Semaphore failed tasks) normalizes API timestamps by
  replacing `+00:00` suffix with `Z` before comparison, ensuring consistent lexicographic ordering
  regardless of Semaphore's timezone format.
- **Security:** The Semaphore API URI task has `no_log: true` to prevent the API token from
  appearing in registered variables or verbose output. The vault variable `semaphore_api_token`
  is referenced directly — no alias variables that could leak in debug output.
- `smart_health`: auto-installs `smartmontools` via apt on ubuntu/pve/pbs hosts before scanning.
  unRAID includes `smartctl` by default. Empty stdout (e.g. sudo unavailable) is treated as
  `not checked` / status `ok` rather than a false positive.
- `pve_cluster` / `ceph_health`: PVE nodes only (`when: "'pve' in group_names"`). Both require
  `become: true` — `pvecm` and `ceph` require root.
- `ssl_cert`: scans `/etc/letsencrypt/live/*/cert.pem`. Hosts with no certs log `no certs` / ok.
- `stale_maintenance`: mirrors the stale_backup query pattern — alerts if any host has no
  successful maintenance run within `health_maintenance_stale_days` (default: 3 days).
- `backup_size_anomaly`: flags the latest backup for any `application + hostname` pair whose
  file size is below `health_backup_size_min_pct`% (default: 50%) of its own 30-day rolling
  average (minimum 3 prior entries). Use `health_backup_size_exclude` (list in
  `vars/semaphore_check.yaml`) to suppress specific application names — e.g. after an
  architectural change that intentionally reduces individual backup file sizes (splitting a
  monolithic archive into per-stack archives, moving DB dumps out of a Docker archive, etc.).
  Add the app names, run one health check, then remove them once the 30-day baseline resets.
- `mariadb_health`: checks connection count vs `max_connections` and scans `information_schema`
  for crashed tables. Warning at `health_db_connections_warn_pct`% (default: 80).
- `wan_connectivity`: simple HTTP GET to `health_wan_url` (default: Cloudflare CDN trace).
  Critical on any failure — indicates outbound internet is down.
- `ntp_sync`: uses `timedatectl` on systemd hosts, `ntpq` on unRAID. Reports sync status or
  offset in milliseconds. Warning if not synced or offset exceeds `health_ntp_max_offset_ms`.
- `dns_resolution`: `getent hosts` against `health_dns_hostname`. Critical on failure.
- `unraid_array`: unRAID only — `mdcmd status` for array state, plus `disks.ini` for disabled
  disk count. Counts two failure modes: `DISK_DSBL` (present but disabled = real problem) and
  `DISK_NP_DSBL` with a non-empty `id` (configured disk went missing = real problem). Ignores
  `DISK_NP_DSBL` with empty `id` (unassigned slot like unused parity2). The `id` field in
  `disks.ini` holds the disk serial from `super.dat` — empty means never assigned.
  Critical if state is not `STARTED` or any configured disks are disabled/missing.
- `pbs_datastore`: PBS only — `proxmox-backup-manager datastore list` to verify datastores
  are present and accessible. Warning if no datastores found.
- `zfs_pool`: runs on all hosts — `zpool list -H -o name,health` detects degraded/faulted pools.
  Skips gracefully if `zpool` not installed or no pools exist. DEGRADED = warning,
  FAULTED/OFFLINE/REMOVED/UNAVAIL = critical.
- `btrfs_health`: runs on all hosts — `btrfs device stats` on each BTRFS mount (discovered via
  `findmnt -t btrfs`). Skips gracefully if `btrfs` not installed or no BTRFS filesystems exist.
  Any non-zero error counter (write_io_errs, read_io_errs, corruption_errs, etc.) = critical.
- `docker_http`: per-host Docker container HTTP endpoint checks. Configured via
  `docker_health_endpoints` variable (list of `{name, url, status_code, validate_certs}` dicts).
  Hosts without endpoints defined are skipped. Critical if any endpoint fails to respond.
- `host_reachable`: Play 3 detects hosts where `host_reachable` was never set (unreachable during
  Play 2 due to `ignore_unreachable: true`). Sends a single Discord alert listing all unreachable
  hosts and logs each with `check_name: 'host_reachable'`, `status: 'critical'` to the DB.

---

## Semaphore Setup

### Python Dependencies

Semaphore's Ansible venv needs the Python packages from `requirements.txt`. On unRAID, these are
installed via the container's post-argument (Docker UI → Post Arguments):

```
/bin/sh -c "pip install PyMySQL proxmoxer requests && /usr/local/bin/semaphore server --config /etc/semaphore/config.json"
```

This runs on every container start, so packages persist across restarts and recreates.

### Container Group Membership

The Semaphore container process must have **read and write** access to the `/backup` bind
mount. If the backup storage is owned by a specific group (e.g. `users`, GID 100), the
container's process user may not be the owner — access is via group membership.

Add the container to that group:

```
--group-add <GID>
```

In Docker Compose this is `group_add: ["<GID>"]`. Without this, archive discovery in
`test_restore.yaml` and `verify_backups.yaml` silently fails — the shell glob returns empty
and `stat` returns permission denied.

The backup directories must have **group write** enabled (`mode: 0770`, not 0750).
`backup_offline.yaml` enforces permissions recursively on every run — if the mode is ever set
to 0750, all fetch-based backup tasks will fail with permission denied on the controller side.

#### fetch tasks and `become`

All `ansible.builtin.fetch` tasks that write to `/backup` must include `become: false`. The
Semaphore template sets `become: true` globally; without the override, the fetch write runs as
root, which a FUSE-mounted backup share (using `default_permissions`) may block. This affects
`backup_single_db.yaml`, `backup_single_stack.yaml`, and `backup_hosts.yaml`.

### Inventories

Inventories are stored in **Semaphore's database**, not in this repository. `inventory.yaml` is
gitignored — it exists on the host running Semaphore as a local reference but is not committed.
To update an inventory, edit it directly in the Semaphore UI or via its database.

Semaphore inventories are organized by **authentication method** — each inventory groups hosts
that share the same SSH key or login password. Within each inventory, hosts belong to
**functional groups** (`[ubuntu]`, `[pve]`, `[docker_stacks]`, etc.) that determine which
playbook logic applies. The `hosts_variable` in each template's variable group scopes the
playbook to the correct functional group.

| Inventory | Credential (Key Store) | Covers |
|-----------|----------------------|--------|
| `ansible-user-ssh` | ansible SSH key (id=8) | Ubuntu, Docker, Proxmox, unRAID, controller, amp, vps; maintain_health (all SSH hosts + localhost) |
| `root` | root login_password (id=13) | Synology, NAS host only |
| `pikvm` | PiKVM login_password (id=11) | pikvm |
| `unifi_network` | root login_password (id=13) | udmp |
| `unifi_protect` | unifi_protect login_password (id=29) | unvr |
| `local` | — | localhost only (no templates currently use this inventory) |

> **Rule:** Use `ansible-user-ssh` (id=3) for all recurring/scheduled templates — Ubuntu, Proxmox, PBS, unRAID, AMP, VPS. The `root` inventory (id=12) is reserved for Synology/NAS targets only; do not assign it to Proxmox or other SSH-key hosts even as a convenience shortcut.

### Variable Groups

Each Semaphore variable group JSON contains only routing information:

```json
{"hosts_variable": "pve:pbs"}
```

When the `vars/*.yaml` filename differs from the `hosts_variable` value, add `config_file`:

```json
{"hosts_variable": "ubuntu", "config_file": "ubuntu_os"}
```

Current cases requiring explicit `config_file`: `ubuntu_os`, `unraid_os`, `synology`,
`db_primary_postgres`, `db_primary_mariadb`, `db_primary_influxdb`, `db_secondary_postgres`,
`download_default`, `download_on_demand`.

**Environment naming convention:** Semaphore environment names match the `config_file` value
(or `hosts_variable` when `config_file` is not needed). For database targets, use role-based
names (`db_primary_postgres`, `db_primary_mariadb`, `db_secondary_postgres`) — never
hostname-based names. Verify and restore templates **share** the same Semaphore environment as
backup templates for the same target — do not create separate environments.

**`hosts_variable` lives in Semaphore only** — it is resolved at `hosts:` parse time before
`vars_files` load. Any copy in a `vars/` file would be ignored for host targeting.

### Key Store

6 entries. **Do not delete any of these.**

| ID | Name | Type | Purpose |
|----|------|------|---------|
| 4 | GitHubSSH | SSH | Semaphore → GitHub repo access |
| 8 | ansible | SSH | ansible-user SSH key; `ansible-user-ssh` inventory |
| 11 | PiKVM | login_password | PiKVM inventory |
| 13 | root | login_password | `root` and `unifi_network` inventories |
| 29 | unifi_protect | login_password | Unifi Protect inventory |
| 30 | ansible-vault | login_password | Vault decryption password |

SSH/login credentials are attached to **inventories** and injected by Semaphore at runtime.
They are not Ansible variables and are not in `vars/` files.

### Template naming convention

Semaphore template (task) names follow `Verb — Target [Subtype]`:

| Pattern | Example |
|---|---|
| `Setup — {Target} [{Subtype}]` | `Setup — Ansible User [SSH]` |
| `Backup — {Target} [{Subtype}]` | `Backup — Proxmox [Config]`, `Backup — unRAID [Offline]` |
| `Backup — Database [{Role} {Engine}]` | `Backup — Database [Primary PostgreSQL]`, `Backup — Database [Secondary PostgreSQL]` |
| `Build — {Target} [{Subtype}]` | `Build — Ubuntu [VM]` |
| `Deploy — {Target} [{Subtype}]` | `Deploy — Docker Stacks`, `Deploy — Grafana [Dashboard]` |
| `Download — {Target} [{Subtype}]` | `Download — Videos [Channels]`, `Download — Videos [On Demand]` |
| `Maintain — {Target} [{Subtype}]` | `Maintain — AMP [Cleanup]`, `Maintain — Cache [Flush]`, `Maintain — Docker [Cleanup]`, `Maintain — Health [Check]` |
| `Restore — {Target} [{Subtype}]` | `Restore — Database [Primary PostgreSQL]`, `Restore — Docker Run [Appdata]` |
| `Rollback — {Target} [{Subtype}]` | `Rollback — Docker [Containers]` |
| `Update — {Target} [{Subtype}]` | `Update — Proxmox [Appliance]`, `Update — Ubuntu [OS]`, `Update — Docker Stacks [Containers]` |
| `Verify — {Target} [{Subtype}]` | `Verify — Database [Primary PostgreSQL]`, `Verify — Proxmox [Config]` |

The `[Subtype]` suffix makes templates instantly distinguishable when a target has more than one
variant (e.g., `Backup — unRAID [Config]` vs `Backup — unRAID [Offline]`, or `Download — Videos [Channels]`
vs `Download — Videos [On Demand]`). Database templates use `Database` as the target so all DB
operations cluster together alphabetically, with `[Role Engine]` (e.g., `[Primary PostgreSQL]`,
`[Secondary PostgreSQL]`) as the subtype.

### Template views

Templates are organized into views (tabs in the Semaphore UI) by verb:

| View | Templates | Verb prefix |
|------|-----------|-------------|
| Backups | 13 | `Backup —` |
| Updates | 6 | `Update —` |
| Maintenance | 8 | `Maintain —` |
| Downloads | 2 | `Download —` |
| Verify | 9 | `Verify —` |
| Restore | 9 | `Restore —`, `Rollback —` |
| Deploy | 3 | `Deploy —`, `Build —` |
| Setup | 2 | `Setup —` |

When adding a new template, assign it to the matching view. Views are stored in the
`project__view` table; templates reference views via the `view_id` column in
`project__template`.

### Managing templates via SQL (Adminer)

Semaphore stores templates, environments, and vault associations in MariaDB (MySQL syntax —
use `LIKE` not `ILIKE`, backtick-quoted identifiers). When creating a new template
programmatically (e.g., for a new download profile), four tables are involved:

| Table | Purpose |
|-------|---------|
| `project__environment` | Variable groups — extra vars passed to the playbook |
| `project__template` | Template definitions — playbook, inventory, environment, view, settings |
| `project__template_vault` | Links a template to a vault decryption key |
| `project__view` | UI views (tabs) — templates must reference a `view_id` to appear in filtered views |

Run all three statements together in Adminer — subqueries resolve IDs automatically so no
manual lookup is needed. Replace the `<placeholders>` with actual values:

```sql
-- 1. Create environment (variable group with extra vars as JSON)
INSERT INTO project__environment (project_id, name, json, password, env)
VALUES (1, '<env_name>', '{"var_name": "value"}', '', '{}');

-- 2. Create template (subqueries resolve repository_id, environment_id, and view_id)
--    IMPORTANT: view_id must be set or the template won't appear in filtered views.
--    Use the subquery below to resolve view_id by view title (e.g., 'Deploy', 'Backups').
INSERT INTO project__template
  (project_id, inventory_id, repository_id, environment_id, view_id,
   playbook, name, type, app,
   suppress_success_alerts, arguments, allow_override_args_in_task,
   allow_override_branch_in_task, allow_parallel_tasks, autorun, tasks)
VALUES
  (1, <inventory_id>,
   (SELECT id FROM project__repository WHERE project_id = 1 AND git_branch = '<branch_name>'),
   (SELECT id FROM project__environment WHERE project_id = 1 AND name = '<env_name>'),
   (SELECT id FROM project__view WHERE project_id = 1 AND title = '<View Title>'),
   '<path/to/playbook.yaml>', '<Template Name>', '',
   'ansible', 1, '[]', 1, 0, 0, 0, 0);

-- 3. Link vault key (subquery resolves template_id)
INSERT INTO project__template_vault (project_id, template_id, vault_key_id, type)
VALUES (1,
  (SELECT id FROM project__template WHERE project_id = 1 AND name = '<Template Name>'),
  30, 'password');
```

Common `inventory_id` values: 3 (`ansible-user-ssh`), 12 (`root`). See [Inventories](#inventories)
for the full list. `vault_key_id = 30` is `ansible-vault` — the vault decryption password.
See [Key Store](#key-store). The `git_branch` subquery resolves the repository by branch name —
use `'main'` for production templates or the feature branch name for testing.
`allow_override_args_in_task = 1` enables CLI args (e.g., `--limit`) when running from the
Semaphore UI — always set to `1` so operators can target specific hosts.

**View titles** match the verb prefix: `Backups`, `Updates`, `Maintenance`, `Downloads`,
`Verify`, `Restore`, `Deploy`, `Setup`. See [Template views](#template-views) for the full list.

**Verify the result:**

```sql
SELECT t.id, t.name, t.playbook, t.inventory_id, t.environment_id,
       t.view_id, vw.title AS view_name, v.vault_key_id
FROM project__template t
LEFT JOIN project__template_vault v ON v.template_id = t.id
LEFT JOIN project__view vw ON t.view_id = vw.id AND t.project_id = vw.project_id
WHERE t.project_id = 1 AND t.name = '<Template Name>';
```

> **Note:** All `project_id = 1` references, `inventory_id` values (3, 12, 30), and numeric IDs
> in the SQL above are specific to one Semaphore instance. Replace them with actual IDs from your
> deployment — use the verify query above to confirm IDs after insertion.

> **Warning — duplicate environment names:** If two environments share the same `name`, any
> subquery using `WHERE name = '...'` will return multiple rows and the `INSERT` will fail silently
> (0 rows affected, no error in Adminer). Before inserting a template, run:
> `SELECT id, name, json FROM project__environment WHERE project_id = 1 AND name = '<env_name>';`
> If duplicates exist, use the correct `id` directly as a literal in the template INSERT instead
> of the subquery. Clean up the orphan with `DELETE FROM project__environment WHERE id = <orphan_id>;`.

### Managing schedules via SQL (Adminer)

Schedules are stored in `project__schedule`. Add a schedule whenever a new template is created
or when a template gains a new operational mode (e.g., a monthly extra-vars variant).

```sql
-- Add a schedule to an existing template (run in semaphore database)
INSERT INTO project__schedule (project_id, template_id, name, cron_format, active)
VALUES (
  1,
  (SELECT id FROM project__template WHERE project_id = 1 AND name = '<Template Name>'),
  '<Schedule Label>',
  '<cron expression>',
  1
);

-- Audit all templates and their schedules
SELECT t.id, t.name, s.cron_format, s.active
FROM project__template t
LEFT JOIN project__schedule s ON s.template_id = t.id
WHERE t.project_id = 1
ORDER BY t.name;
```

**Rule:** Every time a new template is created or an existing template gains a new
operational mode, review schedules and add the appropriate cron entry. Templates with no
schedule are ad-hoc only — document that intent in a comment if intentional.

<details>
<summary>Weekly schedule at a glance</summary>

**Every day (not shown in table):** Secondary PG Backup + Unifi Restart @ 1am · Cache Flush @ 5am · Health Check @ 7am & 7pm · Download Videos every 4h

| Time | Sun | Mon | Tue | Wed | Thu | Fri | Sat |
|------|-----|-----|-----|-----|-----|-----|-----|
| 1am  | Docker Stacks Bkp | Verify Proxmox | — | — | unRAID Bkp | — | MariaDB Bkp |
| 2am  | Maintain PVE | — | — | — | — | — | — |
| 3am  | Docker Run Bkp | Proxmox Update | Docker Run Update | Ubuntu Update | AMP Bkp · PiKVM Update | Unifi Net Bkp | PG Primary Bkp · AMP Update |
| 4am  | Verify Docker Stacks | Verify Sec PG | Docker Stacks Update | — | Verify unRAID | Unifi Protect Bkp | Verify MariaDB |
| 6am  | Verify Docker Run | — | — | — | — | — | Verify PG Primary |
| 8am  | — | AMP Cleanup | — | — | — | — | — |
| 11pm | Proxmox Bkp | PiKVM Bkp | — | — | — | — | — |

**Monthly (1st):** unRAID tree index @ 2am · Docker Cleanup @ 3am · Logging DB Cleanup @ 5am

**Intentionally unscheduled:** Restore · Rollback · Deploy · Build · Setup · Test · Download [On Demand] · Semaphore Cleanup *(manual — preserve failed job logs)* · InfluxDB templates *(paused — OOM)*

</details>

---

## Playbook Patterns

### vars_files loading

The main data playbooks (`backup_hosts.yaml`, `backup_databases.yaml`, `update_systems.yaml`,
`backup_offline.yaml`, `download_videos.yaml`, `verify_backups.yaml`, `restore_databases.yaml`,
`restore_hosts.yaml`) load two vars files at the play level:

```yaml
vars_files:
  - vars/secrets.yaml          # all secrets + domain config (domain_local/ext)
  - vars/{{ config_file }}.yaml  # host-specific config
vars:
  config_file: "{{ hosts_variable }}"
```

`config_file` defaults to `hosts_variable`. Override it in the Semaphore variable group JSON when
they differ (download templates always override — `config_file` has no relation to `hosts_variable`
for downloads). A pre-task assertion (`tasks/assert_config_file.yaml`) catches a missing or empty
`config_file` immediately.

### Extra vars conventions

All user-facing `-e` extra vars follow these naming and value patterns:

**Routing (always set via Semaphore variable group JSON, not on CLI):**
| Var | Purpose |
|-----|---------|
| `hosts_variable` | Ansible inventory group to target (e.g., `docker_stacks`, `amp`) |
| `config_file` | vars file to load — defaults to `hosts_variable`; override when they differ |

**Safety gates (required for destructive operations, intentionally never pre-set):**
| Var | Playbooks |
|-----|-----------|
| `confirm=yes` | All restore playbooks (`restore_databases`, `restore_hosts`, `restore_app`, `restore_amp`) and `rollback_docker` |

**Opt-in behaviours (`=yes` to enable, omit to skip):**
| Var | Purpose |
|-----|---------|
| `with_docker=yes` | Stop containers before restore, restart after (`restore_hosts`) |
| `with_databases=yes` | Include coordinated DB restore alongside appdata restore (`restore_hosts`) |
| `validate_only=yes` | Render and validate only, skip `docker compose up` (`deploy_stacks`) |
| `dr_mode=yes` | DR recovery mode — skip snapshot/revert, keep state (`test_restore`) |
| `debug_no_log=yes` | Reveal output normally hidden by `no_log` (any playbook; see [no_log pattern](#no_log-pattern)) |

**Scope selectors (string values, omit for default/all):**
| Var | Purpose |
|-----|---------|
| `restore_mode=inplace` | Inplace restore (default: `staging`) — requires `confirm=yes` |
| `restore_app=<name>` | Restore a single app by key |
| `restore_stack=<name>` | Restore a single stack by name |
| `restore_db=<name>` | Restore a single database |
| `restore_date=YYYY-MM-DD` | Restore from a specific date's backup |
| `restore_target=<fqdn>` | Production host to restore on (`restore_app`, `restore_amp`) |
| `deploy_stack=<name>` | Deploy a single stack instead of all |
| `rollback_stack=<name>` | Rollback a single stack |
| `rollback_service=<name>` | Rollback a single service |
| `amp_instance_filter=<name>` | Target a single AMP instance by name (`backup_hosts`, `restore_amp`) |
| `vm_name=<key>` | VM definition key from `vars/vm_definitions.yaml` |

**Value rules:**
- Boolean triggers always use `=yes` — never `=true`, `=false`, or `=no` on the CLI
- YAML booleans inside playbook code use `true`/`false` (see [Coding conventions](#coding-conventions))
- `pre_backup=no` skips the pre-restore safety backup in `restore_databases.yaml` (default: on)

### Error handling

All production playbooks use `block`/`rescue`/`always`:

```yaml
block:
  - name: Do the work
    ...
rescue:
  - name: Set failure flag
    ansible.builtin.set_fact:
      backup_failed: true   # or maintenance_failed: true
always:
  - name: Notify Discord
    include_tasks: tasks/notify.yaml
  - name: Log to MariaDB
    include_tasks: tasks/log_mariadb.yaml
```

**Backup/update playbooks**: Discord and DB logging always fire — even on failure. Every run is recorded.

**Maintenance playbooks**: DB logging always fires. Discord fires **only on failure** (maintenance
runs frequently; success is silent). The `maintenance_failed` flag is set in `rescue:` and checked
in `always:` to decide whether to notify.

**`download_videos.yaml`** is intentionally different from other operational playbooks: it sends
per-video success notifications to a dedicated MeTube Discord channel and failure alerts to the
operational channel, but does not log to MariaDB. Video downloads are a utility operation —
DB logging would require a new table or awkward fit into existing tables. Successful download
task history is cleaned up by `maintain_semaphore.yaml` after `download_task_retention_days`.

#### Discord notification field patterns

Every `always:` block passes a standard set of vars to `tasks/notify.yaml` using the
standard operational interface. Embed layout is built automatically from three vars:

- **`discord_name`** — system name from `vars/*.yaml` (e.g. `backup_name`, `maintenance_name`,
  `update_name`). Values: `"Database"`, `"Docker"`, `"unRAID"`, `"Ubuntu"`, etc.
- **`discord_operation`** — operation name set inline in the playbook. Values: `"Backup"`,
  `"Sync"`, `"Verification"`, `"Restore"`, `"Deploy"`, `"Build"`, `"Bootstrap"`,
  `"Restore Test"`, `"Update"`, `"Rollback"`, `"Maintenance"`, `"Health"`.
- **`discord_status`** — `"successful"`, `"failed"`, or `"partial"`.
- **`discord_detail`** *(optional)* — appended to description: `"Successful — radarr-log"`.

The task auto-builds: **title** = `"{name} {operation}"` and **description** =
`"{Status}[ — {detail}]"`. Do not add `discord_title` or `discord_description` for standard
operational notifications — those are reserved for the hardcoded `maintain_health` alert titles
and `download_videos` per-video embeds.

**Do not include "Description" or "Host" fields.** The operation is in the title; the host is
already the embed author (Line 1). Both fields were removed in the standardization pass.

**`discord_fields` entries** are dicts with `name`, `value`, and an optional `inline: true` key.
Discord renders up to 3 inline fields per row — use `inline: true` for short identifier fields
(VM Name, Host IP, instance results) and omit it for longer values. The `tasks/notify.yaml`
comment documents the full dict shape.

**"Source File (date)"** in the table above means the field value is extracted to `YYYY-MM-DD`
via `regex_replace('^.*_(\\d{4}-\\d{2}-\\d{2}).*$', '\\1')` — showing just the backup date
rather than the full filename.

| Category | `discord_url` source | Fields | Fires on |
|----------|---------------------|--------|----------|
| **Backup** (`backup_hosts`) | `backup_url` | Backup Name, Backup Size | Always |
| **Backup — DB** (`backup_databases`) | `backup_url` | Date, Backup Size | Always |
| **Sync** (`backup_offline`) | `backup_url` | Share, Size Transferred | Always |
| **Verify** (`verify_backups`) | `backup_url` | Source File (date), Detail | Always |
| **Restore — DB** (`restore_databases`) | `backup_url` | Source File (date), Tables/Measurements | Always |
| **Restore — Host** (`restore_hosts`) | `backup_url` | Mode, Source File (date), Detail | Always |
| **Restore — App** (`restore_app`) | `backup_url` | Detail | Always |
| **Restore — AMP** (`restore_amp`) | `backup_url` | Per-instance ✅/❌ fields (inline), Detail | Always |
| **Update — OS** (`update_systems` non-Docker) | `backup_url` | Version | Change or failure only |
| **Update — Docker** (`update_systems` Docker) | `backup_url` | Updated | Change or failure only |
| **Rollback** (`rollback_docker`) | `backup_url` | Services, Snapshot Date, Detail | Always |
| **Deploy** (`deploy_stacks`, `deploy_grafana`) | `semaphore_ext_url` | Stacks/Detail | Always |
| **Build** (`build_ubuntu` Play 1) | `semaphore_ext_url` | VM Name (inline), Host IP (inline), Proxmox Node (inline), Detail | Always |
| **Bootstrap** (`build_ubuntu` Play 2) | `semaphore_ext_url` | Detail | Always |
| **Restore Test** (`test_restore`) | `semaphore_ext_url` | Source Host (inline), Test VM (inline), Stacks, Detail | Always |
| **Test Backup Restore** (`test_backup_restore`) | `semaphore_ext_url` | Per-app ✅/❌ inline fields, Detail | Always |
| **Maintenance** (`maintain_*`) | `maintenance_url` | *(none)* | Failure only |
| **Health** (`maintain_health` failure) | `maintenance_url` | *(none)* | Failure only |
| **Health alerts** (`maintain_health` checks) | `maintenance_url` / per-check | Custom per alert type | Per-check logic |
| **Download** (`download_videos`) | `video.url` (per-video) | Custom per video | Always |

**`backup_offline` uses `discord_operation: "Sync"`** (not `"Backup"`) to distinguish offline
NAS syncs from regular appdata backups. Both use `backup_name: "unRAID"`, producing
`"unRAID Backup"` vs `"unRAID Sync"` as the embed title.

**URL variable conventions:**

- `backup_url` — defined in `vars/*.yaml`. Used by backup AND update playbooks (same host web UI).
  Set to `""` for hosts with no web UI (e.g., `ubuntu_os.yaml`).
- `maintenance_url` — defined inline in each maintenance playbook's `vars:` block. Set to the
  host's web UI URL or `""` if none. `maintain_semaphore` and `maintain_health` use
  `semaphore_ext_url` instead (localhost plays linking to Semaphore UI).

When adding a new playbook, follow the operational interface above. Set `discord_name`,
`discord_operation`, `discord_status`, and `discord_color`. Add `discord_detail` when there
is a meaningful per-item identifier (DB name, stack name, source host). Do not add Description
or Host fields.

> **Shared task review:** When modifying playbooks, check whether any inline task blocks are
> duplicated across multiple playbooks. If so, they are a candidate for extraction into `tasks/`.
> The current 26 shared task files cover notifications, logging, assertions, provisioning,
> bootstrapping, Docker management, appdata restore, health checks, per-stack/per-DB backup
> orchestration, and DB engine abstraction. Inline cleanup patterns or host-type detection logic
> may have accumulated in individual playbooks and could be worth consolidating if the same
> pattern appears more than once.

### Roles vs. flat tasks/ structure

Ansible roles bundle tasks, defaults, handlers, templates, and files into a named unit. They are
the right choice when a component needs its own defaults, handlers, templates, or test isolation.

**Current state — tasks are the right fit:**

The project has 26 shared task files and no handlers or templates. The `tasks/` files are thin
glue code (send a Discord embed, run an INSERT, assert a precondition, dump/restore a database).
At this scale, promoting them to roles would add directory structure without gaining any
role-specific features.

**When to add roles:**

- A component needs **handlers** — e.g., a restart-service handler that deduplicates across multiple task calls
- A component needs **role-level templates** — e.g., a config file rendered from a Jinja2 template
- You want **Molecule testing** — Molecule is designed around roles and works most naturally with them
- You want to publish or reuse this automation across multiple separate projects
- The shared task files grow beyond ~5 files and grouping them by domain becomes valuable

The decision should be pragmatic — use roles when they provide concrete benefits, stick with
flat tasks when they don't.

**If roles are needed later:**

1. Create `roles/<name>/tasks/main.yaml` for each shared task file
2. Replace `include_tasks: tasks/<name>.yaml` with `include_role: name: <name>` in each playbook
3. Move any role-specific defaults into `roles/<name>/defaults/main.yaml` (keep `vars/*.yaml` for host config)
4. Update `ansible.cfg` or `roles_path` if roles live outside the standard location

### Coding conventions

**YAML booleans:** Use `true`/`false` (modern YAML standard), never `yes`/`no`. This applies to
`become`, `gather_facts`, `append`, `changed_when`, `failed_when`, `check_mode`, `no_log`,
`recurse`, `remove`, `flat`, `create`, and all other boolean parameters.

**YAML document markers:** All playbooks and task files start with `---` on line 1.

**Play names:** Use imperative verb style — the name describes what the play *does*, not what it
*is*. Examples: "Back up host configurations and appdata" (not "Unified Backup Task"), "Clean up
Semaphore task history" (not "Delete stopped or error tasks from Semaphore database").

**Task names:** Use imperative verbs — "Check disk space" (not "Disk space check"), "Delete old
backup files" (not "Old backup deletion"). Health check block names in `maintain_health.yaml`
follow the pattern "Check X" (e.g., "Check MariaDB health", "Check WAN connectivity").

**Register variable names:** Internal/temporary variables (used only in the next task or two)
use a `_` prefix: `_old_backups`, `_docker_containers`, `_ping_result`. Primary operation
results used across task boundaries (logging, Discord, conditionals) stay unprefixed:
`backup_status`, `file_size`, `unvr_backup`, `docker_update_results`. The `maintain_health.yaml`
playbook uses a `*_raw` suffix convention for shell output variables.

**Header comments:** Every playbook has a comment block after `---` describing: purpose (what
the playbook does), mechanism (how it works), and optionally usage examples and schedule notes.

### Hostname normalization

**One variable, two sources.** Every shared task that writes to the database accepts
`log_hostname` — this is the single hostname parameter for both `log_mariadb.yaml` and
`log_health_check.yaml`. Callers pass one of two values:

| Context | Value passed | Example |
|---|---|---|
| Remote host plays | `"{{ inventory_hostname }}"` | `myhost.home.local` |
| Localhost plays | `"{{ controller_fqdn }}"` | `controller.home.local` |

Discord embed author (Line 1) uses the same sources — `inventory_hostname` for remote hosts,
`discord_author: "{{ controller_fqdn }}"` for localhost plays. The embed author replaced the
old explicit "Host" field; do not re-add a Host field.

**Why `inventory_hostname` instead of `ansible_fqdn`:** `ansible_fqdn` is what the remote host
reports about itself, which varies by OS and configuration. unRAID may return `myhost.local`;
some hosts may return short names or unexpected capitalization. `inventory_hostname` is the
canonical identifier defined in the inventory — it is always the correct FQDN, always lowercase,
and completely under the user's control. The DB and Discord output reflect exactly what is in
the inventory, regardless of what any host reports about itself.

**`controller_fqdn`** is defined in `vars/semaphore_check.yaml` as a Jinja2 expression:
`"{{ semaphore_controller_hostname }}.{{ domain_local }}"`. Both `semaphore_controller_hostname` and
`domain_local` come from the vault. Playbooks that run on `hosts: localhost`
(`maintain_semaphore.yaml`, `maintain_health.yaml` Plays 1/3) use it for both DB logging and
the `discord_author` override because `inventory_hostname` resolves to `localhost` in that
context.

### URL construction

Discord embed URLs (`backup_url`) use the **external domain suffix** from the vault
(`domain_ext`) combined with the short hostname extracted from `inventory_hostname`:

```yaml
backup_url: "https://{{ inventory_hostname.split('.')[0] }}.{{ domain_ext }}"
```

This produces URLs like `https://myhost.example.com` — the short hostname (`myhost`) joined
with the external domain (`example.com`). Do **not** use the full `inventory_hostname` in URL
construction, as that would create invalid double-domain URLs (e.g., `myhost.home.local.example.com`).

Some hosts use fixed URL patterns instead:
- Synology: `https://synology.{{ domain_ext }}` (runs on NAS host, URL is for synology)
- Database: `https://sql.{{ domain_ext }}` (shared URL regardless of host)
- Unifi Network: `https://unifi.ui.com/` (hardcoded external cloud portal)

**Semaphore dual URLs:** Semaphore has two URL variables because the API needs an internal
IP-based URL while Discord notification links need the external domain:

| Variable | Source | Example | Purpose |
|---|---|---|---|
| `semaphore_url` | `semaphore_host_url` from vault (trailing slash stripped) | `http://10.0.0.1:3000` | API calls (`/api/project/...`) |
| `semaphore_ext_url` | Built from `controller_fqdn` + `domain_ext` | `https://controller.example.com` | Discord embed links, `maintenance_url` |

Both are defined in `vars/semaphore_check.yaml`. Only `maintain_health.yaml` uses both — the
API URL for the Semaphore task query and the external URL for Discord task links and the
`maintenance_url` clickable embed.

### PiKVM RW/RO mode

PiKVM boots into read-only mode by default. `update_systems.yaml` and `backup_hosts.yaml` both
temporarily switch it to read-write before doing work, then restore read-only afterward.

| Playbook | Set RW | Restore RO |
|---|---|---|
| `update_systems.yaml` | pre_task: `raw: rw` | always block: `raw: ro` (only if no reboot) |
| `backup_hosts.yaml` | pre_task: `raw: rw` | always block: `raw: ro` |

For updates, `pikvm-update --no-reboot` is used to suppress the device's built-in 30-second
auto-reboot (ref: [pikvm/pikvm#1270](https://github.com/pikvm/pikvm/issues/1270)). Exit codes:
- `0` — nothing was updated; no reboot needed; `raw: ro` restores read-only mode
- `100` — update installed; reboot needed; `ansible.builtin.reboot` fires and device returns in RO

For backups, `raw: ro` in the `always:` block restores read-only mode after the archive is
created and fetched.

### Inventory group checks

Playbooks use inventory group membership for host-type decisions, not hostname string matching:

```yaml
when: inventory_hostname in groups['pbs']    # not: ansible_hostname == 'backup'
when: inventory_hostname in groups['docker_stacks']
```

### Docker and unRAID group hierarchy

`docker` is a **parent group** with two children:
- `docker_stacks` — hosts using Docker Compose (per-stack backup/start/stop orchestration)
- `docker_run` — hosts running containers via `docker run` directly (monolithic backup + Docker API mode)

`unraid` is a **platform identity group** — hosts running the unRAID OS. It is semantically distinct
from Docker groups. Currently `docker_run` and `unraid` contain the same hosts, but this is
**coincidental, not structural**. Future hosts may diverge:
- A plain Linux host could join `docker_run` without running unRAID
- A new unRAID host might not run Docker at all

**unRAID platform characteristics:**
- Management is via a custom PHP-based web UI/API; some operations (array, disk, container
  management) go through this PHP layer — Ansible playbooks must use `uri` or shell wrappers
  where a direct Ansible module doesn't exist
- Flash-based boot config at `/boot/config/`; array state at `/var/local/emhttp/`
- Disk assignment data at `/var/local/emhttp/disks.ini`
- Nerd Tools is deprecated — playbooks must never assume any Nerd Tools binary is installed.
  Any required tool must be installed by the playbook itself or be part of the base unRAID image.
  Always assert with `command -v <bin>` before use.

**Rules:**
- `unraid` membership does NOT imply Docker. Never use `unraid` in place of `docker_run`.
- `docker_run` membership does NOT imply unRAID.
- Docker playbooks target `hosts: "docker"` — never append `:unraid` (would duplicate current
  hosts and break future non-Docker unRAID hosts).
- unRAID-specific tasks (disk assignment snapshot, array find-index, boot backup) guard on
  `groups['unraid']`, not `groups['docker_run']`.
- Docker stop/start in backup must guard on `groups['docker_run']`, not just
  `not in groups['docker_stacks']` (too broad — would target future non-Docker unRAID hosts).

### Check mode (dry-run) support

All playbooks support `ansible-playbook --check` for safe dry-run previews. Three annotation
patterns are used:

**`check_mode: false`** — on state-gathering tasks whose registered output is needed by downstream
`when:` or `set_fact` tasks. Without this, the task would be skipped in check mode and downstream
tasks would crash on undefined variables. Examples: version queries via `community.mysql.mysql_query`,
disk usage checks, DB connectivity probes, `df` in assertion tasks.

**`when: not ansible_check_mode`** — on tasks with real side effects that bypass Ansible's
built-in check mode awareness. The `community.general.discord` module, `raw` module, `uri`
POST calls, and `community.general.wakeonlan` all execute regardless of `--check`. This guard
explicitly skips them. Also applied to DB write operations (`log_mariadb.yaml`,
`log_health_check.yaml`) to suppress INSERTs during dry runs. Applied inside the shared task
files so all callers are automatically protected.

**No annotation needed** — for tasks using modules with native check mode support
(`ansible.builtin.apt`, `ansible.builtin.file`, `ansible.builtin.copy`,
`community.docker.docker_compose_v2`, `community.docker.docker_prune`). These modules
report "would have changed" in check mode without taking action.

#### How to run a dry run in Semaphore

When launching a task manually in Semaphore, enter `--check` in the **CLI Args** field. Semaphore
passes it directly to `ansible-playbook`. All state-gathering tasks still execute, but no changes
are made, no Discord notifications fire, and no DB logging happens.

For recurring dry runs, create a duplicate template with `--check` set permanently in CLI Args
and name it with a `[Dry Run]` suffix (e.g., `Maintain — Health [Dry Run]`).

#### Playbook-specific behavior in check mode

| Playbook | Check mode behavior |
|---|---|
| `maintain_health.yaml` | All 26 health checks run and evaluate. Discord alerts and DB logging suppressed. State timestamp not updated in DB. |
| `update_systems.yaml` | Cluster quorum pre-check runs. Version queries run. Actual upgrade simulated. PiKVM RW/RO skipped. |
| `backup_hosts.yaml` | Disk space pre-checks run. Docker container list gathered. Archive/fetch simulated. UNVR API call skipped. |
| `backup_databases.yaml` | Disk space and DB connectivity pre-checks run. Dump simulated (shell skipped). |
| `backup_offline.yaml` | Ping check runs. WoL, Synology shutdown and verification skipped. Rsync simulated. |
| `maintain_amp.yaml` | AMP version list gathered. File deletions simulated. |
| `setup_test_network.yaml` | All GETs and prereq assertions run. POSTs skipped. Prints what would be created and the derived Semaphore IP + prod LAN CIDR. |
| `maintain_cache.yaml` | Cache drop simulated. |
| `maintain_docker.yaml` | Docker prune simulated. |
| `maintain_semaphore.yaml` | DB cleanup and retention pruning simulated. |
| `maintain_unifi.yaml` | Service restart simulated. |
| `download_videos.yaml` | Config deploy simulated. yt-dlp execution skipped. Manifest read runs (empty). Temp file discovery runs; deletions simulated. |
| `verify_backups.yaml` | Backup file search runs. Integrity checks run. No temp databases created. No archives extracted. Discord/DB suppressed. |
| `restore_databases.yaml` | Backup file search runs. Safety backup skipped. No restore performed. No container management. Discord/DB suppressed. |
| `restore_hosts.yaml` | Backup file search runs. Archive integrity verified. No extraction or container management. Discord/DB suppressed. |
| `restore_app.yaml` | Backup file search runs. Safety gate skipped. No stack stop/start. No DB or appdata restore. Discord/DB suppressed. |
| `restore_amp.yaml` | AMP archive discovery runs. Safety gate skipped. No instance stop/start or data replacement. Discord/DB suppressed. |
| `rollback_docker.yaml` | Snapshot read and parsed. Safety gate skipped. No image re-tag/pull or container recreation. Discord/DB suppressed. |
| `maintain_pve.yaml` | keepalived + ansible user + SSH config tasks simulated. VIP reachability check skipped. Snapshot API queries run (read-only GET). PBS task list query runs (read-only shell, `check_mode: false`). Discord/DB suppressed. |
| `maintain_logging_db.yaml` | DB purge queries simulated. Discord/DB suppressed. |
| `deploy_grafana.yaml` | Dashboard JSON read and parsed. All API calls skipped (datasource check, create, dashboard import). Discord/DB suppressed. |
| `test_backup_restore.yaml` | VM provisioned. Stacks deployed. No app restores performed (restore and health check steps skipped). Discord/DB suppressed. |
| `build_ubuntu.yaml` | VM state assertion and cluster resource query run. VM clone/configure/destroy simulated (proxmox_kvm). URI PUT resize and VLAN config skipped. wait_for SSH skipped. Snapshot POST and rollback POST skipped. Discord/DB suppressed. |
| `setup_pve_vip.yaml` | keepalived install and config tasks simulated. VRRP election pause and VIP reachability check skipped. |

### Pre-task validations

Production playbooks include pre-task assertions to catch environmental problems early, before
any work starts. Two shared assertion task files are available:

**`tasks/assert_disk_space.yaml`** — Checks free space on a filesystem path. Caller passes
`assert_disk_path` and `assert_disk_min_gb` via `vars:`. Used by `backup_hosts.yaml` (remote
`backup_tmp_dir` + controller `/backup`), `backup_databases.yaml` (remote `backup_tmp_dir`),
`update_systems.yaml` (root filesystem `/`), `restore_databases.yaml` (`backup_tmp_dir`), and
`restore_hosts.yaml` (`backup_tmp_dir` for staging, `/` for inplace).

**`tasks/assert_db_connectivity.yaml`** — Verifies the MariaDB logging database is reachable
via `SELECT 1`. Used by all 18 operational playbooks that call `tasks/log_mariadb.yaml` or
`tasks/log_restore.yaml` in their `always:` block (every playbook except `download_videos.yaml`
and `setup_ansible_user.yaml`). Catches MariaDB outages early — before any backup, update, or
maintenance work starts — rather than failing silently in the `always:` logging step.

**`tasks/assert_config_file.yaml`** — Asserts `config_file` is defined and non-empty, catching
misconfigured Semaphore variable groups before work starts. Used by `backup_hosts.yaml`,
`backup_databases.yaml`, `update_systems.yaml`, `backup_offline.yaml`, `download_videos.yaml`,
`verify_backups.yaml`, `restore_databases.yaml`, and `restore_hosts.yaml`.

The `assert_disk_space` and `assert_db_connectivity` tasks have `check_mode: false` on their
shell/query pre-steps so they validate during `--check`. `assert_config_file` uses
`ansible.builtin.assert` which runs natively in check mode.

### `no_log` policy

Apply the debug-aware expression to any task that would echo a password, token, or key:

```yaml
no_log: "{{ not (debug_no_log | default(false) | bool) and (ansible_verbosity | default(0) | int < 3) }}"
```

This masks output by default and reveals it when explicitly requested (see [Debug nolog toggle](#debug-nolog-toggle) below).

- Vault secrets via `cipassword`, `api_token_secret`, `login_password`: use the expression above
- Tasks rendering `.env` files via `ansible.builtin.template`: use the expression above
- DB/influxdb dump+restore tasks have an extended conditional that also accounts for the influxdb
  branch: `not (_db_is_influxdb | default(false) | bool) and not (debug_no_log...) and (verbosity...)`
- Version queries, disk checks, or any task that reads but never outputs a secret: no annotation needed
- DB `community.mysql.mysql_query` tasks used for logging (no credentials in query): no annotation needed;
  tasks that pass `login_password` use the expression above

### Debug nolog toggle

To reveal masked task output on a specific run, pass `debug_no_log=yes` as an extra var:

```
-e debug_no_log=yes
```

Alternatively, pass `-vvv`; verbosity ≥ 3 automatically disables `no_log` across all tasks.
Both mechanisms are OR-combined — either alone is sufficient to reveal output.

When a `no_log` task fails in Semaphore, the output is censored and shows only:
`the output has been hidden due to the fact that 'no_log: true' was specified`. To get useful
context without re-running, wrap the sensitive task in a `block/rescue` that re-fails with a hint:

```yaml
- block:
    - name: Sensitive task
      some_module: ...
      no_log: "{{ not (debug_no_log | default(false) | bool) ... }}"
  rescue:
    - ansible.builtin.fail:
        msg: "Sensitive task failed. Re-run with -e debug_no_log=yes for details."
```

This pattern is applied to all tasks files and key playbook sections. The hint appears even when
output is masked, directing the operator to the correct debugging step.

### `validate_certs` policy

- **External endpoints** (Discord, Docker Hub, GitHub, cloud portals): always validate (`validate_certs: true`,
  or omit — `true` is the default)
- **Internal endpoints with valid TLS** (Grafana, Home Assistant, Uptime Kuma via reverse proxy): validate
- **Internal endpoints with self-signed certs** (Proxmox API, Semaphore on LAN): `validate_certs: false`
  is acceptable; note the reason in a comment (e.g., `# Proxmox self-signed cert`)
- **Vendor API limitation** (UNVR): `validate_certs: false` — vendor API incompatibility prevents
  cert management (see Blocked items in future/TODO.md)

### `host_vars` vs `group_vars` rules

- **`group_vars/all.yaml`** — defaults that apply everywhere: shared Ansible settings, backup path
  patterns, Discord colors. Also provides `backup_type`/`update_type` (default `"Servers"`),
  `backup_tmp_dir` (default `"/backup"`), and DB engine flags (`is_postgres`/`is_mariadb`/`is_influxdb`,
  all default `false`). Override these in `vars/*.yaml` only when needed.
- **`group_vars/<group>.yaml`** — per-group overrides for a specific inventory group (e.g.,
  `group_vars/tantiveiv.yaml` for `docker_mem_limit`; `group_vars/pikvm.yaml` for `ansible_remote_tmp`)
- **`vars/*.yaml`** — per-platform configs loaded explicitly via `vars_files:` in playbooks; use for
  operational variables (backup paths, task names, feature flags) that vary by platform
- **`host_vars/<hostname>.yaml`** — reserved for truly host-specific overrides that don't fit a group
  pattern; currently unused (prefer group_vars for host groups)

### Image pinning convention

Docker service images generally use `latest`. Exceptions:

- **Images without a `latest` tag** (e.g., Authentik — always use versioned tags): must be pinned
  via a variable in `vars/docker_stacks.yaml` (e.g., `docker_authentik_tag: "2025.12.4"`)
- **DB images that need version stability** (e.g., MariaDB, PostgreSQL) may also be pinned

Pinned versions are intentional. Update them manually by editing `vars/docker_stacks.yaml`.
Do not auto-update pinned images — they require testing before a version bump.

### Triple alerting: Discord push + Grafana pull + Uptime Kuma dead man's switch

Stale backups and failed maintenance are monitored by three independent systems:

1. **Discord (push):** `maintain_health.yaml` checks the DB for stale backups (>216h) and failed
   maintenance runs since the last health check, then sends Discord alerts immediately.
2. **Grafana (pull):** The dashboard has "Stale Backups (9+ Days)" and "Stale Updates (14+ Days)"
   panels that query the same tables with aligned thresholds (216h = 9 days for backups).
3. **Uptime Kuma (dead man's switch):** `maintain_health.yaml` sends a push heartbeat to Uptime
   Kuma at the end of every successful run. If the playbook crashes, hangs, or the scheduler dies,
   Uptime Kuma detects the missing heartbeat and alerts independently of both Discord and Grafana.

This is intentional redundancy — Discord is push-based (immediate alerting even if nobody is
looking at the dashboard), Grafana is pull-based (visual overview with drill-down), and Uptime
Kuma catches total-failure scenarios where the health playbook itself never completes. All three
are independent — do **not** consolidate them. Losing any notification path reduces observability.

**Uptime Kuma setup:** Create a **Push** monitor in Uptime Kuma, set the heartbeat interval to
match the `maintain_health.yaml` schedule (e.g., every 1 hour), and copy the push URL into the
vault as `uptime_kuma_push_url`. The heartbeat is a simple HTTP GET — it fires only when the
playbook completes without failure (`not maintenance_failed`) and is silently skipped in check
mode or when the variable is not defined. See `vars/secrets.yaml.example` for the key format.

### Data retention

`maintain_semaphore.yaml` handles data retention for both the Semaphore `task` table and the
`ansible_logging` database. It performs three cleanup operations:
1. Delete `stopped`/`error` tasks from Semaphore's `task` table
2. Delete successful `Download *` tasks older than `download_task_retention_days` (default: 7)
   from the `task` table — prevents download history from clogging the Semaphore UI
3. Prune rows older than `retention_days` (default: 365) from `health_checks`, `maintenance`,
   `backups`, and `restores` tables

The `updates` table is intentionally excluded from retention — it stores one row per distinct
version via `INSERT ... ON DUPLICATE KEY UPDATE`, making it a sparse version history rather than a run log. Rows
accumulate slowly and are all valuable for long-term version tracking.

The `health_checks` table is the most aggressive grower (~26 checks x ~10 hosts per run). At
hourly health checks, that's ~1.9M rows/year. Annual pruning keeps the `INNER JOIN ... MAX(timestamp)`
queries in Grafana performant.

### Health check state management

`maintain_health.yaml` tracks the last successful health check timestamp in the
`health_check_state` table (single-row, `id=1`). This timestamp determines which Semaphore task
failures and maintenance failures are "new" since the last run.

The state was previously stored in `/tmp/health_check_state.json` on the Semaphore container,
which was lost on container restart (causing re-alerts or missed failures). Moving it to MariaDB
makes it persistent across container lifecycle events.

The state is read at the start of Play 1 and written at the end of Play 3 via
`INSERT ... ON DUPLICATE KEY UPDATE`. If the row doesn't exist (first run), it defaults to
1 hour ago. The `when: not ansible_check_mode` guard prevents state updates during dry runs.

### Version detection pattern (`update_systems.yaml`)

Version commands for each host type live in two play-level dicts — one for OS versions (`[os]` tag),
one for software versions (`[software]` tag). Each dict key matches an inventory group name:

```yaml
vars:
  _os_version_commands:
    pikvm:  "pacman -Q | grep 'kvmd ' | cut -c 6-"
    ubuntu: "uname -r | sed 's/-generic//'"
    pve:    "pveversion | awk -F'/' '{print $2}'"
    pbs:    "proxmox-backup-manager version | grep 'proxmox-backup-server' | awk '{print $2}'"
  _sw_version_commands:
    amp:    "ampinstmgr -version | sed -n 's/.*\\(v[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\).*/\\1/p'"
```

Two tasks per dict handle all host types:

```yaml
- name: Get current version
  ansible.builtin.shell: "{{ _os_version_commands[group_names | select('in', _os_version_commands) | first] }}"
  register: _version_raw_os
  changed_when: false
  when: group_names | select('in', _os_version_commands) | list | length > 0
  tags: [os]

- name: Set current_version
  ansible.builtin.set_fact:
    current_version: "{{ _version_raw_os.stdout | trim }}"
  when: group_names | select('in', _os_version_commands) | list | length > 0
  tags: [os]
```

`group_names | select('in', _os_version_commands)` returns the intersection of the current host's
groups and the dict keys. A host not in any keyed group skips both tasks cleanly. The `when:`
condition never needs updating when a new host type is added.

**To add a new host type:** add one line to the appropriate dict. No `when:` or task changes needed.

- New OS platform → one entry in `_os_version_commands` + one entry in the `Update` vars table above
- New managed application → one entry in `_sw_version_commands` + one entry in the `Update` vars table above

**Docker container updates** use a separate flow (`[docker]` tag) that does not use the version
command dicts. Compose hosts use `docker compose pull` + `docker compose up -d`; unRAID hosts
use the Dynamix Docker Manager's `update_container` PHP script to keep the unRAID UI in sync.
Container exclude lists (`update_exclude_services` / `update_exclude_containers`) are defined in
the vars files. Per-container results are logged individually to the `updates` table (same pattern
as `backup_databases.yaml` per-database logging).

**Version detection** uses a label fallback chain: `org.opencontainers.image.version` →
`org.label-schema.version` → `version` label → image tag (e.g. `latest`). The version string
logged to the DB is `<version> (<12-char image ID>)` — the image ID suffix ensures `INSERT IGNORE`
uniqueness even when the version label stays the same across rebuilds.

**Update delay** — the `update_delay_days` variable (default `0`) delays updates by N days after
an image is published. A `get_push_epoch` shell function (defined in both compose and unRAID shell
tasks) checks the publish date with a three-tier fallback:

1. **Docker Hub API** `tag_last_pushed` — unauthenticated query to
   `hub.docker.com/v2/namespaces/{ns}/repositories/{repo}/tags/{tag}`. Covers Docker Hub images
   and LinuxServer images (`lscr.io/` prefix stripped — LinuxServer publishes to both GHCR and
   Docker Hub). 10-second timeout per request.
2. **Image `.Created` timestamp** — fallback for non-Docker Hub registries (e.g. `ghcr.io`).
   Build time, not push time, but typically within minutes of publish for CI-built images.
3. **Epoch 0** — if both above fail (e.g. BuildKit reproducible builds with `0001-01-01` or
   `1970-01-01` `.Created`), treats the image as old enough and updates immediately.

---

## Database Architecture

### Database: `ansible_logging`

Seven tables (six operational + one state). All columns are set by Ansible — no DB-side triggers,
functions, or computed columns. Run `mysql -u root -p < sql/init.sql` to create the database and
all tables. The init script uses `CREATE TABLE IF NOT EXISTS` so re-running is safe.

#### Timezone convention

**Storage: always UTC.** All `timestamp` columns are written with `UTC_TIMESTAMP()` — never
`NOW()`. The MariaDB server runs in the host's local timezone (CST/UTC-6, `@@global.time_zone`
= `SYSTEM`), so `NOW()` returns CST — **not** UTC. `UTC_TIMESTAMP()` always returns UTC
regardless of the server's timezone, making it the only safe choice. Do not change the server
timezone to UTC — other databases on the same MariaDB instance depend on the current setting.

**Display: viewer-local.** Each display system converts UTC to the viewer's timezone:

| System | Mechanism | Configuration |
|--------|-----------|---------------|
| **Grafana** | Returns raw `DATETIME` columns from SQL; Grafana converts to viewer timezone | Default datasource timezone (UTC) — no explicit configuration needed |
| **Discord** | Embed `timestamp` field accepts ISO 8601 UTC; Discord auto-converts to viewer local | No configuration — `ansible_date_time.iso8601` and `now(utc=true)` already produce UTC |
| **Discord (inline text)** | Stale backup alert uses `CONVERT_TZ` with `display_timezone` variable | `display_timezone` in `vars/semaphore_check.yaml` (default: `America/Chicago`); requires MariaDB timezone tables loaded |

**Grafana table panels** do not use `DATE_FORMAT()` in SQL — timestamps are returned as raw
`DATETIME` values and formatted by Grafana's built-in time column handling. This allows Grafana
to respect the dashboard/user timezone setting. The dashboard `timezone` field is `""` (browser
default), so each viewer sees times in their own timezone.

**Grafana time series panels** use `UNIX_TIMESTAMP()` to produce epoch values — Grafana handles
timezone conversion for axis labels and tooltips via the dashboard timezone setting.

**MariaDB timezone tables** are required for `CONVERT_TZ` with named timezones (e.g.
`'America/Chicago'`). Load them once on the MariaDB container (root password required):
```bash
docker exec mariadb bash -c "mariadb-tzinfo-to-sql /usr/share/zoneinfo | mariadb -u root -p'PASSWORD' mysql"
```
If the password contains `!`, disable bash history expansion first: `set +H` (re-enable with
`set -H`). Without timezone tables, `CONVERT_TZ` with named timezones returns `NULL`.

```sql
CREATE DATABASE IF NOT EXISTS ansible_logging
  CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE ansible_logging;
```

#### `backups` table

```sql
CREATE TABLE backups (
  id INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(255),      -- What was backed up (e.g., 'PVE', 'immich-db', 'Docker')
  hostname VARCHAR(255),          -- FQDN, normalized by Ansible before INSERT
  file_name VARCHAR(255),         -- Backup filename
  file_size DECIMAL(10,2),        -- Size in MB
  timestamp DATETIME,             -- UTC_TIMESTAMP() — always UTC regardless of server timezone
  backup_type VARCHAR(50),        -- Set by vars/*.yaml (e.g., 'Appliances', 'Servers')
  backup_subtype VARCHAR(50),     -- Set by vars/*.yaml (e.g., 'Config', 'Appdata', 'Database')
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_backup_type (backup_type),
  INDEX idx_backup_subtype (backup_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

All backup attempts are logged — both successes and failures. Failed backups use a `FAILED_`
prefix in `file_name` and `file_size` of 0 to distinguish them from successful backups.

#### `updates` table

```sql
CREATE TABLE updates (
  id INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(255),      -- What was updated (e.g., 'Proxmox', 'Ubuntu', 'AMP')
  hostname VARCHAR(255),          -- FQDN, normalized by Ansible before INSERT
  version VARCHAR(100),           -- Version number after update
  timestamp DATETIME,             -- UTC_TIMESTAMP() — always UTC regardless of server timezone
  update_type VARCHAR(50),        -- Set by vars/*.yaml (e.g., 'Appliances', 'Servers')
  update_subtype VARCHAR(50),     -- Set by vars/*.yaml (e.g., 'PVE', 'PBS', 'OS', 'Game Server')
  status VARCHAR(20) NOT NULL DEFAULT 'success',  -- 'success' or 'failed'
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_update_type (update_type),
  INDEX idx_update_subtype (update_subtype),
  UNIQUE INDEX idx_unique_version (application, hostname, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

The `UNIQUE INDEX` on `(application, hostname, version)` combined with
`INSERT ... ON DUPLICATE KEY UPDATE` refreshes the timestamp on re-runs while ensuring each
distinct version is stored once — the DB is a clean version history, not a run log. Failed updates are logged with `version = 'FAILED'` and
`status = 'failed'`. The explicit `status` column enables clean querying (e.g., "last successful
update per host") without relying on magic string detection in the version column.

#### `maintenance` table

```sql
CREATE TABLE maintenance (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(100) NOT NULL,   -- System maintained (e.g., 'AMP', 'Docker', 'Semaphore')
  hostname    VARCHAR(255) NOT NULL,   -- FQDN of host that ran the task
  type        VARCHAR(50)  NOT NULL,   -- 'Servers', 'Appliances', or 'Local'
  subtype     VARCHAR(50)  NOT NULL,   -- 'Cleanup', 'Prune', 'Cache', 'Restart', 'Maintenance', 'Health Check', 'Verify', 'Deploy', 'Build', 'Test Restore', 'Test Backup Restore'
  status      VARCHAR(20)  NOT NULL DEFAULT 'success',  -- 'success', 'failed', or 'partial'
  timestamp   DATETIME,            -- UTC_TIMESTAMP() — always UTC regardless of server timezone
  INDEX idx_application (application),
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp)
);
```

Every maintenance playbook logs one row per host per run. Status is always written — even failures
are recorded so the health check can detect silent breakage. The health check playbook uses a
three-value status: `success` (all checks passed), `partial` (some per-host checks encountered
`check_error` but Play 1 DB/API checks succeeded), or `failed` (a Play 1 check errored out).

#### `health_checks` table

One row per check per host per run. All results (Play 1 localhost checks, unreachable hosts, and
per-host SSH checks) are collected into a unified list and inserted via a single multi-row
`INSERT` statement (`tasks/log_health_checks_batch.yaml`) for efficiency. Enables Grafana trend
graphs (disk usage growth, memory pressure history, recurring journal errors, OOM patterns over time).

```sql
CREATE TABLE health_checks (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  hostname     VARCHAR(255) NOT NULL,
  check_name   VARCHAR(100) NOT NULL,   -- 'disk_space', 'memory', 'cpu_load', 'journal_errors',
                                        --  'oom_kills', 'docker_health', 'smart_health',
                                        --  'pve_cluster', 'ceph_health', 'ssl_cert',
                                        --  'stale_backup', 'failed_maintenance', 'semaphore_tasks',
                                        --  'backup_size_anomaly', 'stale_maintenance',
                                        --  'mariadb_health', 'wan_connectivity',
                                        --  'appliance_reachable', 'ntp_sync', 'dns_resolution',
                                        --  'unraid_array', 'pbs_datastore', 'zfs_pool',
                                        --  'btrfs_health', 'docker_http', 'host_reachable'
  check_status VARCHAR(20)  NOT NULL,   -- 'ok', 'warning', 'critical'
  check_value  VARCHAR(255),            -- e.g. '89%', 'load: 3.2 / 4 vcpus', '2 kills'
  check_detail TEXT,                    -- e.g. '/var at 89% | /home at 72%'
  timestamp    DATETIME,            -- UTC_TIMESTAMP() — always UTC regardless of server timezone
  INDEX idx_hostname     (hostname),
  INDEX idx_check_name   (check_name),
  INDEX idx_check_status (check_status),
  INDEX idx_timestamp    (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

`localhost` checks (Semaphore tasks, stale backups, failed maintenance) are logged with
`hostname = controller_fqdn`. SSH-host checks (disk, memory, CPU,
journal, OOM, Docker) are logged with the actual remote `inventory_hostname`.

#### `health_check_state` table

```sql
CREATE TABLE health_check_state (
  id          INT PRIMARY KEY DEFAULT 1,
  last_check  DATETIME NOT NULL,
  CONSTRAINT single_row CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

Single-row table storing the timestamp of the last health check run. Used by `maintain_health.yaml`
to determine which Semaphore task failures and maintenance failures are "new" since the last run.
Updated via `INSERT ... ON DUPLICATE KEY UPDATE` at the end of each successful health check.
Replaces the previous `/tmp/health_check_state.json` file, which was lost on container restart.

#### `restores` table

```sql
CREATE TABLE restores (
  id INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(255) NOT NULL,
  hostname VARCHAR(255) NOT NULL,
  source_file VARCHAR(255),
  restore_type VARCHAR(50) NOT NULL,
  restore_subtype VARCHAR(50) NOT NULL,
  operation VARCHAR(20) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'success',
  detail TEXT,
  timestamp DATETIME,
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_operation (operation)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

Logs restore operations (`operation: restore`). Verification operations log to the `maintenance`
table (subtype `Verify`) instead. `source_file` tracks which backup file was used. `detail`
holds free-text context (e.g., "sonarr restored inplace + sonarr-log, sonarr-main DB(s) restored").
Written by `tasks/log_restore.yaml`, separate from `log_mariadb.yaml` due to the different schema.

#### `docker_sizes` table

```sql
CREATE TABLE docker_sizes (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  hostname      VARCHAR(255) NOT NULL,
  timestamp     DATETIME     NOT NULL,
  images_count  INT,
  images_mb     DECIMAL(10,2),
  volumes_count INT,
  volumes_mb    DECIMAL(10,2),
  containers_mb DECIMAL(10,2),
  INDEX idx_hostname  (hostname),
  INDEX idx_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

Written by `maintain_docker.yaml` after each Docker prune run. Captures Docker storage usage
per host — images, volumes, and containers — enabling Grafana trend panels to detect image
accumulation and measure the effectiveness of each prune cycle.

#### `playbook_runs` table

```sql
CREATE TABLE playbook_runs (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  playbook    VARCHAR(255) NOT NULL,
  hostname    VARCHAR(255) NOT NULL,
  run_vars    TEXT,
  timestamp   DATETIME     NOT NULL,
  INDEX idx_playbook  (playbook),
  INDEX idx_hostname  (hostname),
  INDEX idx_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

Written by `tasks/log_run_context.yaml`, called in `pre_tasks` of every scheduled playbook
(24 total) after `assert_db_connectivity` and after any `confirm=yes` safety gate. One row per
host per invocation — remote-host plays log `inventory_hostname`; localhost-only plays log
`controller_fqdn`. `run_vars` is a JSON string of non-sensitive extra vars (e.g.
`{"config_file":"docker_stacks"}`) or `{}` for routine playbooks. Pruned by `maintain_semaphore.yaml`
on the shared `retention_days` schedule (default 365 days).

### Naming scheme

Every DB row uses four columns to describe what happened. Their meaning and naming conventions
are fixed — new hosts and platforms must follow the same pattern.

#### Product name branding

Use official product branding in task names, `backup_name`/`update_name`/`maintenance_name`
values, documentation, and Discord notifications:

| Product | Correct | Incorrect |
|---------|---------|-----------|
| unRAID | `unRAID` | `Unraid`, `UnRAID`, `UNRAID` |
| Unifi | `Unifi` | `UniFi`, `UNIFI` |
| PiKVM | `PiKVM` | `Pikvm`, `piKVM` |

Lowercase forms (`unraid`, `unifi`) are used in inventory groups, variable names, and file
names — these follow Ansible/code convention, not branding.

#### `application` — the "what"

The name of the software or system that was backed up or updated. Title-case proper nouns.
Never a hostname. Comes from `backup_name` (backups) or `update_name` (updates) in the vars file.

Examples: `PVE`, `PBS`, `PiKVM`, `Unifi Network`, `Unifi Protect`, `AMP`, `Docker`, `unRAID`,
`Ubuntu`, `Proxmox`. Database backups use the individual DB name with a `-db` suffix
(e.g., `immich-db`, `nextcloud-db`) — the suffix is appended by `backup_databases.yaml`.

#### `type` — the "category of system"

Three values:

- **Appliances** — purpose-built network/infrastructure gear with its own OS you don't manage:
  Proxmox nodes, PiKVM, Unifi Network, Unifi Protect
- **Servers** — general-purpose hosts running user-managed software:
  Docker hosts, game servers, Ubuntu VMs, unRAID, databases
- **Local** — Ansible controller localhost operations: Semaphore cleanup, health monitoring

#### `subtype` — the "specific kind"

Narrows the category. Current values:

- Backups: `Config`, `Appdata`, `Database`, `Offline`
- Updates: `PVE`, `PBS`, `OS`, `Game Server`, `KVM`, `Container`
- Maintenance: `Cleanup`, `Prune`, `Cache`, `Restart`, `Maintenance`, `Health Check`, `Verify`, `Deploy`, `Build`, `Test Restore`, `Test Backup Restore`

  (`Health Check` is reserved for `maintain_health.yaml` — its subtype value regardless of how many
  checks are added to that playbook)

#### `hostname` — inventory FQDN

Always the fully qualified domain name from `inventory_hostname` (or `controller_fqdn` for
localhost plays) — exactly as defined in the Ansible inventory. Passed to shared tasks via the
unified `log_hostname` parameter. No transformation is applied. The inventory is the single
source of truth for how hostnames appear in the DB and in Discord.

#### The Proxmox split — and why it exists

Proxmox is the only platform where `application` means different things for backups vs. updates:

| table | `application` | `subtype` | reason |
|-------|--------------|-----------|--------|
| backups | `PVE` or `PBS` | `Config` | Two separate systems; the *what* distinguishes them |
| updates | `Proxmox` | `PVE` or `PBS` | One software family; the *kind* distinguishes them |

For backups you want to see which Proxmox system produced the file; for updates you want to
group all Proxmox software history together and use subtype to drill down.

This is why **`backup_name` and `update_name` are separate variables in `vars/proxmox.yaml`**,
and why `update_systems.yaml` must use `update_name` (not `backup_name`) for DB operations.

#### Variable names → DB columns

| Ansible var | DB column | Set in |
|---|---|---|
| `log_hostname` | `*.hostname` (all tables) | `include_tasks vars:` — always `inventory_hostname` or `controller_fqdn` |
| `backup_name` | `backups.application` | `vars/*.yaml` |
| `update_name` | `updates.application` | `vars/*.yaml` |
| `backup_type` | `backups.backup_type` | `group_vars/all.yaml` (default `"Servers"`); override in `vars/*.yaml` for Appliances |
| `backup_subtype` | `backups.backup_subtype` | `vars/*.yaml` |
| `update_type` | `updates.update_type` | `group_vars/all.yaml` (default `"Servers"`); override in `vars/*.yaml` for Appliances |
| `update_subtype` | `updates.update_subtype` | `vars/*.yaml` |
| `maintenance_name` | `maintenance.application` | inline `vars:` in playbook |
| `maintenance_type` | `maintenance.type` | inline `vars:` in playbook |
| `maintenance_subtype` | `maintenance.subtype` | inline `vars:` in playbook |

`backup_name`, `backup_description`, `backup_subtype`, `backup_file`, `update_name`, `update_description`,
and `update_subtype` must be present in any vars file used with `backup_hosts.yaml` or
`update_systems.yaml`. `backup_type` and `update_type` are inherited from `group_vars/all.yaml`
(`"Servers"`) unless overridden.
Maintenance operation metadata (`maintenance_name`, `_type`, `_subtype`, `_description`) is defined
in each playbook's `vars:` block — these describe the operation, not the deployment. Deployment-specific
maintenance values (e.g., `maintenance_url` when it's a hardcoded URL) go in the platform vars file.

---

### Categorization values

All type/subtype values are declared in `vars/*.yaml` files. To change a category, edit the file.
No SQL changes needed.

**Backups:**

| vars file | application | backup_type | backup_subtype |
|---|---|---|---|
| `vars/proxmox.yaml` | PVE or PBS (Jinja2 group check) | Appliances | Config |
| `vars/pikvm.yaml` | PiKVM | Appliances | Config |
| `vars/unifi_network.yaml` | Unifi Network | Appliances | Config |
| `vars/unifi_protect.yaml` | Unifi Protect | Appliances | Config |
| `vars/amp.yaml` | AMP | Servers | Config |
| `vars/docker_stacks.yaml` | (stack name) | Servers | Appdata |
| `vars/docker_run.yaml` | Docker | Servers | Appdata |
| `vars/unraid_os.yaml` | unRAID | Servers | Config |
| `vars/synology.yaml` | unRAID | Servers | Offline |
| `vars/db_*.yaml` | (individual db name)-db | Servers | Database |

Database vars files also set `backup_ext` (`"sql"` for PostgreSQL/MariaDB, `"tar.gz"` for InfluxDB)
which controls file extensions in find/copy/cleanup paths across all three database playbooks.

**Updates:**

| vars file | application | update_type | update_subtype |
|---|---|---|---|
| `vars/proxmox.yaml` | Proxmox | Appliances | PVE or PBS (Jinja2) |
| `vars/pikvm.yaml` | PiKVM | Appliances | KVM |
| `vars/amp.yaml` | AMP | Servers | Game Server |
| `vars/ubuntu_os.yaml` | Ubuntu | Servers | OS |
| `vars/docker_stacks.yaml` | (container name) | Servers | Container |
| `vars/docker_run.yaml` | (container name) | Servers | Container |

Docker container updates log per-container rows: `application` is the individual service/container
name (e.g., `nginx`, `plex`), not `"Docker"`. This follows the same pattern as `backup_databases.yaml`
where `application` is the individual database name. The `update_exclude_services` (compose) and
`update_exclude_containers` (unRAID) vars control which containers are skipped — Semaphore is
always excluded; unRAID also excludes MariaDB and Ansible (infrastructure containers).

**Maintenance** (vars defined inline in each playbook):

| playbook | application | type | subtype |
|---|---|---|---|
| `maintain_semaphore.yaml` | Semaphore | Local | Cleanup |
| `maintain_docker.yaml` | Docker | Servers | Prune |
| `maintain_cache.yaml` | Ubuntu / unRAID | Servers | Cache |
| `maintain_unifi.yaml` | Unifi | Appliances | Restart |
| `maintain_amp.yaml` | AMP | Servers | Maintenance |
| `maintain_health.yaml` | Semaphore | Local | Health Check |
| `maintain_logging_db.yaml` | Logging DB | Local | Cleanup |
| `maintain_pve.yaml` (Play 1) | Proxmox | Appliances | Maintenance |
| `maintain_pve.yaml` (Play 3) | Proxmox | Appliances | Snapshot Check |
| `maintain_pve.yaml` (Play 4) | PBS | Appliances | Task Check |
| `deploy_stacks.yaml` | Docker | Servers | Deploy |
| `deploy_grafana.yaml` | Grafana | Local | Deploy |
| `build_ubuntu.yaml` | Ubuntu | Servers | Build |
| `test_restore.yaml` | Docker | Servers | Test Restore |
| `test_backup_restore.yaml` | Docker | Servers | Test Backup Restore |

**Restores** (type/subtype reuse the backup vars file values):

| vars file source | application | restore_type | restore_subtype | operation |
|---|---|---|---|---|
| `vars/db_*.yaml` | (db name)-db | Servers | Database | restore |
| `vars/proxmox.yaml` | PVE or PBS | Appliances | Config | restore |
| `vars/docker_stacks.yaml` | (stack name) or (app name) | Servers | Appdata | restore |
| `vars/docker_run.yaml` | Docker or (app name) | Servers | Appdata | restore |
| `vars/unraid_os.yaml` | unRAID | Servers | Config | restore |
| `vars/pikvm.yaml` | PiKVM | Appliances | Config | restore |
| `vars/docker_stacks.yaml` | (service name) | Servers | Container | rollback |
| `vars/docker_stacks.yaml` | (app name) | Servers | Appdata | restore (`restore_app.yaml`) |

**Verification** logs to the `maintenance` table (subtype `Verify`) using the same
`backup_type` / `backup_name` values from the vars file. See [Maintenance](#maintenance).

### Expected hostname format

All hostnames in the inventory should be FQDNs using a consistent internal domain suffix
(matching `domain_local` in the vault). External hosts (e.g., a VPS) use the external
domain (`domain_ext`). Examples:

```
pve-node1.home.local
docker-host.home.local
pikvm.home.local
udmp.home.local
controller.home.local
vps.example.com
```

---

## Vault / Secrets

The vault file (`vars/secrets.yaml`) is AES256-encrypted and committed to Git. Semaphore decrypts
it at runtime using the password stored in Key Store id=30 (`ansible-vault`).

### Current vault contents

```yaml
# --- Required: used by all playbooks ---
discord_webhook_id: "..."
discord_webhook_token: "..."
logging_db_host: "..."
logging_db_port: "..."
logging_db_user: "..."
logging_db_password: "..."
logging_db_name: "..."
domain_local: "..."         # e.g. "home.local" — internal domain suffix
domain_ext: "..."           # e.g. "example.com" — external domain suffix for URLs
semaphore_api_token: "..."
semaphore_host_url: "..."           # internal IP URL, e.g. "http://10.0.0.1:3000"
semaphore_controller_hostname: "..."          # short hostname of Semaphore controller (builds controller_fqdn)
db_host_primary: "..."              # inventory_hostname of primary DB host (restore cross-host delegate_to)
db_host_secondary: "..."            # inventory_hostname of secondary DB host (restore cross-host delegate_to)

# --- Optional: only needed for specific playbooks ---
discord_download_webhook_id: "..."            # MeTube Discord channel — per-video download notifications (download_videos.yaml)
discord_download_webhook_token: "..."         # MeTube Discord channel — per-video download notifications (download_videos.yaml)
unvr_api_key: "..."                 # Unifi Protect UNVR API key (backup_hosts.yaml)
unifi_network_api_key: "..."        # Unifi Network API key for device inventory export (backup_hosts.yaml via vars/unifi_network.yaml)
docker_trusted_proxy_cidrs: "..."   # CIDR(s) of reverse proxy — trusted proxy header (stacks/auth/env.j2, Authentik)
ansible_user_ssh_pubkey: "..."      # SSH public key for ansible user (setup_ansible_user.yaml, maintain_pve.yaml)
synology_ip: "..."                  # Synology NAS IP address (backup_offline.yaml via vars/synology.yaml)
synology_mac: "..."                 # Synology NAS MAC for WOL (backup_offline.yaml via vars/synology.yaml)
synology_name: "..."                # Synology NAS mount name (backup_offline.yaml via vars/synology.yaml)
db_password: "..."                  # Database password for Docker container DB dumps (backup/restore/verify playbooks)
grafana_url: "..."                  # Grafana base URL, e.g. "http://grafana-host:3000" (deploy_grafana.yaml)
grafana_service_account_token: "..." # Grafana service account token with Editor role (deploy_grafana.yaml)
vps_fqdn: "..."                    # VPS hostname for WireGuard config extraction (backup_hosts.yaml via vars/unifi_network.yaml)

# --- PVE cluster (setup_pve_vip.yaml, maintain_pve.yaml, provision_vm.yaml, build_ubuntu.yaml) ---
pve_api_host: "..."              # Floating VIP — updated to vault_pve_vip after setup_pve_vip.yaml runs
pve_api_user: "..."
pve_api_token_id: "..."
pve_api_token_secret: "..."
pve_template_node: "..."         # Short name of the node that holds the cloud-init template
pve_template_vmid: "..."
pve_template_name: "..."
pve_storage: "..."               # Ceph/local storage pool name
pve_bridge: "..."                # VM network bridge (e.g. vmbr0)
pve_cloud_image_url: "..."
vault_pve_vip: "..."             # Floating management VIP (10.x.x.x, unused host in /24)
vault_pve_vrrp_password: "..."
vault_pve_vrrp_priorities: {}    # Dict: short node name → integer priority (highest = MASTER)
vault_test_vm_ip_prefix: "..."   # e.g. "10.10.10." — prefix for test-vm pool
vault_test_vm_ip_offset: "..."   # e.g. 90 — first slot; slots 0–9 = offset .. offset+9
vm_user: "..."
pve_vm_password: "..."
vm_cidr: "..."
vm_gateway: "..."
vm_dns: "..."
vm_search_domain: "..."
vm_template_memory: "..."
vm_template_cores: "..."
# Per-VM IPs and node assignments — one entry per VM in vars/vm_definitions.yaml:
# vault_vm_<name>_ip: "..."      vault_vm_<name>_hostname: "..."    vault_vm_<name>_node: "..."

# --- VPN stack (stacks/vpn/env.j2) ---
vault_wg_internal_subnet: "..."  # WireGuard internal subnet (e.g. 10.x.x.0) — NOT a host IP
```

### What must be in the vault (public repo rules)

This repo is public and Semaphore pulls directly from GitHub. Any value that reveals internal
network topology must live in the vault:

- **All private IPs** — host IPs, gateway IPs, DNS IPs, WireGuard subnets (e.g. `10.x.x.0`), PVE node IPs
- **All internal domain names** — `*.home.local`, `*.internal.lan`, internal search domains
- **Infrastructure node names** — Proxmox node short names (e.g. `homeone`, `defiance`)
- **All credentials** — passwords, tokens, API keys, SSH keys

Plain `vars/*.yaml` files (committed, public) must only contain `{{ vault_... }}` references
for any of the above. Never put a raw IP or internal hostname in a committed non-vault file,
**including in comments** — use TEST-NET examples (`192.0.2.x`) instead.

### Editing the vault

```bash
ansible-vault edit vars/secrets.yaml    # opens decrypted in $EDITOR; re-encrypts on save
ansible-vault view vars/secrets.yaml    # read-only view
```

After editing: `git add vars/secrets.yaml && git commit && git push`, then sync in Semaphore UI
(or the next run will auto-sync).

### Changing the vault password

```bash
ansible-vault rekey vars/secrets.yaml
```

Then update Key Store id=30 in Semaphore UI to match.

---

## Making Common Changes

### Add a new host

1. Add the host to the inventory in Semaphore (each inventory is stored in the Semaphore DB, not in this repo)
2. Create or reuse a `vars/<config>.yaml` with `backup_type`/`backup_subtype` set
3. Create a Semaphore template with the correct inventory and variable group
4. If the host's group should be health-monitored, add the group to `health_check_groups` in `vars/semaphore_check.yaml`
5. No DB changes needed — Ansible sends all column values

### Add a new secret

1. `ansible-vault edit vars/secrets.yaml` — add the new key/value
2. Reference it in the playbook: `{{ new_secret_name }}`
3. Commit, push, sync Semaphore

### Change a category (type/subtype)

Edit the relevant `vars/*.yaml` file. No SQL changes needed. Future runs use the new values.
Existing rows keep old values — update them manually if needed:
```sql
UPDATE backups SET backup_type = 'NewValue' WHERE hostname LIKE 'myhost%';
```

### Change domain suffixes

Edit `vars/secrets.yaml` (`ansible-vault edit vars/secrets.yaml`) and update `domain_local`
and `domain_ext`. Propagates automatically to all future INSERTs and the health check query.
Existing rows are not affected — update them manually if needed.

### Restore a database from backup

```bash
# Restore a single DB (latest backup) — e.g., nextcloud on shared MariaDB
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_mariadb -e confirm=yes -e restore_db=nextcloud

# Restore from a specific date
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_postgres -e confirm=yes -e restore_db=authentik -e restore_date=2026-02-20
```

### Restore an app's appdata + database together

```bash
# Coordinated restore — appdata on one host, DB on another (cross-host via delegate_to)
ansible-playbook restore_hosts.yaml -e hosts_variable=docker_run --limit <hostname> \
  -e restore_app=sonarr -e with_databases=yes -e restore_mode=inplace -e confirm=yes -e with_docker=yes
```

Always use `--limit <hostname>` with `-e restore_app` since `docker_stacks`/`docker_run` are
multi-host groups. For docker_stacks apps, `app_info` in `vars/docker_stacks.yaml` provides the
stack name + DB names (pre-deploy); DB config and health URLs are discovered from container labels
at runtime. For docker_run apps, `app_restore` in `vars/docker_run.yaml` provides the container
list, DB config file, and `db_host` for cross-host `delegate_to`.

### Roll back a Docker container update

```bash
# Dry run — show snapshot info without rolling back
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname>

# Rollback a single stack
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e rollback_stack=vpn -e confirm=yes

# Rollback a single service
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e rollback_service=jellyseerr -e confirm=yes

# Rollback all containers in the snapshot
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e confirm=yes
```

The rollback snapshot (`.rollback_snapshot.json`) is saved automatically before each Docker
Compose update. If the old image is still on disk, rollback is instant (local re-tag). If it
was pruned by `maintain_docker.yaml`, the old version is pulled from the registry.

**For unRAID `docker_run` hosts** (no automated rollback):
1. `docker stop <container>` and `docker rm <container>`
2. Find old image ID from the snapshot or `updates` table
3. `docker tag <old_image_id> <image_name>` (if image still local)
4. Recreate via the unRAID Docker UI (uses the saved template XML)
5. If image was pruned: restore from backup via `restore_hosts.yaml -e hosts_variable=docker_run`

**Database schema changes:** Some container updates run DB migrations on startup. Rolling back
the container alone may leave the DB in an incompatible state. For those cases, combine with
`restore_databases.yaml`.

### Useful DB queries

```sql
-- All backup hosts
SELECT DISTINCT hostname FROM backups ORDER BY hostname;

-- All update hosts
SELECT DISTINCT hostname FROM updates ORDER BY hostname;

-- Recent maintenance runs
SELECT application, hostname, subtype, status, timestamp FROM maintenance
ORDER BY timestamp DESC LIMIT 20;

-- Unexpected hostnames (should be empty — all rows come from inventory_hostname now)
-- Replace %.home.local and %.example.com with your actual domain suffixes
SELECT CONCAT(hostname, ' (', tbl, ')') FROM (
  SELECT hostname, 'backups' AS tbl FROM backups
  WHERE hostname NOT LIKE '%.home.local' AND hostname NOT LIKE '%.example.com'
  UNION
  SELECT hostname, 'updates' FROM updates
  WHERE hostname NOT LIKE '%.home.local' AND hostname NOT LIKE '%.example.com'
  UNION
  SELECT hostname, 'maintenance' FROM maintenance
  WHERE hostname NOT LIKE '%.home.local' AND hostname NOT LIKE '%.example.com'
  UNION
  SELECT hostname, 'restores' FROM restores
  WHERE hostname NOT LIKE '%.home.local' AND hostname NOT LIKE '%.example.com'
) AS bad ORDER BY hostname;

-- Recent backups
SELECT application, hostname, file_name, file_size, timestamp FROM backups
ORDER BY timestamp DESC LIMIT 20;

-- Version history for a host
SELECT application, hostname, version, timestamp FROM updates
WHERE hostname LIKE 'myhost%' ORDER BY timestamp DESC;

-- Failed maintenance runs
SELECT application, hostname, subtype, timestamp FROM maintenance
WHERE status = 'failed' ORDER BY timestamp DESC;

-- Recent health check results (latest run)
SELECT hostname, check_name, check_status, check_value FROM health_checks
ORDER BY timestamp DESC LIMIT 30;

-- Hosts with non-ok health (latest run per host/check)
SELECT hostname, check_name, check_status, check_value, check_detail, timestamp
FROM health_checks WHERE check_status != 'ok'
ORDER BY timestamp DESC LIMIT 20;

-- Disk usage trend for a host
SELECT hostname, check_value, timestamp FROM health_checks
WHERE check_name = 'disk_space' AND hostname LIKE 'myhost%'
ORDER BY timestamp DESC LIMIT 30;
```

### Grafana dashboard

`grafana/grafana.json` is a Grafana dashboard that visualizes the data written by the Ansible
playbooks. Deploy it using `deploy_grafana.yaml`, which provisions both the datasource and
dashboard via the Grafana API. For manual import: **Dashboards → Import → Upload JSON file**.

**Automated deploy:** `deploy_grafana.yaml` runs from localhost (no SSH). It creates the
`Ansible-Logging` MySQL datasource if missing (using `logging_db_*` vault credentials), then
imports the dashboard JSON with `overwrite: true`. Requires `grafana_url` and
`grafana_service_account_token` in vault. See Semaphore template `Deploy — Grafana [Dashboard]`.

**Datasource required:** a MySQL datasource named exactly `Ansible-Logging`, pointed at the
`ansible_logging` database. All panel queries reference this datasource by name — the name must
match exactly or every panel will show "datasource not found". The default datasource timezone
(UTC) correctly interprets stored UTC timestamps and converts them to the viewer's local
timezone — no explicit timezone configuration is needed on the datasource.

**Template variables:** The dashboard includes three template variables at the top:
- `DS_MYSQL` — datasource selector (defaults to `Ansible-Logging`)
- `hostname` — multi-select host filter (populated from all tables via UNION query)
- `backup_type` — multi-select backup type filter (populated from `backups` table)

All panel queries use `WHERE hostname IN ($hostname)` conditions so filtering applies across the
entire dashboard. When "All" is selected, Grafana expands `$hostname` to all values automatically.

**Panel layout (5 collapsible row groups, 23 content panels):**

| Row | Default | Panels |
|-----|---------|--------|
| **Alerts** | Expanded | Stale Backups (9+ Days), Stale Updates (14+ Days), Current Non-OK Health Checks (with check_detail) |
| **Trends** | Expanded | Backups/Updates/Maintenance Over Time, Backup Size Trend by Application, Health Issues Over Time, Docker Storage Trend (images MB + volumes MB from `docker_sizes`) |
| **Distributions** | Collapsed | 5 bar charts: backup subtype, update type, maintenance subtype, backups by host, updates by application |
| **Recent Activity** | Collapsed | Last Successful Backup per Host, Last Successful Update per Host, Recent Backups/Updates/Maintenance |
| **Status** | Collapsed | Current Version Status, Latest Health Status per Host, Recent Health Check Results |

Key panel features:
- **Stale Backups** uses threshold aligned with `health_backup_stale_days` (default 10 days)
- **Backup Size Trend** shows `AVG(file_size)` per application over 90 days (excludes FAILED_ entries)
- **Non-OK Health Checks** includes `check_detail` column for actionable context
- **Maintenance status** maps three colors: green=success, yellow=partial, red=failed
- **Last Successful** tables show `days_ago` with color thresholds: green <7d, yellow 7-14d, red >14d
- **Recent Updates** includes the `status` column (success/failed)
- **Time series panels** use `spanNulls: true` — lines connect across days with no data instead
  of breaking, producing continuous trend lines even with irregular schedules (e.g., weekly backups)
- **Threshold syncing:** `deploy_grafana.yaml` automatically syncs Ansible thresholds into the
  dashboard JSON before importing. The Stale Backups panel thresholds and SQL query use
  `health_backup_stale_days`, and the Stale Updates SQL uses `health_update_stale_days` —
  both from `vars/semaphore_check.yaml`. Change the Ansible variable, re-deploy, and the
  Grafana panels update automatically. The raw `grafana/grafana.json` file keeps the default
  values (216 hours, 14 days) as a baseline — the playbook replaces them at deploy time

---

## Security Hardening

### Credential protection

- **`no_log` policy**: Every task that handles credentials — database passwords, API tokens,
  SSH keys, HTTP Bearer tokens, or webhook secrets — uses the debug-aware `no_log` expression
  (see [no_log policy](#no_log-policy)) to prevent exposure in Ansible logs and verbose output.
  This covers `docker exec -e` commands with `MYSQL_PWD`, `ansible.builtin.uri` calls with
  Bearer/API-key headers, `community.mysql.mysql_query` tasks with `login_password`, and Discord
  webhook notifications. Pass `-e debug_no_log=yes` or `-vvv` to reveal output for a specific run.
- **mysqldump password**: Uses `MYSQL_PWD` environment variable via `docker exec -e` instead of
  `--password=` on the command line. The env var approach avoids exposing the password in
  `/proc/<pid>/cmdline` (visible to `ps aux` on the host).
- **SSH public key** (`setup_ansible_user.yaml`): The key is stored in a vault variable
  (`ansible_user_ssh_pubkey`), not hardcoded in the playbook. The Ubuntu play uses `ansible.builtin.authorized_key`
  module which automatically manages `.ssh` directory permissions (0700) and `authorized_keys` file
  permissions (0600). The unRAID play uses `copy: content:` to write the key to boot config, since
  it persists through reboots via `/boot/config/`. The unRAID play also creates the
  `~/.ansible/tmp` directory (required by `ansible_remote_tmp` in `group_vars/all.yaml`) both live
  and in the `/boot/config/go` persistence block. Validation assertions at the end verify all
  critical paths exist with correct ownership, the go script block is present, and sshd config
  includes the ansible user.
- **SQL parameterization** (`maintain_semaphore.yaml`): Retention pruning queries use `%s`
  placeholder with `positional_args` instead of string-interpolating `{{ retention_days }}` directly
  into the DELETE statement. This prevents SQL injection if the variable were ever modified.

### File permissions

- **Backup source directory** (`backup_offline.yaml`): Uses `mode: '0770'` (owner rwx, group
  rwx, other none). Group write is required because the Semaphore container process accesses
  the backup storage via group membership, not as the owner. Using 0750 would strip group write
  on every offline backup run and break all fetch-based backups.
- **UNVR temp backup file** (`backup_hosts.yaml`): The downloaded `.unf` file gets `mode: "0600"`
  and is cleaned up in the `always:` block after backup completes.
- **ansible_remote_tmp** (`group_vars/all.yaml`): Set to `~/.ansible/tmp` (user-private) instead
  of the default `/tmp`. Ansible creates temporary module files in this directory during execution
  — using `/tmp` could leak module arguments to other users on multi-user systems.
- **Awk variable injection** (`maintain_health.yaml`): Disk usage threshold is passed via `awk -v`
  flag (variable name `warn_pct`) instead of Jinja2 interpolation inside the awk script body.
  Prevents shell injection if the threshold variable contained special characters. The variable
  must not be named `warn` — Ansible's shell/command module parses `key=value` pairs from the
  command string, and `warn` collides with a removed module parameter (ansible-core 2.18+).

### Restore safety guards

- **`confirm=yes` gate** (all restore + rollback playbooks): Destructive operations require explicit
  `-e confirm=yes` on the command line. Without it, the pre-task assertion fails with a guidance
  message. Prevents accidental data overwrites. Applies to: `restore_databases.yaml`,
  `restore_hosts.yaml`, `restore_app.yaml`, `restore_amp.yaml`, `rollback_docker.yaml`.
- **Pre-restore safety backup** (`restore_databases.yaml`): Before restoring a database, the current
  state is dumped to `<backup_tmp_dir>/pre_restore_<db>_<date>.sql` as a safety net. Controlled by
  `pre_backup` (defaults to `yes`).

### Backup integrity verification

- **Gzip archives** (`backup_hosts.yaml`): `gunzip -t` validates archive integrity after creation,
  before the file is fetched to the controller. Corrupted archives trigger the rescue block.
  UNVR `.unf` files (not gzipped) are skipped.
- **PostgreSQL dumps** (`backup_databases.yaml`): `gzip -t` validates each gzipped dump file
  (loops over `db_names`).
- **MariaDB dumps** (`backup_databases.yaml`): `gzip -t` validates each gzipped dump
  (matching PostgreSQL pattern; loops over `db_names`).
- **InfluxDB backups** (`backup_databases.yaml`): `tar tzf` validates each tar.gz archive
  (InfluxDB portable backups are directory-based, tar+gzipped before transfer).

## Disaster Recovery

Manual bootstrap procedure for full host reconstruction from the CLI, without Semaphore UI.
Use this when the primary controller host is completely lost (Semaphore + MariaDB + ansible_logging gone).

For non-DR testing, use `test_restore.yaml` instead — it automates the full restore cycle on a
disposable VM and reverts when done.

### Controller Total Loss — CLI Sequence

Run from any machine with Ansible installed, the repo cloned, and the vault password available.

```bash
# 0. Restore PVE node config: keepalived VIP + ansible user + SSH hardening (skip if ping <vault_pve_vip> succeeds)
# maintain_pve.yaml is the preferred option — idempotent, covers ansible user + VIP in one shot, logs to MariaDB
ansible-playbook maintain_pve.yaml --ask-vault-pass
# Alternatively, setup_pve_vip.yaml restores only the VIP with no logging (useful before MariaDB is restored):
# ansible-playbook setup_pve_vip.yaml --ask-vault-pass

# 1. Provision a new VM
ansible-playbook build_ubuntu.yaml -e vm_name=<new-hostname> --ask-vault-pass

# 2. Deploy database stack first (includes MariaDB + Postgres)
ansible-playbook deploy_stacks.yaml --limit <controller-fqdn> -e deploy_stack=databases --ask-vault-pass

# 3. Restore MariaDB from backup (restores semaphore + ansible_logging databases)
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_mariadb -e confirm=yes --ask-vault-pass

# 4. Restore Postgres from backup
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_postgres -e confirm=yes --ask-vault-pass

# 5. Deploy remaining stacks (Semaphore is now functional via MariaDB)
ansible-playbook deploy_stacks.yaml --limit <controller-fqdn> --ask-vault-pass

# 6. Restore appdata
ansible-playbook restore_hosts.yaml -e hosts_variable=docker_stacks --limit <controller-fqdn> -e restore_mode=inplace -e confirm=yes --ask-vault-pass

# 7. Re-render .env files (restore overwrites them with backup copies) and restart stacks
ansible-playbook deploy_stacks.yaml --limit <controller-fqdn> --ask-vault-pass

# 8. Semaphore is back — use UI for remaining hosts
```

**Prerequisites:**
- A working machine with Ansible installed and the repo cloned
- The vault password (stored securely outside the infrastructure)
- SSH access to Proxmox API host (for VM creation)
- Network connectivity to the target subnet

### Automation Coverage

| Step | Automated? | Notes |
|------|------------|-------|
| VM creation + OS | Yes | `build_ubuntu.yaml` |
| Docker + security hardening | Yes | `build_ubuntu.yaml` bootstrap |
| NFS mounts + UFW rules | Yes | `vm_definitions.yaml` → `bootstrap_vm.yaml` |
| Stack deployment | Yes | `deploy_stacks.yaml` |
| Database restore | Yes | `restore_databases.yaml` |
| Appdata restore | Yes | `restore_hosts.yaml` |
| PVE node OS config (keepalived, ansible user, SSH) | Yes | `maintain_pve.yaml` — idempotent; also restores ansible user + SSH hardening |
| PVE cluster VIP only (no logging) | Yes | `setup_pve_vip.yaml` — lightweight alternative when MariaDB not yet available |
| DNS records (internal) | No | DHCP reservation with hostname — network layer |
| DNS records (external) | No | Static IP, managed by `cloudflareddns` container |
| Reverse proxy config | Yes | SWAG/NPM config in appdata (restored from backup) |
| SSL certificates | Yes | Let's Encrypt certs in appdata (restored from backup) |

> **⚠️ Network Isolation — Setup Required**
> Test VMs provisioned without network isolation will connect to live production services.
> Because service `.env` files contain **hardcoded production IPs**, restored Docker stacks
> can reach live databases, external APIs, SMTP relays, sync endpoints, and any other service
> referenced by IP — using real credentials. Inter-container references using Docker service
> names (within the same compose network) are safe — those stay local to the test VM.
>
> Network isolation **must be implemented** before running test restores against a live
> environment. The mechanism depends on your network stack. See
> [Test VM Network Isolation](#test-vm-network-isolation) below for how this project
> implements it with Unifi + Proxmox VLANs.

### Test VM Network Isolation

This project isolates test VMs on a dedicated VLAN with no route to the production LAN
(internet access allowed for Docker pulls). Cross-stack container references using hardcoded
production IPs are handled via persistent loopback aliases — containers reach each other
locally instead of reaching production.

#### Architecture

```
Production LAN (vault_prod_lan_subnet)
  ├── Semaphore (vault_semaphore_ip — static)
  └── Tantive-iv, Odyssey, NAS, DBs...

Test Isolation VLAN (vault_test_vlan_id / vault_test_vlan_subnet)
  └── test-vm0..9
        ├── net0: vmbr0, tag=vault_test_vlan_id   (set by provision_vm.yaml)
        ├── IP: vault_test_vm_ip_prefix + offset   (in isolated subnet)
        └── lo aliases: vault_vm_tantiveiv_ip, vault_vm_odyssey_ip
              ↑ containers use hardcoded prod IPs → hit local Docker services, not prod

Unifi firewall rules (LAN_IN):
  ALLOW  tcp  vault_semaphore_ip/32 → vault_test_vlan_subnet  port 22
  DROP   any  vault_test_vlan_subnet → vault_prod_lan_subnet
  (WAN egress allowed by default — Docker pulls work)
```

#### Manual Prerequisite (one-time)

The Proxmox uplink switch ports use a named tagged-only port profile. Adding a new VLAN
to that profile requires a read-modify-write of the entire profile object — a mistake
corrupts the profile and drops all Proxmox nodes simultaneously. This step is manual:

1. **Devices → Switch → Ports → click a Proxmox uplink port** — note the port profile name
2. **Settings → Profiles → Port Profiles → `<vault_pve_port_profile_name>`**
   - Under **Tagged Networks**, add `test-isolation` (VLAN `<vault_test_vlan_id>`)
   - Save — this is additive; existing tagged VLANs are unaffected; no traffic disruption
3. If Proxmox nodes connect to different switches, repeat on each switch
4. Record the profile name as `vault_pve_port_profile_name` in vault

`setup_test_network.yaml` asserts this prereq is done before creating any resources.

#### Required Vault Vars

| Var | Purpose | Example |
|-----|---------|---------|
| `vault_test_vlan_id` | VLAN ID for isolation | `88` |
| `vault_test_vlan_gateway` | Gateway IP on test VLAN (Unifi `ip_subnet` format) | `192.168.88.1` |
| `vault_test_vlan_subnet` | Network CIDR (firewall rule format) | `192.168.88.0/24` |
| `vault_semaphore_ip` | Semaphore container static IP (SSH allow rule) | prod LAN IP |
| `vault_prod_lan_subnet` | Production LAN CIDR to block | `192.168.1.0/24` |
| `vault_pve_port_profile_name` | Unifi switch port profile for Proxmox uplinks | `Servers-Trunk` |
| `vault_test_vm_ip_prefix` | Update to isolated subnet prefix | `192.168.88.` |

Note: `vault_test_vlan_gateway` and `vault_test_vlan_subnet` describe the same subnet in
different formats — Unifi's `networkconf` API uses gateway-address format; firewall rule
`src_address`/`dst_address` fields use network-address format.

#### Setup

Run once after completing the manual port profile step:

```bash
ansible-playbook setup_test_network.yaml --vault-password-file ~/.vault_pass
```

This creates the `test-isolation` VLAN network and two LAN_IN firewall rules in Unifi.
Idempotent — safe to re-run.

#### Loopback IP Aliases

Test VMs get persistent loopback aliases for production host IPs (via
`/etc/netplan/60-loopback-aliases.yaml`). When a restored container tries to reach a
production service by IP, it hits the loopback alias and connects to the local Docker
service on the test VM instead. Aliases are baked into the `pre-test-restore` snapshot
and survive OOM recovery reboots (netplan is filesystem-persistent).

#### Verification

After setup:
1. `build_ubuntu.yaml -e vm_name=test-vm` — Proxmox shows `VLAN Tag: <vault_test_vlan_id>` on net0; VM gets IP in isolated subnet
2. From test VM SSH:
   - `ping <prod_host_ip>` → times out ✓
   - `curl https://hub.docker.com` → 200 OK ✓
   - `ip addr show lo` → prod host IPs listed as /32 aliases ✓
3. `test_backup_restore.yaml -e source_host=<fqdn>` → all apps restore + health checks pass with no prod connections

### Test Restore (automated)

`test_restore.yaml` uses **ephemeral** VM slots defined in `vars/vm_definitions.yaml`.
The `test-vm` key is the standard ephemeral slot (VMIDs 199–208, IPs from `vault_test_vm_ip_prefix`).
Pass `vm_index=0..9` to select a slot (default 0). The playbook:
1. Provisions the VM if it doesn't exist (idempotent — resumes if VMID already exists from a prior partial run)
2. Snapshots it (`pre-test-restore`), runs the restore, then reverts — leaving the VM ready for the next test
3. In `dr_mode=yes` mode, keeps the restored state (no revert) for real DR recovery

**Do not use permanent VM keys** (`tantiveiv`, `odyssey`, `amp`) with `test_restore.yaml` — those
are production VMs. Only `test-vm` (or a custom ephemeral key) is appropriate.

```bash
# Test any host's full restore cycle (disposable VM, auto-reverts on completion)
ansible-playbook test_restore.yaml -e vm_name=test-vm -e source_host=<source-fqdn> --vault-password-file ~/.vault_pass

# DR mode — same playbook, keeps the VM running after restore (no revert)
ansible-playbook test_restore.yaml -e vm_name=test-vm -e source_host=<source-fqdn> -e dr_mode=yes --vault-password-file ~/.vault_pass

# Test all app_info apps on a disposable VM (per-app DB + appdata restore, health check, revert)
ansible-playbook test_backup_restore.yaml -e source_host=<source-fqdn> --vault-password-file ~/.vault_pass

# Restore a single app to production (requires confirm=yes safety gate)
ansible-playbook restore_app.yaml -e restore_app=authentik -e restore_target=<host-fqdn> -e confirm=yes --vault-password-file ~/.vault_pass
```

**Per-stack health check timeouts** — Timeouts are discovered at runtime from `homelab.health_timeout`
labels on running containers. `tasks/verify_docker_health.yaml` takes the maximum value across all
running containers; the default fallback is 120 s. Services with slow startup (e.g. Authentik at
420 s, databases at 180 s) set their own label value — no central config required.

**OOM auto-recovery** — `test_backup_restore.yaml` detects OOM kill events (via `dmesg`) in its rescue
block. If any app fails due to OOM, after the full app loop it doubles VM memory via the PVE API,
reboots the VM, and retries only the OOM-failed apps with the new memory ceiling.
