#!/bin/sh
# All destructive paths are local quarantine paths; the remote is only read.
set -u
umask 077

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "Required environment variable is missing: $name" >&2
    exit 2
  fi
}

integer() {
  name="$1"; value="$2"; minimum="$3"; maximum="$4"
  case "$value" in ''|*[!0-9]*) echo "$name must be an integer" >&2; exit 2 ;; esac
  while [ "${value#0}" != "$value" ] && [ "${#value}" -gt 1 ]; do value="${value#0}"; done
  if [ "${#value}" -gt 9 ] || [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
    echo "$name is outside the supported range" >&2; exit 2
  fi
  printf '%s\n' "$value"
}

require_env REMOTE_NAME
require_env ACCOUNT_NAME
case "$REMOTE_NAME" in ''|[-._]*|*[!A-Za-z0-9_.-]*) echo 'Invalid REMOTE_NAME' >&2; exit 2 ;; esac
case "$ACCOUNT_NAME" in ''|*[!A-Za-z0-9_.-]*) echo 'Invalid ACCOUNT_NAME' >&2; exit 2 ;; esac

interval="$(integer INTERVAL_SECONDS "${INTERVAL_SECONDS:-1800}" 1 604800)" || exit 2
retention_days="$(integer RETENTION_DAYS "${RETENTION_DAYS:-180}" 0 36500)" || exit 2
max_delete="$(integer MAX_DELETE "${MAX_DELETE:-10000}" 0 100000000)" || exit 2
preflight_timeout="$(integer PREFLIGHT_TIMEOUT_SECONDS "${PREFLIGHT_TIMEOUT_SECONDS:-90}" 1 3600)" || exit 2
max_run_seconds="$(integer MAX_RUN_SECONDS "${MAX_RUN_SECONDS:-43200}" 1 604800)" || exit 2
retry_initial="$(integer RETRY_INITIAL_SECONDS "${RETRY_INITIAL_SECONDS:-300}" 1 604800)" || exit 2
retry_max="$(integer RETRY_MAX_SECONDS "${RETRY_MAX_SECONDS:-21600}" 1 604800)" || exit 2
stop_grace="$(integer STOP_KILL_SECONDS "${STOP_KILL_SECONDS:-5}" 1 60)" || exit 2
[ "$retry_initial" -le "$retry_max" ] || { echo 'Retry initial exceeds maximum' >&2; exit 2; }
max_delete_size="${MAX_DELETE_SIZE:-200Gi}"
expected_fs_magic="${EXPECTED_FS_MAGIC:-6969}"
case "$expected_fs_magic" in ''|*[!a-fA-F0-9]*) echo 'EXPECTED_FS_MAGIC must be hexadecimal' >&2; exit 2 ;; esac
expected_fs_magic="$(printf '%s' "$expected_fs_magic" | tr 'A-F' 'a-f')"

