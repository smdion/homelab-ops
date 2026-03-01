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

**The database is a log with minimal state reads.** The MariaDB `ansible_logging` database stores
backup and update records for visibility and history. Two lightweight exceptions read back: version
comparison (`update_systems`) and health-check baseline (`maintain_health`). No triggers, no stored
functions, no computed columns. All categorization (type, subtype) and hostname normalization happen
in Ansible before the INSERT.

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
| **`group_vars/all.yaml`** | Shared defaults that apply to all hosts | `ansible_remote_tmp`, `stacks_base_path`, `backup_base_dir`, `backup_url` template |
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
│   └── pikvm.yaml                  # PiKVM override: ansible_remote_tmp → /tmp/.ansible/tmp (RO filesystem; /tmp is tmpfs)
│
├── vars/
│   ├── secrets.yaml                # AES256-encrypted vault — ALL secrets (incl. domain config, docker_* keys, pve_* keys)
│   ├── secrets.yaml.example         # Template with all vault keys documented (copy → encrypt)
│   ├── example.yaml                # Template for creating new platform vars files
│   ├── semaphore_check.yaml         # Health thresholds (26 checks), controller_fqdn, semaphore_db_name, semaphore_url/semaphore_ext_url, display_timezone, retention_days, appliance_check_hosts
│   ├── pve_definitions.yaml          # PVE cluster node definitions (IPs, FQDNs, Guacamole metadata)
│   ├── proxmox.yaml                 # Proxmox PVE + PBS
│   ├── pikvm.yaml                   # PiKVM KVM — backup/update config; see group_vars/pikvm.yaml for connection override
│   ├── unifi_network.yaml           # Unifi Network — backup, gateway paths (unifi_state_file), unifi_backup_retention, maintenance_url
│   ├── unifi_protect.yaml           # Unifi Protect — backup, API paths (unifi_protect_api_backup_path, unifi_protect_temp_file)
│   ├── amp.yaml                     # AMP — backup/update + maintenance config (amp_user, amp_home, amp_versions_keep)
│   ├── app_definitions.yaml         # Apps + infrastructure DBs — restore discovery, scope selector, single source for db_names
│   ├── container_definitions.yaml   # Pinned container image:tag pairs (authentik, postgres, victoria-metrics, jellyseerr)
│   ├── docker_stacks.yaml           # Docker Compose — backup/update, stack_assignments, docker_network_name, docker_* defaults
│   ├── docker_vips.yaml             # Keepalived VRRP config for Docker VM VIPs — interface, CIDR, test VIP offsets; vault vars for VIPs + priorities
│   ├── docker_run.yaml              # Docker run / unRAID — backup/update, backup/update exclude lists
│   ├── guacamole.yaml               # Guacamole connection management — groups, admin groups, SSH defaults, user group permissions
│   ├── ubuntu_os.yaml               # Ubuntu OS updates
│   ├── unraid_os.yaml               # unRAID OS backup
│   ├── synology.yaml                # Synology NAS sync
│   ├── vm_definitions.yaml          # Consolidated VM spec — VMID/IP/resources/stacks/deploy_ssh_key per role; derives stack_roles + host_roles dynamically
│   ├── host_definitions.yaml       # Non-Proxmox managed hosts (VPS, legacy, NAS) — merged into host_roles/stack_roles
│   ├── db_primary_postgres.yaml     # Primary host Postgres DB backup + db_container_deps for restore
│   ├── db_primary_mariadb.yaml      # Primary host MariaDB backup + db_container_deps for restore
│   ├── db_primary_influxdb.yaml     # Primary host InfluxDB backup + db_container_deps for restore
│   ├── db_secondary_postgres.yaml   # Secondary host Postgres DB backup + db_container_deps for restore
│   ├── download_base.yaml          # Shared infrastructure for all download profiles (container, paths, Discord icons)
│   ├── download_default.yaml       # yt-dlp download profile: default (scheduled — per-user preference overrides)
│   └── download_on_demand.yaml    # yt-dlp download profile: on_demand (bookmarklet — per-user preference overrides)
│
├── tasks/
│   ├── notify.yaml                  # Shared notification task (Discord + Apprise)
│   ├── log_mariadb.yaml             # Shared MariaDB logging task (backups, updates, maintenance tables)
│   ├── log_run_context.yaml         # Shared MariaDB logging task (playbook_runs table — one row per playbook invocation)
│   ├── log_restore.yaml             # Shared MariaDB logging task (restores table — restore operations only)
│   ├── log_health_checks_batch.yaml # Shared MariaDB logging task (health_checks table — multi-row batch INSERT)
│   ├── assert_config_file.yaml      # Shared pre-task: assert config_file is set
│   ├── assert_disk_space.yaml       # Shared pre-task: assert sufficient disk space
│   ├── assert_db_connectivity.yaml  # Shared pre-task: assert MariaDB logging DB is reachable
│   ├── backup_single_stack.yaml     # Per-stack backup loop body (stop stack, archive appdata, verify, fetch, restart, record result)
│   ├── capture_image_versions.yaml  # Record Docker container image versions (.versions.txt manifest) alongside backup archives
│   ├── backup_single_db.yaml        # Per-DB backup loop body (dump, verify integrity, fetch to controller, record result)
│   ├── backup_combined_db_group.yaml # Per-config-file DB backup (load vars, create temp dir, loop backup_single_db, cleanup)
│   ├── db_dump.yaml                 # Dump a single DB from a Docker container — PostgreSQL/MariaDB/InfluxDB engine abstraction
│   ├── db_restore.yaml              # Restore a single DB from backup — verify (temp DB) or production overwrite, all engines
│   ├── db_count.yaml                # Count tables/measurements in a database — PostgreSQL/MariaDB/InfluxDB
│   ├── db_drop_temp.yaml            # Drop a database and clean up container temp files — all engines
│   ├── wait_db_ready.yaml           # Wait for database container to accept connections after Docker restart — engine-specific readiness probe
│   ├── deploy_single_stack.yaml     # Per-stack deploy loop body (mkdir, template .env, copy compose, validate, pull, up); retries transient image pull failures
│   ├── provision_vm.yaml            # Provision VM on Proxmox via cloud-init template clone + API config; optional QEMU args (pve_args)
│   ├── resolve_or_provision_vm.yaml # Resolve existing test VM or provision new one — shared by test/verify playbooks
│   ├── bootstrap_vm.yaml            # Bootstrap Ubuntu VM: apt, Docker, SSH hardening, UFW, NFS server/client, CephFS (test VMs only), desktop env (desktop role)
│   ├── ssh_hardening.yaml           # Passwordless sudo + SSH config hardening + service restart
│   ├── docker_stop.yaml             # Stop Docker containers/stacks (selective, stack, or unRAID mode)
│   ├── docker_start.yaml            # Start Docker containers/stacks (selective, stack, or unRAID mode)
│   ├── resolve_scope.yaml           # Shared scope resolution — role injection for unmapped hosts, stack/role meta:end_host filtering
│   ├── restore_appdata.yaml         # Restore appdata archive — copy to target, detect root, extract (selective or full)
│   ├── restore_single_stack.yaml    # Per-stack restore loop body (find→verify→stop→restore→start→record); mirrors backup_single_stack
│   ├── restore_selective_app.yaml   # Selective app restore from monolithic archive + optional cross-host DB restore
│   ├── restore_monolithic.yaml      # Full-host monolithic restore (verify→stop→extract→start)
│   ├── restore_and_deploy.yaml      # Shared restore-deploy-verify pipeline for test_restore and dr_rebuild (extract appdata, patch SWAG, deploy stacks, restore DBs, health check)
│   ├── restore_app_step.yaml        # Per-app restore loop body (stop stack, restore DB+appdata, restart, health check; OOM detection)
│   ├── verify_app_http.yaml         # Per-app HTTP endpoint verification (used by restore_app.yaml and test_backup_restore.yaml)
│   ├── backup_single_amp_instance.yaml  # Per-AMP-instance backup loop body (stop→archive→verify→fetch→start)
│   ├── restore_single_amp_instance.yaml # Per-AMP-instance restore loop body (stop→remove→extract→start); mirrors backup_single_amp_instance
│   ├── patch_swag_confs.yaml        # Patch SWAG nginx configs after restore — old IPs → Docker DNS (same-VM) / VIPs (cross-VM) + authentik outpost
│   ├── patch_compose_networks.yaml  # Post-template patching: bridge-to-homelab network + env IP fixes for test/DR deploys
│   ├── pre_restore_safety_dump.yaml # Create pre-restore safety backup of databases before overwriting — shared by restore/rollback playbooks
│   ├── rollback_images.yaml         # Shared image rollback — re-tag local or pull from registry; retries transient pull failures (no compose up)
│   ├── rollback_restore_stack.yaml  # Per-stack appdata restore during rollback with_backup (find→verify→extract→clean)
│   ├── rollback_restore_dbs.yaml    # Per-app DB restore during rollback with_backup (load config→find dumps→restore→clean)
│   ├── verify_docker_health.yaml    # Poll Docker container health until all healthy or timeout
│   ├── assert_test_vm.yaml          # Safety gate — assert /opt CephFS mount is not a production directory before destructive writes
│   ├── verify_docker_network.yaml   # Verify cross-stack DNS resolution and TCP connectivity on shared Docker network
│   ├── verify_network_isolation.yaml # Verify test VLAN isolation — PVE VIP unreachable, CephFS monitors + public DNS reachable
│   ├── verify_vip.yaml             # Verify keepalived VIP is active on expected interface (skips if no role assigned)
│   ├── pre_task_assertions.yaml    # Pre-task bundle: assert_config_file (optional) → assert_db_connectivity → log_run_context; used by 26 playbooks
│   ├── pre_test_assertions.yaml   # Shared pre-flight assertions for test/DR playbooks: vm_name in vm_definitions, source_host/role validation
│   ├── apply_role_resolve.yaml     # Per-role resolution loop body: resolve VM definition, display scope, add target to inventory
│   ├── resolve_effective_vips.yaml # Shared VIP resolution — test VIP offsets vs vault VIPs
│   ├── resolve_test_vm_index.yaml  # Auto-detect free test-vm slot from vm_test_slot_base pool
│   └── reset_db_auth.yaml         # Force-reset MariaDB/Postgres passwords via socket auth after backup restore
│
├── scripts/
│   └── dr_rebuild_all.sh           # Shell wrapper: run dr_rebuild.yaml for multiple roles sequentially (core,apps,dev)
│
├── templates/
│   ├── keepalived.conf.j2          # Jinja2 template for keepalived VRRP config on PVE nodes — floating management VIP
│   ├── keepalived-docker.conf.j2   # Jinja2 template for keepalived VRRP — floating VIP per Docker VM role; test mode derives VIP from VM subnet
│   ├── metube.conf.j2              # Jinja2 template for yt-dlp config — rendered per profile from vars/download_<name>.yaml
│   └── netplan-loopback-aliases.j2 # Jinja2 template for netplan loopback alias config — VIP addresses on loopback interface
│
├── backup_hosts.yaml               # Config/Appdata backups (Proxmox, PiKVM, Unifi, AMP, Docker, unRAID); with_databases=yes for combined appdata + DB backup
├── backup_databases.yaml           # Database backups (Postgres + MariaDB dumps, InfluxDB portable backup); integrity verification; standalone scheduling
├── backup_offline.yaml             # unRAID → Synology offline sync (WOL + rsync); shutdown verification; logs both successful and failed syncs; hosts via hosts_variable
├── backup_offsite.yaml             # Backblaze B2 offsite sync — rclone sync /backup/ to B2 bucket; runs on localhost; dry_run/bwlimit support
├── verify_backups.yaml             # On-demand backup verification — DB backups restored to temp DB; config archives integrity-checked and staged
├── restore_databases.yaml          # Database restore from backup dumps — safety-gated; supports single-DB restore on shared instances
├── restore_hosts.yaml              # Config/appdata restore — per-stack, selective app, or monolithic; stack=/role= scope selectors; coordinated cross-host DB restore
├── rollback_docker.yaml            # Docker container rollback — revert to previous image versions; with_backup=yes for combined image+appdata+DB recovery; safety-gated
├── update_systems.yaml             # OS, application, and Docker container updates (Proxmox, PiKVM, AMP, Ubuntu, Docker); PVE cluster quorum pre-check; rollback snapshot; unRAID update_container script
├── maintain_amp.yaml               # AMP game server maintenance (versions, dumps, prune, journal)
├── maintain_semaphore.yaml         # Delete stopped/error + old download tasks from Semaphore DB + prune ansible_logging retention (runs on localhost)
├── maintain_logging_db.yaml        # Purge failed/warning records from ansible_logging; purge_all=yes + confirm=yes truncates all 8 tables (full reset) — runs on localhost
├── maintain_docker.yaml            # Prune unused Docker images + drop Linux page cache (Ubuntu/unRAID); logs metrics to docker_sizes table
├── maintain_unifi.yaml             # Restart Unifi Network service
├── maintain_health.yaml            # Scheduled health monitoring — 26 checks across all SSH hosts + DB/API; Uptime Kuma dead man's switch
├── maintain_guacamole.yaml          # Declarative Guacamole connection + user group permission management — converges from definition files via docker exec
├── maintain_pve.yaml               # Idempotent Proxmox node config (keepalived VIP, ansible user, SSH hardening); stale snapshot check (>14d alert); PBS task error check (last 2d via proxmox-backup-manager); notification + MariaDB logging
├── download_videos.yaml            # MeTube yt-dlp downloads — per-video notifications + temp file cleanup; parameterized on config_file; hosts via hosts_variable
├── setup_ansible_user.yaml         # One-time utility: create ansible user on PVE/PBS/unRAID hosts (SSH key from vault, ansible_remote_tmp dir, validation assertions)
├── setup_pve_vip.yaml              # One-time VIP setup: install and configure keepalived on PVE nodes; verifies VIP reachable on port 22
├── deploy_stacks.yaml             # Deploy Docker stacks from Git — templates .env from vault, copies compose, starts stacks; serial:1; role=/stack= scope
├── apply_role.yaml                # Idempotent VM reconciliation — OS/network/Docker/stacks/verification layers; serial:1 all-roles mode; -e role= filter
├── dr_rebuild.yaml                # DR rebuild — provision VM → bootstrap → restore backups → deploy stacks → health check; -e role=core|apps|dev
├── build_ubuntu.yaml              # Provision Ubuntu VMs on Proxmox via API — cloud-init, Docker install, SSH config
├── restore_amp.yaml               # AMP instance restore — stop instance(s), replace data dir from archive, restart; per-instance or all; requires confirm=yes
├── restore_app.yaml               # Production single-app restore — stop stack, restore DB(s) + appdata inplace, restart, health check; requires confirm=yes
├── test_restore.yaml              # Automated restore testing — provision disposable VM, deploy stacks, health check, revert; vm_name defaults to test-vm; role can substitute for source_host
├── test_backup_restore.yaml          # Test all app_definitions apps on disposable VM — per-app DB+appdata restore, OOM auto-recovery, notification summary, revert
├── verify_cephfs.yaml             # Verify CephFS mount on a target VM — checks mount source, writes/reads marker file; requires -e vm_name=<key>
├── verify_isolation.yaml          # Lightweight test VLAN isolation verification — provisions bare test VM, runs network checks, then destroys
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

