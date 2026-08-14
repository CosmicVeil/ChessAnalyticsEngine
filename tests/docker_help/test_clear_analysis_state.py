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
    preview_job_id=\"${FAKE_PREVIEW_JOB_ID:-00000000-0000-0000-0000-000000000001}\"
    if [[ -n \"${FAKE_JOB_STATE:-}\" ]]; then
      IFS= read -r preview_job_id < \"${FAKE_JOB_STATE}\"
    fi
    printf '%s|%s|%s\\n' \"$preview_job_id\" \"${FAKE_SPOOL_PATH:-}\" \"${FAKE_GAME_COUNT:-0}\"
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

    def test_sql_failure_retains_validated_spool_file(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("[Event \\\"Temporary\\\"]", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_SQL_FAIL": "1",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(spool_file.exists())
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
        self.assertFalse((self.repo / ".runtime" / "held-spool" / "temporary.pgn").exists())
        self.assertTrue(any((self.repo / ".runtime" / "held-spool").iterdir()))

    def test_kafka_seek_failure_restores_quarantined_spool_file(self):
        spool_file = self.repo / ".runtime" / "pgn-spool" / "temporary.pgn"
        spool_file.write_text("[Event \\\"Temporary\\\"]", encoding="utf-8")

        result = self.run_script(
            "--yes",
            env={
                "FAKE_SPOOL_PATH": ".runtime/pgn-spool/temporary.pgn",
                "FAKE_KAFKA_SEEK_FAIL": "1",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(spool_file.exists())
        self.assertFalse(list((self.repo / ".runtime" / "pgn-spool").glob(".cleanup-*")))

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
