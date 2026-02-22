#!/bin/sh
# get_push_epoch — returns the Docker Hub "tag_last_pushed" epoch for an image,
# falling back to the local image creation date. Used by update_systems.yaml
# to enforce update_delay_days (skip images pushed too recently).
#
# Usage: source this file, then call: get_push_epoch "image:tag"
# Returns: epoch timestamp (or 0 if unknown)

get_push_epoch() {
  local img="$1" hub_img tag ns repo pd epoch cr
  hub_img="${img#docker.io/}"; hub_img="${hub_img#lscr.io/}"
  local first="${hub_img%%/*}"
  if ! echo "$first" | grep -q '\.'; then
    case "$hub_img" in *:*) tag="${hub_img##*:}"; hub_img="${hub_img%:*}" ;; *) tag="latest" ;; esac
    case "$hub_img" in */*) ns="${hub_img%%/*}"; repo="${hub_img#*/}" ;; *) ns="library"; repo="$hub_img" ;; esac
    pd=$(curl -sf --max-time 10 \
      "https://hub.docker.com/v2/namespaces/${ns}/repositories/${repo}/tags/${tag}" 2>/dev/null \
      | grep -o '"tag_last_pushed":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [ -n "$pd" ]; then
      epoch=$(date -d "$pd" +%s 2>/dev/null)
      [ -n "$epoch" ] && [ "$epoch" -gt 0 ] && echo "$epoch" && return
    fi
  fi
  cr=$(docker image inspect --format '{{.Created}}' "$img" 2>/dev/null | cut -dT -f1)
  if [ -n "$cr" ] && [ "$cr" != "0001-01-01" ] && [ "$cr" != "1970-01-01" ]; then
    epoch=$(date -d "$cr" +%s 2>/dev/null)
    [ -n "$epoch" ] && [ "$epoch" -gt 86400 ] && echo "$epoch" && return
  fi
  echo 0
}
