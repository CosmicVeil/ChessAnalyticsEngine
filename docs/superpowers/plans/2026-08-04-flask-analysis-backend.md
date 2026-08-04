# Flask and PostgreSQL Analysis Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent Flask adapter that persists PGN batch jobs in PostgreSQL, publishes parsed games to the existing Kafka pipeline, stores joined results, and streams durable progress events.

**Architecture:** Flask serves JSON and SSE while two companion worker processes handle PGN ingestion and joined-result consumption. PostgreSQL owns job state, leases, results, and ordered events. Tests inject repository and Kafka protocols so unit coverage never needs either model library or a live broker.

**Tech Stack:** Python 3.14, Flask 3.1, SQLAlchemy 2.0, Alembic, psycopg 3, PostgreSQL 17, python-chess, confluent-kafka, `unittest`.

## Global Constraints

- Package all web code under `webapp/`; Flask must not import PyTorch or XGBoost.
- Use PostgreSQL in production and integration tests; do not substitute SQLite.
- Keep Flask, ingestion workers, and result workers separate from the core pipeline processes.
- Publish moves to `chess-moves` and consume joined results from `chess-predictions`.
- Use opaque `web:<job UUID>:<sequence>` pipeline game IDs.
- Treat Kafka replays as idempotent database operations.
- Keep Lichess Study as an explicit unavailable connector.
- Remove spooled PGN content after parsing reaches a terminal state.

## Shared View and Event Contracts

`JobView` uses:

```python
{
    "id": str(job.id),
    "source_type": job.source_type,
    "source_name": job.source_name,
    "status": job.status,
    "counts": {
        "total": job.total_games,
        "queued": job.queued_games,
        "analyzing": job.analyzing_games,
        "completed": job.completed_games,
        "failed": job.failed_games,
    },
    "created_at": iso(job.created_at),
    "updated_at": iso(job.updated_at),
    "completed_at": iso(job.completed_at),
}
```

`GameView` uses `id`, `sequence`, `status`, nested `white` and `black` objects with `name` and `elo`, `result`, `played_at`, `opening`, `error`, and nested scores:

```python
"scores": {
    "pytorch": {"score": 0.8437, "prediction": True},
    "xgboost": {"score": 0.7161, "prediction": True},
    "agreement": True,
}
```

The repository emits only `job.updated` with `{"job": JobView}` and `game.updated` with `{"job": JobView, "game": GameView}`. The SSE event ID comes from `job_events.id`.

The games endpoint accepts `status=all|queued|analyzing|completed|failed`, `agreement=all|agree|disagree`, `score_min`, `score_max`, `cursor`, and `limit`. It caps `limit` at 50 and returns `items`, `next_cursor`, and `has_more`.

---

### Task 1: Add dependencies, settings, PostgreSQL, and the app factory

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `infra/docker-compose.yaml`
- Create: `.env.example`
- Create: `webapp/__init__.py`
- Create: `webapp/config.py`
- Create: `webapp/database.py`
- Create: `webapp/domain.py`
- Create: `tests/web_app/__init__.py`
- Create: `tests/web_app/test_config.py`

**Interfaces:**
- Produces: immutable `Settings.from_env(environ=None) -> Settings`.
- Produces: `create_session_factory(database_url: str) -> sessionmaker[Session]`.
- Produces: `create_app(settings: Settings | None = None, dependencies: WebDependencies | None = None) -> Flask`.

- [ ] **Step 1: Write failing settings and isolation tests**

```python
def test_settings_parse_limits_and_brokers(self):
    settings = Settings.from_env({
        "DATABASE_URL": "postgresql+psycopg://chess:chess@db/chess",
        "KAFKA_BOOTSTRAP_SERVERS": "one:9092,two:9092",
        "MAX_PGN_UPLOAD_BYTES": "4294967296",
        "MAX_PASTED_PGN_BYTES": "10485760",
        "SECRET_KEY": "test-secret",
    })
    self.assertEqual(settings.kafka_bootstrap_servers, "one:9092,two:9092")
    self.assertEqual(settings.max_pgn_upload_bytes, 4_294_967_296)
```

Add cases for non-positive or non-integer limits. Build an app with injected fakes, then assert `torch` and `xgboost` remain absent from `sys.modules`.

- [ ] **Step 2: Run `tests.web_app.test_config` and verify RED**

- [ ] **Step 3: Add dependencies with `uv`**

Add version floors for `Flask>=3.1.2`, `SQLAlchemy>=2.0.43`, `alembic>=1.16.5`, `psycopg[binary]>=3.2.9`, and `chess>=1.11.2`. Regenerate `uv.lock` through `uv add` or `uv lock`; do not hand-edit lock entries.

