# Ansible Home Lab — Design & Architecture

This document covers the philosophy, file layout, Semaphore setup, playbook patterns, database
architecture, and vault configuration for this Ansible home lab automation project. Its purpose is
to give a future maintainer (or future self) enough context to make changes confidently.

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
| **`vars/secrets.yaml`** (vault) | Credentials, API keys, domain suffixes, IP addresses | `discord_webhook_token`, `logging_db_password`, `logging_domain_ext` |
| **`group_vars/all.yaml`** | Shared defaults that apply to all hosts | `ansible_remote_tmp`, `backup_base_dir`, `backup_url` template |
| **Inventory** | Host definitions, group membership, SSH/connection settings | `[ubuntu]`, `[pve]`, `[docker_stacks]`, host FQDNs |
| **Semaphore variable groups** | Routing-only — `hosts_variable` and `config_file` | `{"hosts_variable": "pve:pbs"}` |
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

### Roadmap

The project is organized into three phases:

| Phase | Scope |
|-------|-------|
| **Phase 1 — Backup / Maintain / Update** | Automated backups, maintenance, and updates for all homelab systems |
| **Phase 2 — Verify / Restore** | Backup verification and automated restore procedures |
| **Phase 3 — Deploy / Build / Test** | Docker stack deployment, VM provisioning, shared task extraction, automated restore testing |

Phase 1 covers: `backup_*.yaml`, `maintain_*.yaml`, `update_systems.yaml`,
`download_videos.yaml`, and `add_ansible_user.yaml`. Phase 2 adds `verify_backups.yaml`,
`restore_databases.yaml`, `restore_hosts.yaml`, and `rollback_docker.yaml` — recovering systems
from the backups created in Phase 1. Phase 3 adds `deploy_stacks.yaml` (Docker stack deployment
from Git with vault-templated `.env` files), `build_ubuntu.yaml` (Proxmox VM provisioning with
cloud-init and Docker bootstrap), `deploy_grafana.yaml` (Grafana dashboard deployment via API),
shared task extraction (composable building blocks in `tasks/`), and `test_restore.yaml`
(automated restore testing on disposable VMs). Full design in `future/PHASE3_DESIGN.md`;
task tracking in `future/TODO.md`.

---

## File Structure

