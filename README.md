# Chess Analytics Engine

Chess Analytics Engine turns complete chess games into one feature vector per game, scores that vector with isolated PyTorch and XGBoost workers, and joins both outputs into a live result. The Flask application is a separate adapter: it accepts PGN batches, publishes their games into the existing Kafka pipeline, and streams durable game-by-game updates back to the browser.

The browser calls the two outputs **model scores**. They are bounded to zero through one, but they are not calibrated probabilities and they do not prove cheating.

## System shape

```text
PGN upload or paste
        |
        v
Flask ----> PostgreSQL <---- browser SSE
  |             ^
  | queued job  | joined result + ordered event
  v             |
web ingestion   | web result worker
  |             |
  v             |
chess-moves -> Go feature extraction -> chess-features
    -> game-level aggregation -> chess-game-features
    -> PyTorch worker + XGBoost worker
    -> chess-model-predictions -> prediction reporter
    -> chess-predictions
```

Flask, the PGN ingestion worker, and the result worker run as three independent processes. Flask never imports PyTorch or XGBoost and does not start the core pipeline.

## What the web application provides

- A combined upload and live-results Home with an editorial, animated presentation, layered chess-piece motion, and qualified local benchmark figures.
- PGN file upload and pasted PGN submission. The Lichess Study control is present but deliberately returns `501 connector_unavailable` until its connector is implemented.
- Streaming PGN parsing with bounded private disk spooling instead of loading a large upload into memory.
- Per-game state and separate PyTorch/XGBoost scores, rendered in original PGN order as results arrive.
- Status, agreement, and score-range filtering with cursor pagination.
- Resumable Server-Sent Events backed by PostgreSQL event rows. Reconnecting with `Last-Event-ID` does not lose finished games.
- Reduced-motion support, keyboard focus states, responsive layouts, and true-alpha PNG piece assets.

Anonymous job IDs are unguessable UUIDs, but this release has no accounts or authorization. Add access control before accepting private games on a public deployment.

## Why PostgreSQL instead of SQLite

SQLite would work for a single-process prototype. This application has concurrent Flask requests, ingestion workers, result consumers, and potentially several web instances. PostgreSQL is used because the job protocol depends on:

- row-level locks and `FOR UPDATE SKIP LOCKED` so multiple workers can claim different jobs safely;
- concurrent writes from Kafka result workers and browser submissions;
- transactional counter updates and durable SSE events committed together;
- JSONB result payloads, constraints, and server-side migrations;
- one shared database reachable by independently scaled processes.

SQLite serializes writers around a database file and is awkward to share across hosts. PostgreSQL avoids making the persistence layer a later architectural rewrite.

## Feature preparation and model transformations

### Game-level aggregation and splitting

Training data begins as move rows. `prepare_game_training_data` groups them by `GameID` and builds one row per complete game using `mean`, `standard deviation`, `minimum`, `maximum`, and `last` for every numeric move feature, plus `move_count`.

The grouped rows are then split by game. No move from a held-out game can appear in training. The split and the numerical transformation solve different problems:

- the grouped split prevents train/test leakage between games;
- the transformation puts live values in the same numerical space the model saw during training.

You already had transformations in the training scripts. The live workers did not have a reliable copy of the XGBoost training statistics, and the PyTorch worker did not apply the same per-game normalization before inference. That training/inference mismatch is why the live path needed an explicit preprocessing contract.

### XGBoost min-max transformation

After the game split is chosen, only raw training rows are used to calculate each feature's minimum and observed range:

```text
range = training_max - training_min
transformed_value = (value - training_min) / safe_range
```

A zero range is replaced with `1.0`, preventing division by zero. Held-out rows never influence these statistics. Values seen later are not clipped, so a live value outside the training range may legitimately transform below zero or above one.

Training writes `ml-model/models/xgboost_preprocessing.json` with the ordered feature names, schema fingerprint, minimums, and safe ranges. Live XGBoost inference validates that sidecar and reuses it exactly. It never fits min/max values from an uploaded batch; otherwise the same game could receive a different score depending on the other games in that upload.

The small min/max training bug was fixed by persisting the actual observed range (`max - min`) and deriving it from training rows only. Retrain XGBoost once before running live inference so the model and sidecar are produced together.

### PyTorch L2 normalization and sigmoid

PyTorch training normalizes each complete game independently:

```text
normalized_game = game_features / L2_norm(game_features)
```

There are no dataset-wide values to persist for L2 normalization. The live worker aligns the feature schema and repeats the same operation for each game. The checkpoint records `preprocessing: l2`, its input size, and the schema fingerprint so an incompatible artifact fails clearly.