- [ ] **Step 4: Implement settings and dependency injection**

```python
@dataclass(frozen=True)
class Settings:
    database_url: str
    kafka_bootstrap_servers: str = "localhost:19092"
    kafka_web_result_group: str = "chess-web-results-v1"
    pgn_spool_directory: Path = Path(".runtime/pgn-spool")
    max_pgn_upload_bytes: int = 4 * 1024**3
    max_pasted_pgn_bytes: int = 10 * 1024**2
    secret_key: str = "development-only-change-me"
```

`WebDependencies` holds optional `store`, `spool_service`, and `health_probe` objects. The factory creates defaults only for dependencies that tests do not supply.

- [ ] **Step 5: Add PostgreSQL to Compose**

Use `postgres:17-alpine`, database/user/password `chess_analytics`/`chess`/`chess`, host port `15432`, a named volume, and a `pg_isready` health check. Leave all Redpanda services and ports unchanged.

- [ ] **Step 6: Run focused tests and import checks**

- [ ] **Step 7: Commit Task 1**

Commit with `feat: scaffold Flask analysis adapter`.

### Task 2: Define the PostgreSQL schema and migration

**Files:**
- Create: `alembic.ini`
- Create: `webapp/models.py`
- Create: `webapp/migrations/env.py`
- Create: `webapp/migrations/script.py.mako`
- Create: `webapp/migrations/versions/20260804_0001_analysis_adapter.py`
- Create: `tests/web_app/test_migrations_postgres.py`

**Interfaces:**
- Produces ORM models: `AnalysisJob`, `AnalysisGame`, `AnalysisResult`, `JobEvent`, `UnmatchedResult`.
- Produces string constants for job/game statuses and checked database constraints.

- [ ] **Step 1: Write a PostgreSQL-only migration test**

Read `TEST_DATABASE_URL`. Skip with `PostgreSQL integration test requires TEST_DATABASE_URL` when absent. Upgrade to `head`, inspect the schema, and require these tables:

```python
{
    "analysis_jobs",
    "analysis_games",
    "analysis_results",
    "job_events",
    "unmatched_results",
}
```

Insert duplicate pipeline IDs and scores below zero or above one, then assert PostgreSQL rejects them.

- [ ] **Step 2: Run the test against a fresh PostgreSQL database and verify RED**

- [ ] **Step 3: Implement ORM models and the matching Alembic migration**

Use UUID primary keys and timezone-aware timestamps. Add:

- Unique `(job_id, sequence)` and unique `pipeline_game_id`.
- Unique result `game_id`.
- Unique unmatched `(topic, partition, offset)`.
- Score checks `0 <= score AND score <= 1`.
- Non-negative counter checks.
- Indexes on job status, `(job_id, sequence)`, `(job_id, id)` events, and unmatched pipeline IDs.
- JSONB for `model_payload`, event payloads, and unmatched payloads.

Use strings with `CheckConstraint` for statuses rather than PostgreSQL native enums.

- [ ] **Step 4: Run migration upgrade, downgrade, and upgrade**

- [ ] **Step 5: Commit Task 2**

Commit with `feat: add analysis job database schema`.

### Task 3: Implement transactional job and result storage

**Files:**
- Create: `webapp/repository.py`
- Create: `tests/web_app/fakes.py`
- Create: `tests/web_app/test_repository_unit.py`
- Create: `tests/web_app/test_repository_postgres.py`

**Interfaces:**
- Produces protocol `AnalysisStore` in `webapp/domain.py`.
- Produces PostgreSQL `SqlAlchemyAnalysisStore`.
- Produces DTOs `JobSnapshot`, `GameSnapshot`, `JoinedResult`, `JobEventSnapshot`, `GameQuery`, and `Page[T]`.

- [ ] **Step 1: Define the store protocol and DTO tests**

```python
class AnalysisStore(Protocol):
    def create_queued_job(self, source: SpooledSource) -> JobSnapshot: ...
    def claim_next_job(self, worker_id, now, lease_duration) -> ClaimedJob | None: ...
    def renew_claim(self, job_id, worker_id, now, lease_duration) -> bool: ...
    def upsert_parsed_game(self, job_id, game: ParsedGame) -> GameSnapshot: ...
    def record_invalid_game(self, job_id, failure: InvalidGame) -> None: ...
    def mark_game_published(self, game_id) -> None: ...
    def schedule_retry(self, job_id, worker_id, reason, retry_at) -> None: ...
    def finish_ingestion(self, job_id, worker_id) -> None: ...
    def store_joined_result(self, result: JoinedResult) -> StoreResultOutcome: ...
    def store_unmatched_result(self, message, payload) -> None: ...
    def get_job(self, job_id) -> JobSnapshot | None: ...
    def list_games(self, job_id, query: GameQuery) -> Page: ...
    def events_after(self, job_id, event_id, limit) -> list[JobEventSnapshot]: ...
```

