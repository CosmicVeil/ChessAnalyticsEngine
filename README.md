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

- A combined upload and live-results Home with an editorial, animated presentation and layered chess-piece motion.
- PGN file upload and pasted PGN submission. The Lichess Study control is present but deliberately returns `501 connector_unavailable` until its connector is implemented.
- Streaming PGN parsing with bounded private disk spooling instead of loading a large upload into memory.
- Per-game state and separate PyTorch/XGBoost scores, rendered in original PGN order as results arrive.
- Status, agreement, and score-range filtering with cursor pagination.
- Resumable Server-Sent Events backed by PostgreSQL event rows. Job status includes an additive `event_cursor`; the browser loads the initial game snapshot, then opens the stream with `?after=<event_cursor>` so restoring a large active job skips replayed history without missing later events. Reconnecting with `Last-Event-ID` remains supported.
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

### Stockfish engine features

Each move carries four engine features: `centipawn_loss` (how much the move worsened the mover's evaluation), `evaluation` (the position after the move, in centipawns from White's side), `win_chance` (White's expected score on Lichess's curve), and `mate` (moves to a forced mate from White's side, 0 when none). They come from a local Stockfish searching 5,000 nodes per position. Scores are clamped to +/-1000, a forced mate counts as the clamp value, and checkmate or stalemate positions are scored directly because they have no search score. `ml-model/engine_features.py` and `go-backend/engine.go` implement the same rules, so training and live features match.

- **Training data:** `ml-model/add_engine_features.py` analyses every game in `data/sample.pgn` and `data/AI_sample.pgn`, matched to the dataset by game ID and move count. It caches one line per game, so an interrupted run resumes, then fills the four columns. The untouched dataset is kept as `chess_dataset_without_engine.csv`. On 11 workers the full dataset takes about 50 minutes.
- **Live:** the Go feature extractor runs Stockfish from `STOCKFISH_PATH`, or from `PATH`, for the positions before and after each move. Without Stockfish it logs a warning and sends zeros, which models trained on engine features will misread. The Docker image installs Debian's `stockfish` package. That package can be an older Stockfish version than the one used for training, which shifts the features slightly; for an exact match, use the same Stockfish version in both places.

These features lift both models from about 88% to about 97% accuracy. Cheating games average 14.6 centipawns lost per move, against 40.2 for clean games.

### NNUE engine (switchable alternative to XGBoost)

`ml-model/nnue_model.py` is an NNUE-style classifier. It borrows the structure of a chess-engine NNUE (sparse inputs, a linear accumulator, a small clipped-ReLU head) and applies it to the move stream instead of board positions:

- **Sparse per-move features.** Each move switches on a few one-hot features: a quantile bucket for each move statistic, a *game phase x bucket* cross (the analogue of HalfKP's king x piece cross), and a *previous bucket x current bucket* transition with the same player's last move (lag 2).
- **Efficiently updatable accumulator.** The first layer is linear, so a game's accumulator is the running sum of its moves' active weight columns. `NNUEAccumulator.push_move` adds one move at a time, and the live worker scores games this way. Training uses the equivalent per-game feature histogram because it batches faster.
- **Head.** accumulator + dense game features -> ClippedReLU -> 32 -> 32 -> 1, averaged over a 5-seed ensemble.

Unlike XGBoost, which only sees each game's mean/std/min/max/last summaries, the NNUE reads every move. The collector therefore adds an ordered `moves` matrix (plus `move_columns`) to each `chess-game-features` message. The other workers ignore that field.

`train_nnue.py` uses the same grouped train/test split as `train_xgboost.py`. Inside the training games, 5-fold cross-fitting picks the epoch count and logs the out-of-fold accuracy/FPR curve. The model is then refit on every training game. The 0.55 cutoff is folded into the output logit, so the shared `score >= 0.5` rule applies it.

Measured against XGBoost (same features, same data, with Stockfish engine features), using the NNUE's fixed 0.55 cutoff:

| Evaluation | XGBoost accuracy / FPR | NNUE accuracy / FPR |
| --- | --- | --- |
| Shared held-out test split (8,090 games) | 97.17% / 2.48% | 97.58% / 1.70% |

Ten fresh random 70/30 grouped splits (seeds 201-210, never used for any modelling or cutoff decision; both engines retrained on each) give paired NNUE-minus-XGBoost differences:

| Metric | Mean difference (standard error) | NNUE better on |
| --- | --- | --- |
| Accuracy | 0.47 points higher (0.06) | 10 of 10 splits |
| False positive rate | 0.61 points lower (0.12) | 9 of 10 splits |

Both gaps are many standard errors wide, so the NNUE is reliably better on both metrics. It reads every move's engine evaluation, so it sees patterns such as a run of near-perfect moves from one player, which XGBoost's per-game mean/std/min/max/last summaries blur together.

Before the engine features existed, the same comparison on fresh splits (seeds 101-110) showed the NNUE with a lower FPR (0.80 points, SE 0.19) but statistically tied accuracy (0.13 points lower, SE 0.10). The engine features are what opened the accuracy gap.

The cutoff is `fixed_threshold` in `train_nnue.py`; set it to `None` to pick one from out-of-fold scores, and use the out-of-fold accuracy/FPR curve the trainer prints to choose a different trade-off.

### Consensus engine (XGBoost AND NNUE)

`CHESS_ENGINE=consensus` runs both the XGBoost and NNUE workers and flags a game as AI only when **both** do. Its score is `min(xgboost, nnue)`, which crosses the shared 0.5 cutoff exactly when both scores do. The individual scores are kept in the result's `engine_scores` field. Requiring agreement trades some recall for fewer false accusations:

| Engine (held-out split, 8,090 games) | Accuracy | Human games flagged AI (FPR) | AI games caught (TPR) |
| --- | --- | --- | --- |
| XGBoost | 97.17% | 127 / 5,120 (2.48%) | 2,868 / 2,970 (96.57%) |
| NNUE | 97.58% | 87 / 5,120 (1.70%) | 2,861 / 2,970 (96.33%) |
| Consensus | 97.31% | 70 / 5,120 (1.37%) | 2,822 / 2,970 (95.02%) |

Authenticated web submissions persist an engine choice of `xgboost`, `nnue`, or `consensus` on each analysis job. The supervisor runs both optional engine workers; each worker scores only jobs that require it, and the reporter joins the required outputs per game. The joined result keeps the existing `xgboost_*` compatibility fields while its `engine` metadata records which engine produced them. `CHESS_ENGINE` remains available for focused standalone/debug reporter runs.

## Core architecture changes

The topic topology and process isolation remain intact. Changes to the existing core are limited and additive:

1. Go's `ChessMoveEvent` accepts an optional `game_complete` flag. The extractor emits its completion feature when the board is terminal **or** this flag is set, covering resignations, agreed draws, and time forfeits.
2. Go and Python core processes read `KAFKA_BOOTSTRAP_SERVERS`, with `localhost:19092` retained as the development default.
3. XGBoost training now fits min/max metadata on training rows and writes a validated preprocessing sidecar. Its live worker loads that sidecar before scoring.
4. PyTorch live inference now repeats training-time L2 normalization, validates checkpoint metadata, and converts the logit with sigmoid.
5. The new Flask process and its two web workers sit outside the core. They use existing `chess-moves` and `chess-predictions` topics; no core topic was renamed and the two model libraries remain in separate processes.
6. `live_pipeline.py` is an additive supervisor for the four existing Python core workers. It imports no model or Flask code, and it stops the remaining workers if one exits so a partially running pipeline is visible immediately.

The web layer also adds PostgreSQL, Alembic migrations, private PGN spooling, durable job events, and idempotent result storage. These additions adapt the pipeline for browser workloads without moving feature extraction or inference into Flask. The frontend presentation work changes neither feature extraction nor model inference.

The job-status response now carries top-level `event_cursor` resume metadata alongside `job`. This is an additive web-adapter/API contract: it reads the existing durable event IDs and changes no database schema, Kafka contract, feature pipeline, or inference process.

## Quick start (Docker)

With Docker running and trained artifacts in `ml-model/models/` (see the XGBoost retraining command below), start the whole stack from the repository root:

```bash
docker compose up --build
```

This launches PostgreSQL, Redpanda, the Redpanda console, a one-shot Alembic migration, the Go feature extractor, the core Python pipeline, Flask, and both web workers. Open `http://localhost:5000` (set `WEB_PORT` to change the port and `SECRET_KEY` for anything beyond local use). Stop it with `docker compose down`; add `-v` to also delete the database, Kafka data, and spooled PGNs.

The model artifacts are copied into the image at build time, so rebuild after retraining. The root `compose.yaml` includes `infra/docker-compose.yaml` and shares its project name, so it reuses the same PostgreSQL and Redpanda data as the manual setup below.

## Production deployment (single VM)

The whole stack fits on one Linux VM with Docker (4 vCPU / 8 GB minimum: Stockfish is CPU-bound and the three model workers each load torch). `compose.prod.yaml` layers production settings on top of `compose.yaml`:

- **Caddy** (`infra/Caddyfile`) is the only service with public ports (80/443). It obtains a Let's Encrypt certificate for `DOMAIN`, redirects HTTP to HTTPS, and proxies to Flask without buffering so SSE updates arrive immediately.
- **gunicorn** (`gthread`, 2 workers x 16 threads) replaces the Flask dev server. Each open live-results stream holds one thread, so threads bound concurrent viewers.
- PostgreSQL, Redpanda, and Flask publish **no host ports**. The Redpanda console starts only with `--profile debug`, bound to `127.0.0.1:8080`; reach it with `ssh -L 8080:localhost:8080 <server>`.
- PGN file uploads are capped at **20 MiB** (`MAX_PGN_UPLOAD_BYTES`), so one visitor can't fill the disk or monopolise Stockfish. Caddy rejects request bodies over 21 MiB as a backstop; anything between is answered by Flask with a JSON `413 source_too_large` error that the upload form shows, and the form checks the file size before sending.
- `SECRET_KEY` and `POSTGRES_PASSWORD` have **no fallbacks**: compose refuses to start without them. Session cookies are marked `Secure`.
- **Rate limits** protect the expensive paths: sign-in and account creation per client IP, analysis creation per account (per hour and concurrently running). Counters live in PostgreSQL so both gunicorn workers share them; `TRUST_PROXY_HEADERS=true` lets Flask read the client IP from the `X-Forwarded-For` hop Caddy appends. Over the limit, the API answers `429` with `{"error": {"code": "rate_limited", "message": ..., "retry_after": N}}` and a `Retry-After` header; the sign-in and create-account pages show the message inline.
- **Nightly backups**: the `postgres-backup` service runs `pg_dump` at `BACKUP_HOUR_UTC` (default 03:00 UTC) into `./backups/chess_analytics-<timestamp>.sql.gz` on the host and keeps `BACKUP_RETENTION_DAYS` (default 7) days.

On the server, point the domain's DNS A record at it, open ports 80 and 443, copy trained artifacts into `ml-model/models/`, then create `.env` (git-ignored) next to `compose.yaml`:

```bash
DOMAIN=chess.example.com
SECRET_KEY=<output of: python3 -c "import secrets; print(secrets.token_urlsafe(64))">
POSTGRES_PASSWORD=<a second, different random value>
```

```bash
docker compose -f compose.yaml -f compose.prod.yaml up -d --build
```

`POSTGRES_PASSWORD` is applied only when Postgres initialises an empty volume, so set it before the first start (or change it later with `ALTER USER chess PASSWORD ...`). Inspect the database with `docker compose exec postgres psql -U chess chess_analytics`.

### Backups and restore

Backups land in `./backups` (git-ignored, files readable only by root). They sit on the same disk as the database, so copy them off the VM too (for example a nightly `rsync` or `rclone` to object storage); a single VM whose only backups are local is still this setup's main risk. Take one on demand with:

```bash
docker compose -f compose.yaml -f compose.prod.yaml run --rm postgres-backup --once
```

To restore a dump (this replaces the current database):

```bash
# 1. Stop everything that writes to the database.
docker compose -f compose.yaml -f compose.prod.yaml stop caddy web ingestion-worker result-worker pipeline postgres-backup
# 2. Recreate an empty database.
docker compose -f compose.yaml -f compose.prod.yaml exec postgres psql -U chess -d postgres -c "DROP DATABASE chess_analytics WITH (FORCE)" -c "CREATE DATABASE chess_analytics OWNER chess"
# 3. Load the dump (pick the file you want from ./backups).
gunzip -c backups/chess_analytics-YYYYMMDDTHHMMSSZ.sql.gz | docker compose -f compose.yaml -f compose.prod.yaml exec -T postgres psql -v ON_ERROR_STOP=1 -U chess -d chess_analytics
# 4. Start the stack again (migrate runs first and is a no-op if the dump is current).
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

The dump includes `alembic_version`, so migrations resume from the backed-up revision. Spooled PGN files are not in the dump; jobs that were still queued when the backup was taken can fail with a missing source and need resubmitting.

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
.venv/bin/python python-pipeline/xgboost_inference.py   # or nnue_inference.py with CHESS_ENGINE=nnue
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

To fill the dataset's Stockfish columns (requires `brew install stockfish`; resumable):

```bash
cd ml-model
../.venv/bin/python add_engine_features.py
```

To retrain XGBoost and create its required preprocessing sidecar:

```bash
cd ml-model
../.venv/bin/python train_xgboost.py
```

To train the NNUE engine (writes `ml-model/models/nnue_model.pt`), compare both engines on the shared test split, or compare them with 5-fold grouped cross-validation (slower, retrains both per fold):

```bash
cd ml-model
../.venv/bin/python train_nnue.py
../.venv/bin/python compare_engines.py
../.venv/bin/python compare_engines.py --cv 5
```

Then run the pipeline with the NNUE engine in place of XGBoost:

```bash
.venv/bin/python python-pipeline/live_pipeline.py
```

## Clear unfinished analysis state

Use this local recovery command only when no application process is running. Stop Flask, the web ingestion and result workers, the Go feature extractor, and the core Python workers first; leave PostgreSQL and Redpanda running. The command also refuses to continue while the `chess-state-managers` Kafka group has active members.

> **Warning:** This permanently discards Kafka move lag and every selected unfinished analysis, including its games, results, events, unmatched pipeline results, and matching private PGN spool file. It preserves terminal jobs (`completed`, `partial`, and `failed`), but the deleted analyses cannot be recovered.

From the repository root, preview and clear all unfinished jobs (`queued`, `parsing`, and `live`):

```bash
docker_help/clear_analysis_state.sh
```

The command prints each target, the current Kafka group and lag, and the number of exact spool-file identities it froze. The `CLEAR` prompt follows this destructive preview. Use `--yes` after you have reviewed the impact and need non-interactive execution:

```bash
docker_help/clear_analysis_state.sh --yes
```

To target only unfinished jobs with an expired lease that have been unchanged for at least 30 minutes:

```bash
docker_help/clear_analysis_state.sh --stale-minutes 30
```

After cleanup, restart the stopped services and upload the PGN again to begin a new analysis. If a previewed job becomes `completed`, `partial`, or `failed` before the maintenance transaction locks it, the command reports the race and preserves both that job and its spool file. A successful run also prints the remaining terminal-job count for each status and in total.

## Configuration

| Variable | Purpose | Development default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL connection string | required |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka/Redpanda brokers | `localhost:19092` |
| `KAFKA_WEB_RESULT_GROUP` | Web result consumer group | `chess-web-results-v1` |
| `PGN_SPOOL_DIRECTORY` | Private temporary PGN storage | `.runtime/pgn-spool` |
| `MAX_PGN_UPLOAD_BYTES` | File upload limit (also used for Lichess Study downloads) | 20 MiB |
| `MAX_PASTED_PGN_BYTES` | Pasted-text limit | 10 MiB |
| `SECRET_KEY` | Flask secret | development value only |
| `SESSION_COOKIE_SECURE` | Send the session cookie over HTTPS only (`true`/`false`); `compose.prod.yaml` sets `true` | `false` |
| `TRUST_PROXY_HEADERS` | Take the client IP from the last `X-Forwarded-For` hop (Werkzeug `ProxyFix`). Only safe behind a proxy that sets that header; `compose.prod.yaml` sets `true` | `false` |
| `RATE_LIMITS_ENABLED` | Master switch for all rate limits | `true` |
| `SIGN_IN_ATTEMPTS_PER_15_MIN` | Sign-in form submissions per client IP per 15-minute window | `20` |
| `ACCOUNT_CREATIONS_PER_HOUR` | Create-account form submissions per client IP per hour | `10` |
| `ANALYSES_PER_HOUR` | Analyses one account may start in any rolling hour | `20` |
| `MAX_ACTIVE_ANALYSES` | Unfinished (queued/parsing/live) analyses one account may have at once | `3` |
| `BACKUP_HOUR_UTC` / `BACKUP_RETENTION_DAYS` | `postgres-backup` schedule and retention (`compose.prod.yaml` only) | `3` / `7` |
| `CHESS_ENGINE` | Fallback engine for focused standalone/debug reporter runs; web jobs carry their own persisted choice | `xgboost` |
| `STOCKFISH_PATH` | Stockfish binary for the Go feature extractor | `stockfish` on `PATH` |

## HTTP surface

| Method and path | Purpose |
| --- | --- |
| `GET /` | Public product overview and repository benchmark highlights |
| `GET/POST /create-account` | Create a persisted local account and authenticated session (POST rate-limited per IP) |
| `GET/POST /sign-in` | Sign in to an existing account (POST rate-limited per IP) |
| `GET /evaluation` | Repository-backed model benchmark and interpretation guidance |
| `GET /analyze` | Authenticated engine selection and PGN upload/paste workspace |
| `GET /analyses/<job_id>` | Authenticated, owner-only restorable live job |
| `GET/POST /profile` | Persisted preferences, usage totals, privacy information, and owned history |
| `GET /system` | Information page (legacy route) |
| `POST /api/analyses` | Authenticated creation of an owned file or paste analysis job (rate-limited per account; `429` + `Retry-After`) |
| `GET /api/analyses/<job_id>` | Owner-only job snapshot, counters, engine, and SSE resume cursor |
| `GET /api/analyses/<job_id>/games` | Owner-only filtered, paginated game results |
| `GET /api/analyses/<job_id>/events` | Owner-only durable SSE updates |
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
.venv/bin/python -m unittest tests.web_app.test_migrations_postgres tests.web_app.test_repository_postgres tests.web_app.test_adapter_integration_postgres tests.web_app.test_rate_limits -v
```

The integration test exercises submission, PGN streaming, an immediate per-game result race, idempotent replay, source-ordered API results, and resumable SSE against real PostgreSQL transactions.
