# Ansible Codebase Review — Operative Logic Findings

Independent review of the homelab-ops Ansible project, focused on substantive operative
logic: control flow correctness, failure recovery guarantees, data integrity, race
conditions, and architectural extraction opportunities in shared logic.

Reviewed: all 19 playbooks, 9 shared task files, all vars files, DESIGN.md, stacks.

> **Tracking:** All findings are tracked in `future/TODO.md`. Extraction targets (H1, H5)
> and related fixes (C1, M6) are combined with the shared task extraction work. Standalone
> fixes (C2-C4, L1-L6, M1-M5) have their own section. Deferred extractions (H2-H4) are
> slotted into Phase D-post.

---

## Table of Contents

- [Critical: Operative Safety](#critical-operative-safety)
- [High: Shared Logic Extraction](#high-shared-logic-extraction)
- [Medium: Logic Correctness](#medium-logic-correctness)
- [Low: Consistency and Polish](#low-consistency-and-polish)

---

## Critical: Operative Safety

### C1. Docker safety net in `restore_hosts.yaml` doesn't cover the `manage_docker` gate

**Files:** `restore_hosts.yaml:113-177` (stop), `restore_hosts.yaml:473-521` (rescue)

The block stops Docker containers only when `manage_docker == 'yes'` (lines 119, 133,
159). But the rescue safety net (lines 478-521) starts containers unconditionally —
it has no `manage_docker` guard. If a different failure occurs and `manage_docker` was
*not* set, the rescue block tries to start containers that were never stopped. On
unRAID specifically, `_docker_containers` would be undefined (the enumerate task was
skipped), and while the `when: _docker_containers is defined` guard prevents a crash,
the stack/legacy-mode safety nets lack that gate and would attempt a no-op
`docker_compose_v2: state: present` — which could restart recently-stopped containers
from a *separate* manual maintenance window.

**Fix:** Add `manage_docker | default('') == 'yes'` to all rescue safety-net tasks to
match the block's stop conditions.

### C2. `backup_hosts.yaml` silently logs size=0 on rescue path — **Fixed (Batch 5)**

**File:** `backup_hosts.yaml:94-98`, `backup_hosts.yaml:425-433`

`file_size` is initialized as `{stat: {size: 0}}` (line 96). If the block fails before
the `stat` task (line 230-234), the always block logs `file_size: 0` to both Discord
and MariaDB. The `backup_failed` flag is set to `true`, so the Discord message says
"Backup Failed" — good. But the MariaDB INSERT records `file_size: 0` with no `FAILED_`
prefix in `log_file_name` (unlike `backup_databases.yaml` which prefixes failed entries
with `FAILED_`). This means a failed host backup in the `backups` table looks identical
to a successful zero-byte backup.

**Fix:** Apply the same `FAILED_` prefix pattern used in `backup_databases.yaml`:
```yaml
log_file_name: "{{ ('FAILED_' if backup_failed else '') + backup_file }}"
```

### C3. `backup_databases.yaml` uses the same `backup_tmp_file` for all DBs in a loop — **Fixed (Batch 5)**

**File:** `backup_databases.yaml:63-83`

The dump task loops over `db_names` but writes every dump to `{{ backup_tmp_file }}`
(defined in `group_vars/all.yaml` as `{{ backup_tmp_dir }}/{{ backup_file }}`). The
`backup_file` var in `vars/db_primary_postgres.yaml` is
`backup_{{ item }}_{{ ansible_date_time.date }}.sql` — it uses `{{ item }}` from the
loop. This works because Ansible resolves `backup_tmp_file` lazily, so `item` is
available at render time. However, the `stat` task (lines 85-91) and `verify` tasks
(lines 93-118) also loop over `db_names` and reference the same `backup_tmp_file`
with `item` — so each loop iteration stats/verifies the correct file.

BUT: the `fetch` task (lines 136-142) also loops and fetches `backup_tmp_file` to
`backup_dest_path`, which also contains `{{ item }}`. Each iteration fetches the correct
file and the previous file *still exists* on the remote. This is fine.

The real risk: if `backup_file` in a vars file ever stops including `{{ item }}`, all
dumps silently overwrite each other and only the last DB gets backed up. This is a
latent fragility — the correctness of the multi-DB loop depends on a naming convention
in vars files. The design doc doesn't call this out.

**Recommendation:** Add a comment in `vars/example.yaml` documenting that `backup_file`
MUST include `{{ item }}` when `db_names` has multiple entries, or restructure the dump
task to use an explicit per-DB filename computed in the task itself.

### C4. `restore_databases.yaml` stops deps but rescue restarts ALL deps for ALL DBs — **Fixed (Batch 5)**

**File:** `restore_databases.yaml:117-126` (stop), `restore_databases.yaml:210-219` (rescue)

The block stops dependent containers per-DB in a loop. But the restore task also loops
per-DB (lines 128-148). If the *second* DB restore fails, the rescue block tries to
start deps for *all* DBs — including ones whose deps were already restarted by the
block's "Start dependent containers" task (lines 169-178) for the first DB. This is
idempotent (starting an already-running container is a no-op), so it's not a bug, but
the real issue is subtler: if the first DB's deps were started successfully and then
the second DB restore fails, the first DB's containers are now running against a
*partially restored* state (first DB restored, second not). There's no rollback of the
first DB's restore.

**Recommendation:** Document this as a known limitation. The pre-restore safety backup
mitigates it (operator can manually restore from the pre-backup). Consider: for
multi-DB restores, either batch all restores before starting *any* deps, or accept
the current per-DB granularity with the documented caveat.

---

## High: Shared Logic Extraction

These are the core duplication areas identified in DESIGN.md's "Shared task review"
callout. They represent real operative logic — not cosmetic repetition.

### H1. Docker stop/start tri-modal control flow (highest value extraction)

**Files:**
- `backup_hosts.yaml:102-142` (stop), `245-275` (start), `316-351` (safety net)
- `restore_hosts.yaml:114-177` (stop), `414-463` (start), `478-521` (safety net)

Three execution modes (stack, legacy, unRAID-run) × three lifecycle phases
(stop, start, safety net) = 9 blocks of near-identical logic across two playbooks.
Each mode has its own `when:` conditions, registration patterns, and `ignore_errors`
behavior.

**Proposed extraction:**
```
tasks/docker_stop.yaml   — accepts: mode filter from inventory groups, selective app list
tasks/docker_start.yaml  — accepts: same params + ignore_errors flag
```

Each playbook currently handles ~50-80 lines of this per phase. Extracting saves
~150-200 lines and — more importantly — ensures the safety-net start logic stays in
sync with the stop logic. Today a change to the stop conditions requires manually
updating the matching start conditions in *two* playbooks. A shared task eliminates
that drift risk.

`update_systems.yaml` doesn't stop/start Docker (it does pull+up in one script), so
it doesn't need these shared tasks.

### H2. DB engine Jinja2 branching (dump/restore/verify/drop)

**Files:**
- `backup_databases.yaml:67-78` (dump)
- `verify_backups.yaml:71-92` (create temp + restore)
- `verify_backups.yaml:94-113` (count tables)
- `verify_backups.yaml:116-133` (drop temp)
- `restore_databases.yaml:84-104` (pre-restore safety dump)
- `restore_databases.yaml:129-148` (restore)
- `restore_databases.yaml:150-167` (validate)
- `restore_hosts.yaml:238-257` (cross-host restore)
- `restore_hosts.yaml:259-280` (cross-host validate)

The `{% if is_postgres %}...{% elif is_mariadb %}...{% elif is_influxdb %}...{% endif %}`
shell template pattern is copy-pasted across five playbooks. Each instance has slight
variations (temp DB name prefix, cleanup commands, error handling), but the core shell
commands per engine are identical.

**Proposed extraction:**
```
tasks/db_dump.yaml       — dump a single DB (accepts: engine flags, db_name, container, etc.)
tasks/db_restore.yaml    — restore a single DB (same params + source file)
tasks/db_count.yaml      — count tables/measurements in a DB
tasks/db_drop_temp.yaml  — drop a temp verification DB
```

The primary benefit is bug-fix consistency: a change to how PostgreSQL dumps are
generated (e.g., adding `--no-owner`) would need to be applied in *one* shared task
instead of five playbooks.

### H3. Rollback snapshot shell script duplication

**File:** `update_systems.yaml:236-263` (stack mode) vs `288-316` (legacy mode)

These are 30-line shell scripts that differ only in the `chdir` path and the service
list source variable. The JSON-building loop, version label fallback chain, and printf
format string are identical.

**Proposed extraction:** `tasks/docker_snapshot.yaml` — receives a service list and
working directory, outputs JSON to a register.

### H4. Pull/recreate/compare shell script duplication

**File:** `update_systems.yaml:339-406` (stack mode) vs `432-497` (legacy mode)

~65 lines of identical shell logic: source the helper, iterate services, check delay,
pull, compare before/after image IDs, build JSON output. The only differences are
`chdir`, the service list source, and the pull command arguments.

**Proposed extraction:** `tasks/docker_update.yaml` — receives a service list, working
directory, and delay config. Returns the JSON results array.

Extracting H3 and H4 together would remove ~190 lines of duplicated shell from
`update_systems.yaml` (currently 733 lines — the largest playbook by far).

### H5. SSH hardening + passwordless sudo

**Files:**
- `build_ubuntu.yaml:403-428` (SSH hardening + sudo)
- `add_ansible_user.yaml:29-66` (Play 1: PVE/PBS/Ubuntu)
- `add_ansible_user.yaml:110-206` (Play 2: unRAID)

Three copies of the same SSH `lineinfile` loop (PermitRootLogin, PasswordAuthentication,
PubkeyAcceptedAlgorithms) and passwordless sudo template. The SSH service name differs
between them (`ssh` vs `sshd`) — see L1 below.

**Proposed extraction:** `tasks/ssh_hardening.yaml` — accepts a service name parameter
(defaulting to `ssh` for Ubuntu Noble). Also covers the `ansible_user_ssh_pubkey`
authorized key task.

---

## Medium: Logic Correctness

### M1. `verify_backups.yaml` repeated condition should be a computed fact

**File:** `verify_backups.yaml` (12+ occurrences)

The expression:
```yaml
is_postgres | default(false) | bool or is_mariadb | default(false) | bool or is_influxdb | default(false) | bool
```
appears on nearly every task in the DB verification path. Beyond being verbose, the
real risk is that adding a new DB engine (e.g., Redis RDB) requires updating 12+
`when:` conditions across the file.

**Fix:** Compute `_is_db_verify` once during the init task:
```yaml
_is_db_verify: "{{ is_postgres | default(false) or is_mariadb | default(false) or is_influxdb | default(false) }}"
```

### M2. `backup_offline.yaml` — missing DB log on pre-sync failure path — **Fixed (Batch 5)**

**File:** `backup_offline.yaml:199-214`

The rsync loop uses `ignore_errors: true` so one share's failure doesn't stop the
next. If the block fails *before* the rsync loop (mount, permissions, WOL), `rescue:`
fires and sets `sync_failed: true`. The always block then iterates
`backup_status.results` — which is `{results: []}` (initialized at line 28). The
per-share Discord loop produces nothing. The "pre-sync failure" Discord fires — correct.
But the MariaDB logging (lines 199-214) iterates `backup_status.results`, which is
empty, so *no log entry is written*. The operator gets a Discord alert but the DB has
no record.

**Fix:** Add an unhandled-failure MariaDB log entry (same pattern as
`restore_databases.yaml:274-286`):
```yaml
- name: Log pre-sync failure to MariaDB
  include_tasks: tasks/log_mariadb.yaml
  when: sync_failed and backup_status.results | length == 0
  vars:
    log_table: backups
    log_application: "{{ backup_name }}"
    log_hostname: "{{ inventory_hostname }}"
    log_file_name: "FAILED_sync_{{ ansible_date_time.date }}"
    log_file_size: 0
```

### M3. `rollback_docker.yaml` — registry pull path may pull wrong version — **Fixed (Batch 5)**

**File:** `rollback_docker.yaml:179-196`

The slow path constructs a pull tag from the snapshot's `version` field:
```yaml
rollback_snapshot.containers[item].image | regex_replace(':.*$', '') + ':' + rollback_snapshot.containers[item].version
```

The `version` field in the snapshot comes from the 3-tier label fallback chain
(`org.opencontainers.image.version` → `org.label-schema.version` → `version` label →
image tag). For many images, the label version (e.g., `2.1.3`) does NOT match the
Docker tag (e.g., `latest`, `2`, or `2.1`). Pulling `image:2.1.3` when the original
tag was `latest` may:
1. Fail entirely (no such tag exists in the registry)
2. Pull a different build than expected (some images publish version-specific tags
   that diverge from `latest`)

The snapshot stores the `image_id` (`sha256:...`) which is the authoritative identifier,
but the registry pull path can't use it — registries don't support pulling by digest
without the `@sha256:` syntax.

**Recommendation:** Store the original image *reference* (including tag) in the
snapshot separately from the label-derived `version`. The snapshot already has the
`image` field (e.g., `lscr.io/linuxserver/sonarr:latest`) — the slow path should pull
using that exact reference, not the label version. Current behavior:
- Fast path: correct (re-tags by image ID)
- Slow path: fragile (pulls by label version, not original tag)

**Fix:** Change the slow-path pull to use `rollback_snapshot.containers[item].image`
(the original image reference) instead of constructing a version-tagged reference:
```yaml
_registry_pull: >-
  {{ _registry_pull | default({}) | combine({item: rollback_snapshot.containers[item].image}) }}
```
This pulls the *current* latest — not the old version. To truly pull the old version,
the snapshot would need to record the full image digest
(`image@sha256:abc123...`). This is a design limitation worth documenting.

### M4. `backup_databases.yaml` — per-DB failure doesn't isolate

**File:** `backup_databases.yaml:62-148`

The dump task loops over `db_names` but runs as a flat loop — if the second DB dump
fails, the rescue block fires and sets `backup_db_failed: true`. The always block then
sends Discord for whatever results were collected in `combined_results`. But the
`file_size` stat task (lines 85-91) and the `combine` task (lines 120-134) only ran for
the DBs that completed before the failure. The DBs that failed mid-loop have no entry
in `combined_results`, so:
- No per-DB Discord notification for the failed DB
- No per-DB MariaDB log entry for the failed DB
- The "unhandled block error" Discord fires (line 171-184) only when
  `backup_db_failed` is true — but it fires *in addition to* the per-DB notifications,
  not *instead of*. So the operator gets both "Backup Successful — db1" and "Backup
  Failed — check Semaphore logs" in Discord.

**Recommendation:** Consider wrapping the dump in an inner `block/rescue` per DB
(or use `ignore_errors: true` on the dump task and check `rc` in the combine step,
similar to how `backup_offline.yaml` handles per-share failures). This would ensure
every DB gets a result entry even on partial failure.

### M5. `update_systems.yaml` — Docker update shell scripts swallow non-zero exits

**File:** `update_systems.yaml:339-416` (stack mode), `432-507` (legacy mode)

The shell scripts use `docker compose pull` which may fail (network issues, registry
down). If `docker compose pull` fails, the script continues to the comparison/output
section and may report no updates (because the pull failed, not because images are
current). The task has `changed_when: true` (always reports changed) and no
`failed_when` override, so the shell's exit code determines success. If `pull` fails
but the script exits 0 (because the last command is `echo ']'`), the failure is
invisible.

**Impact:** A failed `docker compose pull` (e.g., rate-limited by Docker Hub, DNS
failure) produces a successful-looking update run with "0 containers updated". No alert
fires. The operator assumes everything is current when it isn't.

**Fix:** Either add `set -e` to the shell scripts, or explicitly check the pull exit
code:
```bash
docker compose pull $target_services >&2 || pull_failed=true
```
Then set a non-zero exit code at the end if `pull_failed` is set, so Ansible can
detect and report the failure.

### M6. `deploy_single_stack.yaml` — no rollback on validation failure

**File:** `tasks/deploy_single_stack.yaml:35-41`

If `docker compose config --quiet` fails (invalid compose file), the task fails and
Ansible stops the current host (the rescue block in `deploy_stacks.yaml` fires). But
at this point, the `.env` file and `docker-compose.yaml` have *already been written*
to `/opt/stacks/<name>/`. The previous working versions have been overwritten. The
running containers are still on the *old* images/config (because `docker compose up`
never ran), but the compose files on disk are now broken.

If the operator tries to manually restart the stack (or if `backup_hosts.yaml` runs
and restarts stacks as part of the backup safety net), it will use the broken compose
files and fail.

**Recommendation:** Write the new compose file to a temp location first, validate it,
then move it into place. Or: capture the previous compose file content before
overwriting, and restore it on validation failure. This is a standard "atomic deploy"
pattern.

---

## Low: Consistency and Polish

### L1. SSH service name inconsistency (`ssh` vs `sshd`)

**Files:**
- `build_ubuntu.yaml:428` — uses `ssh` (correct for Ubuntu Noble)
- `add_ansible_user.yaml:66` — handler uses `sshd` (incorrect for Ubuntu Noble)

If `add_ansible_user.yaml` is run on an Ubuntu Noble host, the handler will fail
because the service is named `ssh`, not `sshd`. The recent commit `8270712` fixed this
in `build_ubuntu.yaml` but `add_ansible_user.yaml` was not updated.

**Fix:** Change the handler service name or make it conditional on host group.

### L2. `rollback_docker.yaml` — reversed `vars_files` order — **Fixed (Batch 5)**

**File:** `rollback_docker.yaml:37-39`

All other playbooks load `vars/secrets.yaml` first, then the config file. This playbook
reverses the order. While there are no known key collisions, this inconsistency could
cause unexpected precedence behavior if keys are ever shared between secrets and config.

**Fix:** Swap to match the standard order.

### L3. `rollback_docker.yaml` — missing `assert_db_connectivity` — **Fixed (Batch 5)**

**File:** `rollback_docker.yaml:43-133`

This playbook logs to MariaDB in the always block (line 287-298) but has no
`assert_db_connectivity` pre-task. If MariaDB is down, the logging task will silently
retry for 400 seconds (40 retries × 10s delay) before the playbook continues. Every
other playbook that logs to MariaDB has this pre-task.

**Fix:** Add the standard `assert_db_connectivity` pre-task.

### L4. `deploy_grafana.yaml` — missing `assert_db_connectivity` — **Fixed (Batch 5)**

**File:** `deploy_grafana.yaml`

Same issue as L3 — logs to MariaDB but has no connectivity pre-check.

### L5. `log_health_check.yaml` appears unused — **Fixed (Batch 5)**

**File:** `tasks/log_health_check.yaml`

The single-row health check logging task is defined but no playbook calls it.
`maintain_health.yaml` uses the batch version (`log_health_checks_batch.yaml`). This
may be dead code left from before the batch optimization, or it may be kept as a
utility for future single-check playbooks.

**Recommendation:** Add a comment to the file documenting its status, or remove it if
no future use is planned.

### L6. Discord color magic numbers

**Files:** All playbooks.

Colors are hardcoded as integers: `32768` (green), `16711680` (red), `16753920` (orange),
`16776960` (yellow). These appear ~40 times across the codebase.

**Recommendation:** Define named variables in `group_vars/all.yaml`:
```yaml
discord_color_success: 32768
discord_color_failure: 16711680
discord_color_warning: 16753920
discord_color_rollback: 16776960
```
This improves readability and makes the color scheme easy to change globally.

---

## Summary Priority Matrix

| ID | Severity | Effort | Impact |
|----|----------|--------|--------|
| C1 | Critical | Low | Safety net starts containers that were never stopped |
| C2 | Critical | Low | Failed backups indistinguishable from zero-byte successes in DB |
| C3 | Critical | Low | Latent fragility — multi-DB backup correctness depends on vars convention |
| C4 | Critical | Low | Multi-DB restore has no rollback for partial failure |
| H1 | High | Medium | ~200 lines of duplicated Docker stop/start/safety-net logic |
| H2 | High | Medium | ~200 lines of duplicated DB engine branching across 5 playbooks |
| H3 | High | Low | ~60 lines duplicated rollback snapshot shell |
| H4 | High | Low | ~130 lines duplicated update shell |
| H5 | High | Low | ~60 lines duplicated SSH hardening |
| M1 | Medium | Low | Verbose repeated condition; new-engine maintenance burden |
| M2 | Medium | Low | Missing DB log on pre-sync failure |
| M3 | Medium | Medium | Registry rollback pulls wrong version for label != tag images |
| M4 | Medium | Medium | Per-DB failure info lost mid-loop |
| M5 | Medium | Medium | Failed docker pull silently reports "no updates" |
| M6 | Medium | Medium | Broken compose files left on disk after validation failure |
| L1 | Low | Low | SSH handler fails on Ubuntu Noble |
| L2 | Low | Low | Inconsistent vars_files order |
| L3 | Low | Low | Missing db_connectivity pre-task |
| L4 | Low | Low | Missing db_connectivity pre-task |
| L5 | Low | Low | Potentially dead code |
| L6 | Low | Low | Magic number readability |
