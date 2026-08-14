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

    def run_script(self, *args, env=None):
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


if __name__ == "__main__":
    unittest.main()
