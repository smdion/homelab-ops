# Contributing

Guidelines for modifying or extending this project.

## Code Style

- **File extension:** `.yaml` everywhere (playbooks, vars, tasks, group_vars, requirements)
- **Shared tasks:** Reusable logic goes in `tasks/` — not duplicated across playbooks
- **Vars separation:** Deployment-specific values (URLs, paths, containers, thresholds) go in
  `vars/` files. Operation metadata (names, types, descriptions) stays in play-level `vars:`
- **No hardcoded values:** Anything that would change between deployments belongs in a vars file
  or the vault
- **Hostname source:** Always `inventory_hostname`, never `ansible_fqdn` (avoids unRAID
  capitalization issues)
- **Timestamps:** Always `UTC_TIMESTAMP()` in SQL, never `NOW()` (MariaDB server is CST)
- **DB queries:** Use `community.mysql.mysql_query` with `%s` parameterized queries, not shell
  `mariadb -e`

## Testing

### Dry-run (check mode)

Every operational playbook supports `--check`. This validates syntax, evaluates conditions, and
runs state-gathering shell tasks without making changes:

```bash
ansible-playbook maintain_health.yaml -i inventory.yaml --vault-password-file ~/.vault_pass --check
```

Check mode skips Discord notifications, DB writes, and destructive operations. Shell tasks that
gather state (not change it) use `check_mode: false` to run even in check mode.

### Live testing

Run against a single host to limit blast radius:

```bash
ansible-playbook backup_hosts.yaml -i inventory.yaml \
  -e hosts_variable=docker_stacks -e config_file=docker_stacks \
  --vault-password-file ~/.vault_pass --limit myhost.example.local
```

## Adding a New Platform

1. Create `vars/<platform>.yaml` (copy from `vars/example.yaml`)
2. Add hosts to inventory under a new group
3. Create a Semaphore variable group: `{"hosts_variable": "<platform>"}`
4. If the platform has a version command, add it to `_os_version_commands` in `update_systems.yaml`
5. Platform-specific health checks need `when: "'<platform>' in group_names"` in `maintain_health.yaml`

## Adding a New Health Check

1. Add the check block to `maintain_health.yaml` in the appropriate play (Play 1 for localhost
   DB/API checks, Play 2 for SSH host checks)
2. Follow the existing block/rescue pattern — record results to `play1_health_results` or
   `host_health_issues`
3. Add any new thresholds to `vars/semaphore_check.yaml`
4. Update the CHECK comment numbering (sequential across all 3 plays)
5. Update the check list comment in `tasks/log_health_check.yaml`

## Pull Request Expectations

- Run `--check` on any modified playbook before submitting
- Update `DESIGN.md` if the change affects file structure, patterns, or architecture
- Keep commits focused — one logical change per commit
