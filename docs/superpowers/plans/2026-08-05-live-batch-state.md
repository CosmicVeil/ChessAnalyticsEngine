# Live Batch State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make batch status visibly `live` while PGN discovery and per-game inference overlap, then terminalize only after EOF and all discovered games settle.

**Architecture:** Replace job-level `analyzing` with a durable `live` state across SQLAlchemy, Alembic, repository transitions, SSE payloads, and browser copy. Add a fail-fast launcher for the four existing core Python workers without importing model libraries into the launcher or coupling Flask to the pipeline.

**Tech Stack:** Python 3.14, Flask, SQLAlchemy, Alembic, PostgreSQL 17, Redpanda/Kafka, browser-native ES modules, `unittest`.

## Global Constraints

- Keep Flask, the web ingestion worker, and the web result worker separate from the core pipeline launcher.
- Do not change Kafka topic names, move-event schemas, feature vectors, preprocessing artifacts, XGBoost, PyTorch, or the Go extractor.
- A game must become `completed` as soon as its joined model result arrives, including before ingestion reaches EOF.
- A job must remain non-terminal as `live` while the spool is open or any game remains queued or analyzing.
- Preserve the user's unstaged `ml-model/train_xgboost.py` change. Never stage or commit it.
- Use test-first red-green cycles for every behavior change.

---

### Task 1: Durable `live` Job State and Migration

**Files:**
- Create: `webapp/migrations/versions/20260805_0002_live_job_state.py`
- Modify: `webapp/models.py`
- Modify: `tests/web_app/test_repository_unit.py`
- Modify: `tests/web_app/test_migrations_postgres.py`

**Interfaces:**
- Produces: `webapp.models.JOB_STATUS_LIVE == "live"` and `JOB_STATUSES` without job-level `analyzing`.
- Produces: Alembic revision `20260805_0002`, which upgrades active `analyzing` rows to `live` and restores them on downgrade.

- [ ] **Step 1: Write the failing unit contract for job statuses**

Add this test to `tests/web_app/test_repository_unit.py`:

```python
def test_job_status_contract_uses_live_while_game_status_keeps_analyzing(self):
    from webapp.models import GAME_STATUSES, JOB_STATUSES, JOB_STATUS_LIVE

    self.assertEqual(JOB_STATUS_LIVE, "live")
    self.assertEqual(
        JOB_STATUSES,
        ("received", "queued", "parsing", "live", "completed", "partial", "failed"),
    )
    self.assertEqual(
        GAME_STATUSES,
        ("queued", "analyzing", "completed", "failed"),
    )
```

- [ ] **Step 2: Run the focused unit test and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.web_app.test_repository_unit.FakeStoreContractTests.test_job_status_contract_uses_live_while_game_status_keeps_analyzing -v
```

Expected: error importing `JOB_STATUS_LIVE`.

- [ ] **Step 3: Add the model constant and status tuple**

In `webapp/models.py`, replace the job-level analyzing constant with:

```python
JOB_STATUS_LIVE = "live"
JOB_STATUSES = (
    JOB_STATUS_RECEIVED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_PARSING,
    JOB_STATUS_LIVE,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PARTIAL,
    JOB_STATUS_FAILED,
)
```

Keep `GAME_STATUS_ANALYZING = "analyzing"` unchanged.
Replace `JOB_STATUS_ANALYZING` with `JOB_STATUS_LIVE` in `__all__`.

- [ ] **Step 4: Run the focused unit test and verify GREEN**

Run the command from Step 2. Expected: pass.

- [ ] **Step 5: Write the failing PostgreSQL migration test**

In `tests/web_app/test_migrations_postgres.py`, import `UTC` and `datetime`, update the existing `JOB_STATUSES` expectation to use `live`, and add a test that downgrades to `20260804_0001`, inserts one active row, upgrades, and checks every preserved field:

```python
def test_live_state_migration_preserves_active_job_data(self):
    command.downgrade(self.alembic_config, "20260804_0001")
    job_id = uuid.uuid4()
    lease = datetime(2026, 8, 5, 18, 0, tzinfo=UTC)
    with self.engine.begin() as connection:
        self._insert_job(
            connection,
            job_id=job_id,
            status="analyzing",
            total_games=9,
            analyzing_games=7,
            completed_games=2,
            claimed_by="worker-live",
            lease_expires_at=lease,
        )

    command.upgrade(self.alembic_config, "head")

    with self.engine.connect() as connection:
        row = connection.execute(sa.text("""
            SELECT status, total_games, analyzing_games, completed_games,
                   claimed_by, lease_expires_at
            FROM analysis_jobs WHERE id = :id
        """), {"id": job_id}).mappings().one()
    self.assertEqual(row["status"], "live")
    self.assertEqual(row["total_games"], 9)
    self.assertEqual(row["analyzing_games"], 7)
    self.assertEqual(row["completed_games"], 2)
    self.assertEqual(row["claimed_by"], "worker-live")
    self.assertEqual(row["lease_expires_at"], lease)
