# Live Batch State Design

## Goal

Show a batch as `live` while the PGN ingestor discovers games and the model pipeline completes earlier games. Update game verdicts and counters as results arrive. Use `completed`, `partial`, or `failed` only after ingestion reaches EOF and every discovered game settles.

## Scope

This change covers the Flask/PostgreSQL adapter, the analysis page, and local process orchestration. It does not change PGN move events, Kafka topic names, Go feature extraction, model features, preprocessing, XGBoost, or PyTorch.

## Job State Contract

The adapter uses these job states:

| State | Meaning |
| --- | --- |
| `received` | The adapter accepted the source but has not queued it. |
| `queued` | An ingestion worker can claim the source. |
| `parsing` | The worker claimed the source and is locating the first game. |
| `live` | Ingestion, analysis, or both are active. Games may complete in this state. |
| `completed` | Ingestion reached EOF and every valid game completed. |
| `partial` | Ingestion reached EOF and the final batch contains completed and failed games. |
| `failed` | Ingestion reached EOF without a completed game, or the job reached a terminal failure. |

The new `live` state replaces job-level `analyzing`. Per-game states remain `queued`, `analyzing`, `completed`, and `failed`.

The first published game moves its job to `live`. Each joined model result immediately moves that game to `completed`, increments the completed counter, stores both scores, and emits a durable `game.updated` event. The job stays `live` while the ingestor owns the spool or any discovered games remain unsettled.

EOF clears the spool and ingestion lease. If games remain queued or analyzing, the job stays `live`. The last settled game moves the job to its terminal status. The SSE endpoint treats `live` as non-terminal and keeps the browser connection open.

## Persistence and Migration

Add `JOB_STATUS_LIVE` to the SQLAlchemy model and replace job-level writes of `analyzing` with `live`. Keep the game-level `analyzing` constant unchanged.

Add an Alembic revision that:

1. drops `ck_analysis_jobs_status`;
2. changes existing `analysis_jobs.status = 'analyzing'` rows to `live`;
3. recreates the constraint with `live` and without job-level `analyzing`.

The migration preserves active job counters, leases, spool paths, result rows, and event history. Existing historical event payloads may still contain `analyzing`; event replay must accept them without changing durable rows.

## Browser Behavior

The analysis page displays `Live analysis` whenever the job status is `live`. The total, queued, analyzing, completed, and failed counters continue updating from every SSE payload. Game cards change to `completed` as soon as joined results arrive, even while the total count continues growing.

The browser closes its EventSource only for `completed`, `partial`, or `failed`. During `parsing`, it displays `Reading PGN`. It maps a historical `analyzing` event payload to `Live analysis` without writing that legacy value back to PostgreSQL. A reconnect replays durable events and restores the same state.

The progress denominator is the number of games discovered so far. The percentage may move backward when the ingestor discovers another game, while the completed count only increases.

## Core Worker Launcher

Add one command that starts and monitors these four existing, isolated processes:

- `live_game_collector.py`
- `xgboost_inference.py`
- `torch_inference.py`
- `prediction_reporter.py`

The launcher uses the active Python interpreter, forwards termination to every child, and exits nonzero if any child exits unexpectedly. It does not import either model library into its own process, restart failed workers, launch Flask, or launch the web ingestion and result workers. This preserves model-process isolation and keeps Flask separate.

The README will replace four easy-to-miss core commands with the launcher command while retaining the individual commands for debugging.

## Failure Handling

An ingestion publish failure returns the job to `queued` under the existing retry contract. A retry returns it to `parsing`, then `live` after the first published game. Invalid games update the failed counter while valid games continue through the pipeline.

If a core child process exits, the launcher stops its siblings and reports the failed process. Operators can restart one command and recover from committed Kafka offsets instead of running a partial pipeline without noticing.

## Tests

Tests will cover:

- model constants and the new database check constraint;
- migration of an active `analyzing` row to `live` without changing counters or lease data;
- repository transitions from `parsing` to `live`, live per-game completion before EOF, live state after EOF with outstanding games, and final terminalization;
- SSE staying open for `live` and closing only for terminal states;
- page copy and JavaScript handling for `parsing`, `live`, and terminal states;
- launcher command construction, child failure propagation, and child cleanup;
- the existing PostgreSQL integration flow completing early games during ingestion.

## Architecture Changes

The change adds one job status, one Alembic migration, and one process launcher. It does not modify the core message schema, topic topology, feature vectors, preprocessing contracts, or model code.
