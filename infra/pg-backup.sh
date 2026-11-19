#!/bin/sh
# Nightly logical backup of the chess_analytics database for compose.prod.yaml.
#
#   pg-backup.sh          sleep until BACKUP_HOUR_UTC, dump, prune, repeat
#   pg-backup.sh --once   dump and prune immediately, then exit
#
# Connection comes from the standard libpq variables (PGHOST, PGUSER,
# PGPASSWORD, PGDATABASE). Dumps are plain SQL, gzipped, so a restore needs
# only psql and gunzip.
set -eu
set -o pipefail

BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-7}
BACKUP_HOUR_UTC=${BACKUP_HOUR_UTC:-3}

# Dumps contain password hashes and private PGN metadata: owner-only files.
umask 077

backup() {
	target="$BACKUP_DIR/${PGDATABASE}-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
	# Write under a temporary name and rename only after pg_dump succeeds, so
	# a failed or interrupted dump never looks like a valid backup.
	if pg_dump --no-owner --no-privileges | gzip > "$target.partial"; then
		mv "$target.partial" "$target"
		echo "backup: wrote $target ($(du -h "$target" | cut -f1))"
		# Prune only after a successful dump: if dumps start failing, the
		# last good backups are kept instead of aging out.
		find "$BACKUP_DIR" -name "${PGDATABASE}-*.sql.gz" -type f \
			-mtime +"$((BACKUP_RETENTION_DAYS - 1))" -print -delete \
			| sed 's/^/backup: pruned /'
	else
		rm -f "$target.partial"
		echo "backup: pg_dump FAILED; previous backups kept" >&2
		return 1
	fi
}

if [ "${1:-}" = "--once" ]; then
	backup
	exit
fi

while :; do
	# Seconds until the next BACKUP_HOUR_UTC:00, from integer arithmetic so
	# it does not depend on busybox date parsing.
	now=$(date -u +%s)
	wait=$(( (BACKUP_HOUR_UTC * 3600 - now % 86400 + 86400) % 86400 ))
	[ "$wait" -eq 0 ] && wait=86400
	echo "backup: next run in ${wait}s"
	sleep "$wait"
	backup || true
done