```

Extend `_insert_job` so its SQL includes `claimed_by` and `lease_expires_at` from the values mapping.

- [ ] **Step 6: Run the migration test and verify RED**

Create and use a dedicated test database, never the development database:

```bash
docker compose -f infra/docker-compose.yaml exec -T postgres \
  createdb -U chess chess_analytics_test_live
TEST_DATABASE_URL=postgresql+psycopg://chess:chess@localhost:15432/chess_analytics_test_live \
  .venv/bin/python -m unittest tests.web_app.test_migrations_postgres.MigrationPostgresTests.test_live_state_migration_preserves_active_job_data -v
```

If the dedicated database already exists, reuse it. Expected test result before implementation: the status remains `analyzing`.

- [ ] **Step 7: Create the reversible Alembic revision**

Implement `20260805_0002_live_job_state.py` with these operations:

```python
revision = "20260805_0002"
down_revision = "20260804_0001"

LIVE_CHECK = (
    "status IN ('received', 'queued', 'parsing', 'live', "
    "'completed', 'partial', 'failed')"
)
LEGACY_CHECK = (
    "status IN ('received', 'queued', 'parsing', 'analyzing', "
    "'completed', 'partial', 'failed')"
)

def upgrade() -> None:
    op.drop_constraint("ck_analysis_jobs_status", "analysis_jobs", type_="check")
    op.execute("UPDATE analysis_jobs SET status = 'live' WHERE status = 'analyzing'")
    op.create_check_constraint("ck_analysis_jobs_status", "analysis_jobs", LIVE_CHECK)

def downgrade() -> None:
    op.drop_constraint("ck_analysis_jobs_status", "analysis_jobs", type_="check")
    op.execute("UPDATE analysis_jobs SET status = 'analyzing' WHERE status = 'live'")
    op.create_check_constraint("ck_analysis_jobs_status", "analysis_jobs", LEGACY_CHECK)
