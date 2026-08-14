#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: clear_analysis_state.sh [--yes] [--stale-minutes MINUTES]'
}

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

yes=false
stale_minutes=''

while (($#)); do
  case "$1" in
    --help)
      usage
      exit 0
      ;;
    --yes)
      yes=true
      shift
      ;;
    --stale-minutes)
      (($# >= 2)) || fail '--stale-minutes requires MINUTES'
      [[ "$2" =~ ^[0-9]+$ ]] || fail '--stale-minutes must be a non-negative integer'
      stale_minutes="$2"
      shift 2
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

script_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -P -- "$script_dir/.." && pwd -P)"
compose_file="$repo_root/infra/docker-compose.yaml"

[[ -f "$compose_file" ]] || fail "Docker Compose file not found: $compose_file"

if pgrep -f '(^|/)(python|python3)([0-9.]*)? -m webapp[.]ingestion_worker$' >/dev/null; then
  fail 'ingestion worker is running; stop the app processes first'
fi

if pgrep -f '(^|/)go[[:space:]]+run[[:space:]]+([.]|[.]/cmd/extractor)([[:space:]]|$)' >/dev/null || \
  pgrep -f '(^|/)go-build[^/[:space:]]*/[^/[:space:]]*/exe/processor([[:space:]]|$)' >/dev/null; then
  fail 'Go feature extractor is running; stop the app processes first'
fi

available_services="$(docker compose -f "$compose_file" config --services)"
missing_services=()
for required_service in postgres redpanda-0; do
  if ! printf '%s\n' "$available_services" | grep -Fxq "$required_service"; then
    missing_services+=("$required_service")
  fi
done

if ((${#missing_services[@]})); then
  fail "missing required Docker services: ${missing_services[*]}"
fi

printf '%s\n' 'Safety checks passed.'

spool_root="$repo_root/.runtime/pgn-spool"
target_predicate="status IN ('queued', 'parsing', 'live')"
if [[ -n "$stale_minutes" ]]; then
  target_predicate+=" AND lease_expires_at < NOW() AND updated_at < NOW() - make_interval(mins => :'stale_minutes'::integer)"
fi

run_psql() {
  if [[ -n "$stale_minutes" ]]; then
    set -- -v "stale_minutes=$stale_minutes" "$@"
  fi
  docker compose -f "$compose_file" exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U chess -d chess_analytics "$@"
}

spool_helper() {
  SPOOL_BASENAMES="$spool_basenames" python3 - "$repo_root" "$@" <<'PY'
import os
import stat
import sys
import uuid


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def open_directory(path):
    if not os.path.isabs(path):
        fail("repository root is not absolute")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in path.split("/")[1:]:
            if not component:
                continue
            if component in (".", ".."):
                fail("repository root is not canonical")
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_spool(repo_root):
    repo_fd = open_directory(repo_root)
    try:
        runtime_fd = os.open(".runtime", DIRECTORY_FLAGS, dir_fd=repo_fd)
        try:
            spool_fd = os.open("pgn-spool", DIRECTORY_FLAGS, dir_fd=runtime_fd)
        finally:
            os.close(runtime_fd)
    finally:
        os.close(repo_fd)
    return spool_fd


def direct_basenames():
    names = []
    for name in os.environ.get("SPOOL_BASENAMES", "").splitlines():
        if not name:
            continue
        if name in (".", "..") or "/" in name:
            fail("unsafe spool basename")
        if name not in names:
            names.append(name)
    return names


def metadata(value):
    try:
        quarantine, device, inode = value.split("|", 2)
        if not quarantine.startswith(".cleanup-") or len(quarantine) != 41:
            raise ValueError
        int(quarantine[9:], 16)
        return quarantine, int(device), int(inode)
    except ValueError:
        fail("invalid spool quarantine metadata")


def require_same_spool(spool_fd, device, inode):
    current = os.fstat(spool_fd)
    if (current.st_dev, current.st_ino) != (device, inode):
        fail("spool directory changed; refusing cleanup")


def prepare(repo_root):
    spool_fd = open_spool(repo_root)
    quarantine_fd = None
    moved = []
    quarantine = ".cleanup-" + uuid.uuid4().hex
    try:
        original = os.fstat(spool_fd)
        os.mkdir(quarantine, mode=0o700, dir_fd=spool_fd)
        quarantine_fd = os.open(quarantine, DIRECTORY_FLAGS, dir_fd=spool_fd)
        for name in direct_basenames():
            try:
                item = os.stat(name, dir_fd=spool_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(item.st_mode):
                fail("unsafe spool file")
            os.rename(name, name, src_dir_fd=spool_fd, dst_dir_fd=quarantine_fd)
            moved.append(name)
        print(f"{quarantine}|{original.st_dev}|{original.st_ino}")
    except BaseException:
        if quarantine_fd is not None:
            for name in reversed(moved):
                try:
                    os.rename(name, name, src_dir_fd=quarantine_fd, dst_dir_fd=spool_fd)
                except OSError:
                    pass
            os.close(quarantine_fd)
            quarantine_fd = None
        try:
            os.rmdir(quarantine, dir_fd=spool_fd)
        except OSError:
            pass
        raise
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        os.close(spool_fd)


def reopen_quarantine(repo_root, value):
    quarantine, device, inode = metadata(value)
    spool_fd = open_spool(repo_root)
    require_same_spool(spool_fd, device, inode)
    try:
        quarantine_fd = os.open(quarantine, DIRECTORY_FLAGS, dir_fd=spool_fd)
    except BaseException:
        os.close(spool_fd)
        raise
    return spool_fd, quarantine_fd, quarantine


def restore(repo_root, value):
    spool_fd, quarantine_fd, quarantine = reopen_quarantine(repo_root, value)
    try:
        for name in os.listdir(quarantine_fd):
            item = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                fail("unsafe quarantined spool file")
            try:
                os.stat(name, dir_fd=spool_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                fail("spool file appeared during restore")
            os.rename(name, name, src_dir_fd=quarantine_fd, dst_dir_fd=spool_fd)
    finally:
        os.close(quarantine_fd)
        os.close(spool_fd)
    spool_fd = open_spool(repo_root)
    try:
        _, device, inode = metadata(value)
        require_same_spool(spool_fd, device, inode)
        os.rmdir(quarantine, dir_fd=spool_fd)
    finally:
        os.close(spool_fd)


def discard(repo_root, value):
    spool_fd, quarantine_fd, quarantine = reopen_quarantine(repo_root, value)
    try:
        for name in os.listdir(quarantine_fd):
            item = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                fail("unsafe quarantined spool file")
            os.unlink(name, dir_fd=quarantine_fd)
    finally:
        os.close(quarantine_fd)
        os.close(spool_fd)
    spool_fd = open_spool(repo_root)
    try:
        _, device, inode = metadata(value)
        require_same_spool(spool_fd, device, inode)
        os.rmdir(quarantine, dir_fd=spool_fd)
    finally:
        os.close(spool_fd)


try:
    root, action = sys.argv[1:3]
    if action == "prepare":
        prepare(root)
    elif action == "restore" and len(sys.argv) == 4:
        restore(root, sys.argv[3])
    elif action == "discard" and len(sys.argv) == 4:
        discard(root, sys.argv[3])
    else:
        fail("invalid spool helper action")
except OSError as error:
    fail(f"spool helper failed: {error}")
PY
}

preview_sql="
SELECT analysis_jobs.id,
       COALESCE(analysis_jobs.spool_path, ''),
       COUNT(analysis_games.id)
FROM analysis_jobs
LEFT JOIN analysis_games ON analysis_games.job_id = analysis_jobs.id
WHERE $target_predicate
GROUP BY analysis_jobs.id, analysis_jobs.spool_path
ORDER BY MIN(analysis_jobs.created_at), analysis_jobs.id;
"

if ! target_rows="$(run_psql -At -F '|' <<<"$preview_sql")"; then
  fail 'could not preview unfinished analysis jobs'
fi

target_values=''
spool_basenames=''
while IFS='|' read -r job_id spool_path game_count; do
  [[ -n "$job_id" ]] || continue
  [[ "$job_id" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || \
    fail "unsafe target job id: $job_id"
  target_values+="${target_values:+, }('$job_id'::uuid)"
  printf 'Target job: %s (games: %s%s)\n' \
    "$job_id" "${game_count:-0}" "${spool_path:+; spool: $spool_path}"

  [[ -n "$spool_path" ]] || continue

  case "$spool_path" in
    /*) candidate_path="$spool_path" ;;
    *) candidate_path="$repo_root/$spool_path" ;;
  esac
  case "$candidate_path" in
    "$spool_root"/*) ;;
    *) fail "unsafe spool path: $spool_path" ;;
  esac

  [[ "$(dirname -- "$candidate_path")" == "$spool_root" ]] || fail "unsafe spool path: $spool_path"
  spool_basename="$(basename -- "$candidate_path")"
  [[ "$spool_basename" != '.' && "$spool_basename" != '..' ]] || fail "unsafe spool path: $spool_path"
  spool_basenames+="$spool_basename"$'\n'
done <<<"$target_rows"

if ! "$yes"; then
  printf '%s' 'Type CLEAR to delete the previewed unfinished analysis state: ' >&2
  if ! IFS= read -r confirmation || [[ "$confirmation" != 'CLEAR' ]]; then
    fail 'cleanup confirmation declined'
  fi
fi

if ! group_description="$(docker compose -f "$compose_file" exec -T redpanda-0 \
  rpk group describe chess-state-managers)"; then
  fail 'could not describe Kafka consumer group chess-state-managers'
fi
printf '%s\n' "$group_description"
group_members="$(printf '%s\n' "$group_description" | awk '
  $1 == "MEMBERS" && NF == 2 { print $2; exit }
')"
[[ "$group_members" =~ ^[0-9]+$ && "$group_members" == '0' ]] || \
  fail 'Kafka group has active members; stop the application first'

quarantine_metadata=''
if [[ -n "$spool_basenames" ]]; then
  if ! quarantine_metadata="$(spool_helper prepare)"; then
    fail 'could not safely quarantine spool files'
  fi
fi

if ! docker compose -f "$compose_file" exec -T redpanda-0 \
  rpk group seek chess-state-managers --to end --topics chess-moves; then
  if [[ -n "$quarantine_metadata" ]] && ! spool_helper restore "$quarantine_metadata"; then
    fail 'Kafka group seek failed and spool files could not be restored'
  fi
  fail 'Kafka group seek failed; PostgreSQL was not changed'
fi

if [[ -n "$target_values" ]]; then
  target_insert_sql="INSERT INTO cleanup_targets (id) VALUES $target_values;"
else
  target_insert_sql='INSERT INTO cleanup_targets (id) SELECT NULL::uuid WHERE FALSE;'
fi

maintenance_sql="
BEGIN;
CREATE TEMPORARY TABLE cleanup_targets (id uuid PRIMARY KEY) ON COMMIT DROP;
$target_insert_sql

SET LOCAL session_replication_role = replica;
DELETE FROM unmatched_results
WHERE pipeline_game_id IN (
  SELECT analysis_games.pipeline_game_id
  FROM analysis_games
  JOIN cleanup_targets ON cleanup_targets.id = analysis_games.job_id
);
DELETE FROM job_events
WHERE job_id IN (SELECT id FROM cleanup_targets);
DELETE FROM analysis_results
WHERE game_id IN (
  SELECT analysis_games.id
  FROM analysis_games
  JOIN cleanup_targets ON cleanup_targets.id = analysis_games.job_id
);
DELETE FROM analysis_games
WHERE job_id IN (SELECT id FROM cleanup_targets);
DELETE FROM analysis_jobs
WHERE id IN (SELECT id FROM cleanup_targets);

DO \$\$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM analysis_games
    LEFT JOIN analysis_jobs ON analysis_jobs.id = analysis_games.job_id
    WHERE analysis_jobs.id IS NULL
  ) THEN
    RAISE EXCEPTION 'orphaned analysis_games row';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM analysis_results
    LEFT JOIN analysis_games ON analysis_games.id = analysis_results.game_id
    WHERE analysis_games.id IS NULL
  ) THEN
    RAISE EXCEPTION 'orphaned analysis_results row';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM job_events
    LEFT JOIN analysis_jobs ON analysis_jobs.id = job_events.job_id
    WHERE analysis_jobs.id IS NULL
  ) THEN
    RAISE EXCEPTION 'orphaned job_events job row';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM job_events
    LEFT JOIN analysis_games ON analysis_games.id = job_events.game_id
    WHERE job_events.game_id IS NOT NULL AND analysis_games.id IS NULL
  ) THEN
    RAISE EXCEPTION 'orphaned job_events game row';
  END IF;
END
\$\$;
COMMIT;
"

if ! run_psql <<<"$maintenance_sql"; then
  if [[ -n "$quarantine_metadata" ]] && ! spool_helper restore "$quarantine_metadata"; then
    fail 'analysis cleanup transaction failed and spool files could not be restored'
  fi
  fail 'analysis cleanup transaction failed; spool files were retained'
fi

if [[ -n "$quarantine_metadata" ]] && ! spool_helper discard "$quarantine_metadata"; then
  fail 'analysis cleanup succeeded but quarantined spool files were retained'
fi

printf '%s\n' 'Unfinished analysis state cleared. Restart services and upload again.'
