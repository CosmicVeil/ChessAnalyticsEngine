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

command -v pgrep >/dev/null 2>&1 || fail 'pgrep is required to inspect application processes'

require_no_processes() {
  inspection_label="$1"
  active_message="$2"
  shift 2
  for process_pattern in "$@"; do
    if pgrep -f "$process_pattern" >/dev/null 2>&1; then
      fail "$active_message"
    else
      pgrep_status=$?
    fi
    if [[ "$pgrep_status" -ne 1 ]]; then
      fail "could not inspect $inspection_label processes (pgrep exit $pgrep_status); refusing cleanup"
    fi
  done
}

require_no_processes \
  'ingestion worker' \
  'ingestion worker is running; stop the app processes first' \
  '(^|/)(python|python3)([0-9.]*)? -m webapp[.]ingestion_worker$'

require_no_processes \
  'Go feature extractor' \
  'Go feature extractor is running; stop the app processes first' \
  '(^|/)go[[:space:]]+run[[:space:]]+([.]|[.]/cmd/extractor)([[:space:]]|$)' \
  '(^|/)go-build[^/[:space:]]*/[^/[:space:]]*/exe/processor([[:space:]]|$)'

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
target_predicate="analysis_jobs.status IN ('queued', 'parsing', 'live')"
if [[ -n "$stale_minutes" ]]; then
  target_predicate+=" AND analysis_jobs.lease_expires_at < NOW() AND analysis_jobs.updated_at < NOW() - make_interval(mins => :'stale_minutes'::integer)"
fi

run_psql() {
  if [[ -n "$stale_minutes" ]]; then
    set -- -v "stale_minutes=$stale_minutes" "$@"
  fi
  docker compose -f "$compose_file" exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U chess -d chess_analytics "$@"
}