**`group_vars/all.yaml`** — Shared Ansible defaults for all hosts. Key contents:
`ansible_remote_tmp` (home dir to avoid world-readable `/tmp`), `stacks_base_path` (Docker
Compose project root, default `/opt/stacks`), `_gz_detect` (pigz/gzip auto-detection snippet),
centralized backup path defaults (`backup_base_dir`, `backup_tmp_dir`, `backup_dest_path`,
`backup_url`), type defaults (`backup_type: "Servers"`, `update_type: "Servers"` — override to
`"Appliances"` for purpose-built gear), and DB engine flags (`is_postgres`/`is_mariadb`/
`is_influxdb`, all default `false`).

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
all eight tables (`backups`, `updates`, `maintenance`, `health_checks`, `health_check_state`, `restores`, `docker_sizes`, `playbook_runs`).
Run once with `mysql -u root -p < sql/init.sql`. Uses `CREATE TABLE IF NOT EXISTS` so re-running
is safe.

**`tasks/notify.yaml`** — Shared notification task (Discord + optional Apprise). Called via
`include_tasks` with `vars:` block. Required: `discord_title`, `discord_color`, `discord_fields`.
All notification channels are optional — unconfigured channels are silently skipped. Discord
embed is built dynamically; optional vars (`discord_description`, `discord_url`, `discord_author`,
`discord_footer`, `discord_image`, etc.) are only included when set. Webhook credentials are
inherited from vault; `download_videos` overrides to a separate MeTube channel. Apprise support
adds `apprise_urls` or `apprise_api_url` + `apprise_api_key` alongside Discord.

**`templates/metube.conf.j2`** — Jinja2 template for yt-dlp configuration, rendered per download
profile from `vars/{{ config_file }}.yaml`. Profile-specific settings (quality, paths, batch
file, filters) come from the vars file; common options (ignore errors, embed metadata,
sponsorblock) and the `--print-to-file` JSONL metadata export are in the template. Two vars
control conditional rendering:
- `ytdlp_quiet` — includes `-q` when true; `download_on_demand` sets `false` for verbose logs
- `ytdlp_filter_live` — includes `--match-filter !is_live` when true; `download_on_demand`
  sets `false` to allow live stream downloads

Deployed to the host via `ansible.builtin.template` to `/mnt/user/appdata/youtube-dl/<config_name>/`.

**`vars/download_base.yaml`** — Shared infrastructure for all download profiles. Contains
MeTube container name, host paths, temp cleanup settings, container-internal paths, output
template, extractor configuration, and Discord notification URLs/icons. Loaded automatically
by `download_videos.yaml` before the profile-specific file.

**`vars/download_default.yaml`** — Per-user preference overrides for scheduled channel downloads.
Contains `config_name`, batch file, archive path, quality, format, rate limit, content filters,
and mode flags (`ytdlp_quiet: true`, `ytdlp_filter_live: true`).

**`vars/download_on_demand.yaml`** — Per-user preference overrides for bookmarklet-triggered
downloads. Shares `config_name: default` with `download_default.yaml` (same config directory
and download archive) but overrides `ytdlp_batch_file` to read from the bookmarklet feeder
file, and sets `ytdlp_quiet: false` and `ytdlp_filter_live: false` for verbose output and
live stream support.

Each Semaphore template uses its own `config_file` to load the correct profile:
- **Default** environment: `{"hosts_variable": "<download_host>", "config_file": "download_default"}`
- **On Demand** environment: `{"hosts_variable": "<download_host>", "config_file": "download_on_demand"}`

Adding a new per-user profile = create `vars/download_<user>_default.yaml` and
`vars/download_<user>_on_demand.yaml` with preference overrides + create Semaphore templates.
Infrastructure vars inherit from `download_base.yaml` automatically.

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

