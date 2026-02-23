# Ongoing To-Do

Tasks that aren't blockers but should be completed when time allows.

## From Audit Remediation (Feb 2026)

- [ ] **REL-3: Uptime Kuma push monitor** — Set up a Push monitor in Uptime Kuma, set heartbeat interval to match health check schedule (e.g., every 30 min with 60 min grace). Copy the push URL to vault as `uptime_kuma_push_url`. The health playbook task is already in place and will activate once the variable is defined.

## Phase 3: Full Infrastructure as Code

Full design in `future/PHASE3_DESIGN.md`. Structure-IaC for all Docker Compose hosts — every piece
of infrastructure reproducible from Git + vault + backups.

### Core playbooks

- [x] **IAC-1: Vault-encrypt inventory** — Encrypt and commit `inventory.yaml`, remove from `.gitignore`
- [x] **IAC-3: `deploy_stacks.yaml`** — Docker stack deployment from Git with vault-templated `.env` files, 9 functional stacks, dependency-ordered deploys
- [x] **IAC-2: `build_ubuntu.yaml`** — Proxmox VM provisioning via cloud-init template cloning, Docker bootstrap, SSH hardening, snapshot/revert support
- [x] **`deploy_grafana.yaml`** — Grafana dashboard + Ansible-Logging datasource deployment via API
- [x] **Phase D-pre: Dual-mode playbook updates** — `backup_hosts`, `update_systems`, `restore_hosts`, `rollback_docker` updated for both legacy and stack modes

### Host migration

- [x] **Phase A: VPS** — Single `vpn` stack, self-contained
- [x] **Phase B: Odyssey** — `infra`, `media`, `apps`, `nfs` stacks
- [x] **Phase C: Tantive-iv** — `infra`, `databases`, `auth`, `monitoring`, `dev` stacks

### Shared task extraction + operative fixes

Extract composable building blocks from existing playbooks into `tasks/` (design in
`future/PHASE3_DESIGN.md`). Combine with operative logic fixes from
`future/operative-logic-review.md` that touch the same code — fix the foundation while
refactoring it.

#### New task files

- [ ] **`tasks/provision_vm.yaml`** — Extract VM creation from `build_ubuntu.yaml` Play 1
- [ ] **`tasks/bootstrap_vm.yaml`** — Extract Ubuntu bootstrap from `build_ubuntu.yaml` Play 2. Includes NFS mount automation (per-host `/etc/fstab` entries) and per-host UFW rules — added during extraction since we're already refactoring this code
- [ ] **`tasks/restore_appdata.yaml`** — Extract backup archive copy + extract from `restore_hosts.yaml`
- [ ] **`tasks/verify_docker_health.yaml`** — New: poll Docker container health (unhealthy filter, configurable timeout)

> **Note on DB extractions:** The operative review (H2) identifies ~200 lines of DB engine branching
> duplicated across 5 playbooks (`db_dump`, `db_restore`, `db_count`, `db_drop_temp`). These are
> deferred to Phase D-post because they'll be touched during per-stack backup/restore surgery.
> There is no separate `restore_config` extraction — config files live inside appdata archives,
> and `.env` re-rendering after restore is handled by `deploy_stacks.yaml`.

#### Additional extractions (from operative review H1, H5)

- [ ] **`tasks/docker_stop.yaml` + `tasks/docker_start.yaml`** — Extract tri-modal Docker stop/start/safety-net from `backup_hosts.yaml` + `restore_hosts.yaml` (H1: ~200 lines duplicated across both, 3 modes × 3 phases each)
- [ ] **`tasks/ssh_hardening.yaml`** — Extract SSH hardening + passwordless sudo from `build_ubuntu.yaml` + `add_ansible_user.yaml` (H5: ~60 lines duplicated; also fixes L1 service name inconsistency)

#### Playbook refactors

- [ ] **Refactor `build_ubuntu.yaml`** — Use `provision_vm` + `bootstrap_vm` + `ssh_hardening` task files
- [ ] **Refactor `restore_hosts.yaml`** — Use `restore_appdata` + `docker_stop`/`docker_start` task files

#### Operative fixes during extraction

Fix while touching the same code. Full details in `future/operative-logic-review.md`.

- [ ] **C1: `restore_hosts.yaml` safety-net gate** — Add `manage_docker` guard to rescue safety-net tasks (fix during `docker_stop`/`docker_start` extraction)
- [ ] **M6: `deploy_single_stack.yaml` atomic deploy** — Write compose file to temp location, validate, then move into place (prevents broken files on disk after validation failure)

### Automated restore testing + DR mode

Full design in `future/PHASE3_DESIGN.md`.

- [ ] **`test_restore.yaml`** — Fully automated restore testing on disposable VM: build VM if needed, snapshot, restore appdata, deploy stacks, verify container health, revert to clean state. Works with any host in `stack_assignments`. Supports DR mode (`-e dr_mode=true`) — skips snapshot/revert, keeps the restored state for production use. See DR runbook in PHASE3_DESIGN.md for full rebuild path
- [ ] **Semaphore template: `Test — Restore [VM]`** — Restore view, `blank` environment, SQL in plan file
- [ ] **Update DESIGN.md + README.md** — Add `test_restore.yaml` description, expand shared tasks philosophy, update task file list, add "Test Restore" to categorization, fix stale references (see below). Done as final step of implementation

### Standalone operative fixes

From `future/operative-logic-review.md`. Independent of extraction work.

#### Critical

- [x] **C2: `backup_hosts.yaml`** — Prefix failed DB entries with `FAILED_` (Batch 5)
- [x] **C3: `backup_databases.yaml`** — Document `backup_file` `{{ item }}` requirement (Batch 5)
- [x] **C4: `restore_databases.yaml`** — Document multi-DB partial restore limitation (Batch 5)

