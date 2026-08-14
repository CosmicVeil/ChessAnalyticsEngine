# Analysis Cleanup Utility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guarded local utility that discards Kafka move lag and every unfinished web analysis while preserving terminal history.

**Architecture:** A Bash entry point orchestrates existing Docker Compose services and PostgreSQL. It refuses to run around active producers or consumers, previews its targets, performs a child-first maintenance transaction, validates database integrity, and then removes only verified spool files. Python `unittest` tests execute the real script with fake `docker` and `pgrep` commands.

**Tech Stack:** Bash 3.2+, Docker Compose, Redpanda `rpk`, PostgreSQL `psql`, Python `unittest`

## Global Constraints

- Preserve `completed`, `partial`, and `failed` jobs.
- Delete `queued`, `parsing`, and `live` jobs by default.
- Refuse to mutate data while ingestion or Go extraction is active.
- Require `CLEAR` unless `--yes` is supplied.
- Delete no filesystem path outside `.runtime/pgn-spool`.
- Do not change model inference, Kafka topics, Flask APIs, or database migrations.

---

### Task 1: Command safety surface

**Files:**
- Create: `tests/docker_help/test_clear_analysis_state.py`
- Create: `docker_help/clear_analysis_state.sh`

**Interfaces:**
- Consumes: repository-relative `infra/docker-compose.yaml`
- Produces: `clear_analysis_state.sh [--yes] [--stale-minutes MINUTES]`

- [ ] **Step 1: Write failing command-interface tests**

Create a temporary repository containing the script and fake commands. Cover `--help`, invalid arguments, missing Docker services, and an active `webapp.ingestion_worker` process. The active-worker test must assert that the fake Docker log contains no `group seek` call.

```python
def test_refuses_when_ingestion_worker_is_active(self):
    result = self.run_script("--yes", env={"FAKE_ACTIVE_WORKER": "1"})
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("ingestion worker is running", result.stderr)
    self.assertNotIn("group seek", self.docker_log())
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.docker_help.test_clear_analysis_state -v`

Expected: FAIL because `docker_help/clear_analysis_state.sh` does not exist.

- [ ] **Step 3: Implement argument parsing and process refusal**

Use `set -euo pipefail`, resolve the repository from the script path, accept only `--yes`, `--stale-minutes`, and `--help`, validate non-negative integer minutes, and check exact host-process patterns with `pgrep -f` before calling Docker.

```bash
if pgrep -f '(^|/)(python|python3)([0-9.]*)? -m webapp[.]ingestion_worker$' >/dev/null; then
  fail "ingestion worker is running; stop the app processes first"
fi
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m unittest tests.docker_help.test_clear_analysis_state -v`

Expected: PASS.

- [ ] **Step 5: Commit the command safety surface**

Run:

```bash
git add docker_help/clear_analysis_state.sh tests/docker_help/test_clear_analysis_state.py
git commit -m "feat(dev): guard analysis cleanup command"
```

### Task 2: Transactional cleanup and spool isolation

**Files:**
- Modify: `tests/docker_help/test_clear_analysis_state.py`
- Modify: `docker_help/clear_analysis_state.sh`

**Interfaces:**
- Consumes: `analysis_jobs`, `analysis_games`, `analysis_results`, `job_events`, `unmatched_results`, `chess-moves`, and `.runtime/pgn-spool`
- Produces: zero `chess-state-managers` lag, no unfinished jobs, and no matching spool files

- [ ] **Step 1: Write failing cleanup-order tests**

Add tests that assert confirmation refusal causes no seek, an active Kafka group member refuses cleanup, Kafka seek failure causes no cleanup SQL, success seeks before SQL and removes a valid temporary spool file, and an outside or symlinked spool path aborts before mutation.

```python
def test_unsafe_spool_path_aborts_before_kafka_seek(self):
    outside = self.repo / "outside.pgn"
    outside.write_text("data", encoding="utf-8")
    result = self.run_script("--yes", env={"FAKE_SPOOL_PATH": "../outside.pgn"})
    self.assertNotEqual(result.returncode, 0)
    self.assertTrue(outside.exists())
    self.assertNotIn("group seek", self.docker_log())
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.docker_help.test_clear_analysis_state -v`

Expected: FAIL because cleanup orchestration is absent.

- [ ] **Step 3: Implement preview, seek, SQL, and deletion**

Query targets with a fixed status predicate. When `--stale-minutes` is present, add an expired-lease and age predicate through validated `psql` variables. Capture spool paths before mutation and resolve every path under the repository spool directory before seeking Kafka.

Run `rpk group describe chess-state-managers`, require `MEMBERS 0`, then run:

```bash
docker compose -f "$COMPOSE_FILE" exec -T redpanda-0 \
  rpk group seek chess-state-managers --to end --topics chess-moves
```

The PostgreSQL transaction creates a temporary target table, sets `session_replication_role` to `replica`, deletes child rows before jobs, and raises an exception if any orphan check returns a row. Only after commit may the script remove each previously validated regular spool file with `rm -- "$path"`.

- [ ] **Step 4: Verify GREEN and syntax**

Run:

```bash
.venv/bin/python -m unittest tests.docker_help.test_clear_analysis_state -v
bash -n docker_help/clear_analysis_state.sh
```

Expected: all tests pass and Bash exits zero.

- [ ] **Step 5: Commit the cleanup behavior**

Run:

```bash
git add docker_help/clear_analysis_state.sh tests/docker_help/test_clear_analysis_state.py
git commit -m "feat(dev): clear unfinished analysis state"
```

### Task 3: Usage documentation and full verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the completed utility interface
- Produces: copyable local cleanup instructions and data-loss warning

- [ ] **Step 1: Add README usage**

Document the required stopped-process state, preview/confirmation behavior, default full unfinished-job purge, optional `--stale-minutes 30`, and the need to restart services and re-upload after cleanup.

- [ ] **Step 2: Run focused and regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.docker_help.test_clear_analysis_state -v
.venv/bin/python -m unittest discover -s tests -v
bash -n docker_help/clear_analysis_state.sh
git diff --check
```

Expected: all test commands exit zero with no failures, and `git diff --check` prints nothing.

- [ ] **Step 3: Review scope**

Run: `git status --short`

Expected: only the cleanup script, its tests, README, and plan are changed in addition to the user's pre-existing `.idea/material_theme_project_new.xml` and `ml-model/train_torch.py` edits.

- [ ] **Step 4: Commit the documentation**

Run:

```bash
git add README.md docs/superpowers/plans/2026-08-13-analysis-cleanup-utility.md
git commit -m "docs: explain analysis cleanup utility"
```
