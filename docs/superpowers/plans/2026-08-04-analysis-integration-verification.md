# Analysis Experience Integration Verification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that a PGN submitted through Flask reaches both isolated models and returns ordered, bounded game results through PostgreSQL and SSE.

**Architecture:** The verification harness runs the web adapter in-process while the existing Redpanda, Go, collector, model workers, and reporter remain separate services. One small PGN fixture exercises explicit completion and the real model artifacts. Documentation gives operators one process boundary and one command per service.

**Tech Stack:** Docker Compose, PostgreSQL, Redpanda/Kafka, Flask test client, Go service, Python workers, Server-Sent Events, `unittest`.

## Global Constraints

- Do not make Flask launch or supervise the core pipeline.
- Keep PyTorch and XGBoost in separate processes.
- Use the real retrained XGBoost model and sidecar for the live smoke run.
- Treat every browser model value as a score from zero to one.
- Preserve existing topic names and service boundaries.
- Record all core architecture changes in README documentation.

---

### Task 1: Add an opt-in live pipeline integration test

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_live_web_pipeline.py`
- Create: `tests/fixtures/webapp/live_two_games.pgn`

**Interfaces:**
- Test runs only when `RUN_LIVE_PIPELINE_TEST=1` and `TEST_DATABASE_URL` exist.
- Assumes Redpanda, Go, collector, both model workers, and reporter already run.

- [ ] **Step 1: Write the opt-in test around public interfaces**

```python
@unittest.skipUnless(
    os.getenv("RUN_LIVE_PIPELINE_TEST") == "1",
    "set RUN_LIVE_PIPELINE_TEST=1 for the live pipeline test",
)
def test_two_uploaded_games_arrive_in_source_order(self):
    response = self.client.post(
        "/api/analyses",
        data={"pgn_file": (fixture_stream(), "live_two_games.pgn")},
        content_type="multipart/form-data",
    )
    self.assertEqual(response.status_code, 202)
    job_id = response.get_json()["job"]["id"]
    self.ingestion_worker.process_one()
    results = self.wait_for_terminal_results(job_id, timeout=60)
    self.assertEqual([item["sequence"] for item in results], [1, 2])
    for item in results:
        self.assertGreaterEqual(item["scores"]["pytorch"]["score"], 0.0)
        self.assertLessEqual(item["scores"]["pytorch"]["score"], 1.0)
        self.assertGreaterEqual(item["scores"]["xgboost"]["score"], 0.0)
        self.assertLessEqual(item["scores"]["xgboost"]["score"], 1.0)
```

Use condition polling against the job API; do not sleep for a fixed completion time.

- [ ] **Step 2: Confirm the test skips without opt-in and fails before the live stack is ready**

- [ ] **Step 3: Start each external service and run the live test**

Start Compose, Go, `live_game_collector.py`, `torch_inference.py`, `xgboost_inference.py`, `prediction_reporter.py`, and the web result worker in separate sessions. Apply migrations before the test.

- [ ] **Step 4: Fix only reproduced integration defects and rerun focused suites**

- [ ] **Step 5: Commit Task 1**

Commit with `test: exercise the live web analysis pipeline`.

### Task 2: Document operation and architecture changes

**Files:**
- Modify or create: `README.md`
- Create: `docs/core-architecture-changes.md`
- Modify: `.env.example`

**Interfaces:**
- Gives one startup command per independent service.
- Identifies new fields, artifacts, database, and web processes.

- [ ] **Step 1: Add a tested command reference**

Document:

```text
docker compose -f infra/docker-compose.yaml up -d
cd go-backend && go run .
./.venv/bin/python python-pipeline/live_game_collector.py
./.venv/bin/python python-pipeline/torch_inference.py
./.venv/bin/python python-pipeline/xgboost_inference.py
./.venv/bin/python python-pipeline/prediction_reporter.py
./.venv/bin/alembic upgrade head
./.venv/bin/python -m webapp.ingestion_worker
./.venv/bin/python -m webapp.result_worker
./.venv/bin/flask --app webapp run
```

Include dependency setup, model retraining, PostgreSQL URL, ports, and stop commands.

- [ ] **Step 2: List the core changes**

Record explicit `game_complete`, shared Kafka configuration, XGBoost sidecar enforcement, PyTorch L2 plus sigmoid, and unchanged topics/process isolation. Separate those changes from the new Flask/PostgreSQL adapter.

- [ ] **Step 3: Qualify scores and benchmarks**

State that live values are model-estimated suspicious-play scores and that the four landing-page metrics came from local benchmarks rather than production guarantees.

- [ ] **Step 4: Verify every documented command has a matching file or console entry point**

- [ ] **Step 5: Commit Task 2**

Commit with `docs: explain the analysis web stack`.

### Task 3: Run the final repository verification and review

**Files:**
- Modify only files required by verified failures.

**Interfaces:**
- Produces a clean verification record for Python, Go, PostgreSQL, JavaScript, live pipeline, and browser behavior.

- [ ] **Step 1: Run all unit suites**

Run Python discovery, Go tests with a workspace-safe cache, and native Node tests.

- [ ] **Step 2: Run PostgreSQL migration and repository integration suites**

Use a fresh test database and run Alembic downgrade/upgrade coverage.

- [ ] **Step 3: Run isolated artifact smoke tests and the opt-in live test**

- [ ] **Step 4: Repeat browser verification after the live result path works**

Confirm the result page handles real SSE events, reconnects without duplicates, and closes on a terminal job.

- [ ] **Step 5: Run `git diff --check`, inspect staged scope, and request final code review**

- [ ] **Step 6: Commit any verification-only correction through its own focused test**

Use `fix: resolve analysis integration verification` only when Step 1 through Step 5 reveal a real defect; otherwise create no empty commit.
