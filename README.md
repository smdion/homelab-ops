# Ansible Home Lab Automation

Ansible playbooks for home lab backup, verification, restore, health monitoring, updates,
maintenance, deployment, and provisioning — orchestrated via
[Semaphore](https://semaphoreui.com/), logged to MariaDB, alerted via Discord or Apprise, and visualized in Grafana.

> **Note:** This project was built for my own home lab. I've made it as portable as possible —
> deployment-specific values live in vault-encrypted vars files, and playbooks are
> platform-conditional — but it reflects the needs and topology of one particular setup. Use it
> as a working reference, adapt what fits, and skip what doesn't.

## What it does

| Category | What it does |
|---|---|
| **Backup** | Config/appdata archives and database dumps, with offline rsync to NAS and offsite sync to B2 |
| **Verify** | Restores each database to a temp instance and validates config archives |
| **Restore** | Safety-gated database and appdata restore with pre-restore snapshots |
| **Rollback** | Revert Docker containers to previous image versions |
| **Health** | 31 scheduled checks — disk, memory, CPU, Docker, SSL, ZFS, BTRFS, SMART, NTP, DNS, plus platform-specific |
| **Updates** | OS package and Docker container updates with version tracking |
| **Maintenance** | Docker pruning, cache clearing, Semaphore cleanup, service restarts |
| **Deploy** | Docker stacks from Git — `.env` templating, compose validation, dependency-ordered start |
| **Build** | Provision Ubuntu VMs on Proxmox — cloud-init, Docker, SSH hardening, UFW |
| **DR** | Full disaster recovery rebuild + end-to-end restore testing on disposable VMs |

Every run logs a structured record to MariaDB. The included Grafana dashboard shows backup
history, version status, stale detection, health trends, and maintenance logs.

## Quick start

```bash
# 1. Clone and install dependencies
git clone <this-repo> && cd homelab-ops
pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml

# 2. Set up vault
cp vars/secrets.yaml.example vars/secrets.yaml
ansible-vault encrypt vars/secrets.yaml    # set a password, then edit with your values

# 3. Create the database
mysql -u root -p < sql/init.sql

# 4. Set up inventory
cp inventory.example.yaml inventory.yaml   # or configure in Semaphore UI

# 5. Run a backup
ansible-playbook backup_hosts.yaml \
  -i inventory.yaml \
  -e hosts_variable=docker_stacks \
  --vault-password-file ~/.vault_pass
```

Once that works, add more platforms by creating `vars/configs/` files and inventory groups.

## Stack

| Component | Required? | Purpose |
|---|---|---|
| **Ansible** >= 2.14 | Yes | Automation engine |
| **Python** >= 3.9 | Yes | Controller (PyMySQL, proxmoxer) |
| **MariaDB** >= 10.5 | Yes | Logging database (`ansible_logging`) |
| **Semaphore** | No | Scheduling UI — [CLI works too](#running-playbooks) |
| **Grafana** | No | Dashboard (data is in MariaDB regardless) |
| **Discord / Apprise** | No | Notifications (silently skipped if not configured) |

## Health checks

`maintain_health.yaml` runs checks conditionally based on inventory group membership. Checks
for platforms you don't have are automatically skipped.

<!-- BEGIN AUTO-GENERATED: readme-health-checks -->
| Check | Runs on | What it checks |
|---|---|---|
| `disk_space` | All SSH hosts | Filesystem usage (warning/critical thresholds) |
| `memory` | All SSH hosts | RAM usage percentage |
| `cpu_load` | All SSH hosts | 5-minute load average vs. vCPU count |
| `journal_errors` | All SSH hosts | Systemd journal errors since last check |
| `oom_kills` | All SSH hosts | Out-of-memory kills in dmesg |
| `docker_health` | All SSH hosts | Unhealthy Docker containers |
| `smart_health` | All SSH hosts | SMART disk status (auto-installs `smartmontools`) |
| `ssl_cert` | All SSH hosts | Let's Encrypt certificate expiry |
| `zfs_pool` | All SSH hosts | ZFS pool health (skips if no ZFS) |
| `btrfs_health` | All SSH hosts | BTRFS device error counters (skips if no BTRFS) |
| `ntp_sync` | All SSH hosts | Time synchronization status |
| `dns_resolution` | All SSH hosts | DNS resolver working |
| `docker_http` | Configured hosts | HTTP endpoint checks for Docker containers |
| `opt_orphan_entries` | `[docker_stacks]` only | Unexpected files/directories in `/opt` |
| Docker image updates | All SSH hosts | A version-pinned container has a newer release upstream (see below) |
| `pve_cluster` | `[pve]` only | Proxmox cluster quorum |
| `ceph_health` | `[pve]` only | Ceph cluster status |
| `unraid_array` | `[unraid]` only | Array state + disabled/missing disks |
| `pbs_datastore` | `[pbs]` only | PBS datastore accessibility |
| `semaphore_tasks` | localhost | Failed Semaphore tasks since last check |
| `stale_backup` | localhost | Hosts with no backup in 10+ days |
| `backup_size_anomaly` | localhost | Backups significantly smaller than 30-day average |
| `failed_maintenance` | localhost | Failed maintenance runs since last check |
| `stale_maintenance` | localhost | Hosts with no maintenance in 3+ days |
| `mariadb_health` | localhost | Connection count + crashed tables |
| `wan_connectivity` | localhost | Outbound internet check |
| `appliance_reachable` | localhost | TCP connectivity to network appliances (PiKVM, UDMP, UNVR) |
| `schedule_coverage` | localhost | Templates missing active Semaphore schedules |
| `unifi_device_health` | localhost | UniFi device status (disconnected/upgradable) via API |
| `host_reachable` | Aggregated | Detects hosts unreachable during SSH checks |
<!-- END AUTO-GENERATED: readme-health-checks -->

## Running playbooks

Every playbook can be run directly with `ansible-playbook` or through Semaphore. Pass
`hosts_variable` as an extra var — this routes the playbook to the right inventory group.

```bash
ansible-playbook <playbook>.yaml \
  -i inventory.yaml \
  -e hosts_variable=<group> \
  --vault-password-file ~/.vault_pass
```

All boolean extra vars use `=yes` (not `=true`). Destructive operations require `confirm=yes`.

### Common examples

```bash
# Backup Docker hosts
ansible-playbook backup_hosts.yaml -e hosts_variable=docker_stacks

# Backup databases
ansible-playbook backup_databases.yaml -e hosts_variable=db_primary_postgres

# Update all Ubuntu hosts
ansible-playbook update_systems.yaml -e hosts_variable=ubuntu -e config_file=ubuntu_os

# Run health checks
ansible-playbook maintain_health.yaml -e hosts_variable=semaphore_check

# Deploy stacks to a single role
ansible-playbook deploy_stacks.yaml -e hosts_variable=docker_stacks -e role=core

# Restore a database (safety-gated)
ansible-playbook restore_databases.yaml -e hosts_variable=db_primary_postgres -e confirm=yes

# Verify backups
ansible-playbook verify_backups.yaml -e hosts_variable=db_primary_postgres
```

### Playbooks by category

| Category | Playbooks | Key extra vars |
|---|---|---|
| **Backup** | `backup_hosts`, `backup_databases`, `backup_offline`, `backup_offsite` | `hosts_variable` |
| **Verify** | `verify_backups` | `hosts_variable` |
| **Restore** | `restore_hosts`, `restore_databases`, `restore_amp`, `restore_app` | `confirm=yes`, `restore_app=`, `stack=` |
| **Rollback** | `rollback_docker` | `confirm=yes`, `stack=`, `with_backup=yes` |
| **Update** | `update_systems` | `hosts_variable`, `config_file` |
| **Health** | `maintain_health`, `check_logging_db` | `hosts_variable=semaphore_check` |
| **Maintenance** | `maintain_docker`, `maintain_semaphore`, `maintain_logging_db`, `maintain_pve`, `maintain_amp`, `maintain_unifi`, `maintain_guacamole` | `hosts_variable` |
| **Deploy** | `deploy_stacks`, `deploy_grafana`, `deploy_docs` | `role=`, `stack=` |
| **Build** | `build_ubuntu`, `bootstrap_amp` | `confirm=yes`, `vm_state=` |
| **DR/Test** | `dr_rebuild`, `test_restore`, `test_backup_restore` | `confirm=yes`, `role=`, `dr_mode=yes` |
| **Setup** | `setup_ansible_user`, `setup_pve_vip`, `setup_test_network` | One-time, platform-specific |
| **Utility** | `manage_vault`, `review_pve`, `cleanup_test_vms`, `reip_vmid` | `action=`, `confirm=yes` |

See the [extra vars reference](https://homelab-docs/playbooks/extra-vars/) for the full list
and the [playbook docs](https://homelab-docs/playbooks/) for detailed per-playbook documentation.

## Configuration reference

| File | Purpose |
|---|---|
| `vars/secrets.yaml.example` | All vault keys with descriptions |
| `vars/example.yaml` | All vars file keys with descriptions |
| `vars/configs/semaphore_check.yaml` | Health check thresholds (all tunable) |
| `inventory.example.yaml` | Expected inventory group structure |

## Documentation

Full documentation — architecture, patterns, playbook details, database schema, runbooks, and
design decisions — lives on the [docs site](https://homelab-docs/).

- [Quick Reference](docs/quick-reference.md) — all templates at a glance with their extra variables *(auto-generated)*

Key pages:

- [Playbook patterns](https://homelab-docs/architecture/playbook-patterns/) — block/rescue/always, variable loading, error handling
- [File structure](https://homelab-docs/architecture/file-structure/) — repo layout and conventions
- [Database schema](https://homelab-docs/database/schema/) — `ansible_logging` tables
- [Extra vars](https://homelab-docs/playbooks/extra-vars/) — full reference for all `-e` variables
- [Backup & restore](https://homelab-docs/runbooks/backup-restore/) — operational procedures
- [Notifications](https://homelab-docs/architecture/notifications/) — Discord, Apprise, Uptime Kuma

## License

MIT — see [LICENSE](LICENSE)