**`restore_hosts.yaml`** — Config/appdata restore from backup archives. Safety-gated with
`confirm=yes`. Always stops containers before restore and restarts after (restoring while running
corrupts state). Thin orchestrator dispatching to three task files based on restore path:
`tasks/restore_single_stack.yaml` (per-stack, loopable), `tasks/restore_selective_app.yaml`
(single app + optional cross-host DB), and `tasks/restore_monolithic.yaml` (full host). Supports
`stack=<name>` and `role=<name>` scope selectors for docker_stacks hosts — auto-resolves target
host via `stack_assignments`/`host_roles` (no `--limit` needed). Supports selective app
restore via `-e restore_app=sonarr` (convention-based: app name maps to subdirectory under
`src_raw_files[0]`). Supports coordinated DB+appdata restore via `-e with_databases=yes` — loads
DB vars into a `_db_vars` namespace (avoiding collision with play-level Docker vars) and uses
`delegate_to: db_host` to restore databases on the correct host. `serial: 1` matches
`backup_hosts.yaml`. Use `verify_backups.yaml` to inspect archives without restoring.

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
deploy via `-e stack=<name>` (or legacy `-e deploy_stack=<name>`), render-only mode via `-e validate_only=yes`, and debug
output via `-e debug_no_log=yes`. Runs `serial: 1` to avoid parallel deploy issues.

**`apply_role.yaml`** — Idempotent VM state reconciliation. Two-play structure: Play 1 (localhost)
resolves VM definitions and adds targets to the `apply_target` inventory group; Play 2 (target
VMs, `serial: 1`) applies layered reconciliation: OS + Network (bootstrap) → Docker (shared
network) → Application (stack deployment) → Hardware (PVE resize, off by default) → Verification
(health, network, VIP). Omit `-e role` to target all production Docker VMs in VMID order
(core=300, apps=301, dev=302), respecting NFS dependencies. Pass `-e role=core` to target a
single role. Basic VMs without stacks (e.g. `amp`, `desktop`) require explicit `-e role=<name>`. Per-layer skip flags (`skip_bootstrap`, `skip_stacks`, `skip_verify`) and
`validate_only=yes` for dry-run scope resolution. Per-host role data is passed via `add_host`
hostvars (not `hostvars['localhost']`), enabling true multi-host serial processing.
Uses `tasks/apply_role_resolve.yaml` for per-role resolution loop body.

**`dr_rebuild.yaml`** — DR rebuild / production migration. Three-play structure: Play 1 (localhost)
validates inputs, discovers per-stack backup archives and DB dumps, provisions or detects
pre-built VM, adds to `dr_target`. Play 2 (new VM) bootstraps with Docker, keepalived, and
security hardening. Play 3 (new VM) restores appdata from archives, patches SWAG configs,
deploys databases stack first (with password reset), restores SQL dumps, deploys remaining stacks,
and runs health/network/VIP verification. Supports `-e role=core|apps|dev`, with `vm_name`
defaulting to role. Test VMs override with `-e vm_name=test-vm`. Multi-role sequential execution
via `scripts/dr_rebuild_all.sh` shell wrapper (NFS ordering: core before dev).

**`build_ubuntu.yaml`** — Proxmox VM lifecycle management via cloud-init template cloning. Two
plays: Play 1 (localhost) manages VM via Proxmox API; Play 2 (new VM) bootstraps Ubuntu with
Docker, SSH hardening, UFW. Supports four `vm_state` values: `present` (default — clone template,
cloud-init config, resize disk, start), `absent` (destroy), `snapshot` (disk-only), `revert`
(rollback + restart). VM specs from `vars/vm_definitions.yaml` via `-e vm_name=<key>`.
`tasks/provision_vm.yaml` is idempotent — resumes from partial failures. CephFS caveat: PVE
snapshots only revert RBD disk, not CephFS data.

**`restore_amp.yaml`** — AMP game server instance restore. Safety-gated with `confirm=yes`. Stops
each instance, removes the existing data directory, extracts the latest archive from the
controller, and restarts instances that were running before. Supports restoring all instances
(default) or a single one via `-e amp_instance_filter=<instance>`. Accepts `-e restore_target=<host>`.
Play 1 (localhost) discovers and validates backup archives; Play 2 (the AMP host) performs the
restore. Per-instance results logged to the `restores` table; partial success supported (some
instances succeed, others fail). Uses `tasks/restore_single_amp_instance.yaml` (loop var: `_amp_instance`).
Semaphore template: `Restore — AMP [Instance]` (id=76). Required extra vars: `-e restore_target=<host> -e confirm=yes`.

**`maintain_amp.yaml`** — AMP game server maintenance: prune old version binaries, rotate backup
files, remove crash dumps, purge old logs, prune Docker images, vacuum journal. Supports
`-e amp_scope=versions|cleanup|coredumps|prune|journal` to target a single operation.

**`restore_app.yaml`** — Production single-app restore. Safety-gated with `confirm=yes`. Stops
the target stack, restores DB(s) + appdata inplace, restarts the stack, runs HTTP health checks,
and logs to the `restores` table (`restore_subtype: Appdata`). Accepts `-e restore_app=<app>` and
`-e restore_target=<host>`. Uses `tasks/restore_app_step.yaml` for per-app logic. Sends
notification on success or failure.

**`test_backup_restore.yaml`** — Automated all-app restore test on a disposable VM. Provisions a
test VM (or reuses one with `-e provision=false`), deploys all stacks, restores each `app_definitions`
app in sequence (DB + appdata inplace), runs HTTP health checks, and summarizes results via
notification. Includes OOM auto-recovery: if a restore OOM-kills the VM, saves partial results to
localhost, doubles RAM via PVE API, reboots, and retries the OOM-failed apps. Reverts the VM to
a pre-restore snapshot when done. Uses `tasks/restore_app_step.yaml` (loop var: `_test_app`).
Logs to the `maintenance` table (type: `Servers`, subtype: `Test Backup Restore`).

**`rollback_docker.yaml`** — Reverts Docker containers to their previous image versions using
the snapshot saved by `update_systems.yaml`. Two rollback paths: **fast** (old image still on
disk — `docker tag` re-tag, no network needed) and **slow** (image pruned by
`maintain_docker.yaml` — pulls old version tag from registry). Safety-gated with
`confirm=yes`. Without it, shows snapshot info and exits (dry-run). Supports three
scopes: all containers (default), per-stack via `-e stack=<name>` (or legacy `-e rollback_stack=<name>`),
or per-service via `-e rollback_service=<name>`. Docker Compose hosts (`docker_stacks`) only —
for unRAID `docker_run` hosts, see manual rollback guidance below. Uses `tasks/log_restore.yaml`
with `operation: rollback` (per-service). Notification uses yellow (16776960) to
distinguish from green/red. Supports combined recovery via `-e with_backup=yes`: restores
appdata from backup archives and auto-detects dependent databases from `app_definitions` (e.g.,
rolling back `auth` stack also restores the `authentik` database). The `with_backup` path
stops stacks, restores appdata per stack (`tasks/rollback_restore_stack.yaml`), starts the DB
stack, restores DB dumps (`tasks/rollback_restore_dbs.yaml`), then applies image rollback
(`tasks/rollback_images.yaml`) and brings all services up.

**`update_systems.yaml` — rollback snapshot:** Before pulling new Docker Compose images, the
update playbook captures a `.rollback_snapshot.json` per stack in `/opt/stacks/<stack>/` with
the timestamp, image name, full image ID (`sha256:...`), and version label for every target
service. Uses the same 3-tier label detection as the update comparison. Each update overwrites
the previous snapshot — only the last pre-update state is kept. The snapshot files are included
in regular `/opt` appdata backups automatically. Separately, `tasks/capture_image_versions.yaml`
records a `.versions.txt` manifest at **backup time** (container name, compose image ref, resolved
`sha256` digest) alongside each backup archive. The rollback snapshot captures update-time state;
the versions manifest captures backup-time state — together they provide full image provenance.

**Per-stack backup architecture:** Docker stacks hosts use per-stack backup archives instead of
a monolithic `/opt` tar.gz. Each stack is stopped individually, archived, and restarted — minimizing
downtime. Backup paths are discovered at runtime from `homelab.backup.paths` labels on containers
(stopped containers retain labels); `/opt/stacks/{name}/` (compose + .env) is always appended
automatically. Database data directories (postgres, mariadb) are intentionally excluded from
appdata archives — they do NOT carry `homelab.backup.paths` labels. Instead, databases are backed
up via dedicated SQL dump jobs (`backup_single_db.yaml`) which produce portable `.sql`/`.sql.gz`
files. This two-tier strategy means appdata archives are smaller and faster (no multi-GB data dirs),
while SQL dumps provide reliable, engine-version-independent database recovery. During restore,
database containers start with empty data dirs (postgres auto-initializes, mariadb creates defaults),
then SQL dumps are restored separately via `db_restore.yaml`.
Non-docker_stacks hosts (proxmox, pikvm, unraid, unifi) still use monolithic archives with
`backup_exclude_dirs | default([])` for hosts that define exclusions.

**Per-instance AMP backup architecture:** AMP hosts use per-instance archives (excludes `Backups/`
and `Versions/` subdirs). Loop tasks: `backup_single_amp_instance.yaml` (stop → archive → fetch →
restart) and `restore_single_amp_instance.yaml` (stop → remove → extract → restart). Both always
restart instances in `always:` block regardless of outcome.

**`tasks/backup_single_stack.yaml`** — Per-stack backup loop body called by `backup_hosts.yaml`
(loop var: `_backup_stack`). Stops the named stack via `docker_stop.yaml`, captures container image
versions via `tasks/capture_image_versions.yaml` (`.versions.txt` manifest saved alongside archive),
discovers backup paths from `homelab.backup.paths` container labels, appends `/opt/stacks/<stack>/`,
filters to paths that exist (via `stat`), creates a `.tar.gz` archive, fetches to the controller,
and records success/failure in `_stack_backup_results`. The `always:` block restarts the stack and
deletes the temp archive regardless of outcome — containers are always brought back up even when
the backup fails. Integrity verification is deferred to `verify_backups.yaml`.

**`tasks/backup_single_db.yaml`** — Per-database backup loop body called by `backup_databases.yaml`
(standalone) and `backup_hosts.yaml` (combined mode via `with_databases=yes`). Loop var:
`_current_db`. Calls `db_dump.yaml`, fetches the dump to the controller, and appends a
success/failure record to `combined_results` (keys: `db_name`, `backup_rc`, `file_size`,
`db_host`). Combined mode passes `_db_delegate_host` to run dump/stat/fetch on the DB host;
standalone mode omits it (defaults to `inventory_hostname`). Inherits engine flags (`is_postgres`,
`is_mariadb`, `is_influxdb`), credentials, and paths from the caller's scope.

