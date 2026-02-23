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

- [x] **`tasks/provision_vm.yaml`** — Extract VM creation from `build_ubuntu.yaml` Play 1 (Batch 1)
- [x] **`tasks/bootstrap_vm.yaml`** — Extract Ubuntu bootstrap from `build_ubuntu.yaml` Play 2 (Batch 1)
- [x] **`tasks/restore_appdata.yaml`** — Extract backup archive copy + extract from `restore_hosts.yaml` (Batch 2)
- [x] **`tasks/verify_docker_health.yaml`** — New: poll Docker container health (Batch 3)

> **Note on DB extractions:** The operative review (H2) identifies ~200 lines of DB engine branching
> duplicated across 5 playbooks (`db_dump`, `db_restore`, `db_count`, `db_drop_temp`). These are
> deferred to Phase D-post because they'll be touched during per-stack backup/restore surgery.
> There is no separate `restore_config` extraction — config files live inside appdata archives,
> and `.env` re-rendering after restore is handled by `deploy_stacks.yaml`.

#### Additional extractions (from operative review H1, H5)

- [x] **`tasks/docker_stop.yaml` + `tasks/docker_start.yaml`** — Extract tri-modal Docker stop/start/safety-net (Batch 2)
- [x] **`tasks/ssh_hardening.yaml`** — Extract SSH hardening + passwordless sudo (Batch 1, fixes L1)

#### Playbook refactors

- [x] **Refactor `build_ubuntu.yaml`** — Use `provision_vm` + `bootstrap_vm` + `ssh_hardening` task files (Batch 1)
- [x] **Refactor `restore_hosts.yaml`** — Use `restore_appdata` + `docker_stop`/`docker_start` task files (Batch 2)

#### Operative fixes during extraction

Fix while touching the same code. Full details in `future/operative-logic-review.md`.

- [x] **C1: `restore_hosts.yaml` safety-net gate** — Add `manage_docker` guard to rescue safety-net tasks (Batch 2)
- [x] **M6: `deploy_single_stack.yaml` atomic deploy** — Write compose file to temp location, validate, then move into place (Batch 3)

### Automated restore testing + DR mode

Full design in `future/PHASE3_DESIGN.md`.

- [x] **`test_restore.yaml`** — Fully automated restore testing on disposable VM (Batch 4)
- [x] **Semaphore template: `Test — Restore [VM]`** — Restore view, `blank` environment, id=69 (Batch 4)
- [x] **Update DESIGN.md + README.md** — Updated task file list, shared tasks, stale refs (Batch 6)

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
- [x] **L6: Discord colors** — Extract magic numbers to named vars in `group_vars/all.yaml` (Batch 7)
- [x] **M1: `verify_backups.yaml`** — Compute `_is_db_verify` fact once (Batch 7)
- [x] **M2: `backup_offline.yaml`** — Add MariaDB log entry for pre-sync failures (Batch 5)

#### Medium effort

- [x] **M3: `rollback_docker.yaml`** — Fix registry pull to use `.image` directly (Batch 5)
- [x] **M4: `backup_databases.yaml`** — Isolate per-DB failures with `ignore_errors` (Batch 7)
- [x] **M5: `update_systems.yaml`** — Detect failed `docker compose pull` via `pull_failed` flag (Batch 7)

### DESIGN.md stale references

Fixed in Batch 6 — task file tree updated to 16 shared tasks, all playbook lists current.

- [x] Shared task review callout — updated count and list (Batch 6)
- [x] Roles vs flat tasks section — updated shared task file count (Batch 6)

### Phase D-post (after migration stable)

- [ ] **Per-stack backup archives** — Replace monolithic `/opt` tar.gz with per-stack archives in `backup_hosts.yaml`
- [ ] **Per-stack restore** — Accept per-stack archive selection in `restore_hosts.yaml`
- [x] **Per-stack updates** — Per-stack pull/up loop + per-stack rollback snapshots in `update_systems.yaml` (completed during D-pre/migration)
- [x] **Per-stack rollback** — Accept `-e rollback_stack=<name>` in `rollback_docker.yaml` (completed during D-pre/migration)
- [ ] **Per-stack verify** — Loop over per-stack archives in `verify_backups.yaml`
- [x] **Remove legacy vars** — Removed `compose_project_path` and legacy mode code (Batch 6)
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