```

- [ ] **Step 8: Verify migration and model tests GREEN**

Run the focused tests from Steps 2 and 6. Expected: both pass.

- [ ] **Step 9: Commit the state contract**

Stage only the four Task 1 files and commit:

```bash
git commit -m "feat: add live analysis job state"
```

---

### Task 2: Live Repository Transitions and SSE Continuity

**Files:**
- Modify: `webapp/repository.py`
- Modify: `tests/web_app/fakes.py`
- Modify: `tests/web_app/test_repository_unit.py`
- Modify: `tests/web_app/test_repository_postgres.py`
- Modify: `tests/web_app/test_ingestion_worker.py`
- Modify: `tests/web_app/test_adapter_integration_postgres.py`
- Modify: `tests/web_app/test_sse.py`

**Interfaces:**
- Consumes: `JOB_STATUS_LIVE` from Task 1.
- Produces: all non-terminal repository transitions use `live`; `stream_events()` remains open for `live` jobs.

- [ ] **Step 1: Write failing fake-store tests for live completion before EOF**

Import `ParsedGame`, `JoinedResult`, and `StoreResultOutcome`. Add helpers that create a queued job, claim it, insert `ParsedGame(sequence=1)`, and mark it published. Assert this sequence:

```python
self.assertEqual(store.get_job(job.id).status, "live")
outcome = store.store_joined_result(JoinedResult(
    pipeline_game_id=game.pipeline_game_id,
    pytorch_score=0.8,
    xgboost_score=0.4,
    pytorch_prediction=True,
    xgboost_prediction=False,
))
self.assertIs(outcome, StoreResultOutcome.STORED)
snapshot = store.get_job(job.id)
self.assertEqual(snapshot.status, "live")
self.assertEqual(snapshot.completed_games, 1)
self.assertEqual(snapshot.analyzing_games, 0)
self.assertIsNone(snapshot.completed_at)
```

Add a second discovered game, call `finish_ingestion`, and assert the job stays `live` until the second result arrives, then becomes `completed`.

Before production changes, replace job-level `analyzing` expectations with `live` in `test_repository_postgres.py`, `test_ingestion_worker.py`, and the job snapshot contract. Keep every game-level `analyzing` expectation and the `counts["analyzing"]` field unchanged.

- [ ] **Step 2: Verify repository tests RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.web_app.test_repository_unit \
  tests.web_app.test_ingestion_worker \
  tests.web_app.test_repository_postgres -v
```

Expected: assertions receive `analyzing` instead of `live`.

- [ ] **Step 3: Replace job-level transitions in the fake and SQL repository**

Update `claim_next_job` lease recovery to accept `parsing` and `live`. Set `JOB_STATUS_LIVE` in `mark_game_published`, the non-terminal branch of `finish_ingestion`, and the non-terminal branch of `store_joined_result`. Mirror those transitions in `FakeAnalysisStore`.

Do not change `game.status = GAME_STATUS_ANALYZING`.

The production replacements are:

```python
from webapp.models import JOB_STATUS_LIVE

# claim_next_job expired lease eligibility
AnalysisJob.status.in_((JOB_STATUS_PARSING, JOB_STATUS_LIVE))

# mark_game_published, finish_ingestion, and store_joined_result
job.status = JOB_STATUS_LIVE
```

The fake uses `{"parsing", "live"}` for expired claims and `status="live"` for the same three non-terminal transitions.

- [ ] **Step 4: Verify repository tests GREEN**

Run the command from Step 2. Expected: pass.

- [ ] **Step 5: Strengthen the PostgreSQL streaming integration test**

Give `ImmediateResultPublisher` a `statuses_after_results` list. After each `ResultWorker.process`, resolve the job ID from `web:<uuid>:<sequence>` and append `store.get_job(job_id).status`. Assert:

```python
self.assertEqual(publisher.statuses_after_results, ["live", "live", "live"])
```

The existing final assertion remains `partial` after EOF because the fixture includes one invalid game.

- [ ] **Step 6: Add the SSE non-terminal regression test**

Create a fake live job, consume all existing events, then request the next generator item:

```python
stream = stream_events(
    store,
    job.id,
    after_id=store.events[-1].id,
    wait=lambda: None,
)
self.assertEqual(next(stream), b": keepalive\n\n")
stream.close()
```

This proves SSE does not close merely because some games completed before EOF.

- [ ] **Step 7: Run focused PostgreSQL and SSE tests**

Run:

```bash
.venv/bin/python -m unittest tests.web_app.test_sse -v
TEST_DATABASE_URL=postgresql+psycopg://chess:chess@localhost:15432/chess_analytics_test_live \
  .venv/bin/python -m unittest \
    tests.web_app.test_adapter_integration_postgres \
    tests.web_app.test_repository_postgres -v
```

Expected: pass.

- [ ] **Step 8: Commit live repository behavior**

Stage only the seven Task 2 files and commit:

```bash
git commit -m "feat: stream live batch completion"
```

---

### Task 3: Live Browser Status

**Files:**
- Modify: `webapp/static/js/analysis.js`
- Modify: `webapp/templates/analysis.html`
- Modify: `tests/frontend/analysis_card_browser.html`
- Modify: `tests/web_app/test_pages.py`