**`tasks/backup_combined_db_group.yaml`** — Per-config-file DB backup helper called in a loop from
`backup_hosts.yaml` combined mode. Takes `_db_config_file` (e.g., `db_primary_mariadb`), loads the
vars file via namespaced `include_vars`, creates a run-scoped temp dir on `db_host`, loops over
`db_names` calling `backup_single_db.yaml` with `_db_delegate_host`, syncs results back to
`_db_combined_results`, and cleans up the temp dir. Adding a new DB tier requires only adding a
line to the loop list and creating the vars file with `db_host`.

**`tasks/db_dump.yaml`** — Single-database dump with engine abstraction (PostgreSQL `pg_dump`,
MariaDB `mysqldump`, InfluxDB `influxd backup -portable`). All use `{{ _gz_detect }}` compression.
MariaDB passwords passed via `MYSQL_PWD` env var, never on the command line.

**`tasks/db_restore.yaml`** — Single-database restore with engine abstraction. Optional
`_db_target_name` restores to a temp DB (for verification) without touching production.

**`tasks/capture_image_versions.yaml`** — Records container image versions (`.versions.txt`
manifest) alongside backup archives via `docker inspect`. Complements the update-time
`.rollback_snapshot.json`.

**`tasks/db_count.yaml`** — Count tables/measurements in a database for verification. Used by
`verify_backups.yaml` to confirm restored backups are non-empty.

**`tasks/db_drop_temp.yaml`** — Drop a temp database and clean up container files. Used by
`verify_backups.yaml` after verification.

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

**`tasks/assert_config_file.yaml`** — Pre-task assertion that `config_file` is defined and
non-empty. Catches misconfigured Semaphore variable groups before any work starts.

**`tasks/assert_disk_space.yaml`** — Shared pre-task assertion that a given filesystem path has
sufficient free space. Called via `include_tasks` with `vars:` block providing `assert_disk_path`
and `assert_disk_min_gb`. Uses `df --output=avail` + `ansible.builtin.assert`. Has
`check_mode: false` on the df task so it runs during `--check`.

**`tasks/assert_db_connectivity.yaml`** — Shared pre-task assertion that the MariaDB logging
database is reachable. Runs `SELECT 1` via `community.mysql.mysql_query`. Inherits `logging_db_*`
vars from playbook scope. Has `check_mode: false` so it validates connectivity during `--check`.
Used by all 26 playbooks that log to MariaDB (every playbook except `download_videos.yaml`,
`setup_ansible_user.yaml`, `setup_pve_vip.yaml`, `setup_test_network.yaml`, and `verify_isolation.yaml`).

**`maintain_health.yaml` — check notes:**
- **Host groups:** Defined by `health_check_groups` in `vars/semaphore_check.yaml` — add a group
  to include it in monitoring.
- **State management:** Last check timestamp in `health_check_state` table (single-row, survives
  container restarts). Read at Play 1 start, written at Play 3 end.
- **Security:** Semaphore API URI task has `no_log: true` to protect the API token.

<details>
<summary>Health check details (26 checks)</summary>

- `smart_health`: auto-installs `smartmontools` on ubuntu/pve/pbs; unRAID has it built-in
- `pve_cluster` / `ceph_health`: PVE nodes only, requires `become: true`
- `ssl_cert`: scans `/etc/letsencrypt/live/*/cert.pem`; hosts with no certs log ok
- `stale_maintenance`: alerts if no successful maintenance within `health_maintenance_stale_days`
- `backup_size_anomaly`: flags backups below `health_backup_size_min_pct`% of 30-day rolling avg;
  use `health_backup_size_exclude` list to suppress during architectural changes
- `mariadb_health`: connection count vs `max_connections` + crashed table scan
- `wan_connectivity`: HTTP GET to `health_wan_url`; critical on any failure
- `ntp_sync`: `timedatectl` (systemd) or `ntpq` (unRAID); warning if offset exceeds threshold
- `dns_resolution`: `getent hosts` against `health_dns_hostname`; critical on failure
- `unraid_array`: array state + `disks.ini` disabled disk count (ignores unassigned slots)
- `pbs_datastore`: `proxmox-backup-manager datastore list`; warning if no datastores
- `zfs_pool`: `zpool list` health; DEGRADED=warning, FAULTED/OFFLINE=critical; skips if no zpool
- `btrfs_health`: `btrfs device stats` on all BTRFS mounts; any non-zero error=critical
- `docker_http`: per-host HTTP endpoint checks via `docker_health_endpoints` variable
- `host_reachable`: Play 3 detects unreachable hosts (from `ignore_unreachable: true` in Play 2)

</details>

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
committed vault-encrypted as a local reference — but Semaphore reads inventories from its own
database, not from this file. To update an inventory, edit it in the Semaphore UI or via its
database.

Semaphore inventories are organized by **authentication method** — each inventory groups hosts
that share the same SSH key or login password. Within each inventory, hosts belong to
**functional groups** (`[ubuntu]`, `[pve]`, `[docker_stacks]`, etc.) that determine which
playbook logic applies. The `hosts_variable` in each template's variable group scopes the
playbook to the correct functional group.

| Inventory | Credential (Key Store) | Covers |
|-----------|----------------------|--------|
| `ansible-user-ssh` | ansible SSH key | Ubuntu, Docker, Proxmox, unRAID, controller, amp, vps; maintain_health (all SSH hosts + localhost) |
| `root` | root login_password | Synology, NAS host only |
| `pikvm` | PiKVM login_password | pikvm |
| `unifi_network` | root login_password | udmp |
| `unifi_protect` | unifi_protect login_password | unvr |
| `local` | — | localhost only (no templates currently use this inventory) |

> **Rule:** Use `ansible-user-ssh` for all recurring/scheduled templates. The `root` inventory is reserved for Synology/NAS targets only.
>
> See `future/CLAUDE_REFERENCE.md` for instance-specific Semaphore IDs (inventory, key store, template IDs).

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
`download_default`, `download_on_demand`. Database config keys (`db_primary_*`,
`db_secondary_*`) no longer need `config_file` — the resolver play handles them.

**Resolver play (database targeting):** `backup_databases.yaml`, `restore_databases.yaml`, and
`verify_backups.yaml` prepend a resolver play that runs on `localhost`. The resolver loads
`vars/{{ hosts_variable }}.yaml` and, if the vars file defines `db_host`, dynamically creates
an in-memory inventory group via `add_host`. This lets `hosts_variable=db_primary_postgres`
work even though `db_primary_postgres` is not a real inventory group. When `hosts_variable`
is a real inventory group (e.g. `docker_stacks`), the vars file does not define `db_host`
and the resolver is a no-op. A runtime assertion prevents DB config key names from colliding
with real inventory group names.

**`combined_db_configs`** (in `group_vars/all.yaml`) — list of all active DB config keys.
Used by `backup_hosts.yaml` combined mode to loop over DB tiers. Adding a new DB tier
requires only appending to this list and creating the vars file with `db_host`.

**Environment naming convention:** Semaphore environment names match the `config_file` value
(or `hosts_variable` when `config_file` is not needed). For database targets, use role-based
names (`db_primary_postgres`, `db_primary_mariadb`, `db_secondary_postgres`) — never
hostname-based names. Verify and restore templates **share** the same Semaphore environment as
backup templates for the same target — do not create separate environments.

**`hosts_variable` lives in Semaphore only** — it is resolved at `hosts:` parse time before
`vars_files` load. Any copy in a `vars/` file would be ignored for host targeting.

### Key Store

6 entries — SSH keys and login credentials attached to inventories, injected by Semaphore at
runtime. They are not Ansible variables and are not in `vars/` files. **Do not delete any.**

### Template naming convention

Semaphore template (task) names follow `Verb — Target [Subtype]`:

| Pattern | Example |
|---|---|
| `Setup — {Target} [{Subtype}]` | `Setup — Ansible User [SSH]` |
| `Backup — {Target} [{Subtype}]` | `Backup — Proxmox [Config]`, `Backup — unRAID [Offline]` |
| `Backup — Database [{Role} {Engine}]` | `Backup — Database [Primary PostgreSQL]`, `Backup — Database [Secondary PostgreSQL]` |
| `Build — {Target} [{Subtype}]` | `Build — Ubuntu [VM]`, `Build — Ubuntu [VM] (CephFS)` |
| `Deploy — {Target} [{Subtype}]` | `Deploy — Docker Stacks`, `Deploy — Grafana [Dashboard]` |
| `Download — {Target} [{Subtype}]` | `Download — Videos [Channels]`, `Download — Videos [On Demand]` |
| `Maintain — {Target} [{Subtype}]` | `Maintain — AMP [Cleanup]`, `Maintain — Docker [Cleanup]`, `Maintain — Health [Check]` |
| `Apply — {Target} [{Subtype}]` | `Apply — Role [All]`, `Apply — Role [Core]` |
| `DR — {Target} [{Subtype}]` | `DR — Rebuild [Core]`, `DR — Rebuild [All]` |
| `Restore — {Target} [{Subtype}]` | `Restore — Database [Primary PostgreSQL]`, `Restore — Docker Run [Appdata]` |
| `Rollback — {Target} [{Subtype}]` | `Rollback — Docker [Containers]` |
| `Test — {Target} [{Subtype}]` | `Test — Restore [VM]`, `Test — Backup Restore [VM]`, `Test — Restore [CephFS VM]` |
| `Update — {Target} [{Subtype}]` | `Update — Proxmox [Appliance]`, `Update — Ubuntu [OS]`, `Update — Docker Stacks [Containers]` |
| `Verify — {Target} [{Subtype}]` | `Verify — Database [Primary PostgreSQL]`, `Verify — Proxmox [Config]`, `Verify — CephFS [Mount]` |

