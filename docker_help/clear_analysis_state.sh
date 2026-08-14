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