The network trains with `BCEWithLogitsLoss`, so its final value is a logit. Live inference applies `sigmoid` once to produce the displayed zero-to-one score. XGBoost uses the positive-class value from `predict_proba`.

## Core architecture changes

The topic topology and process isolation remain intact. Changes to the existing core are limited and additive:

1. Go's `ChessMoveEvent` accepts an optional `game_complete` flag. The extractor emits its completion feature when the board is terminal **or** this flag is set, covering resignations, agreed draws, and time forfeits.
2. Go and Python core processes read `KAFKA_BOOTSTRAP_SERVERS`, with `localhost:19092` retained as the development default.
3. XGBoost training now fits min/max metadata on training rows and writes a validated preprocessing sidecar. Its live worker loads that sidecar before scoring.
4. PyTorch live inference now repeats training-time L2 normalization, validates checkpoint metadata, and converts the logit with sigmoid.
5. The new Flask process and its two web workers sit outside the core. They use existing `chess-moves` and `chess-predictions` topics; no core topic was renamed and the two model libraries remain in separate processes.
6. `live_pipeline.py` is an additive supervisor for the four existing Python core workers. It imports no model or Flask code, and it stops the remaining workers if one exits so a partially running pipeline is visible immediately.

The web layer also adds PostgreSQL, Alembic migrations, private PGN spooling, durable job events, and idempotent result storage. These additions adapt the pipeline for browser workloads without moving feature extraction or inference into Flask. The frontend presentation work changes neither feature extraction nor model inference.

## Local setup

Install the Python environment and start the local infrastructure:

```bash
uv sync
docker compose -f infra/docker-compose.yaml up -d postgres redpanda-0 console
```

Use the values in `.env.example`, then apply the schema:

```bash
export DATABASE_URL=postgresql+psycopg://chess:chess@localhost:15432/chess_analytics
export KAFKA_BOOTSTRAP_SERVERS=localhost:19092
.venv/bin/alembic upgrade head
```

Run the Go processor in its own terminal:

```bash
cd go-backend
go run .
```

Start all four Python core workers together from the repository root:

```bash
.venv/bin/python python-pipeline/live_pipeline.py
```

For debugging, stop the supervisor and start the same workers individually:

```bash
.venv/bin/python python-pipeline/live_game_collector.py
.venv/bin/python python-pipeline/xgboost_inference.py
.venv/bin/python python-pipeline/torch_inference.py
.venv/bin/python python-pipeline/prediction_reporter.py
```

Keep Flask and both web adapter workers separate from the core launcher, with each command in its own terminal:

```bash
.venv/bin/flask --app webapp run --port 5000
.venv/bin/python -m webapp.ingestion_worker
.venv/bin/python -m webapp.result_worker
```

Open `http://127.0.0.1:5000`. The health endpoint is `GET /api/health`.

To retrain XGBoost and create its required preprocessing sidecar:

```bash
cd ml-model
../.venv/bin/python train_xgboost.py
```

## Configuration

| Variable | Purpose | Development default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL connection string | required |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka/Redpanda brokers | `localhost:19092` |
| `KAFKA_WEB_RESULT_GROUP` | Web result consumer group | `chess-web-results-v1` |
| `PGN_SPOOL_DIRECTORY` | Private temporary PGN storage | `.runtime/pgn-spool` |
| `MAX_PGN_UPLOAD_BYTES` | File upload limit | 4 GiB |
| `MAX_PASTED_PGN_BYTES` | Pasted-text limit | 10 MiB |
| `SECRET_KEY` | Flask secret | development value only |

## HTTP surface

| Method and path | Purpose |
| --- | --- |
| `GET /` | Home: combined PGN upload and live-results workspace |
| `GET /analyze` | Compatibility redirect to Home (`/`) |
| `GET /analyses/<job_id>` | Restorable Home state for a specific job |
| `GET /system` | Information page |
| `POST /api/analyses` | Create a file or paste analysis job |
| `GET /api/analyses/<job_id>` | Job snapshot and counters |
| `GET /api/analyses/<job_id>/games` | Filtered, paginated game results |
| `GET /api/analyses/<job_id>/events` | Durable SSE updates |
| `GET /api/health` | Web, PostgreSQL, and Kafka health |

## Verification

Unit tests do not require live services:

```bash
.venv/bin/python -m unittest discover -s tests -v
cd go-backend
go test -count=1 ./...
```

The PostgreSQL tests are opt-in:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://chess:chess@localhost:15432/chess_analytics
.venv/bin/python -m unittest tests.web_app.test_migrations_postgres tests.web_app.test_repository_postgres tests.web_app.test_adapter_integration_postgres -v
```

The integration test exercises submission, PGN streaming, an immediate per-game result race, idempotent replay, source-ordered API results, and resumable SSE against real PostgreSQL transactions.
