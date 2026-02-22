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
- **Timestamps:** Always `UTC_TIMESTAMP()` in SQL, never `NOW()`
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

## Restore / Verify Playbooks

Restore and verify playbooks follow the same `block`/`rescue`/`always` error handling, Discord
notification, and MariaDB logging patterns as backup playbooks. Additional conventions:

- **Safety gate:** Destructive restore playbooks (`restore_databases.yaml`, `restore_hosts.yaml`
  inplace mode) require `-e confirm_restore=yes`. A pre-task assertion fails with guidance if
  omitted. Never remove this gate.
- **Shared environments:** Verify and restore templates share the same Semaphore environment as
  backup templates for the same target — do not create separate environments.
- **`gunzip -cf`:** Always use `-cf` (not `-c`) when piping backup files to restore commands. The
  `-f` flag handles both gzipped and plain SQL files transparently.
- **Logging:** Verify operations log with `operation: verify`, restore operations log with
  `operation: restore` to the `restores` table via `tasks/log_restore.yaml`.

## Public Repository — Security

This is a **public GitHub repository**. Never commit:

- **Secrets or credentials** — passwords, API keys, tokens, webhook URLs, SSH keys
- **Internal IP addresses** — private IPs (e.g., `192.168.x.x`, `10.x.x.x`)
- **Internal domain names** — local DNS names (e.g., `*.home.local`, `*.internal.lan`)
- **Personally identifiable information** — real names, email addresses, physical locations

All secrets belong in `vars/secrets.yaml` (encrypted vault). Internal hostnames and domains
should use placeholder values in documentation and examples (e.g., `myhost.example.local`).
The `vars/secrets.yaml.example` template demonstrates this pattern.

If you accidentally commit sensitive data, **do not** just delete it in a follow-up commit —
it remains in git history. Instead, rotate the exposed credential immediately and contact
the maintainer.

## Pull Request Expectations

- Run `--check` on any modified playbook before submitting
- Update `DESIGN.md` if the change affects file structure, patterns, or architecture
- Keep commits focused — one logical change per commit