**Interfaces:**
- Produces: `jobConnectionState(status: string) -> { label: string, terminal: boolean, failed: boolean }`.
- Consumes: job `status` values from SSE and the status endpoint.

- [ ] **Step 1: Extend the browser harness before production code**

Import `jobConnectionState` and add assertions:

```javascript
check(jobConnectionState("parsing").label === "Reading PGN", "parsing copy missing");
check(jobConnectionState("live").label === "Live analysis", "live copy missing");
check(jobConnectionState("analyzing").label === "Live analysis", "legacy copy missing");
check(!jobConnectionState("live").terminal, "live state closes SSE");
check(jobConnectionState("completed").terminal, "completed state stays open");
```

Bump the harness module query string so the browser cannot reuse the old module.

- [ ] **Step 2: Verify browser RED**

Serve the repository locally and open `tests/frontend/analysis_card_browser.html`. Expected report: `jobConnectionState` is not exported.

- [ ] **Step 3: Implement status copy and connection rendering**

Add this pure function to `analysis.js`:

```javascript
export function jobConnectionState(status) {
  const normalized = status === "analyzing" ? "live" : status;
  if (normalized === "parsing") return { label: "Reading PGN", terminal: false, failed: false };
  if (normalized === "live") return { label: "Live analysis", terminal: false, failed: false };
  if (["completed", "partial", "failed"].includes(normalized)) {
    return { label: `Batch ${normalized}`, terminal: true, failed: normalized === "failed" };
  }
  return { label: "Waiting for ingestion", terminal: false, failed: false };
}
```

Track the latest job status. `updateJob()` must render this copy, toggle `is-offline` only for failed jobs, and close EventSource only when `terminal` is true. The EventSource `open` handler must render the latest job state instead of overwriting it with `Event log connected`.

Change the template's initial message to `Connecting to live analysis`.

- [ ] **Step 4: Verify browser GREEN and interactions**

Reload the harness with a fresh query string. Expected report: `PASS`. Click `Details` and confirm it still opens and displays both percentages.

- [ ] **Step 5: Add and run the page-copy regression test**

In `test_pages.py`, assert the analysis page contains `Connecting to live analysis`. Run:

```bash
.venv/bin/python -m unittest tests.web_app.test_pages -v
node --check webapp/static/js/analysis.js
```

Expected: pass.

- [ ] **Step 6: Commit the live browser state**

Stage only the four Task 3 files and commit:

```bash
git commit -m "feat: show live batch progress"
```

---

### Task 4: Fail-fast Core Pipeline Launcher

**Files:**
- Create: `python-pipeline/live_pipeline.py`
- Create: `tests/test_live_pipeline.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `worker_commands(python: str, pipeline_directory: Path) -> tuple[tuple[str, ...], ...]`.
- Produces: `run_pipeline(process_factory=subprocess.Popen, sleep=time.sleep) -> int`.

- [ ] **Step 1: Write failing launcher tests**

Test exact command order and isolation:

```python
self.assertEqual(
    worker_commands("/venv/python", Path("/repo/python-pipeline")),
    (
        ("/venv/python", "/repo/python-pipeline/live_game_collector.py"),
        ("/venv/python", "/repo/python-pipeline/xgboost_inference.py"),
        ("/venv/python", "/repo/python-pipeline/torch_inference.py"),
        ("/venv/python", "/repo/python-pipeline/prediction_reporter.py"),
    ),
)
```

Use fake child processes whose `poll()` returns `None` or `7`. Assert that an unexpected exit returns `7`, terminates all still-running siblings, and waits for them. Add a KeyboardInterrupt test that returns zero and performs the same cleanup.

- [ ] **Step 2: Verify launcher tests RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_live_pipeline -v
```

Expected: import failure because `live_pipeline.py` does not exist.

- [ ] **Step 3: Implement the launcher**

Define the four-script tuple, build commands from `sys.executable`, and start every child with `process_factory(command)`. Poll all children every 250 ms. If one exits, return its nonzero status or `1` for an unexpected zero exit. In `finally`, terminate running children, wait up to five seconds, then kill and wait for any child that ignores termination.