- [ ] **Step 2: Write failing repository integration tests**

Cover concurrent `FOR UPDATE SKIP LOCKED` claims, expired lease recovery, idempotent sequence upsert, replayed joined results, partial versus completed jobs, and increasing event IDs.

- [ ] **Step 3: Implement transaction boundaries**

`create_queued_job` writes the job and initial `job.updated` event in one transaction. Result insertion, game completion, counters, terminal job status, and `game.updated` event also share one transaction. `finish_ingestion` must not replace a terminal status produced by fast results.

- [ ] **Step 4: Run unit and PostgreSQL repository suites**

- [ ] **Step 5: Commit Task 3**

Commit with `feat: persist analysis job state transitions`.

### Task 4: Stream and parse bounded PGN sources

**Files:**
- Create: `webapp/spool.py`
- Create: `webapp/pgn.py`
- Create: `tests/fixtures/webapp/small_batch.pgn`
- Create: `tests/fixtures/webapp/malformed_then_valid.pgn`
- Create: `tests/web_app/test_spool.py`
- Create: `tests/web_app/test_pgn.py`

**Interfaces:**
- Produces: `SpoolService.spool_file(stream, source_name) -> SpooledSource`.
- Produces: `SpoolService.spool_text(text, source_name="pasted-pgn") -> SpooledSource`.
- Produces: `iter_pgn_games(stream) -> Iterator[ParsedGame | InvalidGame]`.
- Produces: `pipeline_game_id(job_id, sequence) -> str`.
- Produces: `build_move_events(game, game_id) -> Iterator[dict]`.

- [ ] **Step 1: Write failing spool tests**

Use a fake binary stream whose `read()` raises unless the caller supplies a positive chunk size. Test exact limits, one byte over, UTF-8 pasted-text size, traversal names, opaque file paths, `0o700` directories, and partial-file cleanup.

- [ ] **Step 2: Implement private chunked spooling**

Use UUID filenames below the configured spool root. Sanitize only the display name. Count bytes while writing and delete partial output before raising `SourceTooLarge`.

- [ ] **Step 3: Write failing PGN contract tests**

Require SAN order, stable IDs, safe defaults for ratings and clocks, and `game_complete: true` only on the final ply. Include checkmate, resignation, agreed draw, empty mainline, and malformed-then-valid fixtures.

Expected event shape:

```python
{
    "game_id": "web:<uuid>:1",
    "move": "e4",
    "move_number": 1,
    "White Rating": "1500",
    "Black Rating": "1450",
    "time": 0.0,
    "game_complete": False,
}
```

- [ ] **Step 4: Implement streaming parsing and event conversion**

Read one game with `chess.pgn.read_game` per iteration. Build moves for one game only; do not materialize the whole source. Use `game.errors` for recoverable parse failures and continue at the next readable header.

- [ ] **Step 5: Run both focused suites and compile the modules**

- [ ] **Step 6: Commit Task 4**

Commit with `feat: parse and spool PGN analysis batches`.

### Task 5: Publish jobs and consume joined results

**Files:**
- Create: `webapp/kafka.py`
- Create: `webapp/ingestion_worker.py`
- Create: `webapp/result_worker.py`
- Create: `tests/web_app/test_kafka.py`
- Create: `tests/web_app/test_ingestion_worker.py`
- Create: `tests/web_app/test_result_worker.py`

**Interfaces:**
- Produces protocol `MovePublisher.publish_game(game_id, events) -> None`.
- Produces protocol `ResultConsumer.poll`, `commit`, and `close`.
- Produces `IngestionWorker.process_one(now=None) -> bool`.
- Produces `ResultWorker.process(message) -> ResultDisposition`.

- [ ] **Step 1: Write failing ingestion-worker tests**

Use `FakeAnalysisStore` and `FakeMovePublisher`. Cover source order, stable keys, parse-error continuation, job transition to analyzing, Kafka retry without spool deletion, reclaim replay without duplicate rows, and no-valid-game failure.

- [ ] **Step 2: Implement one-job ingestion**

Claim a job, reopen its spool, renew the lease at game boundaries, upsert metadata, publish each game with one flush checkpoint, finish ingestion, clear the database spool reference, then remove the file. A Kafka error schedules retry and retains the spool.