#### Quick wins (batch together)

- [x] **L2: `rollback_docker.yaml`** — Fix `vars_files` order (Batch 5)
- [x] **L3: `rollback_docker.yaml`** — Add `assert_db_connectivity` pre-task (Batch 5)
- [x] **L4: `deploy_grafana.yaml`** — Add `assert_db_connectivity` pre-task (Batch 5)
- [x] **L5: `tasks/log_health_check.yaml`** — Documented as unused utility (Batch 5)
- [ ] **L6: Discord colors** — Extract magic numbers (32768, 16711680, etc.) to named vars in `group_vars/all.yaml`
- [ ] **M1: `verify_backups.yaml`** — Compute `_is_db_verify` fact once instead of repeating 12+ `is_postgres or is_mariadb or is_influxdb` conditions
- [x] **M2: `backup_offline.yaml`** — Add MariaDB log entry for pre-sync failures (Batch 5)

#### Medium effort

- [x] **M3: `rollback_docker.yaml`** — Fix registry pull to use `.image` directly (Batch 5)
- [ ] **M4: `backup_databases.yaml`** — Isolate per-DB failures (currently failed DB mid-loop has no Discord/DB entry)
- [ ] **M5: `update_systems.yaml`** — Detect failed `docker compose pull` (currently swallowed by shell script, reports "0 updates" on network failure)

### DESIGN.md stale references

Fix as part of the implementation work (final step of each batch), not as separate tasks.

- [ ] `controller_fqdn`/`semaphore_ext_url` source — says `vars/semaphore_check.yaml`, actually `group_vars/all.yaml`
- [ ] Discord notification table — add `deploy_stacks` and `build_ubuntu` rows
- [ ] Shared task review callout — include `deploy_single_stack.yaml`, update count from 8 to current
- [ ] Roles vs flat tasks section — update shared task file count to match actual

### Phase D-post (after migration stable)

- [ ] **Per-stack backup archives** — Replace monolithic `/opt` tar.gz with per-stack archives in `backup_hosts.yaml`
- [ ] **Per-stack restore** — Accept per-stack archive selection in `restore_hosts.yaml`
- [x] **Per-stack updates** — Per-stack pull/up loop + per-stack rollback snapshots in `update_systems.yaml` (completed during D-pre/migration)
- [x] **Per-stack rollback** — Accept `-e rollback_stack=<name>` in `rollback_docker.yaml` (completed during D-pre/migration)
- [ ] **Per-stack verify** — Loop over per-stack archives in `verify_backups.yaml`
- [ ] **Remove legacy vars** — Remove `compose_project_path` and `src_raw_files` from `vars/docker_stacks.yaml`
- [ ] **Port conflict detection** — Parse compose files for all stacks on a host, fail on duplicate ports
- [ ] **Stack-driven `app_restore`** — Derive container/DB mappings from `stack_assignments` + compose definitions
- [ ] **Stack-aware health checks** — Derive expected containers per host from `stack_assignments` + compose services
- [ ] **Extract `update_systems.yaml` shell duplication** — `tasks/docker_snapshot.yaml` (H3: ~60 lines) + `tasks/docker_update.yaml` (H4: ~130 lines) — extract during D-post when `update_systems` gets per-stack surgery
- [ ] **Extract DB engine branching** — `tasks/db_dump.yaml`, `tasks/db_restore.yaml`, etc. (H2: ~200 lines across 5 playbooks) — extract when touching DB playbooks for per-stack changes

### Container placement (data-driven review needed)

The PHASE3_DESIGN.md recommendations were based on RAM allocation, not actual utilization.
Before making any moves, review real resource usage from the maintenance DB.

```sql
-- Run this to get actual resource utilization per host
SELECT h.hostname,
       ROUND(AVG(h.cpu_percent), 1) AS avg_cpu,
       ROUND(AVG(h.memory_percent), 1) AS avg_mem,
       ROUND(MAX(h.memory_percent), 1) AS peak_mem,
       COUNT(*) AS sample_count
FROM health_checks h
WHERE h.check_time >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY h.hostname
ORDER BY avg_mem DESC;
```

Revisit placement decisions after reviewing actual data. Original candidates were:
firefox (odyssey→tantive-iv), cloudflareddns (odyssey→vps), beszel hub (vps→tantive-iv),
dozzle hub (odyssey→tantive-iv) — but tantive-iv may already be the most utilized host.

## InfluxDB Testing (Feb 2026)

- [ ] **InfluxDB container OOM** — Container is shut down due to OOM issues. Once resolved:
  - [ ] Populate `db_names` in `vars/db_primary_influxdb.yaml` (replace `placeholder` with actual database names)
  - [ ] Test backup/verify/restore cycle
  - [ ] Confirm Discord notifications show "Measurements" labels correctly

## From Phase 3 Review (Feb 2026)

- [x] **Remove watchtower from all hosts** — Removed from tantive-iv infra stack, odyssey infra stack, VPS vpn stack during stack migration. `update_systems.yaml` handles all container updates.

## Deferred

- [ ] **IMP-10: UNVR TLS** — ON PAUSE. Vendor API incompatibility prevents proper TLS cert management. Revisit when Protect UI exposes API cert management.
- [ ] **Variable drift audit** — Look for variable drift across the entire project
- [ ] **Secret rotation workflow** — Mechanism to detect which stacks need re-deploy after vault edit
- [ ] **Auto-deploy on git push** — Toggle Semaphore template `autorun` from 0 to 1 once workflow is stable

User Added:
Migration is complete, remove all notes/design/information around legacy and migration. New design.  Look thru all code to remove as well.  I know there are at least comments in some playbooks/vars.