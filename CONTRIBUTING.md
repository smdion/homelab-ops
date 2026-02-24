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
2. Add hosts to the appropriate Semaphore inventory under a new group
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

## Restore / Verify Playbooks

Restore and verify playbooks follow the same `block`/`rescue`/`always` error handling, Discord
notification, and MariaDB logging patterns as backup playbooks. Additional conventions:

- **Safety gate:** Destructive restore playbooks (`restore_databases.yaml`, `restore_hosts.yaml`
  inplace mode) require `-e confirm=yes`. A pre-task assertion fails with guidance if omitted.
  Never remove this gate.
- **Shared environments:** Verify and restore templates share the same Semaphore environment as
  backup templates for the same target — do not create separate environments.
- **`gunzip -cf`:** Always use `-cf` (not `-c`) when piping backup files to restore commands. The
  `-f` flag handles both gzipped and plain SQL files transparently.
- **Logging:** Verify operations log with `operation: verify`, restore operations log with
  `operation: restore` to the `restores` table via `tasks/log_restore.yaml`.

## Public Repository — Security

This is a **public GitHub repository** and the production source that Semaphore pulls from.
Never commit:

- **Secrets or credentials** — passwords, API keys, tokens, webhook URLs, SSH keys
- **Internal IP addresses** — any private IP (`192.168.x.x`, `10.x.x.x`), including subnets
  and gateway addresses. This includes WireGuard internal subnets and PVE node IPs.
- **Internal domain names** — local DNS names (`*.home.local`, `*.internal.lan`, etc.)
- **Infrastructure node names** — Proxmox node names, VM hostnames, server names
- **Personally identifiable information** — real names, email addresses, physical locations

### What goes where

| Value type | Where it belongs |
|---|---|
| Passwords, tokens, API keys | `vars/secrets.yaml` (vault) |
| Internal IPs (hosts, gateways, subnets) | `vars/secrets.yaml` (vault) |
| Internal FQDNs / search domain | `vars/secrets.yaml` (vault) |
| Proxmox node names | `vars/secrets.yaml` (vault) |
| Non-sensitive deployment config | `vars/*.yaml` (plain, use `vault_*` references) |

### Comment examples

When writing inline documentation or examples in `.yaml`/`.j2` files, use
**TEST-NET addresses** (`192.0.2.x`, `198.51.100.x`) for IP examples and
`myhost.example.local` for hostname examples. Never use real internal IPs even in comments.

```yaml
# Good — TEST-NET example
nfs_mounts:
  - src: "192.0.2.10:/mnt/share"

# Bad — internal subnet even in a comment
nfs_mounts:
  - src: "10.10.10.10:/mnt/share"
```

### Accidentally committed sensitive data

Deleting in a follow-up commit is **not enough** — the value remains in git history.
Use `git filter-repo --replace-text` (or `git filter-branch --tree-filter` + `--msg-filter`
if Python is unavailable) to rewrite history, then force-push. Coordinate with anyone who
has cloned the repo — their local clones will diverge and need to be re-cloned.

When in doubt, add the value to the vault first, reference it via `{{ vault_... }}` in the
tracked file, and never include the raw value in any committed file or commit message.

## Pull Request Expectations

- Run `--check` on any modified playbook before submitting
- Update `DESIGN.md` if the change affects file structure, patterns, or architecture
- Keep commits focused — one logical change per commit