```
├── ansible.cfg                     # Ansible settings: disable retry files, YAML stdout callback
├── inventory.yaml                  # Local reference copy of Semaphore inventory — NOT version-controlled
├── inventory.example.yaml          # Template inventory with example hosts and group structure
│
├── group_vars/
│   └── all.yaml                    # Shared defaults: ansible_remote_tmp, ansible_python_interpreter, backup_base_dir/tmp_file/dest_path/url
│
├── vars/
│   ├── secrets.yaml                # AES256-encrypted vault — ALL secrets (incl. domain config, docker_* keys, pve_* keys)
│   ├── secrets.yaml.example         # Template with all vault keys documented (copy → encrypt)
│   ├── example.yaml                # Template for creating new platform vars files
│   ├── semaphore_check.yaml         # Health thresholds (26 checks), controller_fqdn, semaphore_db_name, semaphore_url/semaphore_ext_url, display_timezone, retention_days, appliance_check_hosts
│   ├── proxmox.yaml                 # Proxmox PVE + PBS
│   ├── pikvm.yaml                   # PiKVM KVM
│   ├── unifi_network.yaml           # Unifi Network — backup, gateway paths (unifi_state_file), maintenance_url
│   ├── unifi_protect.yaml           # Unifi Protect — backup, API paths (protect_api_backup_path, protect_temp_file)
│   ├── amp.yaml                     # AMP — backup/update + maintenance config (amp_user, amp_home, amp_versions_keep)
│   ├── docker_stacks.yaml           # Docker Compose — backup/update, stack_assignments, docker_* defaults, app_restore mapping
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
│   ├── notify_discord.yaml          # Shared Discord notification task
│   ├── log_mariadb.yaml             # Shared MariaDB logging task (backups, updates, maintenance tables)
│   ├── log_restore.yaml             # Shared MariaDB logging task (restores table — restore operations only)
│   ├── log_health_check.yaml        # Shared MariaDB logging task (health_checks table — single row)
│   ├── log_health_checks_batch.yaml # Shared MariaDB logging task (health_checks table — multi-row batch INSERT)
│   ├── assert_config_file.yaml      # Shared pre-task: assert config_file is set
│   ├── assert_disk_space.yaml       # Shared pre-task: assert sufficient disk space
│   ├── assert_db_connectivity.yaml  # Shared pre-task: assert MariaDB logging DB is reachable
│   └── deploy_single_stack.yaml     # Per-stack deploy loop body (mkdir, template .env, copy compose, validate, up)
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
├── maintain_docker.yaml            # Prune unused Docker images across all Docker hosts
├── maintain_cache.yaml             # Drop Linux page cache on Ubuntu and unRAID hosts
├── maintain_unifi.yaml             # Restart Unifi Network service
├── maintain_health.yaml            # Scheduled health monitoring — 26 checks across all SSH hosts + DB/API; Uptime Kuma dead man's switch
├── download_videos.yaml            # MeTube yt-dlp downloads — per-video Discord notifications + temp file cleanup; parameterized on config_file; hosts via hosts_variable
├── add_ansible_user.yaml           # One-time utility: create ansible user on PVE/PBS/unRAID hosts (SSH key from vault, ansible_remote_tmp dir, validation assertions)
├── deploy_stacks.yaml             # Deploy Docker stacks from Git — templates .env from vault, copies compose, starts stacks
├── build_ubuntu.yaml              # Provision Ubuntu VMs on Proxmox via API — cloud-init, Docker install, SSH config
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
- `backup_base_dir`, `backup_tmp_file`, `backup_dest_path`, `backup_url` — centralized backup
  path defaults; `backup_base_dir` is the controller's backup root directory (used in
  `backup_dest_path` and the disk space pre-task assertion). Individual `vars/*.yaml` files
  override only when their pattern differs (e.g., `backup_url` overrides in `unifi_network.yaml`,
  `synology.yaml`, database vars, and `ubuntu_os.yaml`)

**`ansible.cfg`** — Ansible configuration: disables `.retry` files and sets `stdout_callback: yaml`
for human-readable output.

**`vars/secrets.yaml`** — AES256-encrypted vault. Contains Discord webhook credentials
(`discord_webhook_id`, `discord_webhook_token`), MariaDB logging credentials (`logging_db_*`),
API keys (`semaphore_api_token`, `unvr_api_key`), domain suffixes for hostname normalization
(`logging_domain_local`, `logging_domain_ext`), and the ansible user SSH public key
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

**`tasks/notify_discord.yaml`** — Shared Discord notification. Called via `include_tasks` with
`vars:` block providing required vars (`discord_title`, `discord_color`, `discord_fields`) and
optional vars. **Discord is optional** — if `discord_webhook_id` or `discord_webhook_token` are
not defined in the vault, the task is silently skipped (no errors). The embed dict is built
dynamically — optional fields are only included when set, so Discord embeds stay clean. Optional
vars and their usage:

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
via `vars:` on the `include_tasks` call using `metube_webhook_id`/`metube_webhook_token`.

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
Tests config archives by verifying gzip integrity and extracting to a staging directory. Reuses
existing `vars/db_*.yaml` and `vars/*.yaml` via standard `hosts_variable`/`config_file` routing.
Logs results to `restores` table with `operation: verify`.

**`restore_databases.yaml`** — Database restore from backup dumps. Supports restoring a specific
database on a shared instance (e.g., just `nextcloud` on shared MariaDB without touching `semaphore`
or `ansible_logging`). Safety-gated with `confirm_restore=yes` assertion. Creates pre-restore safety
backup by default. Stops only **same-host** dependent containers via `db_container_deps` mapping
(from `vars/db_*.yaml`) — cross-host app containers have empty deps and must be stopped manually or
via `restore_hosts.yaml -e include_databases=yes`. Supports `restore_db` (single DB) and
`restore_date` (specific backup date) parameters.

**`restore_hosts.yaml`** — Config/appdata restore from backup archives. Two modes: `staging` (extract
to `<backup_tmp_dir>/restore_staging/` for inspection) and `inplace` (extract to actual paths, requires
`confirm_restore=yes`). Supports selective app restore via `-e restore_app=sonarr` (convention-based:
app name maps to subdirectory under `src_raw_files[0]`). Supports coordinated DB+appdata restore via
`-e include_databases=yes` — loads DB vars into a `_db_vars` namespace (avoiding collision with
play-level Docker vars) and uses `delegate_to: db_host` to restore databases on the correct host
(handles cross-host scenarios like appdata on one host + DB on another). Multi-container
apps handled via `app_restore[restore_app].containers` mapping in `vars/docker_*.yaml`. `serial: 1`
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
deploy via `-e deploy_stack=<name>`, render-only mode via `-e deploy_skip_up=true`, and debug
output via `-e deploy_debug=true`. Runs `serial: 1` to avoid parallel deploy issues.

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
`pve_template_node` must match the node `pve_api_host` resolves to. **Play 2** (the new VM, added
to in-memory `build_target` group — only runs on create) bootstraps Ubuntu: waits for cloud-init
to finish and apt locks to release, runs dist-upgrade, installs Docker and base packages, enables
Docker service, adds user to docker/sudo groups, configures passwordless sudo, hardens SSH
(matching `add_ansible_user.yaml` — prohibit root password, disable password auth, allow ssh-rsa
for Guacamole), and configures UFW (default deny + allow SSH). Deploy stacks separately via
`deploy_stacks.yaml` after provisioning.

**`rollback_docker.yaml`** — Reverts Docker containers to their previous image versions using
the snapshot saved by `update_systems.yaml`. Two rollback paths: **fast** (old image still on
disk — `docker tag` re-tag, no network needed) and **slow** (image pruned by
`maintain_docker.yaml` — pulls old version tag from registry). Safety-gated with
`confirm_rollback=yes`. Without it, shows snapshot info and exits (dry-run). Supports three
scopes: all containers (default), per-stack via `-e rollback_stack=<name>`, or per-service via
`-e rollback_service=<name>`. Docker Compose
hosts (`docker_stacks`) only — for unRAID `docker_run` hosts, see manual rollback guidance
below. Uses `tasks/log_restore.yaml` with `operation: rollback` (per-service). Discord
notification uses yellow (16776960) to distinguish from green/red.

**`update_systems.yaml` — rollback snapshot:** Before pulling new Docker Compose images, the
update playbook captures a `.rollback_snapshot.json` file in `{{ compose_project_path }}` with
the timestamp, image name, full image ID (`sha256:...`), and version label for every target
service. Uses the same 3-tier label detection as the update comparison. Each update overwrites
the previous snapshot — only the last pre-update state is kept. The snapshot file is included
in regular `/opt` appdata backups automatically.

**Docker appdata archive exclusions:** Docker hosts with dedicated database backup jobs
(SQL dumps or InfluxDB portable backups) define `backup_exclude_dirs` in `vars/docker_stacks.yaml`
to exclude database data directories (e.g. `/opt/mariadb`, `/opt/postgres`, `/opt/influxdb`) from
the appdata tar.gz archive. The `community.general.archive` module's `exclude_path` parameter
only matches against expanded path entries, so the playbook converts directory paths to globs
(e.g. `/opt` → `/opt/*`) when exclusions are defined. Hosts without `backup_exclude_dirs` are
unaffected (`default([])`).

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
Used by all 13 operational playbooks that log to MariaDB (every playbook except `download_videos.yaml`
and `add_ansible_user.yaml`).

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
| `root` | root login_password (id=13) | Synology, NAS host |
| `pikvm` | PiKVM login_password (id=11) | pikvm |
| `unifi_network` | root login_password (id=13) | udmp |
| `unifi_protect` | unifi_protect login_password (id=29) | unvr |
| `local` | — | localhost only (legacy — no templates use this inventory) |

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
| `Add — {Target} [{Subtype}]` | `Add — Ansible User [SSH]` |
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
| Maintenance | 6 | `Maintain —` |
| Downloads | 2 | `Download —` |
| Verify | 8 | `Verify —` |
| Restore | 9 | `Restore —`, `Rollback —` |
| Deploy | 3 | `Deploy —`, `Build —` |
| Setup | 1 | `Add —` |

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

---

## Playbook Patterns

### vars_files loading

The main data playbooks (`backup_hosts.yaml`, `backup_databases.yaml`, `update_systems.yaml`,
`backup_offline.yaml`, `download_videos.yaml`, `verify_backups.yaml`, `restore_databases.yaml`,
`restore_hosts.yaml`) load two vars files at the play level:

```yaml
vars_files:
  - vars/secrets.yaml          # all secrets + domain config (logging_domain_local/ext)
  - vars/{{ config_file }}.yaml  # host-specific config
vars:
  config_file: "{{ hosts_variable }}"
```

`config_file` defaults to `hosts_variable`. Override it in the Semaphore variable group JSON when
they differ (download templates always override — `config_file` has no relation to `hosts_variable`
for downloads). A pre-task assertion (`tasks/assert_config_file.yaml`) catches a missing or empty
`config_file` immediately.

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
    include_tasks: tasks/notify_discord.yaml
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

Every `always:` block passes a standard set of vars to `tasks/notify_discord.yaml`. The fields
vary by playbook category but follow consistent patterns within each category:

| Category | `discord_url` source | Fields | Fires on |
|----------|---------------------|--------|----------|
| **Backup** (`backup_hosts`, `backup_databases`, `backup_offline`) | `backup_url` (from `vars/*.yaml`) | Description, Host, Backup Name, Backup Size | Always (success + failure) |
| **Update — OS** (`update_systems` non-Docker) | `backup_url` (same host URL) | Description, Host, Version | Change or failure only |
| **Update — Docker** (`update_systems` Docker) | `backup_url` (same host URL) | Description, Host, Updated | Change or failure only |
| **Maintenance** (`maintain_*` playbooks) | `maintenance_url` (play-level var) | Description, Host | Failure only |
| **Health** (`maintain_health`) | `maintenance_url` / per-check | Custom per alert type | Per-check logic |
| **Download** (`download_videos`) | `video.url` (per-video) | Custom per video | Always (success + failure) |
| **Verify** (`verify_backups`) | `backup_url` (from `vars/*.yaml`) | Description, Host, Source File, Detail | Always |
| **Restore** (`restore_databases`, `restore_hosts`) | `backup_url` (from `vars/*.yaml`) | Description, Host, Source File, Detail/Tables | Always |
| **Rollback** (`rollback_docker`) | `backup_url` (from `group_vars/all.yaml`) | Description, Host, Services, Snapshot Date, Detail | Always |
| **Deploy** (`deploy_grafana`) | `semaphore_ext_url` | Description, Host, Detail | Always |

**URL variable conventions:**

- `backup_url` — defined in `vars/*.yaml`. Used by backup AND update playbooks (same host web UI).
  Set to `""` for hosts with no web UI (e.g., `ubuntu_os.yaml`).
- `maintenance_url` — defined inline in each maintenance playbook's `vars:` block. Set to the
  host's web UI URL or `""` if none. `maintain_semaphore` and `maintain_health` use
  `semaphore_ext_url` instead (localhost plays linking to Semaphore UI).

When adding a new playbook, follow the matching pattern above. Every backup/update/maintenance
notification should include at minimum the Description and Host fields.

> **Shared task review:** When modifying playbooks, check whether any inline task blocks are
> duplicated across multiple playbooks. If so, they are a candidate for extraction into `tasks/`.
> The current shared files (`notify_discord.yaml`, `log_mariadb.yaml`, `log_restore.yaml`,
> `log_health_check.yaml`, `log_health_checks_batch.yaml`, `assert_config_file.yaml`,
> `assert_disk_space.yaml`, `assert_db_connectivity.yaml`) cover the most common operations,
> but inline cleanup patterns or host-type detection logic may have accumulated in individual
> playbooks and could be worth consolidating if the same pattern appears more than once.

### Roles vs. flat tasks/ structure

Ansible roles bundle tasks, defaults, handlers, templates, and files into a named unit. They are
the right choice when a component needs its own defaults, handlers, templates, or test isolation.

**Current state — tasks are the right fit:**

The project has eight shared task files and no handlers or templates. The `tasks/` files are thin
glue code (send a Discord embed, run an INSERT, assert a precondition). At this scale, promoting
them to roles would add directory structure without gaining any role-specific features.

**When to add roles:**

- A component needs **handlers** — e.g., a restart-service handler that deduplicates across multiple task calls
- A component needs **role-level templates** — e.g., a config file rendered from a Jinja2 template
- You want **Molecule testing** — Molecule is designed around roles and works most naturally with them
- You want to publish or reuse this automation across multiple separate projects
- The shared task files grow beyond ~5 files and grouping them by domain becomes valuable

The decision should be pragmatic — use roles when they provide concrete benefits, stick with
flat tasks when they don't.

**How to migrate when the time comes:**

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

Discord "Host" fields use the same sources directly — `{{ inventory_hostname }}` for remote
hosts, `{{ controller_fqdn }}` for localhost plays. No intermediate variables are needed.

**Why `inventory_hostname` instead of `ansible_fqdn`:** `ansible_fqdn` is what the remote host
reports about itself, which varies by OS and configuration. unRAID may return `myhost.local`;
some hosts may return short names or unexpected capitalization. `inventory_hostname` is the
canonical identifier defined in the inventory — it is always the correct FQDN, always lowercase,
and completely under the user's control. The DB and Discord output reflect exactly what is in
the inventory, regardless of what any host reports about itself.

**`controller_fqdn`** is defined in `vars/semaphore_check.yaml` as a static string
(e.g., `controller.home.local`). Playbooks that run on `hosts: localhost`
(`maintain_semaphore.yaml`, `maintain_health.yaml` Plays 1/3) use it for both DB logging and
Discord fields because `inventory_hostname` resolves to `localhost` in that context.

### URL construction

Discord embed URLs (`backup_url`) use the **external domain suffix** from the vault
(`logging_domain_ext`) combined with the short hostname extracted from `inventory_hostname`:

```yaml
backup_url: "https://{{ inventory_hostname.split('.')[0] }}.{{ logging_domain_ext }}"
```

This produces URLs like `https://myhost.example.com` — the short hostname (`myhost`) joined
with the external domain (`example.com`). Do **not** use the full `inventory_hostname` in URL
construction, as that would create invalid double-domain URLs (e.g., `myhost.home.local.example.com`).

Some hosts use fixed URL patterns instead:
- Synology: `https://synology.{{ logging_domain_ext }}` (runs on NAS host, URL is for synology)
- Database: `https://sql.{{ logging_domain_ext }}` (shared URL regardless of host)
- Unifi Network: `https://unifi.ui.com/` (hardcoded external cloud portal)

**Semaphore dual URLs:** Semaphore has two URL variables because the API needs an internal
IP-based URL while Discord notification links need the external domain:

| Variable | Source | Example | Purpose |
|---|---|---|---|
| `semaphore_url` | `semaphore_host_url` from vault (trailing slash stripped) | `http://10.0.0.1:3000` | API calls (`/api/project/...`) |
| `semaphore_ext_url` | Built from `controller_fqdn` + `logging_domain_ext` | `https://controller.example.com` | Discord embed links, `maintenance_url` |

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
| `maintain_cache.yaml` | Cache drop simulated. |
| `maintain_docker.yaml` | Docker prune simulated. |
| `maintain_semaphore.yaml` | DB cleanup and retention pruning simulated. |
| `maintain_unifi.yaml` | Service restart simulated. |
| `download_videos.yaml` | Config deploy simulated. yt-dlp execution skipped. Manifest read runs (empty). Temp file discovery runs; deletions simulated. |
| `verify_backups.yaml` | Backup file search runs. Integrity checks run. No temp databases created. No archives extracted. Discord/DB suppressed. |
| `restore_databases.yaml` | Backup file search runs. Safety backup skipped. No restore performed. No container management. Discord/DB suppressed. |
| `restore_hosts.yaml` | Backup file search runs. Archive integrity verified. No extraction or container management. Discord/DB suppressed. |
| `rollback_docker.yaml` | Snapshot read and parsed. Safety gate skipped. No image re-tag/pull or container recreation. Discord/DB suppressed. |
| `deploy_grafana.yaml` | Dashboard JSON read and parsed. All API calls skipped (datasource check, create, dashboard import). Discord/DB suppressed. |

### Pre-task validations

Production playbooks include pre-task assertions to catch environmental problems early, before
any work starts. Two shared assertion task files are available:

**`tasks/assert_disk_space.yaml`** — Checks free space on a filesystem path. Caller passes
`assert_disk_path` and `assert_disk_min_gb` via `vars:`. Used by `backup_hosts.yaml` (remote
`backup_tmp_dir` + controller `/backup`), `backup_databases.yaml` (remote `backup_tmp_dir`),
`update_systems.yaml` (root filesystem `/`), `restore_databases.yaml` (`backup_tmp_dir`), and
`restore_hosts.yaml` (`backup_tmp_dir` for staging, `/` for inplace).

**`tasks/assert_db_connectivity.yaml`** — Verifies the MariaDB logging database is reachable
via `SELECT 1`. Used by all 13 operational playbooks that call `tasks/log_mariadb.yaml` or
`tasks/log_restore.yaml` in their
`always:` block. Catches MariaDB outages early — before any backup, update, or maintenance work
starts — rather than failing silently in the `always:` logging step.

**`tasks/assert_config_file.yaml`** — Asserts `config_file` is defined and non-empty, catching
misconfigured Semaphore variable groups before work starts. Used by `backup_hosts.yaml`,
`backup_databases.yaml`, `update_systems.yaml`, `backup_offline.yaml`, `download_videos.yaml`,
`verify_backups.yaml`, `restore_databases.yaml`, and `restore_hosts.yaml`.

The `assert_disk_space` and `assert_db_connectivity` tasks have `check_mode: false` on their
shell/query pre-steps so they validate during `--check`. `assert_config_file` uses
`ansible.builtin.assert` which runs natively in check mode.

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

Five tables (four operational + one state). All columns are set by Ansible — no DB-side triggers,
functions, or computed columns. Run `mysql -u root -p < sql/init.sql` to create the database and
all tables. The init script uses `CREATE TABLE IF NOT EXISTS` so re-running is safe.

#### Timezone convention

**Storage: always UTC.** All `timestamp` columns are written with `UTC_TIMESTAMP()` — never
`NOW()`. The MariaDB server runs in the host's local timezone (CST/UTC-6, `@@global.time_zone`
= `SYSTEM`), so `NOW()` returns CST — **not** UTC. `UTC_TIMESTAMP()` always returns UTC
regardless of the server's timezone, making it the only safe choice. Do not change the server
timezone to UTC — other databases on the same MariaDB instance depend on the current setting.

**Historical data migration (Feb 2026):** All existing rows (written with `NOW()` in CST) were
shifted to UTC via `UPDATE <table> SET timestamp = DATE_ADD(timestamp, INTERVAL 6 HOUR)` on
the `backups`, `maintenance`, and `health_checks` tables. The `updates` table was not migrated
because `INSERT IGNORE` on the unique index means rows are sparse and timestamps are less
critical for display.

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
  subtype     VARCHAR(50)  NOT NULL,   -- 'Cleanup', 'Prune', 'Cache', 'Restart', 'Maintenance', 'Health Check', 'Verify', 'Deploy'
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
- Maintenance: `Cleanup`, `Prune`, `Cache`, `Restart`, `Maintenance`, `Health Check`

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
| `backup_type` | `backups.backup_type` | `vars/*.yaml` |
| `backup_subtype` | `backups.backup_subtype` | `vars/*.yaml` |
| `update_type` | `updates.update_type` | `vars/*.yaml` |
| `update_subtype` | `updates.update_subtype` | `vars/*.yaml` |
| `maintenance_name` | `maintenance.application` | inline `vars:` in playbook |
| `maintenance_type` | `maintenance.type` | inline `vars:` in playbook |
| `maintenance_subtype` | `maintenance.subtype` | inline `vars:` in playbook |

The first six must be present in any vars file used with `backup_hosts.yaml` or `update_systems.yaml`.
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
| `vars/docker_stacks.yaml` | Docker | Servers | Appdata |
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
| `deploy_stacks.yaml` | Docker | Servers | Deploy |
| `build_ubuntu.yaml` | Ubuntu | Servers | Build |

**Restores** (type/subtype reuse the backup vars file values):

| vars file source | application | restore_type | restore_subtype | operation |
|---|---|---|---|---|
| `vars/db_*.yaml` | (db name)-db | Servers | Database | restore |
| `vars/proxmox.yaml` | PVE or PBS | Appliances | Config | restore |
| `vars/docker_stacks.yaml` | Docker or (app name) | Servers | Appdata | restore |
| `vars/docker_run.yaml` | Docker or (app name) | Servers | Appdata | restore |
| `vars/unraid_os.yaml` | unRAID | Servers | Config | restore |
| `vars/pikvm.yaml` | PiKVM | Appliances | Config | restore |
| `vars/docker_stacks.yaml` | (service name) | Servers | Container | rollback |

**Verification** logs to the `maintenance` table (subtype `Verify`) using the same
`backup_type` / `backup_name` values from the vars file. See [Maintenance](#maintenance).

### Expected hostname format

All hostnames in the inventory should be FQDNs using a consistent internal domain suffix
(matching `logging_domain_local` in the vault). External hosts (e.g., a VPS) use the external
domain (`logging_domain_ext`). Examples:

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
logging_domain_local: "..."         # e.g. "home.local" — internal domain suffix
logging_domain_ext: "..."           # e.g. "example.com" — external domain suffix for URLs
semaphore_api_token: "..."
semaphore_host_url: "..."           # internal IP URL, e.g. "http://10.0.0.1:3000"
controller_hostname: "..."          # short hostname of Semaphore controller (builds controller_fqdn)
db_host_primary: "..."              # inventory_hostname of primary DB host (restore cross-host delegate_to)
db_host_secondary: "..."            # inventory_hostname of secondary DB host (restore cross-host delegate_to)

# --- Optional: only needed for specific playbooks ---
metube_webhook_id: "..."            # MeTube Discord channel — per-video download notifications (download_videos.yaml)
metube_webhook_token: "..."         # MeTube Discord channel — per-video download notifications (download_videos.yaml)
unvr_api_key: "..."                 # Unifi Protect UNVR API key (backup_hosts.yaml)
ansible_user_ssh_pubkey: "..."      # SSH public key for ansible user (add_ansible_user.yaml)
synology_ip: "..."                  # Synology NAS IP address (backup_offline.yaml via vars/synology.yaml)
synology_mac: "..."                 # Synology NAS MAC for WOL (backup_offline.yaml via vars/synology.yaml)
synology_name: "..."                # Synology NAS mount name (backup_offline.yaml via vars/synology.yaml)
db_password: "..."                  # Database password for Docker container DB dumps (backup/restore/verify playbooks)
grafana_url: "..."                  # Grafana base URL, e.g. "http://grafana-host:3000" (deploy_grafana.yaml)
grafana_service_account_token: "..." # Grafana service account token with Editor role (deploy_grafana.yaml)
vps_fqdn: "..."                    # VPS hostname for WireGuard config extraction (backup_hosts.yaml via vars/unifi_network.yaml)
```

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

Edit `vars/secrets.yaml` (`ansible-vault edit vars/secrets.yaml`) and update `logging_domain_local`
and `logging_domain_ext`. Propagates automatically to all future INSERTs and the health check query.
Existing rows are not affected — update them manually if needed.

### Restore a database from backup

```bash
# Restore a single DB (latest backup) — e.g., nextcloud on shared MariaDB
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_mariadb -e confirm_restore=yes -e restore_db=nextcloud

# Restore from a specific date
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_postgres -e confirm_restore=yes -e restore_db=authentik -e restore_date=2026-02-20
```

### Restore an app's appdata + database together

```bash
# Coordinated restore — appdata on one host, DB on another (cross-host via delegate_to)
ansible-playbook restore_hosts.yaml -e hosts_variable=docker_run --limit <hostname> \
  -e restore_app=sonarr -e include_databases=yes -e restore_mode=inplace -e confirm_restore=yes -e manage_docker=yes
```

Always use `--limit <hostname>` with `-e restore_app` since `docker_stacks`/`docker_run` are
multi-host groups. The `app_restore` mapping in `vars/docker_*.yaml` provides the container list,
DB config file, and `db_host` for cross-host `delegate_to`.

### Roll back a Docker container update

```bash
# Dry run — show snapshot info without rolling back
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname>

# Rollback a single stack
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e rollback_stack=vpn -e confirm_rollback=yes

# Rollback a single service
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e rollback_service=jellyseerr -e confirm_rollback=yes

# Rollback all containers in the snapshot
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e confirm_rollback=yes
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

**Panel layout (5 collapsible row groups, 21 content panels):**

| Row | Default | Panels |
|-----|---------|--------|
| **Alerts** | Expanded | Stale Backups (9+ Days), Stale Updates (14+ Days), Current Non-OK Health Checks (with check_detail) |
| **Trends** | Expanded | Backups/Updates/Maintenance Over Time, Backup Size Trend by Application, Health Issues Over Time |
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

- **`no_log: true` policy**: Every task that handles credentials — database passwords, API tokens,
  SSH keys, HTTP Bearer tokens, or webhook secrets — uses `no_log: true` to prevent exposure in
  Ansible logs and verbose output. This covers `docker exec -e` commands with `MYSQL_PWD`,
  `ansible.builtin.uri` calls with Bearer/API-key headers, `community.mysql.mysql_query` tasks
  with `login_password`, and Discord webhook notifications. Registered variables from these tasks
  (e.g., full HTTP responses containing `Authorization` headers) are also suppressed.
- **mysqldump password**: Uses `MYSQL_PWD` environment variable via `docker exec -e` instead of
  `--password=` on the command line. The env var approach avoids exposing the password in
  `/proc/<pid>/cmdline` (visible to `ps aux` on the host).
- **SSH public key** (`add_ansible_user.yaml`): The key is stored in a vault variable
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

- **Backup source directory** (`backup_offline.yaml`): Uses `mode: '0750'` (owner rwx, group rx,
  other none) — never world-writable.
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

- **`confirm_restore=yes` gate** (`restore_databases.yaml`, `restore_hosts.yaml`): Destructive
  restore operations require explicit `-e confirm_restore=yes` on the command line. Without it,
  the pre-task assertion fails with a guidance message. Prevents accidental data overwrites.
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
