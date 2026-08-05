# Flask Bulk Analysis Experience Design

## Goal

Add a Flask web application that markets Chess Analytics Engine, accepts large PGN batches, sends each game through the existing Kafka pipeline, and shows completed model scores as they arrive. Keep Flask, PostgreSQL, and the web workers outside the Go and model processes.

## Scope

The first release includes four pages:

- A landing page with qualified local benchmark results and a scroll-driven pipeline story.
- An analysis desk for PGN file upload, pasted PGN text, and a future Lichess Study connector.
- A live batch page that adds each game result when both models finish.
- A system page that explains feature extraction, aggregation, and isolated inference workers.

The release supports anonymous analysis jobs addressed by unguessable UUIDs. It does not add user accounts, authorization, billing, model calibration, automatic model training, or Lichess Study fetching. A production deployment must add access control before it handles private games.

## Existing Pipeline

The web adapter preserves the current topic topology:

```text
chess-moves
    -> Go state manager
    -> chess-features
    -> Python game feature collector
    -> chess-game-features
    -> PyTorch worker and XGBoost worker
    -> chess-model-predictions
    -> prediction reporter
    -> chess-predictions
```

Flask does not import PyTorch or XGBoost. The two inference libraries stay in separate processes because the project has encountered a macOS OpenMP conflict when one process imports both.

## Architecture

### Web process

The Flask application serves HTML, static assets, JSON endpoints, and Server-Sent Events. It writes job records to PostgreSQL and reads durable job events for connected browsers. It does not launch Redpanda, Go, or any Python pipeline worker.

### Ingestion worker

A web-side ingestion process claims queued jobs from PostgreSQL with row locking. It streams the spooled PGN file through `python-chess`, records game headers, and publishes legal SAN moves to `chess-moves`. It bounds the number of in-flight Kafka messages and flushes at job checkpoints rather than after each move.

The worker gives each game a collision-resistant pipeline ID:

```text
web:<job UUID>:<one-based game sequence>
```

The database maps that opaque ID to player names, ratings, result, date, opening metadata, and the source sequence. The core topics continue to use `game_id` as their correlation key.

### Result worker

A result process joins the existing `chess-predictions` consumer group for the web application. It looks up each pipeline game ID, stores the joined result, advances job counters, and inserts one ordered job event in the same PostgreSQL transaction. Unique constraints make Kafka replays idempotent.

Multiple result workers may share one consumer group. Multiple ingestion workers may claim different jobs. Flask instances only read and write PostgreSQL, so the web layer can scale without relying on process memory.

### PostgreSQL

PostgreSQL provides crash recovery, concurrent writes, row-level locking, and a shared store for more than one Flask instance. SQLAlchemy defines the application models, and Alembic owns schema migrations. The application reads the connection string from `DATABASE_URL`.

The local container configuration adds PostgreSQL without changing the Redpanda service or its topics.

## Data Model

### `analysis_jobs`

- `id`: UUID primary key.
- `source_type`: `file`, `paste`, or `lichess`.
- `source_name`: sanitized display name.
- `spool_path`: private server path, cleared after parsing.
- `status`: `received`, `queued`, `parsing`, `analyzing`, `completed`, `partial`, or `failed`.
- `total_games`, `queued_games`, `completed_games`, `failed_games`: integer counters.
- `claimed_by`, `lease_expires_at`, `attempt_count`: ingestion recovery fields.
- `error_code`, `error_message`: nullable terminal job error.
- `created_at`, `updated_at`, `completed_at`: timezone-aware timestamps.

### `analysis_games`

- `id`: UUID primary key.
- `job_id`: foreign key to `analysis_jobs`.
- `sequence`: one-based source order.
- `pipeline_game_id`: unique Kafka correlation key.
- `white_name`, `black_name`, `white_elo`, `black_elo`, `result`, `played_at`, `opening`: parsed PGN metadata.
- `status`: `queued`, `analyzing`, `completed`, or `failed`.
- `error_code`, `error_message`: nullable game error.

The pair `(job_id, sequence)` is unique.

### `analysis_results`

- `game_id`: unique foreign key to `analysis_games`.
- `pytorch_score`, `xgboost_score`: values constrained to the inclusive range zero to one.
- `pytorch_prediction`, `xgboost_prediction`: boolean threshold results.
- `model_payload`: JSONB for diagnostics that do not need typed columns.
- `created_at`: timezone-aware timestamp.

The UI calls both values model-estimated suspicious-play scores. It does not call them accuracy or calibrated probability.

### `job_events`

- `id`: monotonically increasing bigint primary key.
- `job_id`: indexed foreign key to `analysis_jobs`.
- `game_id`: nullable foreign key to `analysis_games`.
- `event_type`: job or game state transition.
- `payload`: JSONB browser payload.
- `created_at`: timezone-aware timestamp.

The SSE endpoint uses `id` as its event ID. A reconnecting browser sends `Last-Event-ID` and receives later rows without losing completed games.

### `unmatched_results`

