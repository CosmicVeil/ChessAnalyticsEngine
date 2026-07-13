# Game-Level Live Inference Design

## Goal

Train PyTorch and XGBoost classifiers on one row per chess game, save both trained models with their input schema, and score each simulated game only after its final move.

## Existing System

`simulator.py` publishes moves to `chess-moves`. The Go backend turns each move into a `chess-features` record. `data_collector.py` stores those feature records in `ml-model/chess_dataset.csv` for training. Game IDs contain `Clean` or `Cheating` only in the local simulator, so they may create labels for evaluation but must never become model inputs.

## Training Data

The training preparation will group raw feature records by `GameID`. It will retain numeric feature columns only and produce aggregate statistics for each feature: mean, standard deviation, minimum, maximum, and final value. It will also add `move_count`. The result is one feature vector and one `Cheating` label per game.

The grouped train/test split will continue to use `GameID`; after aggregation there is one row per group, but the explicit split still documents and enforces that game identity never becomes a feature.

## Model Artifacts

Each training script writes an artifact below `ml-model/models/`:

- `pytorch_model.pt`: PyTorch state dictionary, input dimension, and architecture metadata.
- `xgboost_model.json`: native XGBoost model file.
- `feature_schema.json`: ordered game-level feature column names shared by both models.

The model artifacts are local outputs and are ignored by Git. Training code comments the save and export lines so their purpose is clear.

## Live Data Flow

The Go backend continues publishing normal move-level feature messages to `chess-features`. When the chess library reports a completed game, it publishes a final feature message with `game_complete: true` after the final move features.

`python-pipeline/live_game_collector.py` runs as an independent Kafka consumer group. It collects each game's numeric move features in memory. When it receives `game_complete: true`, it builds the same aggregate feature row used in training and publishes it to `chess-game-features`.

Two isolated inference workers consume every completed-game feature record: `torch_inference.py` imports PyTorch only and `xgboost_inference.py` imports XGBoost only. Each worker publishes its probability to `chess-model-predictions`. `prediction_reporter.py` joins the two scores by GameID and prints:

```
Game: <game ID> | PyTorch: <percentage> | XGBoost: <percentage>
Running accuracy - PyTorch: <percentage> | XGBoost: <percentage>
```

The consumer identifies the simulated ground-truth label from the GameID only to update local evaluation metrics. It removes GameID before model input. For real games without a verified label, it prints the scores and marks accuracy unavailable.

The reporter publishes a JSON result containing game ID, each score, and the final prediction for each model to a `chess-predictions` Kafka topic. This process isolation prevents the known macOS OpenMP crash caused by importing both PyTorch and XGBoost in one interpreter.

## Reliability and Boundaries

The live consumer validates incoming numeric fields and skips malformed values. It removes a game’s in-memory state immediately after publishing its final result. It ignores orphan completion messages without prior feature records. If model artifacts or their schema are missing, it exits with an actionable error instead of scoring with an incompatible feature set.

## Testing

Unit tests will verify game aggregation, absence of GameID from model features, stable schema alignment when an incoming game lacks a feature, simulated accuracy accounting, artifact-schema creation, and final-game-only inference behavior. Existing stream services are not required for these unit tests.
