#!/bin/sh
set -u

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "Required environment variable is missing: $name" >&2
    exit 2
  fi
}

require_env REMOTE_NAME
require_env ACCOUNT_NAME

interval="${INTERVAL_SECONDS:-1800}"
retention_days="${RETENTION_DAYS:-180}"
max_delete="${MAX_DELETE:-10000}"
max_delete_size="${MAX_DELETE_SIZE:-200Gi}"
stopping=0
backup_pid=

case "$retention_days" in
  ''|*[!0-9]*) echo "RETENTION_DAYS must be a non-negative integer" >&2; exit 2 ;;
esac

case "$max_delete" in
  ''|*[!0-9]*) echo "MAX_DELETE must be a non-negative integer" >&2; exit 2 ;;
esac

on_term() {
  stopping=1
  if [ -n "$backup_pid" ]; then
    kill -INT "$backup_pid" 2>/dev/null || true
    wait "$backup_pid" 2>/dev/null || true
  fi
}

trap on_term TERM INT

prune_versions() {
  [ "$retention_days" -eq 0 ] && return 0

  find /versions \
    -mindepth 1 \
    -maxdepth 1 \
    -type d \
    -name '20??????T??????Z' \
    -mtime "+${retention_days}" \
    -print | while IFS= read -r expired_dir; do
      case "$expired_dir" in
        /versions/20??????T??????Z)
          echo "[$(date -u +%FT%TZ)] Removing expired version set: ${expired_dir}"
          rm -rf -- "$expired_dir"
          ;;
        *)
          echo "Refusing to remove unexpected path: ${expired_dir}" >&2
          return 1
          ;;
      esac
    done
}

check_target_mount() {
  if [ ! -d /backup ]; then
    echo "[$(date -u +%FT%TZ)] ERROR: /backup directory does not exist!" >&2
    return 1
  fi

  if [ ! -f /backup/.mount_active ] && [ ! -f /versions/../.mount_active ]; then
    if ! touch /backup/.write_test 2>/dev/null; then
      echo "[$(date -u +%FT%TZ)] ERROR: /backup is not mounted or not writable! Aborting to protect root." >&2
      return 1
    fi
    rm -f /backup/.write_test 2>/dev/null || true
  fi

  return 0
}

run_backup() {
  if ! check_target_mount; then
    echo "[$(date -u +%FT%TZ)] Backup aborted for ${ACCOUNT_NAME}: target storage is not mounted." >&2
    printf '%s exit=99\n' "$(date -u +%Y%m%dT%H%M%SZ)" > /state/last-error
    return 1
  fi

  run_id="$(date -u +%Y%m%dT%H%M%SZ)"
  log_file="/logs/${run_id}.log"

  printf '%s\n' "$run_id" > /state/last-attempt
  echo "[$(date -u +%FT%TZ)] Starting backup for ${ACCOUNT_NAME} (${REMOTE_NAME})"

  nice -n 10 rclone sync "${REMOTE_NAME}:" /backup \
    --backup-dir "/versions/${run_id}" \
    --exclude '/.sync/**' \
    --create-empty-src-dirs \
    --max-delete "$max_delete" \
    --max-delete-size "$max_delete_size" \
    --transfers 2 \
    --checkers 4 \
    --multi-thread-streams 0 \
    --retries 3 \
    --low-level-retries 10 \
    --contimeout 30s \
    --timeout 15m \
    --stats 1m \
    --stats-one-line \
    --log-level INFO \
    --log-file "$log_file" &
  backup_pid=$!
  wait "$backup_pid"
  result=$?
  backup_pid=

  if [ "$result" -eq 0 ]; then
    if ! prune_versions >> "$log_file" 2>&1; then
      result=1
    fi
  fi

  if [ "$result" -eq 0 ]; then
    printf '%s\n' "$run_id" > /state/last-success
    rm -f /state/last-error
    echo "[$(date -u +%FT%TZ)] Backup completed for ${ACCOUNT_NAME}"
  else
    printf '%s exit=%s\n' "$run_id" "$result" > /state/last-error
    echo "[$(date -u +%FT%TZ)] Backup failed for ${ACCOUNT_NAME}, exit=${result}" >&2
  fi

  return "$result"
}

while [ "$stopping" -eq 0 ]; do
  run_backup || true
  [ "$stopping" -ne 0 ] && break
  sleep "$interval" &
  sleep_pid=$!
  wait "$sleep_pid" 2>/dev/null || true
done

echo "[$(date -u +%FT%TZ)] Backup loop stopped for ${ACCOUNT_NAME}"