The module entry point must call `raise SystemExit(run_pipeline())`. It must not import Torch, XGBoost, Flask, or any worker module.

Use this implementation shape:

```python
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
import time

PIPELINE_DIRECTORY = Path(__file__).resolve().parent
WORKER_SCRIPTS = (
    "live_game_collector.py",
    "xgboost_inference.py",
    "torch_inference.py",
    "prediction_reporter.py",
)

def worker_commands(
    python: str = sys.executable,
    pipeline_directory: Path = PIPELINE_DIRECTORY,
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (python, str(pipeline_directory / script))
        for script in WORKER_SCRIPTS
    )

def _stop_children(children: list[tuple[tuple[str, ...], object]]) -> None:
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
    process_factory: Callable = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    children: list[tuple[tuple[str, ...], object]] = []
    try:
        for command in worker_commands():
            children.append((command, process_factory(command)))
        while True:
            for command, child in children:
                returncode = child.poll()
                if returncode is not None:
                    print(
                        f"{Path(command[1]).name} exited with status {returncode}",
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
```

- [ ] **Step 4: Verify launcher tests GREEN and import isolation**

Run:

```bash
.venv/bin/python -m unittest tests.test_live_pipeline -v
.venv/bin/python -c "import importlib.util, pathlib, sys; p=pathlib.Path('python-pipeline/live_pipeline.py'); s=importlib.util.spec_from_file_location('live_pipeline', p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert 'torch' not in sys.modules and 'xgboost' not in sys.modules"
```

Expected: pass with no model modules imported.

- [ ] **Step 5: Update operations documentation**

Document this normal core command:

```bash
.venv/bin/python python-pipeline/live_pipeline.py
```

Keep the four individual commands under a debugging heading. Keep Flask and both web workers in their separate section.

- [ ] **Step 6: Commit the launcher**

Stage only the three Task 4 files and commit:

```bash
git commit -m "feat: supervise live inference workers"
```

---

### Task 5: Full Verification and Architecture Handoff

**Files:**
- Verify: all files changed in Tasks 1 through 4
- Preserve: `ml-model/train_xgboost.py`

**Interfaces:**
- Consumes: the complete live-state feature.
- Produces: test evidence and a clean implementation history while preserving the user's edit.

- [ ] **Step 1: Run the complete Python suite**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all non-service-gated tests pass; PostgreSQL tests may skip when `TEST_DATABASE_URL` is absent.

- [ ] **Step 2: Run the dedicated PostgreSQL suites**

```bash
TEST_DATABASE_URL=postgresql+psycopg://chess:chess@localhost:15432/chess_analytics_test_live \
  .venv/bin/python -m unittest tests.web_app.test_migrations_postgres tests.web_app.test_adapter_integration_postgres tests.web_app.test_repository_postgres -v
```

Expected: all tests pass against the dedicated test database.

- [ ] **Step 3: Run Go and JavaScript checks**

```bash
GOCACHE=/private/tmp/chessanalytics-go-cache go -C go-backend test -count=1 ./...
node --check webapp/static/js/analysis.js
node --check webapp/static/js/analyze.js
node --check webapp/static/js/main.js
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 4: Recheck the protected user edit and repository state**

```bash
git status --short --branch
git diff -- ml-model/train_xgboost.py
```

Expected: `main` contains the feature commits and `ml-model/train_xgboost.py` remains the only unstaged user modification.

- [ ] **Step 5: Report architecture changes and rollout order**

Report these changes explicitly:

1. job-level `analyzing` became durable `live` through Alembic revision `20260805_0002`;
2. Flask/SSE now display and retain a non-terminal live connection;
3. `live_pipeline.py` supervises four core workers while Flask and web workers remain separate;
4. no topic, feature, preprocessing, Go, XGBoost, or PyTorch contract changed.

Recommend stopping old adapter workers, applying `alembic upgrade head`, then restarting the adapter and the new pipeline launcher. Do not mutate the active development database or running processes during verification.
