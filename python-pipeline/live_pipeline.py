"""Supervise the four isolated workers in the core live scoring pipeline."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
WORKER_SCRIPTS = (
    "live_game_collector.py",
    "xgboost_inference.py",
    "torch_inference.py",
    "prediction_reporter.py",
)


class ChildProcess(Protocol):
    """The subprocess surface used by the supervisor and its tests."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[tuple[str, ...]], ChildProcess]


def worker_commands(
    python: str = sys.executable,
    pipeline_directory: Path = PIPELINE_DIRECTORY,
) -> tuple[tuple[str, ...], ...]:
    """Build one interpreter command per worker without importing workers."""

    return tuple(
        (python, str(pipeline_directory / script))
        for script in WORKER_SCRIPTS
    )


def _stop_children(
    children: list[tuple[tuple[str, ...], ChildProcess]],
) -> None:
    for _, child in children:
        if child.poll() is None:
            child.terminate()

    for _, child in children:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run_pipeline(
    process_factory: ProcessFactory = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run all core workers and fail the group if any worker exits."""

    children: list[tuple[tuple[str, ...], ChildProcess]] = []
    try:
        for command in worker_commands():
            children.append((command, process_factory(command)))

        while True:
            for command, child in children:
                returncode = child.poll()
                if returncode is not None:
                    print(
                        f"{Path(command[1]).name} exited with status "
                        f"{returncode}",
                        file=sys.stderr,
                    )
                    return returncode or 1
            sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_children(children)


if __name__ == "__main__":
    raise SystemExit(run_pipeline())