- [ ] **Step 3: Write failing result-worker commit-order tests**

Known `web:` result: store before commit. Replay: commit without duplicate counters/events. Unknown `web:` result: persist unmatched before commit. Non-web result: ignore and commit. Database error: do not commit.

- [ ] **Step 4: Implement Kafka adapters and the result worker**

The publisher writes `chess-moves`. The consumer reads `chess-predictions` with `KAFKA_WEB_RESULT_GROUP`. Preserve typed score and prediction fields; place optional diagnostics in `model_payload`.

- [ ] **Step 5: Add module entry points and run focused tests**

`python -m webapp.ingestion_worker` and `python -m webapp.result_worker` create settings, sessions, store, and Kafka adapters without invoking Flask's development server.

- [ ] **Step 6: Commit Task 5**

Commit with `feat: bridge web analysis jobs to Kafka`.

### Task 6: Add submission, status, game-query, and health APIs

**Files:**
- Create: `webapp/api.py`
- Create: `webapp/services.py`
- Create: `tests/web_app/test_api.py`

**Interfaces:**
- Produces `POST /api/analyses`.
- Produces `GET /api/analyses/<uuid>`.
- Produces `GET /api/analyses/<uuid>/games`.
- Produces `GET /api/health`.

- [ ] **Step 1: Write failing Flask-client tests**

Submit exactly one of `pgn_file`, `pgn_text`, or `lichess_url`. File and paste return HTTP 202 with page, status, games, and events URLs. Lichess returns HTTP 501 with `connector_unavailable` and the supplied URL. Cover 400 source selection, 413 size, 422 empty PGN, 404 job, capped pagination, filters, and 503 PostgreSQL failure.

- [ ] **Step 2: Implement submission and serialization services**

Serialize one stable `JobView` and `GameView` contract. Scores remain decimals from zero to one. Never expose spool paths or raw PGN text.

- [ ] **Step 3: Implement health semantics**

Return separate `database` and `kafka` states. Database failure makes overall state unavailable. Kafka failure reports degraded because the adapter may accept a queued job when PostgreSQL works.

- [ ] **Step 4: Run focused tests and inspect response JSON**

- [ ] **Step 5: Commit Task 6**

Commit with `feat: expose analysis job APIs`.

### Task 7: Stream durable SSE progress

**Files:**
- Create: `webapp/sse.py`
- Create: `tests/web_app/test_sse.py`
- Modify: `webapp/api.py`

**Interfaces:**
- Produces: `format_sse(event: JobEventSnapshot) -> bytes`.
- Produces: `stream_events(store, job_id, after_id, wait) -> Iterator[bytes]`.
- Produces: `GET /api/analyses/<uuid>/events`.

- [ ] **Step 1: Write failing format and reconnection tests**

Assert compact `id`, `event`, and JSON `data` fields. Cover `?after=`, `Last-Event-ID`, ordered batches, no duplicates, idle keepalive, terminal close, 404 job, and 400 negative or invalid event ID.

- [ ] **Step 2: Implement transaction-safe polling**

Open a new repository operation for each poll, close it before waiting, emit `: keepalive\n\n` while idle, and stop after sending the final terminal event.

- [ ] **Step 3: Set streaming headers**

Return `text/event-stream`, `Cache-Control: no-cache`, and `X-Accel-Buffering: no`.

- [ ] **Step 4: Run focused and full webapp unit suites**

- [ ] **Step 5: Commit Task 7**

Commit with `feat: stream durable analysis progress`.

### Task 8: Verify the adapter against PostgreSQL

**Files:**
- Create: `tests/web_app/test_adapter_integration_postgres.py`
- Create: `README.md` if the current branch still lacks it, otherwise modify it.

**Interfaces:**
- Exercises submission, claim, parse, fake publish, joined-result replay, queries, and SSE without a live Kafka broker.

- [ ] **Step 1: Start the Compose PostgreSQL service and apply migrations**

- [ ] **Step 2: Write the integration test**

Submit a two-game fixture, process it through `IngestionWorker` with a fake publisher, inject two joined results plus a replay, and assert one source-ordered completion event per game.

- [ ] **Step 3: Run all webapp unit and PostgreSQL tests**

- [ ] **Step 4: Document process boundaries and commands**

Document Flask, ingestion worker, result worker, Alembic migration, PostgreSQL, and the existing five pipeline processes. List the new core changes separately.

- [ ] **Step 5: Run the repository Python and Go suites**

- [ ] **Step 6: Commit Task 8**

Commit with `test: verify PostgreSQL analysis adapter`.