spool_helper() {
  python3 - "$repo_root" "$@" 3<&0 <<'PY'
import json
import os
import re
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
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
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


def input_lines():
    with os.fdopen(3, "r", encoding="utf-8", closefd=False) as source:
        for line in source:
            yield line.rstrip("\n")


def direct_entries():
    jobs_by_name = {}
    for line in input_lines():
        if not line:
            continue
        try:
            job_id, name = line.split("|", 1)
            if UUID_PATTERN.fullmatch(job_id) is None:
                raise ValueError
        except ValueError:
            fail("invalid spool manifest input")
        if not name or name in (".", "..") or "/" in name or "|" in name:
            fail("unsafe spool basename")
        jobs_by_name.setdefault(name, set()).add(job_id.lower())
    return jobs_by_name


def fingerprint(item):
    return [int(getattr(item, field)) for field in FINGERPRINT_FIELDS]


def parse_manifest(path):
    try:
        manifest_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        manifest_fd = os.open(path, manifest_flags)
        try:
            manifest_stat = os.fstat(manifest_fd)
            if (
                not stat.S_ISREG(manifest_stat.st_mode)
                or manifest_stat.st_nlink != 1
                or manifest_stat.st_uid != os.getuid()
                or manifest_stat.st_mode & 0o077
            ):
                raise ValueError
            with os.fdopen(manifest_fd, "r", encoding="utf-8") as manifest_file:
                manifest_fd = -1
                value = json.load(manifest_file)
        finally:
            if manifest_fd >= 0:
                os.close(manifest_fd)
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
            if not isinstance(entry, dict) or set(entry) != {
                "name",
                "fingerprint",
                "job_ids",
            }:
                raise ValueError
            name = entry["name"]
            expected = entry["fingerprint"]
            job_ids = entry["job_ids"]
            if (
                not isinstance(name, str)
                or not name
                or name in (".", "..")
                or "/" in name
                or "|" in name
                or name in seen
                or not isinstance(expected, list)
                or len(expected) != len(FINGERPRINT_FIELDS)
                or not all(type(part) is int for part in expected)
                or not isinstance(job_ids, list)
                or not job_ids
                or len(set(job_ids)) != len(job_ids)
            ):
                raise ValueError
            for job_id in job_ids:
                if (
                    not isinstance(job_id, str)
                    or UUID_PATTERN.fullmatch(job_id) is None
                ):
                    raise ValueError
            seen.add(name)
            parsed_entries.append((name, expected, set(job_ids)))
        return tuple(spool_identity), parsed_entries
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
        for name, job_ids in sorted(direct_entries().items()):
            try:
                item = os.stat(name, dir_fd=spool_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(item.st_mode):
                fail("unsafe spool file")
            entries.append(
                {
                    "name": name,
                    "fingerprint": fingerprint(item),
                    "job_ids": sorted(job_ids),
                }
            )
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


def deleted_target_ids():
    deleted = set()
    for job_id in input_lines():
        if not job_id:
            continue
        try:
            if UUID_PATTERN.fullmatch(job_id) is None:
                raise ValueError
        except ValueError:
            fail("invalid deleted target id")
        deleted.add(job_id.lower())
    return deleted


def cleanup(repo_root, manifest_path):
    spool_identity, entries = parse_manifest(manifest_path)
    deleted_jobs = deleted_target_ids()
    eligible_entries = []
    skipped = 0
    for name, expected, job_ids in entries:
        if job_ids.issubset(deleted_jobs):
            eligible_entries.append((name, expected))
        else:
            skipped += 1
            print(f"Spool file retained for skipped target: {name}")
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
        for index, (name, expected) in enumerate(eligible_entries):
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
        f"recovery_directories={int(quarantine_retained)}, skipped={skipped}"
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

spool_manifest_file=''
maintenance_output_file=''
cleanup_private_temp_files() {
  if [[ -n "$spool_manifest_file" ]]; then
    rm -f -- "$spool_manifest_file"
  fi
  if [[ -n "$maintenance_output_file" ]]; then
    rm -f -- "$maintenance_output_file"
  fi
}
trap cleanup_private_temp_files EXIT

create_private_temp_file() {
  previous_umask="$(umask)"
  umask 077
  private_temp_file="$(mktemp "${TMPDIR:-/tmp}/clear-analysis-state.XXXXXX")"
  temp_status=$?
  umask "$previous_umask"
  [[ "$temp_status" -eq 0 ]] || return "$temp_status"
  printf '%s\n' "$private_temp_file"
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
spool_records=''
spool_record_count=0
while IFS='|' read -r job_id spool_path game_count; do
  [[ -n "$job_id" ]] || continue
  [[ "$job_id" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || \
    fail "unsafe target job id: $job_id"
  [[ "${game_count:-}" =~ ^[0-9]+$ ]] || fail "unsafe game count for target job: $job_id"
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
  [[ "$spool_basename" != '.' && "$spool_basename" != '..' && "$spool_basename" != *'|'* ]] || \
    fail "unsafe spool path: $spool_path"
  spool_records+="$job_id|$spool_basename"$'\n'
  ((spool_record_count += 1))
done <<<"$target_rows"

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

if [[ -n "$spool_records" ]]; then
  if ! spool_manifest_file="$(create_private_temp_file)"; then
    fail 'could not create private spool manifest file'
  fi
  exec 4>"$spool_manifest_file"
  if ! printf '%s' "$spool_records" | spool_helper freeze >&4; then
    exec 4>&-
    fail 'could not safely freeze spool manifest'
  fi
  exec 4>&-
  printf 'Frozen spool manifest for %s confirmed target path(s).\n' "$spool_record_count"
fi

if ! "$yes"; then
  printf '%s' 'Type CLEAR to delete the previewed unfinished analysis state: ' >&2
  if ! IFS= read -r confirmation || [[ "$confirmation" != 'CLEAR' ]]; then
    fail 'cleanup confirmation declined'
  fi
fi

if ! docker compose -f "$compose_file" exec -T redpanda-0 \
  rpk group seek chess-state-managers --to end --topics chess-moves --allow-new-topics; then
  fail 'Kafka group seek failed; PostgreSQL was not changed and spool cleanup was not attempted'
fi

if ! post_seek_description="$(docker compose -f "$compose_file" exec -T redpanda-0 \
  rpk group describe chess-state-managers --print-commits)"; then
  fail 'could not verify Kafka commits after seek; PostgreSQL was not changed'
fi
printf '%s\n' "$post_seek_description"
if ! printf '%s\n' "$post_seek_description" | awk '
  $1 == "TOPIC" {
    for (column = 1; column <= NF; column += 1) {
      index_by_name[$column] = column
    }
    if (!("TOPIC" in index_by_name) || !("PARTITION" in index_by_name) ||
        !("CURRENT-OFFSET" in index_by_name) ||
        !("LOG-END-OFFSET" in index_by_name) || !("LAG" in index_by_name)) {
      invalid = 1
    }
    header = 1
    next
  }
  header && NF && $index_by_name["TOPIC"] == "chess-moves" {
    partition = $index_by_name["PARTITION"]
    current = $index_by_name["CURRENT-OFFSET"]
    log_end = $index_by_name["LOG-END-OFFSET"]
    lag = $index_by_name["LAG"]
    if (partition !~ /^[0-9]+$/ || current !~ /^[0-9]+$/ ||
        log_end !~ /^[0-9]+$/ || lag !~ /^[0-9]+$/ ||
        lag != 0 || current != log_end || seen[partition]++) {
      invalid = 1
    }
    rows += 1
  }
  END { exit !(header && rows > 0 && !invalid) }
'; then
  fail 'could not prove zero chess-moves lag for every described partition; PostgreSQL was not changed'
fi

if [[ -n "$target_values" ]]; then
  target_insert_sql="INSERT INTO confirmed_targets (id) VALUES $target_values;"
else
  target_insert_sql='INSERT INTO confirmed_targets (id) SELECT NULL::uuid WHERE FALSE;'
fi

maintenance_sql="
BEGIN;
CREATE TEMPORARY TABLE confirmed_targets (id uuid PRIMARY KEY) ON COMMIT DROP;
$target_insert_sql

SELECT analysis_jobs.id
FROM analysis_jobs
JOIN confirmed_targets ON confirmed_targets.id = analysis_jobs.id
ORDER BY analysis_jobs.id
FOR UPDATE OF analysis_jobs;

CREATE TEMPORARY TABLE locked_targets (
  id uuid PRIMARY KEY,
  status varchar(16) NOT NULL
) ON COMMIT DROP;
INSERT INTO locked_targets (id, status)
SELECT analysis_jobs.id, analysis_jobs.status
FROM analysis_jobs
JOIN confirmed_targets ON confirmed_targets.id = analysis_jobs.id;

CREATE TEMPORARY TABLE cleanup_targets (id uuid PRIMARY KEY) ON COMMIT DROP;
INSERT INTO cleanup_targets (id)
SELECT analysis_jobs.id
FROM analysis_jobs
JOIN locked_targets ON locked_targets.id = analysis_jobs.id
WHERE $target_predicate;

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

CREATE TEMPORARY TABLE deleted_targets (id uuid PRIMARY KEY) ON COMMIT DROP;
WITH deleted AS (
  DELETE FROM analysis_jobs
  WHERE id IN (SELECT id FROM cleanup_targets)
  RETURNING id
)
INSERT INTO deleted_targets (id)
SELECT id FROM deleted;

SELECT 'CLEANUP_DELETED|' || id::text
FROM deleted_targets
ORDER BY id;
SELECT 'CLEANUP_SKIPPED|' || confirmed_targets.id::text || '|' ||
       CASE
         WHEN locked_targets.id IS NULL THEN 'missing'
         WHEN locked_targets.status IN ('completed', 'partial', 'failed')
           THEN locked_targets.status
         ELSE 'no-longer-eligible'
       END
FROM confirmed_targets
LEFT JOIN locked_targets ON locked_targets.id = confirmed_targets.id
LEFT JOIN cleanup_targets ON cleanup_targets.id = confirmed_targets.id
WHERE cleanup_targets.id IS NULL
ORDER BY confirmed_targets.id;

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

if ! maintenance_output_file="$(create_private_temp_file)"; then
  fail 'could not create private transaction output file'
fi
if ! run_psql -At -F '|' >"$maintenance_output_file" <<<"$maintenance_sql"; then
  fail 'analysis cleanup transaction failed; spool cleanup was not attempted'
fi

while IFS='|' read -r result_kind result_job_id result_status; do
  [[ "$result_kind" == 'CLEANUP_SKIPPED' ]] || continue
  [[ "$result_job_id" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || \
    fail 'analysis cleanup returned an invalid skipped target id'
  case "$result_status" in
    completed|partial|failed)
      printf 'Skipped now-terminal target: %s (status: %s)\n' \
        "$result_job_id" "$result_status"
      ;;
    *)
      printf 'Skipped raced target: %s (%s)\n' "$result_job_id" "$result_status"
      ;;
  esac
done <"$maintenance_output_file"

if [[ -n "$spool_manifest_file" ]] && \
  ! awk -F '|' '$1 == "CLEANUP_DELETED" { print $2 }' "$maintenance_output_file" | \
    spool_helper cleanup "$spool_manifest_file"; then
  fail 'analysis cleanup committed; spool cleanup incomplete; per-file status is shown above'
fi

terminal_report_sql="
WITH terminal_statuses(status) AS (
  VALUES ('completed'), ('partial'), ('failed')
)
SELECT 'TERMINAL|' || terminal_statuses.status || '|' || COUNT(analysis_jobs.id)::text
FROM terminal_statuses
LEFT JOIN analysis_jobs ON analysis_jobs.status = terminal_statuses.status
GROUP BY terminal_statuses.status
UNION ALL
SELECT 'TERMINAL|total|' || COUNT(*)::text
FROM analysis_jobs
WHERE analysis_jobs.status IN ('completed', 'partial', 'failed');
"
if ! terminal_rows="$(run_psql -At -F '|' <<<"$terminal_report_sql")"; then
  fail 'could not report remaining terminal analysis jobs; cleanup was committed'
fi

terminal_completed=''
terminal_partial=''
terminal_failed=''
terminal_total=''
terminal_report_valid=true
while IFS='|' read -r terminal_marker terminal_status terminal_count; do
  [[ -n "$terminal_marker" ]] || continue
  if [[ "$terminal_marker" != 'TERMINAL' || ! "$terminal_count" =~ ^[0-9]+$ ]]; then
    terminal_report_valid=false
    continue
  fi
  case "$terminal_status" in
    completed) [[ -z "$terminal_completed" ]] || terminal_report_valid=false; terminal_completed="$terminal_count" ;;
    partial) [[ -z "$terminal_partial" ]] || terminal_report_valid=false; terminal_partial="$terminal_count" ;;
    failed) [[ -z "$terminal_failed" ]] || terminal_report_valid=false; terminal_failed="$terminal_count" ;;
    total) [[ -z "$terminal_total" ]] || terminal_report_valid=false; terminal_total="$terminal_count" ;;
    *) terminal_report_valid=false ;;
  esac
done <<<"$terminal_rows"

if ! "$terminal_report_valid" || [[ -z "$terminal_completed" || -z "$terminal_partial" || \
  -z "$terminal_failed" || -z "$terminal_total" ]] || \
  ((terminal_completed + terminal_partial + terminal_failed != terminal_total)); then
  fail 'could not validate remaining terminal analysis job counts; cleanup was committed'
fi

printf 'Remaining terminal jobs: completed=%s, partial=%s, failed=%s, total=%s\n' \
  "$terminal_completed" "$terminal_partial" "$terminal_failed" "$terminal_total"
printf '%s\n' 'Unfinished analysis state cleared. Restart services and upload again.'
