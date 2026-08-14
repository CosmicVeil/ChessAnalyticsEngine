import os
import shutil
import stat
import subprocess
import tempfile
import unittest
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
if [[ \"$*\" == *\"rpk group describe chess-state-managers\"* ]]; then
  printf 'GROUP chess-state-managers\\nMEMBERS %s\\n' \"${FAKE_GROUP_MEMBERS:-0}\"
fi
if [[ \"$*\" == *\"rpk group seek chess-state-managers\"* ]]; then
  observe_spool SEEK
  if [[ \"${FAKE_SWAP_SPOOL_ROOT:-}\" == \"1\" ]]; then
    mv \"${FAKE_SPOOL_ROOT:?}\" \"${FAKE_HELD_SPOOL_ROOT:?}\"
    ln -s \"${FAKE_OUTSIDE_SPOOL_ROOT:?}\" \"${FAKE_SPOOL_ROOT:?}\"
  fi
  if [[ \"${FAKE_KAFKA_SEEK_FAIL:-}\" == \"1\" ]]; then
    exit 23
  fi
fi
if [[ \"$*\" == *\"psql\"* ]]; then
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
    if [[ -n \"${FAKE_JOB_STATE:-}\" ]]; then
      if [[ \"$sql\" == *\"WHERE status IN\"* ]]; then
        : > \"${FAKE_JOB_STATE}\"
      else
        state_tmp=\"${FAKE_JOB_STATE}.tmp\"
        : > \"$state_tmp\"
        while IFS= read -r state_job_id; do
          if [[ \"$sql\" != *\"('$state_job_id'::uuid)\"* ]]; then
            printf '%s\\n' \"$state_job_id\" >> \"$state_tmp\"
          fi
        done < \"${FAKE_JOB_STATE}\"
        mv \"$state_tmp\" \"${FAKE_JOB_STATE}\"
      fi
    fi
    if [[ \"${FAKE_SQL_FAIL:-}\" == \"1\" ]]; then
      exit 24
    fi
  fi
  if [[ \"$sql\" == *\"spool_path\"* && \"$sql\" != *\"CREATE TEMPORARY TABLE\"* ]]; then
    if [[ -n "${FAKE_PREVIEW_ROWS_FILE:-}" ]]; then
      cat -- "${FAKE_PREVIEW_ROWS_FILE}"
    else
      preview_job_id=\"${FAKE_PREVIEW_JOB_ID:-00000000-0000-0000-0000-000000000001}\"
      if [[ -n \"${FAKE_JOB_STATE:-}\" ]]; then
        IFS= read -r preview_job_id < \"${FAKE_JOB_STATE}\"
      fi
      printf '%s|%s|%s\\n' \"$preview_job_id\" \"${FAKE_SPOOL_PATH:-}\" \"${FAKE_GAME_COUNT:-0}\"
    fi
    if [[ -n \"${FAKE_NEW_JOB_ID:-}\" ]]; then
      printf '%s\\n' \"${FAKE_NEW_JOB_ID}\" >> \"${FAKE_JOB_STATE:?}\"
    fi
  fi
fi
""",
        )
        self._write_fake_command(
            "pgrep",
            """#!/usr/bin/env bash
set -euo pipefail
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
        self.assertIn("GROUP chess-state-managers\nMEMBERS 0", result.stdout)
        self.assertIn("group seek chess-state-managers", self.docker_log())

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
        job_state.write_text(f"{preview_job_id}\n", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_JOB_STATE": str(job_state),
                "FAKE_NEW_JOB_ID": new_job_id,
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job_state.read_text(encoding="utf-8"), f"{new_job_id}\n")

    def test_empty_frozen_target_deletion_leaves_state_file_empty(self):
        preview_job_id = "00000000-0000-0000-0000-000000000001"
        job_state = self.repo / "job-state"
        job_state.write_text(f"{preview_job_id}\n", encoding="utf-8")

        result = self.run_script("--yes", env={"FAKE_JOB_STATE": str(job_state)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job_state.read_text(encoding="utf-8"), "")

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