- `topic`, `partition`, `offset`: unique Kafka source coordinates.
- `pipeline_game_id`: indexed result key.
- `payload`: JSONB result body.
- `created_at`: timezone-aware timestamp.

The result worker commits non-web results without storing them. It stores an unknown `web:` result here before it commits the Kafka offset, so an unexpected mapping does not block the partition or disappear.

## HTTP Interface

### Pages

- `GET /` renders the product landing page.
- `GET /analyze` renders the input desk.
- `GET /analyses/<job_id>` renders the live batch view.
- `GET /system` renders the pipeline story.

### JSON and event endpoints

- `POST /api/analyses` accepts one PGN file, pasted PGN text, or a Lichess Study URL.
- `GET /api/analyses/<job_id>` returns job counters and status.
- `GET /api/analyses/<job_id>/games` returns paginated games and results.
- `GET /api/analyses/<job_id>/events` returns an SSE stream with durable IDs.
- `GET /api/health` reports web, PostgreSQL, and Kafka connectivity without loading model libraries.

The Lichess source returns a structured `connector_unavailable` response. The form retains the URL and explains that the connector has not shipped.

`POST /api/analyses` returns HTTP 202 with the job URL after it spools the source and records the job. The default file limit is 4 GiB through `MAX_PGN_UPLOAD_BYTES`; deployments may lower it. Pasted text has a smaller configurable limit because browsers keep textarea contents in memory.

The submission transaction advances a spooled job from `received` to `queued`. An ingestion worker claims it, sets `parsing`, and renews its lease while it reads games. The first published game advances the job to `analyzing`. A worker may reclaim a job whose lease expires after a crash and republish its games; the pipeline and result constraints treat those messages as replays.

## PGN Ingestion

The upload handler writes files to a private spool directory instead of loading them into memory. The ingestion worker parses one game at a time. It removes the spool file after parsing finishes or reaches a terminal error.

For each game, the worker:

1. Reads mainline moves and required headers.
2. Validates each move with `python-chess`.
3. Builds existing move fields, including ratings and clock values with safe defaults.
4. Marks the last move event with `game_complete: true`.
5. Publishes the moves in order under one Kafka key.

The Go `ChessMoveEvent` gains an additive `game_complete` boolean. After it applies the marked move and publishes its features, Go publishes the existing completion feature event when either the input flag is true or the reconstructed board has a terminal outcome. This handles resignations, agreed draws, time forfeits, and normal mates. Existing move-number checks handle a replay while Go still holds the game. A reclaimed web job republishes the full move sequence so Go can rebuild state after a process restart.

A malformed game creates a failed `analysis_games` row. The worker continues with later games when the parser can find the next PGN header. A file with no valid games fails the job.

## Live Inference Corrections

The current training scripts transform features, while the live workers send raw aggregate features to the models. The web interface cannot display those raw outputs as percentages.

### PyTorch

Training applies per-game L2 normalization:

```text
normalized = features / L2_norm(features)
```

The PyTorch worker will apply the same operation after schema alignment. The model trains with `BCEWithLogitsLoss`, so its final layer returns an unrestricted logit. The worker will apply `sigmoid` and publish a score in the inclusive range zero to one. The reporter will retain its `0.5` threshold after it receives the probability score.

Future PyTorch checkpoints will record the preprocessing type and schema fingerprint. The worker will validate the input dimension and fingerprint before scoring. The current checkpoint remains compatible because its training code already uses L2 normalization.

### XGBoost

Training currently performs min-max transformation before it selects the indexed train and test arrays. The split indices exist at that point, but the minimum and range still include held-out rows. The trainer will select raw training rows first, derive statistics from those rows, and apply them to both partitions.

The trainer will save an `xgboost_preprocessing.json` sidecar containing:

- An artifact format version.
- The ordered feature schema fingerprint.
- One training minimum and safe range per feature.
- The transformation name.

The XGBoost worker will load this sidecar, verify the schema fingerprint, transform the aligned live row, and call `predict_proba`. It will refuse to score when the sidecar is missing or incompatible. The existing XGBoost model needs one retraining run to produce the matching sidecar.

The worker will not compute min or max from an uploaded batch. Two identical games must receive identical transformed features regardless of the other games in the job.

### Score meaning

These corrections make the browser show the values that each model produces under its training preprocessing. The project has no probability calibration stage. The interface calls the values model scores or estimated likelihood and reserves accuracy claims for labeled benchmark results.

## Visual System

The site uses an editorial chess-intelligence style. Warm paper, ink black, vermilion, and acidic green create the palette. Display text uses a native editorial serif stack; labels and model data use a compact monospace stack. Square corners, thin rules, coordinate labels, and hard offsets replace generic dashboard cards.

The design excludes gradients, glass panels, neon purple or blue treatments, floating blobs, excessive pills, generic AI language, and repeated feature-card grids.

The project will include isolated chess-piece PNG files with real alpha channels. The landing and system pages place those assets in separate foreground, middle, and background layers. The assets keep crisp edges at high-density display sizes and include empty padding for motion.

### Motion

