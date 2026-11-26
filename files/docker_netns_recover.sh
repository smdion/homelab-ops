#!/bin/sh
# docker_netns_recover — starts containers that lost the boot race for a shared network.
#
# WHY: a container with network_mode "service:X" (compose) runs as "container:<id of X>".
# When the Docker daemon restarts containers at boot it does so in no particular order,
# and compose depends_on does not apply there. If the dependent starts before X, the
# start fails with "cannot join network of a non running container", and the daemon
# never retries it, whatever its restart policy says. It then stays exited until
# someone starts it by hand. (2026-11-25: the VPS rebooted and the Beszel hub, which
# runs inside the wireguard container network, stayed down for 15 hours.)
#
# WHAT: run once at boot by docker-netns-recover.service. For up to RECOVER_TIMEOUT
# seconds it looks for exited or never-started containers whose last start failed
# with that error and whose network container is now running, and starts them. Containers that
# were stopped on purpose never carry that error, so they are left alone.
#
# Deployed by deploy_stacks.yaml (tasks/docker_netns_recovery.yaml) to every stack host.

RECOVER_TIMEOUT="${RECOVER_TIMEOUT:-300}"
INTERVAL=5

waited=0
while [ "$waited" -le "$RECOVER_TIMEOUT" ]; do
  pending=0
  for id in $(docker ps -aq --filter status=exited --filter status=created 2>/dev/null); do
    info=$(docker inspect -f '{{.Name}}|{{.HostConfig.NetworkMode}}|{{.State.Error}}' "$id" 2>/dev/null) || continue
    name=${info%%|*}; name=${name#/}
    rest=${info#*|}
    netmode=${rest%%|*}
    err=${rest#*|}
    case "$netmode" in container:*) ;; *) continue ;; esac
    # Wording differs by Docker version: "cannot join network of a non running container"
    # (VPS, 2026-11-25) and "cannot join network namespace of a non running container: ..." (dev).
    case "$err" in *"cannot join network"*"non running container"*) ;; *) continue ;; esac
    target=${netmode#container:}
    if [ "$(docker inspect -f '{{.State.Running}}' "$target" 2>/dev/null)" = "true" ]; then
      if docker start "$id" >/dev/null 2>&1; then
        echo "started $name (network container $target is running)"
      else
        echo "failed to start $name; will retry"
        pending=$((pending + 1))
      fi
    else
      pending=$((pending + 1))
    fi
  done
  [ "$pending" -eq 0 ] && exit 0
  sleep "$INTERVAL"
  waited=$((waited + INTERVAL))
done

echo "gave up after ${RECOVER_TIMEOUT}s with $pending container(s) still waiting for their network container"
exit 1
