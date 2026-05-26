#!/bin/sh
# pull_with_retry — pulls a Docker image or Compose service with retries and backoff.
# Non-retryable errors (manifest unknown, unauthorized, etc.) short-circuit immediately.
#
# Usage: source this file, then call:
#   pull_with_retry <target> <mode>
#     target: image name (mode=run) or service name (mode=compose)
#     mode:   "run" uses docker pull; "compose" uses docker compose pull
# Returns: 0 on success, 1 on final failure

pull_with_retry() {
  _pwr_target="$1"
  _pwr_cmd="$2"
  _pwr_attempt=0
  _pwr_max=3
  _pwr_backoffs="30 60 90"
  while [ "$_pwr_attempt" -lt "$_pwr_max" ]; do
    _pwr_attempt=$((_pwr_attempt + 1))
    if [ "$_pwr_cmd" = "compose" ]; then
      _pwr_out=$(timeout 300 docker compose pull "$_pwr_target" 2>&1)
    else
      _pwr_out=$(timeout 300 docker pull "$_pwr_target" 2>&1)
    fi
    _pwr_rc=$?
    printf '%s\n' "$_pwr_out" >&2
    [ "$_pwr_rc" -eq 0 ] && return 0
    case "$_pwr_out" in
      *"manifest unknown"*|*"unauthorized"*|*"denied"*|*"not found"*|*"does not exist"*)
        echo "[pull_with_retry] Non-retryable error for '$_pwr_target', aborting." >&2
        return 1 ;;
    esac
    if [ "$_pwr_attempt" -lt "$_pwr_max" ]; then
      _pwr_delay=$(echo "$_pwr_backoffs" | cut -d' ' -f"$_pwr_attempt")
      echo "[pull_with_retry] Attempt $_pwr_attempt/$_pwr_max failed for '$_pwr_target', retrying in ${_pwr_delay}s..." >&2
      sleep "$_pwr_delay"
    fi
  done
  echo "[pull_with_retry] All $_pwr_max attempts failed for '$_pwr_target'." >&2
  return 1
}