The `[Subtype]` suffix makes templates instantly distinguishable when a target has more than one
variant (e.g., `Backup — unRAID [Config]` vs `Backup — unRAID [Offline]`, or `Download — Videos [Channels]`
vs `Download — Videos [On Demand]`). Database templates use `Database` as the target so all DB
operations cluster together alphabetically, with `[Role Engine]` (e.g., `[Primary PostgreSQL]`,
`[Secondary PostgreSQL]`) as the subtype.

Both `Build — Ubuntu [VM]` templates run the same `build_ubuntu.yaml` playbook. The base
template targets standard VMs (local RBD `/opt`), while the `(CephFS)` variant targets
CephFS-backed VMs (where `/opt` is a shared CephFS mount). The playbook adapts automatically
based on whether `cephfs_host_dir` is defined in the VM's entry in `vars/vm_definitions.yaml`.
Having separate templates lets operators rebuild CephFS-backed and standard VMs independently.

### Template views

Templates are organized into views (tabs in the Semaphore UI) by verb:

| View | Templates | Verb prefix |
|------|-----------|-------------|
| Backups | 15 | `Backup —` |
| Updates | 6 | `Update —` |
| Maintenance | 7 | `Maintain —` |
| Downloads | 2 | `Download —` |
| Verify | 11 | `Verify —` |
| Restore | 14 | `Restore —`, `Rollback —`, `Test —` |
| Deploy | 8 | `Deploy —`, `Build —`, `Apply —`, `DR —` |
| Setup | 3 | `Setup —` |

When adding a new template, assign it to the matching view. Views are stored in the
`project__view` table; templates reference views via the `view_id` column in
`project__template`.

### Managing templates and schedules

