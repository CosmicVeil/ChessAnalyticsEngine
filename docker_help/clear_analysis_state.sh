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

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
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
validated_spool_basenames=''
spool_directory=''
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
  if [[ -z "$spool_directory" ]]; then
    [[ -d "$spool_root" && ! -L "$spool_root" ]] || fail "unsafe spool directory: $spool_root"
    cd -P -- "$spool_root" || fail "unsafe spool directory: $spool_root"
    spool_directory="$PWD"
  fi
  [[ ! -L "$spool_basename" ]] || fail "unsafe spool path: $spool_path"
  if [[ -e "$spool_basename" ]]; then
    [[ -f "$spool_basename" ]] || fail "unsafe spool path: $spool_path"
    validated_spool_basenames+="$spool_basename"$'\n'
  fi
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

if ! docker compose -f "$compose_file" exec -T redpanda-0 \
  rpk group seek chess-state-managers --to end --topics chess-moves; then
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
  fail 'analysis cleanup transaction failed; spool files were retained'
fi

if [[ -n "$validated_spool_basenames" ]]; then
  while IFS= read -r spool_basename; do
    if [[ -f "$spool_basename" && ! -L "$spool_basename" ]]; then
      rm -- "$spool_basename"
    fi
  done <<<"$validated_spool_basenames"
fi

printf '%s\n' 'Unfinished analysis state cleared. Restart services and upload again.'
