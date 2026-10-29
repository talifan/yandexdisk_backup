#!/bin/sh
# Health is evidence of a recent complete backup, not of a living shell process.
set -u
state_dir="${STATE_DIR:-/state}"
interval="${INTERVAL_SECONDS:-1800}"
max_age="${HEALTH_MAX_AGE_SECONDS:-}"
case "$interval" in ''|*[!0-9]*) echo 'Invalid interval' >&2; exit 1 ;; esac
while [ "${interval#0}" != "$interval" ] && [ "${#interval}" -gt 1 ]; do interval="${interval#0}"; done
[ "${#interval}" -le 6 ] && [ "$interval" -gt 0 ] || exit 1
[ -n "$max_age" ] || max_age=$((interval * 2 + 300))
case "$max_age" in ''|*[!0-9]*) echo 'Invalid health max age' >&2; exit 1 ;; esac
[ "${#max_age}" -le 9 ] && [ "$max_age" -gt 0 ] || exit 1
[ -d "$state_dir" ] && [ -w "$state_dir" ] || { echo 'State directory is unavailable or read-only' >&2; exit 1; }
if [ -e "$state_dir/last-error" ]; then
  echo 'Last backup attempt failed; inspect last-error and container logs' >&2
  exit 1
fi
success="$(cat "$state_dir/last-success" 2>/dev/null)" || { echo 'No successful backup recorded' >&2; exit 1; }
case "$success" in
  20[0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
  *) echo 'Invalid last-success marker' >&2; exit 1 ;;
esac
timestamp="$(printf '%s' "$success" | awk '{printf "%s-%s-%s %s:%s:%s",substr($0,1,4),substr($0,5,2),substr($0,7,2),substr($0,10,2),substr($0,12,2),substr($0,14,2)}')"
epoch="$(date -u -d "$timestamp" +%s 2>/dev/null)" || exit 1
now="$(date -u +%s)" || exit 1
age=$((now - epoch))
if [ "$age" -lt 0 ] || [ "$age" -gt "$max_age" ]; then
  echo 'Last successful backup is stale or has an invalid future timestamp' >&2
  exit 1
fi
echo 'Recent complete backup recorded'