- The landing page moves board coordinates, pieces, and data labels at separate scroll rates.
- A pinned system sequence carries one game through PGN ingestion, Go features, aggregation, and both models.
- The upload desk stamps accepted files, counts parsed games, and moves queue markers into the analysis rail.
- The result page rolls score numbers, sweeps model meters, and inserts completed games in source order.
- SSE reconnects and job state changes update existing elements instead of replacing the page.

JavaScript limits animation work to transforms and opacity. It batches scroll updates with `requestAnimationFrame`. `prefers-reduced-motion` removes parallax, number rolls, and pinned transitions while preserving status changes. Keyboard focus, contrast, semantic headings, live-region restraint, and touch targets remain part of acceptance testing.

## Page Content

### Landing page

The hero introduces the engine as a streaming game-analysis system. It links to the analysis desk and the system page. The benchmark band may show approximately 1,500 games per minute, 87 percent classification accuracy, results under 550 milliseconds, and a false-positive rate below 5 percent. Each figure carries a `local benchmark` label and avoids a production guarantee.

### Analysis desk

The desk presents three numbered input modes: PGN file, pasted PGN, and Lichess Study. It shows source limits, privacy behavior, and pipeline health before submission. File and paste modes submit real jobs. The Lichess mode explains its future status without pretending to analyze the URL.

### Live batch page

The page shows total, queued, analyzing, completed, and failed counts. It renders source-ordered games with player metadata and separate PyTorch and XGBoost scores. Filters cover status, model agreement, and score range. A game row can reveal model outputs and source headers without loading all PGN moves into the DOM.

### System page

The page explains the existing topic flow, Go feature extraction, game-level aggregation, process-isolated model workers, and joined results. It describes model scores without claiming that the system proves cheating.

## Failure Handling

- PostgreSQL unavailability makes submission endpoints return HTTP 503 and keeps static marketing pages available.
- Kafka unavailability leaves accepted jobs queued and records a retry event. Workers use bounded exponential backoff.
- Parser failures identify the game sequence and retain valid games from the same source.
- Missing model preprocessing metadata causes the affected worker to exit with an artifact error instead of producing a misleading score.
- The result worker commits and ignores non-web pipeline IDs. It stores an unknown `web:` result in `unmatched_results`, writes a structured log, and then commits the message.
- Job counters update in the same transaction as each game transition.
- The UI shows partial completion when at least one game fails and at least one finishes.

The SSE endpoint queries events after the last sent ID and emits a keepalive comment during idle periods. PostgreSQL remains the durable source; the first release does not require Redis or PostgreSQL notifications.

## Configuration

The new web layer reads:

- `DATABASE_URL`
- `KAFKA_BOOTSTRAP_SERVERS`
- `KAFKA_WEB_RESULT_GROUP`
- `PGN_SPOOL_DIRECTORY`
- `MAX_PGN_UPLOAD_BYTES`
- `MAX_PASTED_PGN_BYTES`
- `SECRET_KEY`

Existing Python and Go Kafka entry points will read `KAFKA_BOOTSTRAP_SERVERS` with `localhost:19092` as the development default. Topic names remain unchanged.

## Testing

The project keeps `unittest` for Python and the standard Go test package.

### Unit tests

- Parse a small uploaded file and pasted PGN into ordered move events.
- Mark the final move as complete for checkmate, resignation, and agreed draw headers.
- Continue after one recoverable malformed game.
- Verify job state transitions, counters, idempotent results, and event ordering through repository fakes.
- Resume SSE output after a supplied event ID.
- Apply PyTorch L2 normalization and sigmoid to a known logit.
- Derive XGBoost min and range from training rows only.
- Apply saved XGBoost values to live aggregate features in schema order.
- Reject missing or mismatched preprocessing metadata.
- Handle explicit completion and replayed completion in Go.

### Integration tests

- Run migrations against PostgreSQL and exercise concurrent job claims.
- Use Flask's test client with a fake Kafka gateway.
- Run a result worker against replayed joined results and confirm one database result and one completion event.
- Verify health responses for available and unavailable dependencies.

### End-to-end test

A small committed PGN batch runs through Redpanda, Go, the collector, both inference workers with test artifacts, the reporter, PostgreSQL, and SSE. The test expects one ordered browser event per game and scores between zero and one.

### Visual verification

Browser checks cover desktop and narrow layouts, file and paste flows, live insertions, reconnection, empty and error states, keyboard navigation, transparent asset edges, and reduced motion.

## Core Architecture Changes

The implementation changes these core-adjacent contracts:

1. `ChessMoveEvent` gains an optional `game_complete` field, and Go respects it after processing the final move.
2. Model training writes preprocessing metadata, and live inference applies it before scoring.
3. PyTorch live inference converts logits to a score with sigmoid.
4. Kafka clients accept an environment-configured broker address.

The implementation keeps the existing services, model-process isolation, feature aggregation, topic names, joined result payload, and training architecture. PostgreSQL, Flask, ingestion workers, result workers, templates, scripts, styles, and PNG assets form a separate web adapter.
