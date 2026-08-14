import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


class ClearAnalysisStateCommandTests(unittest.TestCase):
    """Command-boundary tests for the local analysis cleanup utility."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo = Path(self.tempdir.name) / "repo"
        self.repo.mkdir()
        (self.repo / "docker_help").mkdir()
        (self.repo / "infra").mkdir()
        (self.repo / "bin").mkdir()
        (self.repo / ".runtime" / "pgn-spool").mkdir(parents=True)
        (self.repo / "infra" / "docker-compose.yaml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        self.log_path = self.repo / "docker.log"

        source_script = Path(__file__).parents[2] / "docker_help" / "clear_analysis_state.sh"
        self.assertTrue(source_script.is_file(), "cleanup script must exist")
        self.script = self.repo / "docker_help" / "clear_analysis_state.sh"
        shutil.copy2(source_script, self.script)
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)

        self._write_fake_command(
            "docker",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' \"$*\" >> \"${FAKE_DOCKER_LOG:?}\"
observe_spool() {
  local boundary="$1"
  if [[ -z "${FAKE_OBSERVE_SPOOL_FILE:-}" ]]; then
    return
  fi
  if [[ -f "${FAKE_OBSERVE_SPOOL_FILE}" && ! -L "${FAKE_OBSERVE_SPOOL_FILE}" ]]; then
    printf 'SPOOL_%s:present:%s\\n' \
      "$boundary" "$(cat -- "${FAKE_OBSERVE_SPOOL_FILE}")" >> "${FAKE_DOCKER_LOG:?}"
  else
    printf 'SPOOL_%s:missing-or-unsafe\\n' "$boundary" >> "${FAKE_DOCKER_LOG:?}"
  fi
}
if [[ \"$*\" == *\"config --services\"* ]]; then
  if [[ \"${FAKE_MISSING_REDPANDA:-}\" == \"1\" ]]; then
    printf 'postgres\\n'
  else
    printf 'postgres\\nredpanda-0\\n'
  fi
fi
if [[ \"$*\" == *\"rpk group describe chess-state-managers --print-commits\"* ]]; then
  if [[ -n \"${FAKE_POST_SEEK_DESCRIPTION_FILE:-}\" ]]; then
    cat -- \"${FAKE_POST_SEEK_DESCRIPTION_FILE}\"
  elif [[ \"${FAKE_POST_SEEK_AMBIGUOUS:-}\" == \"1\" ]]; then
    printf '%s\\n' \
      'TOPIC        PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG   MEMBER-ID  CLIENT-ID  HOST' \
      'chess-moves  0          12              0                 12              -'
  else
    printf '%s\\n' \
      'TOPIC        PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG   MEMBER-ID  CLIENT-ID  HOST' \
      \"chess-moves  0          12              0                 12              ${FAKE_POST_SEEK_LAG:-0}\"
  fi
elif [[ \"$*\" == *\"rpk group describe chess-state-managers\"* ]]; then
  printf '%s\\n' \
    'GROUP        chess-state-managers' \
    'COORDINATOR  0' \
    'STATE        Empty' \
    'BALANCER' \
    \"MEMBERS      ${FAKE_GROUP_MEMBERS:-0}\" \
    'TOTAL-LAG    2' \
    '' \
    'TOPIC        PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG   MEMBER-ID  CLIENT-ID  HOST' \
    'chess-moves  0          10              0                 12              2'
fi
if [[ \"$*\" == *\"rpk group seek chess-state-managers\"* ]]; then
  observe_spool SEEK
  if [[ -n \"${FAKE_REPLACE_BEFORE_SEEK_FILE:-}\" ]]; then
    mv \"${FAKE_REPLACE_BEFORE_SEEK_FILE}\" \"${FAKE_REPLACED_BEFORE_SEEK_FILE:?}\"
    printf '%s' \"${FAKE_REPLACEMENT_CONTENT:-replacement}\" \
      > \"${FAKE_REPLACE_BEFORE_SEEK_FILE}\"
  fi
  if [[ -n \"${FAKE_REAL_PSQL_HELPER:-}\" && -n \"${FAKE_REAL_RACE_JOB_ID:-}\" ]]; then
    \"${FAKE_REAL_PSQL_PYTHON:?}\" \"${FAKE_REAL_PSQL_HELPER}\" race
  fi
  if [[ \"${FAKE_SWAP_SPOOL_ROOT:-}\" == \"1\" ]]; then
    mv \"${FAKE_SPOOL_ROOT:?}\" \"${FAKE_HELD_SPOOL_ROOT:?}\"
    ln -s \"${FAKE_OUTSIDE_SPOOL_ROOT:?}\" \"${FAKE_SPOOL_ROOT:?}\"
  fi
  if [[ \"${FAKE_KAFKA_SEEK_FAIL:-}\" == \"1\" ]]; then
    exit 23
  fi
fi
if [[ \"$*\" == *\"psql\"* ]]; then
  if [[ -n \"${FAKE_REAL_PSQL_HELPER:-}\" ]]; then
    \"${FAKE_REAL_PSQL_PYTHON:?}\" \"${FAKE_REAL_PSQL_HELPER}\" psql
    exit
  fi
  sql=\"$(cat)\"
  if [[ -n \"$sql\" ]]; then
    printf 'SQL:%s\\n' \"$sql\" >> \"${FAKE_DOCKER_LOG:?}\"
  fi
  if [[ \"$sql\" == *\"CREATE TEMPORARY TABLE cleanup_targets\"* ]]; then
    observe_spool SQL
    if [[ "${FAKE_INJECT_NON_TARGET_DURING_SQL:-}" == "1" ]]; then
      injected=false
      for cleanup_dir in "${FAKE_SPOOL_ROOT:?}"/.cleanup-*; do
        if [[ -d "$cleanup_dir" ]]; then
          printf 'injected' > "$cleanup_dir/not-in-manifest.pgn"
          injected=true
        fi
      done
      if ! "$injected"; then
        printf 'injected' > "${FAKE_SPOOL_ROOT:?}/not-in-manifest.pgn"
      fi
    fi
    if [[ -n "${FAKE_REPLACE_SPOOL_FILE:-}" ]]; then
      mv "${FAKE_REPLACE_SPOOL_FILE}" "${FAKE_REPLACED_SPOOL_FILE:?}"
      printf '%s' "${FAKE_REPLACEMENT_CONTENT:-replacement}" \
        > "${FAKE_REPLACE_SPOOL_FILE}"
    fi
    if [[ \"${FAKE_SQL_FAIL:-}\" == \"1\" ]]; then
      exit 24
    fi
    if [[ -n \"${FAKE_JOB_STATE:-}\" ]]; then
      state_tmp=\"${FAKE_JOB_STATE}.tmp\"
      : > \"$state_tmp\"
      while IFS='|' read -r state_job_id state_status; do
        [[ -n \"$state_job_id\" ]] || continue
        state_status=\"${state_status:-live}\"
        if [[ \"$sql\" == *\"('$state_job_id'::uuid)\"* ]]; then
          case \"$state_status\" in
            queued|parsing|live)
              printf 'CLEANUP_DELETED|%s\\n' \"$state_job_id\"
              continue
              ;;
            *)
              printf 'CLEANUP_SKIPPED|%s|%s\\n' \"$state_job_id\" \"$state_status\"
              ;;
          esac
        fi
        printf '%s|%s\\n' \"$state_job_id\" \"$state_status\" >> \"$state_tmp\"
      done < \"${FAKE_JOB_STATE}\"
      mv \"$state_tmp\" \"${FAKE_JOB_STATE}\"
    elif [[ -n \"${FAKE_PREVIEW_ROWS_FILE:-}\" ]]; then
      while IFS='|' read -r deleted_job_id _; do
        [[ -n \"$deleted_job_id\" ]] || continue
        printf 'CLEANUP_DELETED|%s\\n' \"$deleted_job_id\"
      done < \"${FAKE_PREVIEW_ROWS_FILE}\"
    elif [[ -n \"${FAKE_RACE_TARGET_STATUS:-}\" ]]; then
      printf 'CLEANUP_SKIPPED|%s|%s\\n' \
        \"${FAKE_PREVIEW_JOB_ID:-00000000-0000-0000-0000-000000000001}\" \
        \"${FAKE_RACE_TARGET_STATUS}\"
    else
      printf 'CLEANUP_DELETED|%s\\n' \
        \"${FAKE_PREVIEW_JOB_ID:-00000000-0000-0000-0000-000000000001}\"
    fi
  fi
  if [[ \"$sql\" == *\"terminal_statuses(status)\"* ]]; then
    if [[ \"${FAKE_TERMINAL_REPORT_FAIL:-}\" == \"1\" ]]; then
      exit 25
    fi
    printf 'TERMINAL|completed|%s\\n' \"${FAKE_TERMINAL_COMPLETED:-2}\"
    printf 'TERMINAL|partial|%s\\n' \"${FAKE_TERMINAL_PARTIAL:-1}\"
    printf 'TERMINAL|failed|%s\\n' \"${FAKE_TERMINAL_FAILED:-3}\"
    printf 'TERMINAL|total|%s\\n' \"${FAKE_TERMINAL_TOTAL:-6}\"
  fi
  if [[ \"$sql\" == *\"spool_path\"* && \"$sql\" != *\"CREATE TEMPORARY TABLE\"* ]]; then
    if [[ -n "${FAKE_PREVIEW_ROWS_FILE:-}" ]]; then
      cat -- "${FAKE_PREVIEW_ROWS_FILE}"
    else
      preview_job_id=\"${FAKE_PREVIEW_JOB_ID:-00000000-0000-0000-0000-000000000001}\"
      if [[ -n \"${FAKE_JOB_STATE:-}\" ]]; then
        IFS='|' read -r preview_job_id _ < \"${FAKE_JOB_STATE}\"
      fi
      printf '%s|%s|%s\\n' \"$preview_job_id\" \"${FAKE_SPOOL_PATH:-}\" \"${FAKE_GAME_COUNT:-0}\"
    fi
    if [[ -n \"${FAKE_NEW_JOB_ID:-}\" ]]; then
      printf '%s|live\\n' \"${FAKE_NEW_JOB_ID}\" >> \"${FAKE_JOB_STATE:?}\"
    fi
    if [[ -n \"${FAKE_RACE_TARGET_STATUS:-}\" && -n \"${FAKE_JOB_STATE:-}\" ]]; then
      state_tmp=\"${FAKE_JOB_STATE}.tmp\"
      while IFS='|' read -r state_job_id _; do
        [[ -n \"$state_job_id\" ]] || continue
        printf '%s|%s\\n' \"$state_job_id\" \"${FAKE_RACE_TARGET_STATUS}\"
      done < \"${FAKE_JOB_STATE}\" > \"$state_tmp\"
      mv \"$state_tmp\" \"${FAKE_JOB_STATE}\"
    fi
  fi
fi
""",
        )
        self._write_fake_command(
            "pgrep",
            """#!/usr/bin/env bash
set -euo pipefail
if [[ -n \"${FAKE_PGREP_STATUS:-}\" ]]; then
  if [[ \"${FAKE_PGREP_PATTERN:-all}\" == \"all\" || \"$*\" == *\"${FAKE_PGREP_PATTERN}\"* ]]; then
    exit \"${FAKE_PGREP_STATUS}\"
  fi
fi
if [[ \"${FAKE_ACTIVE_WORKER:-}\" == \"1\" && \"$*\" == *\"webapp\"* ]]; then
  exit 0
fi
pattern=''
for argument in \"$@\"; do
  pattern=\"$argument\"
done
if [[ -n \"${FAKE_PROCESS:-}\" && \"${FAKE_PROCESS}\" =~ $pattern ]]; then
  exit 0
fi
exit 1
""",
        )

    def _write_fake_command(self, name, contents):
        command = self.repo / "bin" / name
        command.write_text(contents, encoding="utf-8")
        command.chmod(command.stat().st_mode | stat.S_IXUSR)

    def run_script(self, *args, env=None, input=None):
        command_env = os.environ.copy()
        command_env.update(
            {
                "PATH": f"{self.repo / 'bin'}:{command_env['PATH']}",
                "FAKE_DOCKER_LOG": str(self.log_path),
            }
        )
        if env:
            command_env.update(env)
        return subprocess.run(
            ["bash", str(self.script), *args],
            cwd=self.repo,
            env=command_env,
            capture_output=True,
            text=True,
            input=input,
            check=False,
        )

    def docker_log(self):
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8")

    def test_help_prints_usage_without_calling_docker(self):
        result = self.run_script("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("clear_analysis_state.sh [--yes] [--stale-minutes MINUTES]", result.stdout)
        self.assertEqual(self.docker_log(), "")

    def test_rejects_unknown_arguments(self):
        result = self.run_script("--force")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option: --force", result.stderr)

    def test_rejects_negative_stale_minutes(self):
        result = self.run_script("--stale-minutes", "-1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-negative integer", result.stderr)

    def test_refuses_when_required_docker_service_is_missing(self):
        result = self.run_script("--yes", env={"FAKE_MISSING_REDPANDA": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required Docker services: redpanda-0", result.stderr)

    def test_refuses_when_ingestion_worker_is_active(self):
        result = self.run_script("--yes", env={"FAKE_ACTIVE_WORKER": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ingestion worker is running", result.stderr)
        self.assertNotIn("group seek", self.docker_log())

    def test_refuses_when_ingestion_worker_inspection_fails(self):
        result = self.run_script(
            "--yes",
            env={"FAKE_PGREP_STATUS": "2", "FAKE_PGREP_PATTERN": "webapp"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not inspect ingestion worker processes", result.stderr)
        self.assertEqual(self.docker_log(), "")

    def test_refuses_when_go_process_inspection_fails(self):
        result = self.run_script(
            "--yes",
            env={"FAKE_PGREP_STATUS": "3", "FAKE_PGREP_PATTERN": "go-build"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not inspect Go feature extractor processes", result.stderr)
        self.assertEqual(self.docker_log(), "")

    def test_pgrep_exit_one_is_treated_as_no_matching_process(self):
        result = self.run_script(
            "--yes", env={"FAKE_PGREP_STATUS": "1", "FAKE_PGREP_PATTERN": "all"}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Safety checks passed.", result.stdout)

    def test_refuses_when_go_run_extractor_has_trailing_arguments(self):
        result = self.run_script("--yes", env={"FAKE_PROCESS": "go run . --poll"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Go feature extractor is running", result.stderr)
        self.assertEqual(self.docker_log(), "")

    def test_refuses_when_go_run_extractor_targets_its_package(self):
        result = self.run_script(
            "--yes", env={"FAKE_PROCESS": "go run ./cmd/extractor --poll"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Go feature extractor is running", result.stderr)
        self.assertEqual(self.docker_log(), "")

    def test_refuses_when_compiled_go_build_processor_is_active(self):
        result = self.run_script(
            "--yes",
            env={
                "FAKE_PROCESS": "/private/var/folders/x/T/go-build123456789/b001/exe/processor"
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Go feature extractor is running", result.stderr)
        self.assertEqual(self.docker_log(), "")

    def test_allows_unrelated_go_command(self):
        result = self.run_script(
            "--yes", env={"FAKE_PROCESS": "go run ./cmd/unrelated --poll"}
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Safety checks passed.", result.stdout)

    def test_confirmation_refusal_causes_no_kafka_seek(self):
        result = self.run_script(input="no\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmation", result.stderr)
        self.assertIn("rpk group describe chess-state-managers", self.docker_log())
        self.assertNotIn("group seek", self.docker_log())

    def test_spool_manifest_is_validated_before_confirmation(self):
        outside = self.repo / "outside.pgn"
        outside.write_text("outside", encoding="utf-8")
        symlink = self.repo / ".runtime" / "pgn-spool" / "linked.pgn"
        symlink.symlink_to(outside)

        result = self.run_script(
            env={"FAKE_SPOOL_PATH": ".runtime/pgn-spool/linked.pgn"},
            input="no\n",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not safely freeze spool manifest", result.stderr)
        self.assertNotIn("confirmation declined", result.stderr)
        self.assertNotIn("group seek", self.docker_log())

    def test_preview_shows_each_target_spool_path_and_game_count(self):
        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_GAME_COUNT": "3",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Target job: 00000000-0000-0000-0000-000000000001 "
            "(games: 3; spool: .runtime/pgn-spool/temporary.pgn)",
            result.stdout,
        )

    def test_summary_style_zero_members_allows_cleanup(self):
        result = self.run_script("--yes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GROUP        chess-state-managers", result.stdout)
        self.assertIn("MEMBERS      0", result.stdout)
        self.assertIn("group seek chess-state-managers", self.docker_log())

    def test_seek_allows_new_topic_partitions_and_verifies_each_commit(self):
        post_seek = self.repo / "post-seek-description"
        post_seek.write_text(
            "TOPIC        PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  "
            "LOG-END-OFFSET  LAG   MEMBER-ID  CLIENT-ID  HOST\n"
            "chess-moves  0          12              0                 "
            "12              0\n"
            "chess-moves  1          7               0                 "
            "7               0\n",
            encoding="utf-8",
        )

        result = self.run_script(
            "--yes", env={"FAKE_POST_SEEK_DESCRIPTION_FILE": str(post_seek)}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.docker_log()
        self.assertIn(
            "group seek chess-state-managers --to end --topics chess-moves "
            "--allow-new-topics",
            log,
        )
        self.assertIn(
            "group describe chess-state-managers --print-commits", log
        )
        self.assertIn("chess-moves  1", result.stdout)

    def test_post_seek_nonzero_lag_refuses_database_cleanup(self):
        result = self.run_script("--yes", env={"FAKE_POST_SEEK_LAG": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not prove zero chess-moves lag", result.stderr)
        self.assertNotIn("CREATE TEMPORARY TABLE cleanup_targets", self.docker_log())

    def test_post_seek_ambiguous_lag_refuses_database_cleanup(self):
        result = self.run_script("--yes", env={"FAKE_POST_SEEK_AMBIGUOUS": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not prove zero chess-moves lag", result.stderr)
        self.assertNotIn("CREATE TEMPORARY TABLE cleanup_targets", self.docker_log())

    def test_summary_style_nonzero_members_refuses_before_seek(self):
        result = self.run_script("--yes", env={"FAKE_GROUP_MEMBERS": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active members", result.stderr)
        self.assertNotIn("group seek", self.docker_log())
        self.assertNotIn("CREATE TEMPORARY TABLE cleanup_targets", self.docker_log())

    def test_kafka_seek_failure_causes_no_cleanup_sql(self):
        result = self.run_script("--yes", env={"FAKE_KAFKA_SEEK_FAIL": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Kafka group seek failed", result.stderr)
        self.assertIn("group seek", self.docker_log())
        self.assertNotIn("CREATE TEMPORARY TABLE cleanup_targets", self.docker_log())

    def test_success_seeks_before_cleanup_sql_and_removes_valid_spool_file(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("[Event \\\"Temporary\\\"]", encoding="utf-8")

        result = self.run_script(
            "--yes", env={"FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn"}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(spool_file.exists())
        log = self.docker_log()
        self.assertLess(
            log.index("group seek chess-state-managers"),
            log.index("CREATE TEMPORARY TABLE cleanup_targets"),
        )

    def test_spool_file_stays_at_original_path_through_seek_and_sql(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("frozen identity", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_OBSERVE_SPOOL_FILE": str(spool_file),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.docker_log()
        self.assertIn("SPOOL_SEEK:present:frozen identity", log)
        self.assertIn("SPOOL_SQL:present:frozen identity", log)
        self.assertFalse(spool_file.exists())

    def test_new_job_after_preview_is_not_mutated_by_cleanup_sql(self):
        preview_job_id = "00000000-0000-0000-0000-000000000001"
        new_job_id = "00000000-0000-0000-0000-000000000002"
        job_state = self.repo / "job-state"
        job_state.write_text(f"{preview_job_id}|live\n", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_JOB_STATE": str(job_state),
                "FAKE_NEW_JOB_ID": new_job_id,
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            job_state.read_text(encoding="utf-8"), f"{new_job_id}|live\n"
        )

    def test_empty_frozen_target_deletion_leaves_state_file_empty(self):
        preview_job_id = "00000000-0000-0000-0000-000000000001"
        job_state = self.repo / "job-state"
        job_state.write_text(f"{preview_job_id}|live\n", encoding="utf-8")

        result = self.run_script("--yes", env={"FAKE_JOB_STATE": str(job_state)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job_state.read_text(encoding="utf-8"), "")

    def test_previewed_live_job_that_becomes_terminal_is_preserved_with_spool(self):
        job_id = "00000000-0000-0000-0000-000000000001"
        job_state = self.repo / "job-state"
        job_state.write_text(f"{job_id}|live\n", encoding="utf-8")
        spool_file = self.repo / ".runtime" / "pgn-spool" / "terminal-race.pgn"
        spool_file.write_text("preserve me", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_JOB_STATE": str(job_state),
                "FAKE_RACE_TARGET_STATUS": "completed",
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/terminal-race.pgn",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job_state.read_text(encoding="utf-8"), f"{job_id}|completed\n")
        self.assertTrue(spool_file.exists())
        self.assertEqual(spool_file.read_text(encoding="utf-8"), "preserve me")
        self.assertIn(
            f"Skipped now-terminal target: {job_id} (status: completed)",
            result.stdout,
        )

    def test_stale_filter_is_rechecked_for_the_locked_subset(self):
        result = self.run_script("--yes", "--stale-minutes", "30")

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.docker_log()
        self.assertGreaterEqual(log.count("lease_expires_at < NOW()"), 2)
        self.assertGreaterEqual(log.count("updated_at < NOW() - make_interval"), 2)

    def test_sql_failure_leaves_original_spool_file_in_place(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("[Event \\\"Temporary\\\"]", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_SQL_FAIL": "1",
                "FAKE_OBSERVE_SPOOL_FILE": str(spool_file),
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(spool_file.exists())
        self.assertEqual(spool_file.read_text(encoding="utf-8"), '[Event \\"Temporary\\"]')
        self.assertIn('SPOOL_SQL:present:[Event \\"Temporary\\"]', self.docker_log())
        self.assertIn("group seek chess-state-managers", self.docker_log())
        self.assertIn("CREATE TEMPORARY TABLE cleanup_targets", self.docker_log())

    def test_spool_parent_swap_after_seek_cannot_delete_outside_file(self):
        spool_root = self.repo / ".runtime" / "pgn-spool"
        spool_file = spool_root / "temporary.pgn"
        spool_file.write_text("safe", encoding="utf-8")
        outside_root = self.repo / "outside-spool"
        outside_root.mkdir()
        outside_file = outside_root / "temporary.pgn"
        outside_file.write_text("outside", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_SWAP_SPOOL_ROOT": "1",
                "FAKE_SPOOL_ROOT": str(spool_root),
                "FAKE_HELD_SPOOL_ROOT": str(self.repo / ".runtime" / "held-spool"),
                "FAKE_OUTSIDE_SPOOL_ROOT": str(outside_root),
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(outside_file.exists())
        self.assertTrue((self.repo / ".runtime" / "held-spool" / "temporary.pgn").exists())

    def test_kafka_seek_failure_leaves_original_spool_file_in_place(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("[Event \\\"Temporary\\\"]", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_KAFKA_SEEK_FAIL": "1",
                "FAKE_OBSERVE_SPOOL_FILE": str(spool_file),
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(spool_file.exists())
        self.assertEqual(spool_file.read_text(encoding="utf-8"), '[Event \\"Temporary\\"]')
        self.assertIn('SPOOL_SEEK:present:[Event \\"Temporary\\"]', self.docker_log())
        self.assertFalse(list((self.repo / ".runtime" / "pgn-spool").glob(".cleanup-*")))

    def test_replacement_identity_is_not_deleted_or_overwritten(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("frozen identity", encoding="utf-8")
        displaced_file = self.repo / "displaced-original.pgn"

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_REPLACE_SPOOL_FILE": str(spool_file),
                "FAKE_REPLACED_SPOOL_FILE": str(displaced_file),
                "FAKE_REPLACEMENT_CONTENT": "concurrent replacement",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(displaced_file.exists())
        self.assertEqual(displaced_file.read_text(encoding="utf-8"), "frozen identity")
        self.assertEqual(spool_file.read_text(encoding="utf-8"), "concurrent replacement")
        self.assertIn(
            "Spool cleanup summary: deleted=0, already_absent=0, retained=1, errors=0",
            result.stdout,
        )

    def test_replacement_after_confirmation_does_not_become_cleanup_baseline(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("confirmed identity", encoding="utf-8")
        displaced_file = self.repo / "confirmed-original.pgn"

        result = self.run_script(
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_REPLACE_BEFORE_SEEK_FILE": str(spool_file),
                "FAKE_REPLACED_BEFORE_SEEK_FILE": str(displaced_file),
                "FAKE_REPLACEMENT_CONTENT": "post-confirmation replacement",
            },
            input="CLEAR\n",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(displaced_file.read_text(encoding="utf-8"), "confirmed identity")
        self.assertEqual(
            spool_file.read_text(encoding="utf-8"), "post-confirmation replacement"
        )
        self.assertIn("Changed spool file retained", result.stdout)

    def test_only_frozen_manifest_entries_are_considered_for_cleanup(self):
        spool_root = self.repo / ".runtime" / "pgn-spool"
        target = spool_root / "temporary.pgn"
        target.write_text("target", encoding="utf-8")
        injected = spool_root / "not-in-manifest.pgn"

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_INJECT_NON_TARGET_DURING_SQL": "1",
                "FAKE_SPOOL_ROOT": str(spool_root),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        self.assertTrue(injected.exists())
        self.assertEqual(injected.read_text(encoding="utf-8"), "injected")

    def test_retained_private_recovery_directory_is_counted_as_an_error(self):
        spool_root = self.repo / ".runtime" / "pgn-spool"
        target = spool_root / "temporary.pgn"
        target.write_text("target", encoding="utf-8")
        (self.repo / "uuid.py").write_text(
            """import os


_calls = 0


class FakeUuid:
    def __init__(self, value):
        self.hex = value


def uuid4():
    global _calls
    _calls += 1
    if _calls == 2:
        quarantine = os.path.join(
            os.environ["FAKE_SPOOL_ROOT"], f".cleanup-{1:032x}"
        )
        with open(
            os.path.join(quarantine, "not-in-manifest.pgn"),
            "w",
            encoding="utf-8",
        ) as injected:
            injected.write("injected")
    return FakeUuid(f"{_calls:032x}")
""",
            encoding="utf-8",
        )

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_SPOOL_ROOT": str(spool_root),
            },
        )

        recovery_directories = list(spool_root.glob(".cleanup-*"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertEqual(len(recovery_directories), 1)
        self.assertEqual(
            (recovery_directories[0] / "not-in-manifest.pgn").read_text(
                encoding="utf-8"
            ),
            "injected",
        )
        self.assertIn(
            "Spool cleanup summary: deleted=1, already_absent=0, retained=0, "
            "errors=1, recovery_directories=1",
            result.stdout,
        )

    def test_partial_cleanup_reports_deleted_and_retained_counts(self):
        spool_root = self.repo / ".runtime" / "pgn-spool"
        first = spool_root / "first.pgn"
        second = spool_root / "second.pgn"
        first.write_text("first frozen identity", encoding="utf-8")
        second.write_text("second frozen identity", encoding="utf-8")
        displaced_second = self.repo / "displaced-second.pgn"
        preview_rows = self.repo / "preview-rows"
        preview_rows.write_text(
            "00000000-0000-0000-0000-000000000001|"
            ".runtime/pgn-spool/first.pgn|0\n"
            "00000000-0000-0000-0000-000000000002|"
            ".runtime/pgn-spool/second.pgn|0\n",
            encoding="utf-8",
        )

        result = self.run_script(
            "--yes",
            env={
                "FAKE_PREVIEW_ROWS_FILE": str(preview_rows),
                "FAKE_REPLACE_SPOOL_FILE": str(second),
                "FAKE_REPLACED_SPOOL_FILE": str(displaced_second),
                "FAKE_REPLACEMENT_CONTENT": "second concurrent replacement",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(first.exists())
        self.assertEqual(
            displaced_second.read_text(encoding="utf-8"), "second frozen identity"
        )
        self.assertEqual(
            second.read_text(encoding="utf-8"), "second concurrent replacement"
        )
        self.assertIn(
            "Spool cleanup summary: deleted=1, already_absent=0, retained=1, errors=0",
            result.stdout,
        )
        self.assertIn("spool cleanup incomplete", result.stderr)
        self.assertNotIn("spool files were retained", result.stderr)

    def test_large_spool_manifest_cleans_only_returned_job_ids(self):
        spool_root = self.repo / ".runtime" / "pgn-spool"
        preview_rows = self.repo / "preview-rows"
        rows = []
        targets = []
        for index in range(300):
            job_id = uuid.UUID(int=index + 1)
            name = f"large-manifest-{index:04d}.pgn"
            target = spool_root / name
            target.write_text(str(index), encoding="utf-8")
            targets.append(target)
            rows.append(f"{job_id}|.runtime/pgn-spool/{name}|0\n")
        preview_rows.write_text("".join(rows), encoding="utf-8")

        result = self.run_script(
            "--yes", env={"FAKE_PREVIEW_ROWS_FILE": str(preview_rows)}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(target.exists() for target in targets))

    def test_reports_remaining_terminal_counts_by_status_and_total(self):
        result = self.run_script(
            "--yes",
            env={
                "FAKE_TERMINAL_COMPLETED": "8",
                "FAKE_TERMINAL_PARTIAL": "2",
                "FAKE_TERMINAL_FAILED": "4",
                "FAKE_TERMINAL_TOTAL": "14",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Remaining terminal jobs: completed=8, partial=2, failed=4, total=14",
            result.stdout,
        )

    def test_terminal_reporting_failure_does_not_report_cleanup_success(self):
        result = self.run_script(
            "--yes", env={"FAKE_TERMINAL_REPORT_FAIL": "1"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not report remaining terminal analysis jobs", result.stderr)
        self.assertNotIn("Unfinished analysis state cleared", result.stdout)

    def test_postgres_transaction_preserves_target_that_races_to_terminal(self):
        database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("PostgreSQL integration test requires TEST_DATABASE_URL")

        import sqlalchemy as sa
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import OperationalError

        if make_url(database_url).get_backend_name() != "postgresql":
            self.fail("TEST_DATABASE_URL must use PostgreSQL")

        source_root = Path(__file__).parents[2]
        alembic_config = Config(str(source_root / "alembic.ini"))
        alembic_config.set_main_option("sqlalchemy.url", database_url)
        engine = sa.create_engine(database_url, pool_pre_ping=True)
        try:
            try:
                command.upgrade(alembic_config, "head")
            except OperationalError as error:
                self.skipTest(f"PostgreSQL unavailable: {error}")

            job_id = uuid.uuid4()
            spool_file = self.repo / ".runtime" / "pgn-spool" / "race.pgn"
            spool_file.write_text("terminal race", encoding="utf-8")
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO analysis_jobs (
                            id, source_type, source_name, spool_path, status
                        ) VALUES (
                            :id, 'file', 'race.pgn', :spool_path, 'live'
                        )
                        """
                    ),
                    {
                        "id": job_id,
                        "spool_path": ".runtime/pgn-spool/race.pgn",
                    },
                )

            helper = self.repo / "real_psql.py"
            helper.write_text(
                """import os
import sys

import psycopg


database_url = os.environ["FAKE_REAL_PSQL_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://", 1
)
with psycopg.connect(database_url, autocommit=True) as connection:
    with connection.cursor() as cursor:
        if sys.argv[1] == "race":
            cursor.execute(
                "UPDATE analysis_jobs SET status = 'completed', "
                "completed_at = NOW(), updated_at = NOW() WHERE id = %s",
                (os.environ["FAKE_REAL_RACE_JOB_ID"],),
            )
        else:
            cursor.execute(sys.stdin.read())
            while True:
                if cursor.description is not None:
                    for row in cursor.fetchall():
                        print("|".join("" if value is None else str(value) for value in row))
                if not cursor.nextset():
                    break
""",
                encoding="utf-8",
            )

            result = self.run_script(
                "--yes",
                env={
                    "FAKE_REAL_PSQL_DATABASE_URL": database_url,
                    "FAKE_REAL_PSQL_HELPER": str(helper),
                    "FAKE_REAL_PSQL_PYTHON": str(source_root / ".venv/bin/python"),
                    "FAKE_REAL_RACE_JOB_ID": str(job_id),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with engine.connect() as connection:
                status = connection.scalar(
                    sa.text("SELECT status FROM analysis_jobs WHERE id = :id"),
                    {"id": job_id},
                )
            self.assertEqual(status, "completed")
            self.assertTrue(spool_file.exists())
        finally:
            try:
                if "job_id" in locals():
                    with engine.begin() as connection:
                        connection.execute(
                            sa.text("DELETE FROM analysis_jobs WHERE id = :id"),
                            {"id": job_id},
                        )
            finally:
                engine.dispose()

    def test_static_runtime_symlink_aborts_before_kafka_seek(self):
        runtime = self.repo / ".runtime"
        held_runtime = self.repo / "held-runtime"
        runtime.rename(held_runtime)
        outside_runtime = self.repo / "outside-runtime"
        outside_spool = outside_runtime / "pgn-spool"
        outside_spool.mkdir(parents=True)
        outside_file = outside_spool / "temporary.pgn"
        outside_file.write_text("outside", encoding="utf-8")
        runtime.symlink_to(outside_runtime, target_is_directory=True)

        result = self.run_script(
            "--yes", env={"FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(outside_file.exists())
        self.assertNotIn("group seek", self.docker_log())

    def test_unsafe_spool_path_aborts_before_kafka_seek(self):
        outside = self.repo / "outside.pgn"
        outside.write_text("data", encoding="utf-8")

        result = self.run_script("--yes", env={"FAKE_SPOOL_PATH": "../outside.pgn"})

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(outside.exists())
        self.assertNotIn("group seek", self.docker_log())

    def test_symlinked_spool_path_aborts_before_kafka_seek(self):
        outside = self.repo / "outside.pgn"
        outside.write_text("data", encoding="utf-8")
        symlink = self.repo / ".runtime" / "pgn-spool" / "linked.pgn"
        symlink.symlink_to(outside)

        result = self.run_script(
            "--yes", env={"FAKE_SPOOL_PATH": ".runtime/pgn-spool/linked.pgn"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(outside.exists())
        self.assertNotIn("group seek", self.docker_log())


if __name__ == "__main__":
    unittest.main()
