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
import json
import os
import stat
import sys
import uuid


DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)
FINGERPRINT_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_size",
    "st_mtime_ns",
)


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


def fingerprint(item):
    return [int(getattr(item, field)) for field in FINGERPRINT_FIELDS]


def parse_manifest(value):
    try:
        value = json.loads(value)
        if set(value) != {"version", "spool", "entries"} or value["version"] != 1:
            raise ValueError
        spool_identity = value["spool"]
        entries = value["entries"]
        if (
            not isinstance(spool_identity, list)
            or len(spool_identity) != 2
            or not all(type(part) is int for part in spool_identity)
            or not isinstance(entries, list)
        ):
            raise ValueError
        parsed_entries = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "fingerprint"}:
                raise ValueError
            name = entry["name"]
            expected = entry["fingerprint"]
            if (
                not isinstance(name, str)
                or not name
                or name in (".", "..")
                or "/" in name
                or name in seen
                or not isinstance(expected, list)
                or len(expected) != len(FINGERPRINT_FIELDS)
                or not all(type(part) is int for part in expected)
            ):
                raise ValueError
            seen.add(name)
            parsed_entries.append((name, expected))
        return tuple(spool_identity), parsed_entries
    except (TypeError, ValueError, json.JSONDecodeError):
        fail("invalid spool manifest")


def require_same_spool(spool_fd, device, inode):
    current = os.fstat(spool_fd)
    if (current.st_dev, current.st_ino) != (device, inode):
        fail("spool directory changed; refusing cleanup")


def freeze(repo_root):
    spool_fd = open_spool(repo_root)
    try:
        spool = os.fstat(spool_fd)
        entries = []
        for name in direct_basenames():
            try:
                item = os.stat(name, dir_fd=spool_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(item.st_mode):
                fail("unsafe spool file")
            entries.append({"name": name, "fingerprint": fingerprint(item)})
        print(
            json.dumps(
                {
                    "version": 1,
                    "spool": [int(spool.st_dev), int(spool.st_ino)],
                    "entries": entries,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    finally:
        os.close(spool_fd)


def safe_location(quarantine, moved_name):
    return f".runtime/pgn-spool/{quarantine}/{moved_name}"


def restore_without_replace(spool_fd, quarantine_fd, name, moved_name):
    try:
        os.link(
            moved_name,
            name,
            src_dir_fd=quarantine_fd,
            dst_dir_fd=spool_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        return "private", "the original name is occupied"
    except OSError as error:
        return "private", f"no-replace restore failed: {error}"
    try:
        os.unlink(moved_name, dir_fd=quarantine_fd)
    except OSError as error:
        return "both", f"restored link but private link remains: {error}"
    return "original", ""


def cleanup(repo_root, manifest):
    spool_identity, entries = parse_manifest(manifest)
    spool_fd = open_spool(repo_root)
    quarantine = ".cleanup-" + uuid.uuid4().hex
    quarantine_fd = None
    deleted = 0
    already_absent = 0
    retained = 0
    errors = 0
    try:
        require_same_spool(spool_fd, *spool_identity)
        os.mkdir(quarantine, mode=0o700, dir_fd=spool_fd)
        quarantine_fd = os.open(quarantine, DIRECTORY_FLAGS, dir_fd=spool_fd)
        for index, (name, expected) in enumerate(entries):
            moved_name = f"{index:08x}-{uuid.uuid4().hex}"
            try:
                os.rename(
                    name,
                    moved_name,
                    src_dir_fd=spool_fd,
                    dst_dir_fd=quarantine_fd,
                )
            except FileNotFoundError:
                already_absent += 1
                print(f"Spool file already absent; nothing removed: {name}")
                continue
            except OSError as error:
                retained += 1
                errors += 1
                print(f"Spool file retained at original name {name}: {error}")
                continue

            location = safe_location(quarantine, moved_name)
            try:
                moved = os.stat(
                    moved_name, dir_fd=quarantine_fd, follow_symlinks=False
                )
            except OSError as error:
                retained += 1
                errors += 1
                print(f"Moved spool entry retained at {location}: {error}")
                continue

            if fingerprint(moved) == expected:
                try:
                    os.unlink(moved_name, dir_fd=quarantine_fd)
                except OSError as error:
                    retained += 1
                    errors += 1
                    print(f"Verified spool file retained at {location}: {error}")
                else:
                    deleted += 1
                    print(f"Deleted verified spool file: {name}")
                continue

            retained += 1
            outcome, detail = restore_without_replace(
                spool_fd, quarantine_fd, name, moved_name
            )
            if outcome == "original":
                print(f"Changed spool file retained at original name: {name}")
            elif outcome == "both":
                errors += 1
                print(
                    f"Changed spool file retained at {name} and {location}: {detail}"
                )
            else:
                if not detail.startswith("the original name is occupied"):
                    errors += 1
                print(f"Changed spool file retained at {location}: {detail}")
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        os.close(spool_fd)

    spool_fd = open_spool(repo_root)
    quarantine_retained = False
    try:
        require_same_spool(spool_fd, *spool_identity)
        try:
            os.rmdir(quarantine, dir_fd=spool_fd)
        except OSError as error:
            quarantine_retained = True
            errors += 1
            print(
                "Private spool cleanup directory retained at "
                f".runtime/pgn-spool/{quarantine}: {error}"
            )
    finally:
        os.close(spool_fd)

    print(
        "Spool cleanup summary: "
        f"deleted={deleted}, already_absent={already_absent}, "
        f"retained={retained}, errors={errors}, "
        f"recovery_directories={int(quarantine_retained)}"
    )
    if retained or errors or quarantine_retained:
        raise SystemExit(1)


try:
    root, action = sys.argv[1:3]
    if action == "freeze" and len(sys.argv) == 3:
        freeze(root)
    elif action == "cleanup" and len(sys.argv) == 4:
        cleanup(root, sys.argv[3])
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

spool_manifest=''
if [[ -n "$spool_basenames" ]]; then
  if ! spool_manifest="$(spool_helper freeze)"; then
    fail 'could not safely freeze spool manifest'
  fi
fi

if ! docker compose -f "$compose_file" exec -T redpanda-0 \
  rpk group seek chess-state-managers --to end --topics chess-moves; then
  fail 'Kafka group seek failed; PostgreSQL was not changed and spool cleanup was not attempted'
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
  fail 'analysis cleanup transaction failed; spool cleanup was not attempted'
fi

if [[ -n "$spool_manifest" ]] && ! spool_helper cleanup "$spool_manifest"; then
  fail 'analysis cleanup committed; spool cleanup incomplete; per-file status is shown above'
fi

printf '%s\n' 'Unfinished analysis state cleared. Restart services and upload again.'
