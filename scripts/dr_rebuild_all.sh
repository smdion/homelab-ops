#!/usr/bin/env bash
# Run dr_rebuild.yaml for multiple roles sequentially.
# Each role completes the full provision -> bootstrap -> restore pipeline before
# the next role starts, respecting NFS dependencies (core/apps before dev).
#
# Usage:
#   scripts/dr_rebuild_all.sh core,apps,dev -e deploy_ssh_key=yes -e debug_no_log=yes
#   scripts/dr_rebuild_all.sh core           # single role (same as ansible-playbook directly)
#
# Requires:
#   - ansible-playbook in PATH
#   - Vault password configured (ANSIBLE_VAULT_PASSWORD_FILE or --vault-password-file in args)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ $# -lt 1 ]; then
  echo "Usage: $0 <roles> [extra ansible-playbook args...]"
  echo "  roles: comma-separated list (e.g. core,apps,dev)"
  exit 1
fi

ROLES="$1"
shift

IFS=',' read -ra ROLE_LIST <<< "$ROLES"

echo "DR Rebuild — roles: ${ROLE_LIST[*]}"
echo ""

for role in "${ROLE_LIST[@]}"; do
  echo "════════════════════════════════════════════════════════════════"
  echo " DR Rebuild: role=$role"
  echo "════════════════════════════════════════════════════════════════"
  ansible-playbook dr_rebuild.yaml -e "role=$role" "$@"
  echo ""
done

echo "All roles completed: ${ROLE_LIST[*]}"