backup_dir="${BACKUP_DIR:-/backup}"
versions_dir="${VERSIONS_DIR:-/versions}"
state_dir="${STATE_DIR:-/state}"
for directory in "$backup_dir" "$versions_dir" "$state_dir"; do
  case "$directory" in /*) ;; *) echo 'Storage paths must be absolute' >&2; exit 2 ;; esac
  canonical="$(CDPATH='' cd "$directory" 2>/dev/null && pwd -P)" || { echo 'Storage directory missing' >&2; exit 2; }
  [ "$directory" = "$canonical" ] && [ "$directory" != / ] || { echo 'Storage paths must be canonical non-root directories' >&2; exit 2; }
done
case "$versions_dir/" in "$backup_dir/"*) echo 'Versions must be outside backup' >&2; exit 2 ;; esac
case "$backup_dir/" in "$versions_dir/"*) echo 'Backup must be outside versions' >&2; exit 2 ;; esac
case "$state_dir/" in "$backup_dir/"*|"$versions_dir/"*) echo 'State must be outside mirrored data' >&2; exit 2 ;; esac

once=0; targets_only=0
case "${1:-}" in '') ;; --once) once=1 ;; --check-targets) targets_only=1 ;; *) echo 'Usage: run-loop.sh [--once|--check-targets]' >&2; exit 2 ;; esac
[ "$#" -le 1 ] || exit 2
stopping=0; backup_pid=; sleep_pid=; failure_class=; retry_delay="$retry_initial"

now_stamp() { date -u +%Y%m%dT%H%M%SZ; }

write_marker() {
  marker="$1"; content="$2"
  temporary="$(mktemp "$state_dir/.marker.XXXXXX")" || return 1
  if ! printf '%s\n' "$content" > "$temporary" || ! mv -f -- "$temporary" "$state_dir/$marker"; then
    rm -f -- "$temporary" 2>/dev/null || true
    return 1
  fi
}

on_term() {
  stopping=1
  if [ -n "$sleep_pid" ]; then kill -TERM "$sleep_pid" 2>/dev/null || true; fi
  if [ -n "$backup_pid" ]; then
    kill -INT "-$backup_pid" 2>/dev/null || kill -INT "$backup_pid" 2>/dev/null || true
    sleep "$stop_grace"
    kill -KILL "-$backup_pid" 2>/dev/null || kill -KILL "$backup_pid" 2>/dev/null || true
    wait "$backup_pid" 2>/dev/null || true
    backup_pid=
  fi
}
trap on_term TERM INT

run_child() {
  deadline="$1"; shift
  setsid timeout -s TERM -k "$stop_grace" "$deadline" "$@" &
  backup_pid=$!
  wait "$backup_pid"
  child_result=$?
  # Reap/terminate any owned descendants left by an interrupted command.
  if [ -n "$backup_pid" ]; then
    kill -TERM "-$backup_pid" 2>/dev/null || true
    kill -KILL "-$backup_pid" 2>/dev/null || true
    backup_pid=
  fi
  [ "$stopping" -eq 0 ] || return 130
  return "$child_result"
}

check_target_mount() {
  for directory in "$backup_dir" "$versions_dir"; do
    if ! mountpoint -q "$directory"; then
      echo "Target is not a mounted filesystem: $directory" >&2; return 1
    fi
    actual_fs="$(stat -f -c %t "$directory" 2>/dev/null)" || return 1
    [ "$actual_fs" = "$expected_fs_magic" ] || { echo 'Target filesystem does not match EXPECTED_FS_MAGIC' >&2; return 1; }
    probe="$(mktemp "$directory/.yandex-backup-write-test.XXXXXX")" || return 1
    if ! rm -f -- "$probe"; then return 1; fi
  done
}

prune_versions() {
  [ "$retention_days" -eq 0 ] && return 0
  list="$(mktemp "$state_dir/.prune.XXXXXX")" || return 1
  # A failed enumeration must not authorize even a partial deletion list.
  if ! find "$versions_dir" -mindepth 1 -maxdepth 1 -type d \
      -name '20??????T??????Z' -mtime "+$retention_days" -print > "$list"; then
    rm -f -- "$list" 2>/dev/null || true
    return 1
  fi
  prune_result=0
  while IFS= read -r expired_dir || [ -n "$expired_dir" ]; do
    case "$expired_dir" in
      "$versions_dir"/20[0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z)
        if [ -L "$expired_dir" ] || [ ! -d "$expired_dir" ]; then prune_result=1; continue; fi
        if [ "$stopping" -ne 0 ]; then prune_result=1; break; fi
        if ! rm -rf -- "$expired_dir"; then prune_result=1; fi
        ;;
      *) echo 'Refusing unexpected quarantine path' >&2; prune_result=1 ;;
    esac
  done < "$list"
  if ! rm -f -- "$list"; then prune_result=1; fi
  return "$prune_result"
}

fail_run() {
  code="$1"; failure_class="$2"
  write_marker last-error "$run_id exit=$code class=$failure_class" || echo 'Cannot persist failure marker' >&2
  write_marker last-status "$run_id failed class=$failure_class" || true
  echo "Backup failed: account=$ACCOUNT_NAME class=$failure_class exit=$code" >&2
  return "$code"
}

run_backup() {
  failure_class=
  run_id="$(now_stamp)" || { echo 'Cannot establish run timestamp' >&2; return 1; }
  write_marker last-attempt "$run_id" || { echo 'Cannot persist attempt marker' >&2; return 1; }
  write_marker last-status "$run_id preflight" || return 1
  if ! check_target_mount; then fail_run 99 target_mount; return $?; fi
  errors="$(mktemp "$state_dir/.preflight.XXXXXX")" || { fail_run 1 local_state; return $?; }
  # Listing proves file access; `about` alone can succeed with a blocked OAuth app.
  run_child "$preflight_timeout" rclone lsf "$REMOTE_NAME:" --dirs-only --max-depth 1 \
    --retries 1 --low-level-retries 1 --contimeout 20s --timeout 60s --log-level ERROR \
    > /dev/null 2> "$errors"
  result=$?
  if [ "$result" -ne 0 ]; then
    failure_class=source_listing
    if grep -Eq '(\[|HTTP[[:space:]]+)410([^0-9]|$)' "$errors"; then failure_class=provider_access; fi
    if grep -Eq '(\[|HTTP[[:space:]]+)(401|403)([^0-9]|$)' "$errors"; then failure_class=authentication; fi
    rm -f -- "$errors" 2>/dev/null || true
    [ "$stopping" -eq 0 ] || failure_class=interrupted
    fail_run "$result" "$failure_class"; return $?
  fi
  if ! rm -f -- "$errors"; then fail_run 1 local_state; return $?; fi
  if [ "$stopping" -ne 0 ]; then fail_run 130 interrupted; return $?; fi
  if ! check_target_mount; then fail_run 99 target_mount; return $?; fi
  write_marker last-status "$run_id syncing" || return 1
  # Logs go to stdout/stderr and are bounded by Docker logging, including failures.
  run_child "$max_run_seconds" nice -n 10 rclone sync "$REMOTE_NAME:" "$backup_dir" \
    --backup-dir "$versions_dir/$run_id" --exclude '/.sync/**' --create-empty-src-dirs \
    --max-delete "$max_delete" --max-delete-size "$max_delete_size" \
    --transfers 2 --checkers 4 --multi-thread-streams 0 \
    --retries 3 --low-level-retries 3 --retries-sleep 10s --contimeout 30s --timeout 15m \
    --stats 1m --stats-one-line --log-level INFO
  result=$?
  if [ "$result" -ne 0 ]; then
    failure_class=sync
    [ "$stopping" -eq 0 ] || failure_class=interrupted
    fail_run "$result" "$failure_class"; return $?
  fi
  if [ "$stopping" -ne 0 ]; then fail_run 130 interrupted; return $?; fi
  if ! check_target_mount; then fail_run 99 target_mount; return $?; fi
  if ! prune_versions; then fail_run 1 retention; return $?; fi
  if [ "$stopping" -ne 0 ]; then fail_run 130 interrupted; return $?; fi
  # Completion time, not start time, defines freshness of a successful full pass.
  completed="$(now_stamp)" || { fail_run 1 local_state; return $?; }
  write_marker last-success "$completed" || { fail_run 1 local_state; return $?; }
  if ! rm -f -- "$state_dir/last-error"; then fail_run 1 local_state; return $?; fi
  write_marker last-status "$completed completed" || { fail_run 1 local_state; return $?; }
  echo "Backup completed: account=$ACCOUNT_NAME"
  return 0
}

if [ "$targets_only" -eq 1 ]; then check_target_mount; exit $?; fi
while [ "$stopping" -eq 0 ]; do
  run_backup
  result=$?
  [ "$once" -eq 0 ] || exit "$result"
  [ "$stopping" -eq 0 ] || break
  if [ "$result" -eq 0 ]; then
    delay="$interval"; retry_delay="$retry_initial"
  else
    case "$failure_class" in provider_access|authentication) delay="$retry_max" ;;
      *) delay="$retry_delay"; retry_delay=$((retry_delay * 2)); [ "$retry_delay" -le "$retry_max" ] || retry_delay="$retry_max" ;;
    esac
  fi
  echo "Next backup attempt in $delay seconds"
  sleep "$delay" & sleep_pid=$!
  wait "$sleep_pid" 2>/dev/null || true
  sleep_pid=
done
echo "Backup loop stopped: account=$ACCOUNT_NAME"