Templates and schedules can be managed via the Semaphore UI or directly via SQL (Adminer).
Every new template needs a cron schedule (or explicit documentation that it's ad-hoc only).
Always set `allow_override_args_in_task = 1` and `allow_override_branch_in_task = 1`.

> See `future/CLAUDE_REFERENCE.md` for SQL INSERT patterns, schedule management, weekly
> schedule grid, and full template listing with IDs.

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
| `with_databases=yes` | Include coordinated DB backup/restore alongside appdata (`backup_hosts`, `restore_hosts`) |
| `with_backup=yes` | Combined recovery: restore appdata + auto-detected DBs alongside image rollback (`rollback_docker`) |
| `validate_only=yes` | Render and validate only, skip `docker compose up` (`deploy_stacks`); dry-run scope resolution (`apply_role`) |
| `dr_mode=yes` | DR recovery mode — skip snapshot/revert, keep state (`test_restore`) |
| `deploy_ssh_key=yes` | Install ansible SSH private key on VM for cross-host operations (`build_ubuntu`, `apply_role`, `dr_rebuild`, `test_restore`, `test_backup_restore`); also auto-set from VM spec `deploy_ssh_key: true` |
| `debug_no_log=yes` | Reveal output normally hidden by `no_log` (any playbook; see [no_log pattern](#no_log-pattern)) |
| `resize=yes` | Enable Hardware layer — PVE API drift detection + reboot (`apply_role`; not yet implemented) |
| `pull_only=yes` | Render, validate, and pull images, but skip `docker compose up` (`deploy_stacks`) |
| `patch_swag_migration=yes` | One-time SWAG nginx config patches: old IPs → Docker DNS / VIPs (`deploy_stacks`; remove after migration) |

**Cross-cutting scope selectors (auto-resolve target host, omit for all):**
| Var | Purpose |
|-----|---------|
| `stack=<name>` | Target a single stack — backup, verify, restore, deploy, rollback (auto-resolves host from `stack_assignments`) |
| `app=<name>` | Target a single app's stack — backup, verify, restore, deploy, rollback (resolves to `stack` via `app_definitions`; valid apps: apps with DB or companion container dependencies) |
| `role=<name>` | Target a specific role — backup, verify, restore, deploy, rollback, apply_role, dr_rebuild (auto-resolves host from `host_roles`); also injects into `host_roles` for unmapped hosts (test VMs); omit for all-roles mode in `apply_role` |

**Playbook-specific scope selectors (string values, omit for default/all):**
| Var | Purpose |
|-----|---------|
| `restore_app=<name>` | Restore a single app by key |
| `restore_stack=<name>` | Alias for `stack=<name>` in restore context (backward compat) |
| `restore_db=<name>` | Restore a single database |
| `restore_date=YYYY-MM-DD` | Restore from a specific date's backup |
| `restore_target=<fqdn>` | Production host to restore on (`restore_app`, `restore_amp`) |
| `deploy_stack=<name>` | Alias for `stack=<name>` in deploy context (backward compat) |
| `rollback_stack=<name>` | Alias for `stack=<name>` in rollback context (backward compat) |
| `rollback_service=<name>` | Rollback a single service |
| `amp_instance_filter=<name>` | Target a single AMP instance by name (`backup_hosts`, `restore_amp`) |
| `vm_name=<key>` | VM definition key from `vars/vm_definitions.yaml` |
| `update_scope=os\|docker\|software` | Override auto-detected scope (`update_systems`); normally derived from `hosts_variable` |
| `amp_scope=versions\|cleanup\|coredumps\|prune\|journal` | Target a specific maintenance operation (`maintain_amp`); omit for all |
| `skip_dbs=<comma-list>` | DB names to exclude from restore (`dr_rebuild`, e.g. `-e skip_dbs=nextcloud`) |

**Skip flags (`=yes` to skip a layer, omit to include — `apply_role` only):**
| Var | Purpose |
|-----|---------|
| `skip_bootstrap=yes` | Skip OS + Network layers |
| `skip_stacks=yes` | Skip Application layer (stack deployment) |
| `skip_verify=yes` | Skip Verification layer (health, network, VIP checks) |

**Value rules:**
- Boolean triggers always use `=yes` — never `=true`, `=false`, or `=no` on the CLI
- YAML booleans inside playbook code use `true`/`false` (see [Coding conventions](#coding-conventions))
- `skip_pre_backup=yes` skips the pre-restore safety backup (default: safety dump runs)

**Per-playbook key vars (concise — see `future/CLAUDE_REFERENCE.md` for full detail):**
| Playbook | Key `-e` vars |
|----------|---------------|
| `backup_hosts` | `hosts_variable` (req), `with_databases`, `stack`, `app`, `role`, `run_tree_index`, `amp_instance_filter` |
| `backup_databases` | `hosts_variable` (req — DB config key) |
| `backup_offline` | `hosts_variable` (req) |
| `backup_offsite` | `dry_run`, `bwlimit` |
| `verify_backups` | `hosts_variable` (req), `stack`, `app`, `role`, `amp_instance_filter` |
| `restore_hosts` | `hosts_variable` (req), `confirm` (req), `stack`, `app`, `role`, `with_databases`, `restore_app`, `skip_pre_backup` |
| `restore_databases` | `hosts_variable` (req — DB config key), `confirm` (req), `restore_db`, `restore_date`, `skip_pre_backup` |
| `restore_app` | `restore_app` (req), `restore_target` (req), `confirm` (req), `skip_pre_backup` |
| `restore_amp` | `restore_target` (req), `confirm` (req), `amp_instance_filter` |
| `rollback_docker` | `hosts_variable` (req), `confirm` (req), `stack`, `app`, `role`, `rollback_service`, `with_backup`, `skip_pre_backup` |
| `deploy_stacks` | `hosts_variable` (req), `stack`, `app`, `role`, `validate_only`, `pull_only` |
| `update_systems` | `hosts_variable` (req), `amp_instance_filter` |
| `test_restore` | `role` or `source_host` (req), `vm_name`, `dr_mode`, `deploy_ssh_key`, `skip_dbs`, `stack` |
| `test_backup_restore` | `source_host` (req), `vm_name`, `test_apps`, `deploy_ssh_key` |
| `dr_rebuild` | `role` (req), `vm_name`, `deploy_ssh_key`, `skip_dbs`, `restore_app` |
| `apply_role` | `role`, `stack`, `validate_only`, `resize`, `skip_bootstrap`, `skip_stacks`, `skip_verify` |
| `build_ubuntu` | `vm_name` (req), `vm_state`, `snapshot_name`, `deploy_ssh_key` |
| `download_videos` | `hosts_variable` (req), `config_file` (req) |

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
  - name: Send notification
    include_tasks: tasks/notify.yaml
  - name: Log to MariaDB
    include_tasks: tasks/log_mariadb.yaml
```

**Backup/update playbooks**: Notifications and DB logging always fire — even on failure. Every run is recorded.

**Maintenance playbooks**: DB logging always fires. Notifications fire **only on failure** (maintenance
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
| **Backup — DB** (`backup_databases`) | `backup_url` | Date, Backup Size | Always (standalone) |
| **Backup — DB (combined)** (`backup_hosts`) | `backup_url` | Per-DB ✅/❌ fields (inline) | `with_databases=yes` |
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
> The current 51 shared task files cover notifications, logging, assertions, provisioning,
> bootstrapping, Docker management, appdata restore, health checks, per-stack/per-DB backup
> orchestration, and DB engine abstraction. Inline cleanup patterns or host-type detection logic
> may have accumulated in individual playbooks and could be worth consolidating if the same
> pattern appears more than once.

### Roles vs. flat tasks/ structure

The project uses 51 shared task files (thin glue code — notifications, DB logging, assertions,
dump/restore, test/DR pipelines, pre-restore safety) with no handlers or role-level templates. Flat `tasks/` is the right fit at this
scale. Add roles when a component needs handlers, role-level templates, Molecule testing, or
cross-project reuse.

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
results used across task boundaries (logging, notifications, conditionals) stay unprefixed:
`backup_status`, `file_size`, `unvr_backup`, `docker_update_results`. The `maintain_health.yaml`
playbook uses a `*_raw` suffix convention for shell output variables.

**Header comments:** Every playbook has a comment block after `---` describing: purpose (what
the playbook does), mechanism (how it works), and optionally usage examples and schedule notes.

### Hostname normalization

**One variable, two sources.** Every shared task that writes to the database accepts
`log_hostname` — this is the single hostname parameter for all shared DB logging tasks. Callers pass one of two values:

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
and completely under the user's control. The DB and notification output reflect exactly what is in
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
IP-based URL while notification links need the external domain:

| Variable | Source | Example | Purpose |
|---|---|---|---|
| `semaphore_url` | `semaphore_host_url` from vault (trailing slash stripped) | `http://10.0.0.1:3000` | API calls (`/api/project/...`) |
| `semaphore_ext_url` | Built from `controller_fqdn` + `domain_ext` | `https://controller.example.com` | Discord embed links, `maintenance_url` |

Both are defined in `vars/semaphore_check.yaml`. Only `maintain_health.yaml` uses both — the
API URL for the Semaphore task query and the external URL for notification task links and the
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

**Docker stop/start for backup on unRAID:** Hosts in both `docker_run` and `unraid` use
unRAID's `DockerClient.php` (Dynamix Docker Manager plugin) for per-container stop/start via the
Docker socket API (Mode 4a in `docker_stop.yaml`/`docker_start.yaml`). This keeps unRAID's
container state consistent with its web UI while preserving `backup_exclude_containers` support —
critical because Semaphore runs on the unRAID host and must stay running during backup. Containers
are enumerated via `docker ps` with the exclude filter, then stopped/started individually through
`DockerClient->stopContainer()`/`startContainer()`. Hosts in `docker_run` but NOT `unraid`
continue to use per-container `docker stop`/`docker start` with the exclusion list (Mode 4b).

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

**Running dry runs:** In Semaphore, enter `--check` in the **CLI Args** field. All
state-gathering tasks still execute, but no changes are made, no notifications fire,
and no DB logging happens. General pattern: pre-checks and queries run, mutations are
simulated, notifications/DB suppressed.

### Pre-task validations

Production playbooks include pre-task assertions to catch environmental problems early, before
any work starts. Two shared assertion task files are available:

**`tasks/assert_disk_space.yaml`** — Checks free space on a filesystem path. Caller passes
`assert_disk_path` and `assert_disk_min_gb` via `vars:`. Used by `backup_hosts.yaml` (remote
`backup_tmp_dir` + controller `/backup`), `backup_databases.yaml` (remote `backup_tmp_dir`),
`update_systems.yaml` (root filesystem `/`), `restore_databases.yaml` (`backup_tmp_dir`), and
`restore_hosts.yaml` (root filesystem `/`).

**`tasks/pre_task_assertions.yaml`** — Consolidated pre-task bundle used by 26 playbooks.
Combines the three universal pre-flight steps into a single `include_tasks` call:
1. `assert_config_file` (optional, gated by `pre_assert_config_file`)
2. `assert_db_connectivity` (always)
3. `log_run_context` (always — logs playbook name + hostname + extra vars to MariaDB)

Required var: `pre_playbook` (playbook filename for audit trail).
Optional vars: `pre_assert_config_file` (default: false), `pre_hostname` (default:
`inventory_hostname`), `pre_run_vars` (default: `{}`).

```yaml
pre_tasks:
  - name: Run standard pre-flight assertions
    include_tasks: tasks/pre_task_assertions.yaml
    vars:
      pre_assert_config_file: true          # only for playbooks that use config_file
      pre_playbook: "backup_hosts.yaml"
      pre_run_vars: "{{ {'config_file': config_file | default('')} | to_json }}"
```

**`tasks/assert_db_connectivity.yaml`** — Verifies the MariaDB logging database is reachable
via `SELECT 1`. Included by `tasks/pre_task_assertions.yaml` (26 playbooks) and directly
by lightweight plays that don't need the full bundle (e.g. `maintain_pve.yaml` health checks).
Catches MariaDB outages early — before any work starts — rather than failing silently in
the `always:` logging step.

**`tasks/resolve_scope.yaml`** — Shared scope resolution included in `pre_tasks` of
`backup_hosts.yaml`, `verify_backups.yaml`, `restore_hosts.yaml`, `deploy_stacks.yaml`,
`rollback_docker.yaml`, `apply_role.yaml`, and `test_restore.yaml`. Injects `role` into
`host_roles` for hosts not already mapped (test VMs) and sets `_role_injected=true` so templates
can derive VIPs from the VM's actual subnet. Resolves `app` → `stack` via `app_definitions`
(with assertion on invalid app names). Then applies `meta: end_host` filters for `stack`
and `role` scope selectors. Playbooks resolve backward-compatible aliases (e.g.
`deploy_stack → stack`, `restore_stack → stack`) before including this file.

**`tasks/assert_config_file.yaml`** — Asserts `config_file` is defined and non-empty, catching
misconfigured Semaphore variable groups before work starts. Now included via the
`pre_task_assertions.yaml` bundle (gated by `pre_assert_config_file: true`).

**`tasks/apply_role_resolve.yaml`** — Per-role resolution loop body for `apply_role.yaml`.
Resolves a single VM definition, displays reconciliation scope, and adds the target host to
the `apply_target` inventory group with role-specific hostvars. Skips `add_host` when
`validate_only=yes` (dry-run mode).

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
  `group_vars/pikvm.yaml` for `ansible_remote_tmp`)
- **`vars/*.yaml`** — per-platform configs loaded explicitly via `vars_files:` in playbooks; use for
  operational variables (backup paths, task names, feature flags) that vary by platform
- **`host_vars/<hostname>.yaml`** — reserved for truly host-specific overrides that don't fit a group
  pattern; currently unused (prefer group_vars for host groups)

### Image pinning convention

Docker service images generally use `latest`. Exceptions:

- **Images without a `latest` tag** (e.g., Authentik — always use versioned tags): must be pinned
- **DB images that need version stability** (e.g., PostgreSQL) may also be pinned

All pinned image:tag pairs live in `vars/container_definitions.yaml`. Compose files reference
them via env vars rendered through `env.j2`. Update them manually; do not auto-update pinned
images — they require testing before a version bump.

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

Version commands for each host type live in two play-level dicts — one for OS versions
(`_version_scope == 'os'`), one for software versions (`_version_scope == 'software'`). Each
dict key matches an inventory group name:

```yaml
vars:
  _version_scope: >-
    {{ update_scope | default(
         'software' if config_file in (_sw_version_commands | default({})).keys()
         else 'docker' if config_file in ['docker_stacks', 'docker_run']
         else 'os'
       ) }}
  _os_version_commands:
    pikvm:  "pacman -Q | grep 'kvmd ' | cut -c 6-"
    ubuntu: "uname -r | sed 's/-generic//'"
    pve:    "pveversion | awk -F'/' '{print $2}'"
    pbs:    "dpkg-query -W -f='${Version}' proxmox-backup-server"
  _sw_version_commands:
    amp:    "ampinstmgr -version | sed -n 's/.*\\(v[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\).*/\\1/p'"
```

`_version_scope` is auto-derived from `hosts_variable` (via `config_file`): `docker_stacks`/`docker_run`
→ docker, `amp` → software, everything else → os. The `-e update_scope=X` override is accepted
via the `update_scope | default(...)` wrapper for backward compatibility.

Two tasks per dict handle all host types:

```yaml
- name: Get current version
  ansible.builtin.shell: "{{ _os_version_commands[group_names | select('in', _os_version_commands) | first] }}"
  register: _version_raw_os
  changed_when: false
  when:
    - group_names | select('in', _os_version_commands) | list | length > 0
    - _version_scope == 'os'

- name: Set current_version
  ansible.builtin.set_fact:
    current_version: "{{ _version_raw_os.stdout | trim }}"
  when:
    - group_names | select('in', _os_version_commands) | list | length > 0
    - _version_scope == 'os'
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

Eight tables (seven operational + one state). All columns are set by Ansible — no DB-side triggers,
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

**Grafana panels** return raw `DATETIME` or `UNIX_TIMESTAMP()` — Grafana handles timezone
conversion via the dashboard setting (`""` = browser default).

**MariaDB timezone tables** required for `CONVERT_TZ` with named timezones. Load once:
```bash
docker exec mariadb bash -c "mariadb-tzinfo-to-sql /usr/share/zoneinfo | mariadb -u root -p'PASSWORD' mysql"
```

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
  backup_level VARCHAR(20) NOT NULL DEFAULT 'host',  -- 'host' or 'stack' — granularity of the backup
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_backup_type (backup_type),
  INDEX idx_backup_subtype (backup_subtype),
  INDEX idx_backup_level (backup_level)
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
  subtype     VARCHAR(50)  NOT NULL,   -- 'Cleanup', 'Prune', 'Cache', 'Restart', 'Maintenance', 'Health Check', 'Verify', 'Deploy', 'Build', 'Test Restore', 'Test Backup Restore', 'CephFS Migration', 'CephFS Verify'
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
holds free-text context (e.g., "sonarr restored + sonarr-log, sonarr-main DB(s) restored").
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
values, documentation, and notifications:

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
- Maintenance: `Cleanup`, `Prune`, `Cache`, `Restart`, `Maintenance`, `Health Check`, `Verify`, `Deploy`, `Build`, `Test Restore`, `Test Backup Restore`, `CephFS Migration`, `CephFS Verify`

  (`Health Check` is reserved for `maintain_health.yaml` — its subtype value regardless of how many
  checks are added to that playbook)

#### `hostname` — inventory FQDN

Always the fully qualified domain name from `inventory_hostname` (or `controller_fqdn` for
localhost plays) — exactly as defined in the Ansible inventory. Passed to shared tasks via the
unified `log_hostname` parameter. No transformation is applied. The inventory is the single
source of truth for how hostnames appear in the DB and in notifications.

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
| `backup_offsite.yaml` (inline) | B2 | Servers | Offsite |
| `vars/db_*.yaml` | (individual db name)-db | Servers | Database |

Database vars files also set `backup_ext` (`"sql.gz"` for PostgreSQL/MariaDB, `"tar.gz"` for InfluxDB)
which controls file extensions in find/copy/cleanup paths across all three database playbooks.
To restore from old `.sql` backups (pre-extension fix), pass `-e backup_ext=sql`.

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
| `maintain_docker.yaml` (Play 1) | Docker | Servers | Prune |
| `maintain_docker.yaml` (Play 2) | Ubuntu / unRAID | Servers | Cache |
| `maintain_unifi.yaml` | Unifi | Appliances | Restart |
| `maintain_amp.yaml` | AMP | Servers | Maintenance |
| `maintain_health.yaml` | Semaphore | Local | Health Check |
| `maintain_logging_db.yaml` | Logging DB | Local | Cleanup |
| `check_logging_db.yaml` | Logging DB | Local | Summary |
| `maintain_guacamole.yaml` | Guacamole | Local | Connections |
| `maintain_pve.yaml` (Play 1) | Proxmox | Appliances | Maintenance |
| `maintain_pve.yaml` (Play 3) | Proxmox | Appliances | Snapshot Check |
| `maintain_pve.yaml` (Play 4) | PBS | Appliances | Task Check |
| `deploy_stacks.yaml` | Docker | Servers | Deploy |
| `deploy_grafana.yaml` | Grafana | Local | Deploy |
| `build_ubuntu.yaml` | Ubuntu | Servers | Build |
| `test_restore.yaml` | Docker | Servers | Test Restore |
| `test_backup_restore.yaml` | Docker | Servers | Test Backup Restore |
| `apply_role.yaml` | Docker | Servers | Apply Role |
| `dr_rebuild.yaml` | Docker | Servers | DR Rebuild |
| `verify_cephfs.yaml` | (vm_name) | Servers | CephFS Verify |

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

The vault contains ~50 variables organized into groups:

- **Required** (all playbooks): Discord webhooks, logging DB credentials, domain suffixes,
  Semaphore API token/URL, controller hostname, DB host identifiers
- **Optional** (specific playbooks): download webhooks, appliance API keys, Synology NAS
  config, B2 offsite backup credentials, Grafana credentials, SSH public key, VPS hostname, trusted proxy CIDRs
- **PVE cluster**: API credentials, template node/VMID, storage pool, network bridge, VIP
  config, VRRP priorities, test VM IP pool, VM credentials (user/password/CIDR/gateway/DNS)
- **Per-VM**: `vault_vm_<name>_ip` and `vault_vm_<name>_node` for each VM in `vm_definitions.yaml`
- **VPN**: WireGuard internal subnet

> See `future/CLAUDE_REFERENCE.md` for the complete vault variable listing with comments.

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
  -e restore_app=sonarr -e with_databases=yes -e confirm=yes
```

Always use `--limit <hostname>` with `-e restore_app` since `docker_stacks`/`docker_run` are
multi-host groups. `app_definitions` in `vars/app_definitions.yaml` provides the stack name (docker_stacks
apps), container list + `db_host` (docker_run apps), and DB names for both. DB config and health
URLs are discovered from container labels at runtime.

### Roll back a Docker container update

```bash
# Dry run — show snapshot info without rolling back
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname>

# Rollback a single stack (image-only — fast, data untouched)
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks \
  -e stack=auth -e confirm=yes

# Rollback a single service
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e rollback_service=jellyseerr -e confirm=yes

# Combined recovery — rollback images + restore appdata + auto-detected databases
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks \
  -e stack=auth -e with_backup=yes -e confirm=yes

# Full role rollback with backup (databases stack included naturally)
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks \
  -e role=core -e with_backup=yes -e confirm=yes

# Image-only rollback for all containers in the snapshot
ansible-playbook rollback_docker.yaml -e hosts_variable=docker_stacks --limit <hostname> \
  -e confirm=yes
```

The rollback snapshot (`.rollback_snapshot.json`) is saved automatically before each Docker
Compose update. If the old image is still on disk, rollback is instant (local re-tag). If it
was pruned by `maintain_docker.yaml`, the old version is pulled from the registry.

**Combined recovery (`with_backup=yes`):** When a bad upstream update also corrupted data or
ran irreversible DB migrations, use `-e with_backup=yes` to restore appdata and databases
alongside the image rollback. DB dependencies are auto-detected from `app_definitions` in
`vars/docker_stacks.yaml` — e.g., rolling back `auth` stack auto-restores the `authentik`
database from its latest SQL dump. The flow: stop stacks → restore appdata per stack → start
DB containers → restore SQL dumps → apply image rollback → bring all services up.

**For unRAID `docker_run` hosts** (no automated rollback):
1. `docker stop <container>` and `docker rm <container>`
2. Find old image ID from the snapshot or `updates` table
3. `docker tag <old_image_id> <image_name>` (if image still local)
4. Recreate via the unRAID Docker UI (uses the saved template XML)
5. If image was pruned: restore from backup via `restore_hosts.yaml -e hosts_variable=docker_run`

### Manage PVE resource pools

Pool membership is derived at runtime from each VM's live `net0` VLAN tag — no VMID lists to maintain.

| Pool | Signal | Purpose |
|------|--------|---------|
| `production` | `net0` defined, no `tag=` | Target of PBS backup jobs |
| `hosted` | `net0` with `tag=1682` | Friend VMs — excluded from PBS |
| `test` | `net0` with `tag=<vault_test_vlan_id>` | Ephemeral test VMs — excluded from PBS |

**Sync pools:** Run `maintain_pve.yaml` (Play 5 "Manage PVE resource pools"). On each run it:
1. Creates any missing pools (idempotent)
2. Adds VMs to their pool based on VLAN tag (additive — never removes)
3. Logs to MariaDB; alert on failure

**To add a hosted friend VM:** Set `net0` VLAN tag to 1682 in PVE, then run `maintain_pve.yaml`. Done.

**One-time PBS setup (after first pool run):** Datacenter → Backup → edit each backup job → set **Pool** = `production`. Backup jobs then only target production VMs.

**Test VMs in pool at creation:** `tasks/provision_vm.yaml` adds test VMs to the `test` pool immediately after provisioning (guarded by `when: pve_pool_test is defined` — no-op when `vars/proxmox.yaml` is not loaded).

### Useful DB queries

> See `future/CLAUDE_REFERENCE.md` for diagnostic SQL queries against both `ansible_logging`
> and `semaphore` databases.

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
- **Pre-restore safety backup**: Before restoring databases, the current state is dumped to
  `<tmp_dir>/pre_restore_<db>_<date>.<ext>` as a safety net. Shared via
  `tasks/pre_restore_safety_dump.yaml`. Used in: `restore_databases.yaml`, `restore_app.yaml`,
  `restore_hosts.yaml` (with_databases path), `rollback_docker.yaml` (with_backup path).
  Skip with `-e skip_pre_backup=yes` (default: safety dump runs). The destination directory
  is `backup_tmp_dir` from the platform vars (`/mnt/user/Backup/ansibletemp/` on unRAID,
  `/tmp/backup` on AMP); `restore_app.yaml` uses a run-scoped temp dir instead.

### Backup filename convention

All backup archives follow the pattern `backup_<identifier>_<date>.<ext>`, where `<date>` is
`{{ ansible_date_time.date }}` (ISO 8601: `YYYY-MM-DD`).

| Backup type | Identifier | Extension | Example | Defined in |
|---|---|---|---|---|
| PostgreSQL / MariaDB DB | `<dbname>` | `.sql` | `backup_authentik_2026-02-25.sql` | `vars/db_primary_*.yaml`, `tasks/backup_single_db.yaml` |
| InfluxDB | `<dbname>` | `.tar.gz` | `backup_telegraf_2026-02-25.tar.gz` | `vars/db_primary_influxdb.yaml`, `tasks/backup_single_db.yaml` |
| Docker stack | `<stackname>` | `.tar.gz` | `backup_auth_2026-02-25.tar.gz` | `tasks/backup_single_stack.yaml` |
| Docker appdata (monolithic) | `docker_appdata` | `.tar.gz` | `backup_docker_appdata_2026-02-25.tar.gz` | `vars/docker_run.yaml` |
| AMP instance | `amp_<instance>` | `.tar.gz` | `backup_amp_Minecraft01_2026-02-25.tar.gz` | `tasks/backup_single_amp_instance.yaml` |
| Proxmox / PBS config | `<hostname>_config` | `.tar.gz` | `backup_pve01_config_2026-02-25.tar.gz` | `vars/proxmox.yaml` |
| PiKVM config | `<hostname>_config` | `.tar.gz` | `backup_pikvm01_config_2026-02-25.tar.gz` | `vars/pikvm.yaml` |
| unRAID boot | `<hostname>_boot` | `.tar.gz` | `backup_unraid_boot_2026-02-25.tar.gz` | `vars/unraid_os.yaml` |
| Unifi Network config | `network_config` | `.tar.gz` | `backup_network_config_2026-02-25.tar.gz` | `vars/unifi_network.yaml` |
| Unifi Protect config | `protect_config` | `.unf` | `backup_protect_config_2026-02-25.unf` | `vars/unifi_protect.yaml` |
| Image versions manifest | `<stackname>` or `<hostname>` | `.versions.txt` | `backup_auth_2026-02-25.versions.txt` | `tasks/capture_image_versions.yaml` |

On failure, the MariaDB log column records `FAILED_` prefixed to the filename (e.g. `FAILED_backup_auth_2026-02-25.tar.gz`).
Archives are stored on the controller at `{{ backup_base_dir }}/{{ inventory_hostname }}/<filename>`.

### Backup integrity verification

All integrity validation is centralized in `verify_backups.yaml` — backup playbooks do **not**
perform inline integrity checks (avoids duplicate work and speeds up backup runs). Archives are
date-stamped so a corrupt backup does not overwrite the previous day's good copy.

All compression and decompression uses `{{ _gz_detect }}` from `group_vars/all.yaml`, which
detects `pigz` (parallel gzip) at runtime with `gzip` fallback.

- **Gzip archives** (`verify_backups.yaml`): `$_gz -t` validates archive integrity; extraction
  via `tar -I "$_gz" -xf` to staging directory; file counts verify non-empty content.
  UNVR `.unf` files (not gzipped) are skipped.
- **PostgreSQL/MariaDB dumps** (`verify_backups.yaml`): restored to a temp database via
  `db_restore.yaml`, table counts verified via `db_count.yaml`, temp database dropped.
- **InfluxDB backups** (`verify_backups.yaml`): restored to a temp database, measurement
  counts verified, temp database dropped.

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
ansible-playbook deploy_stacks.yaml --limit <controller-fqdn> -e stack=databases --ask-vault-pass

# 3. Restore MariaDB from backup (restores semaphore + ansible_logging databases)
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_mariadb -e confirm=yes --ask-vault-pass

# 4. Restore Postgres from backup
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_postgres -e confirm=yes --ask-vault-pass

# 5. Deploy remaining stacks (Semaphore is now functional via MariaDB)
ansible-playbook deploy_stacks.yaml --limit <controller-fqdn> --ask-vault-pass

# 6. Restore appdata
ansible-playbook restore_hosts.yaml -e hosts_variable=docker_stacks --limit <controller-fqdn> -e confirm=yes --ask-vault-pass

# 7. Re-render .env files (restore overwrites them with backup copies) and restart stacks
ansible-playbook deploy_stacks.yaml --limit <controller-fqdn> --ask-vault-pass

# 8. Semaphore is back — use UI for remaining hosts
```

If local backups in `/backup/` are lost, restore from B2 first:

```bash
# Restore all backups from B2 (set RCLONE_B2_ACCOUNT and RCLONE_B2_KEY env vars)
rclone sync :b2:<bucket-name> /backup/ --transfers 4 --progress
```

**Prerequisites:**
- A working machine with Ansible installed and the repo cloned
- The vault password (stored securely outside the infrastructure)
- SSH access to Proxmox API host (for VM creation)
- Network connectivity to the target subnet

### Production VM Recovery (Semaphore Available)

When Semaphore is still running (non-controller host failed, or building a new production VM):

```bash
# 1. Provision a new VM
ansible-playbook build_ubuntu.yaml -e vm_name=<hostname>

# 2. Restore all stacks in the role from backup
ansible-playbook restore_hosts.yaml -e hosts_variable=docker_stacks -e role=core -e confirm=yes

# 3. Deploy stacks (re-renders .env files, pulls images, brings services up)
ansible-playbook deploy_stacks.yaml -e role=core
```

For a single stack: replace `-e role=core` with `-e stack=auth` (auto-resolves host from
`stack_assignments`). Database stacks restore empty and get populated separately via
`restore_databases.yaml` if needed.

### Automation Coverage

| Step | Automated? | Notes |
|------|------------|-------|
| VM creation + OS | Yes | `build_ubuntu.yaml` |
| Docker + security hardening | Yes | `build_ubuntu.yaml` bootstrap |
| NFS mounts + UFW rules | Yes | `vm_definitions.yaml` → `bootstrap_vm.yaml` |
| Stack deployment | Yes | `deploy_stacks.yaml` |
| Database restore | Yes | `restore_databases.yaml` |
| Appdata restore | Yes | `restore_hosts.yaml` |
| Idempotent VM reconciliation | Yes | `apply_role.yaml` — all layers in one command; all-roles or single |
| Full DR rebuild | Yes | `dr_rebuild.yaml` — provision + bootstrap + restore + deploy; `scripts/dr_rebuild_all.sh` for multi-role |
| PVE node OS config (keepalived, ansible user, SSH) | Yes | `maintain_pve.yaml` — idempotent; also restores ansible user + SSH hardening |
| PVE cluster VIP only (no logging) | Yes | `setup_pve_vip.yaml` — lightweight alternative when MariaDB not yet available |
| DNS records (internal) | No | DHCP reservation with hostname — network layer |
| DNS records (external) | No | Static IP, managed by `cloudflareddns` container |
| Offsite backup (B2) | Yes | `backup_offsite.yaml` — rclone sync to Backblaze B2; restore via `rclone sync :b2:<bucket> /backup/` |
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
        └── lo aliases: vault_vm_core_ip, vault_vm_apps_ip, vault_vm_dev_ip
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
`/etc/netplan/60-loopback-aliases.yaml`). Restored containers reaching prod IPs hit the
loopback alias and connect to local Docker services instead. Baked into `pre-test-restore`
snapshot — survives reboots.

### Test Restore (automated)

`test_restore.yaml` and `test_backup_restore.yaml` use **ephemeral**
VM slots drawn from the same shared pool defined in `vars/vm_definitions.yaml`. Pool size and base
values are controlled entirely by two vars:

- `vm_test_slot_base` — base VMID; pool spans `vm_test_slot_base .. vm_test_slot_base + vm_test_slot_count - 1`
- `vault_test_vm_ip_offset` (in `vars/secrets.yaml`) — last-octet of the first slot IP; combined with `vault_test_vm_ip_prefix`

**`vm_index` is auto-detected** via `tasks/resolve_test_vm_index.yaml` — queries the PVE cluster
API and picks the lowest slot (0–`vm_test_slot_count - 1`) whose VMID is either absent or stopped.
A running VM is considered in use. Pass `vm_index=N` explicitly to force a specific slot (e.g. for
parallel test runs). `vm_index` drives VMID, IP, hostname, and name consistently.

**Convention — all ephemeral/test VMs use the same pool and derive VMID and IP from `vm_index`**,
never hardcode numeric IDs:

| VM definition | VMID expression | IP last-octet expression | Notes |
|---|---|---|---|
| `test-vm` | `vm_test_slot_base + vm_index` | `vault_test_vm_ip_offset + vm_index` | isolated VLAN; `ip_aliases` for container prod-IP routing |
| `cephfs-migrate-test` | `vm_test_slot_base + vm_index` | `vault_test_vm_ip_offset + vm_index` | same isolated VLAN; CephFS monitor access via "Allow Test to Ceph" zone policy (ports 3300/6789/6800-7300) |

All VMs in the pool share the same VMID and IP expressions. Definitions differ only in optional fields
(`vm_vlan_tag`, `vm_gateway`, `vm_dns`, `ip_aliases`, `cephfs_host_dir`). Never use a literal VMID or IP number.

The playbook:
1. Provisions the VM if it doesn't exist (idempotent — resumes if VMID already exists from a prior partial run)
2. Snapshots it (`pre-test-restore`), runs the restore, then reverts — leaving the VM ready for the next test.
   For CephFS-backed VMs (`cephfs-migrate-test`), revert restores OS/Docker state but CephFS data
   persists — test playbooks explicitly clean CephFS appdata before each run to replace the
   clean-slate behavior that PVE revert provides for local-`/opt` VMs (`test-vm`).
3. In `dr_mode=yes` mode, keeps the restored state (no revert) for real DR recovery

**Do not use permanent VM keys** (`core`, `apps`, `amp`) with `test_restore.yaml` — those
are production VMs. Only `test-vm` (or a custom ephemeral key) is appropriate.

```bash
# Test a role's restore cycle (vm_name defaults to test-vm)
ansible-playbook test_restore.yaml -e role=core

# DR mode — keeps restored state (no revert)
ansible-playbook test_restore.yaml -e role=core -e dr_mode=yes

# All app_definitions — per-app DB+appdata restore + health check
ansible-playbook test_backup_restore.yaml -e source_host=<fqdn>
```

**Per-stack health check timeouts** — Timeouts are discovered at runtime from `homelab.health_timeout`
labels on running containers. `tasks/verify_docker_health.yaml` takes the maximum value across all
running containers; the default fallback is 120 s. Services with slow startup (e.g. Authentik at
420 s, databases at 180 s) set their own label value — no central config required.

**OOM auto-recovery** — `test_backup_restore.yaml` detects OOM kill events (via `dmesg`) in its rescue
block. If any app fails due to OOM, after the full app loop it doubles VM memory via the PVE API,
reboots the VM, and retries only the OOM-failed apps with the new memory ceiling.

---

## CephFS (Test VMs Only)

CephFS is used **only for test VMs** (`cephfs-migrate-test`). Production VMs use local Ceph RBD
disk — CephFS proved too slow for production workloads. Dev VM uses NFS mounts from core/apps
for code-server workspace access.

### Storage Architecture

| VM type | `/opt` storage | Notes |
|---------|---------------|-------|
| Production (core, apps, dev) | Ceph RBD disk | Standard local storage |
| Dev VM extras | NFS from core/apps (`/mnt/nfs/core`, `/mnt/nfs/apps`) | Read-only workspace access |
| `cephfs-migrate-test` | CephFS mount | Test-only; `cephfs_host_dir` defined in `vm_definitions` |
| `test-vm`, `amp`, `desktop` | Ceph RBD disk | No CephFS (snapshot revert / I/O sensitivity) |

**Vault variables** for CephFS test VMs: `vault_ceph_mons` (monitor IPs) and
`vault_ceph_vm_appdata_key` (client key).

### How It Works

- `bootstrap_vm.yaml` auto-mounts CephFS at `/opt` when `_vm.cephfs_host_dir` is defined
- `test_restore.yaml -e vm_name=cephfs-migrate-test` validates backup/restore on CephFS
  - Auto-creates CephFS dir, wipes appdata before each run, restores archives onto CephFS
- `verify_cephfs.yaml` checks mount status, read/write, and logs to MariaDB + sends notification
- If CephFS goes down, test VMs hang at boot; production VMs are unaffected
