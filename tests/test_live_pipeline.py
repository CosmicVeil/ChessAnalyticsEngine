"""Fail-fast supervision contracts for the core live inference workers."""

from __future__ import annotations

import importlib.util
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIRECTORY = REPOSITORY_ROOT / "python-pipeline"
MODULE_PATH = PIPELINE_DIRECTORY / "live_pipeline.py"
SPEC = importlib.util.spec_from_file_location(
    "live_pipeline_under_test",
    MODULE_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {MODULE_PATH}")
LIVE_PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIVE_PIPELINE)
run_pipeline = LIVE_PIPELINE.run_pipeline
worker_commands = LIVE_PIPELINE.worker_commands


WORKER_CODE = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
    "time.sleep(60)"
)
SUPERVISOR_CODE = "\n".join(
    (
        "import importlib.util, pathlib, sys",
        "module_path = pathlib.Path(sys.argv[1])",
        "worker_code = sys.argv[2]",
        "spec = importlib.util.spec_from_file_location('live_pipeline_child', module_path)",
        "module = importlib.util.module_from_spec(spec)",
        "spec.loader.exec_module(module)",
        "commands = tuple((sys.executable, '-c', worker_code, marker) for marker in sys.argv[3:])",
        "module.worker_commands = lambda: commands",
        "raise SystemExit(module.run_pipeline())",
    )
)


def _process_is_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeProcess:
    def __init__(
        self,
        returncode: int | None = None,
        *,
        ignore_terminate: bool = False,
    ) -> None:
        self.returncode = returncode
        self.ignore_terminate = ignore_terminate
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.ignore_terminate and not self.killed:
            raise subprocess.TimeoutExpired("worker", timeout)
        if self.returncode is None:
            self.returncode = -9 if self.killed else -15
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class RecordingFactory:
    def __init__(self, children: list[FakeProcess]) -> None:
        self.children = children
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> FakeProcess:
        self.commands.append(command)
        return self.children[len(self.commands) - 1]


class LivePipelineTests(unittest.TestCase):
    def test_worker_commands_keep_models_in_four_separate_processes(self) -> None:
        self.assertEqual(
            worker_commands("/venv/python", Path("/repo/python-pipeline")),
            (
                (
                    "/venv/python",
                    "/repo/python-pipeline/live_game_collector.py",
                ),
                (
                    "/venv/python",
                    "/repo/python-pipeline/xgboost_inference.py",
                ),
                (
                    "/venv/python",
                    "/repo/python-pipeline/torch_inference.py",
                ),
                (
                    "/venv/python",
                    "/repo/python-pipeline/prediction_reporter.py",
                ),
            ),
        )

    def test_worker_exit_returns_status_and_stops_running_siblings(self) -> None:
        children = [
            FakeProcess(),
            FakeProcess(7),
            FakeProcess(ignore_terminate=True),
            FakeProcess(),
        ]
        factory = RecordingFactory(children)
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            status = run_pipeline(
                process_factory=factory,
                sleep=lambda _: self.fail("an exited worker should be noticed"),
            )

        self.assertEqual(status, 7)
        self.assertIn(
            "xgboost_inference.py exited with status 7",
            stderr.getvalue(),
        )
        self.assertEqual(len(factory.commands), 4)
        self.assertFalse(children[1].terminated)
        self.assertEqual(children[1].wait_calls, [])
        for child in (children[0], children[2], children[3]):
            self.assertTrue(child.terminated)
            self.assertTrue(child.wait_calls)
        self.assertTrue(children[2].killed)
        self.assertEqual(children[2].wait_calls, [5, None])

    def test_clean_worker_exit_is_still_an_unexpected_failure(self) -> None:
        children = [FakeProcess(0), FakeProcess(), FakeProcess(), FakeProcess()]

        with redirect_stderr(io.StringIO()):
            status = run_pipeline(
                process_factory=RecordingFactory(children),
                sleep=lambda _: None,
            )

        self.assertEqual(status, 1)

    def test_keyboard_interrupt_returns_zero_and_stops_every_worker(self) -> None:
        children = [FakeProcess() for _ in range(4)]
        observed_delays: list[float] = []

        def interrupting_sleep(delay: float) -> None:
            observed_delays.append(delay)
            raise KeyboardInterrupt

        status = run_pipeline(
            process_factory=RecordingFactory(children),
            sleep=interrupting_sleep,
        )

        self.assertEqual(status, 0)
        self.assertEqual(observed_delays, [0.25])
        for child in children:
            self.assertTrue(child.terminated)
            self.assertEqual(child.wait_calls, [5])

    def test_sigterm_stops_real_worker_processes_before_supervisor_exits(
        self,
    ) -> None:
        worker_process_ids: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            marker_paths = [
                Path(directory) / f"worker-{index}.pid"
                for index in range(4)
            ]
            supervisor = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    SUPERVISOR_CODE,
                    str(MODULE_PATH),
                    WORKER_CODE,
                    *(str(path) for path in marker_paths),
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                self.assertTrue(
                    _wait_until(
                        lambda: all(path.exists() for path in marker_paths)
                    ),
                    "the supervisor did not start all four workers",
                )
                worker_process_ids = [
                    int(path.read_text(encoding="utf-8"))
                    for path in marker_paths
                ]

                supervisor.send_signal(signal.SIGTERM)
                returncode = supervisor.wait(timeout=5)

                self.assertEqual(returncode, 0)
                self.assertTrue(
                    _wait_until(
                        lambda: all(
                            not _process_is_running(process_id)
                            for process_id in worker_process_ids
                        )
                    ),
                    "SIGTERM left one or more workers running",
                )
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait()
                for process_id in worker_process_ids:
                    if _process_is_running(process_id):
                        os.kill(process_id, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
