# Game-Level Live Inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Goal: train and export both classifiers from one row per chess game, then score each completed streamed game with both models.

Architecture: aggregate move-level records by GameID into one numeric row. Save each trained model and an ordered feature schema. A second consumer publishes completed-game features, isolated PyTorch and XGBoost workers score them separately, and a reporter joins results, prints simulated accuracy, and publishes JSON.

Tech stack: Python, pandas, PyTorch, XGBoost, confluent-kafka, Go, kafka-go, unittest.

## Global constraints

- GameID defines grouping and simulator labels only. It is never a model feature.
- Numeric feature columns use mean, standard deviation, minimum, maximum, final value, and move count.
- Both models consume the same saved schema.
- Accuracy appears only for simulator labels Clean and Cheating.
- Local artifacts live in ml-model/models and remain ignored by Git.

### Task 1: Game-level training features

Files:
- Modify ml-model/training_data.py
- Modify tests/test_ml_data.py

Interfaces:
- aggregate_game_features(dataframe) returns one numeric feature row per GameID.
- prepare_game_training_data(dataframe) returns aggregate features and one label per GameID.

- [ ] Add a failing test using two move rows for one Clean game and two for one Cheating game. Assert two output rows, move_number_mean equals 1.5, move_count equals 2, GameID is absent from columns, and the labels are 0 and 1.
- [ ] Add a failing test with two rows for one GameID with labels 0 and 1. Assert ValueError contains one Cheating label.
- [ ] Run the focused test. Expected result: failure because prepare_game_training_data does not exist.
- [ ] Implement aggregation by selecting numeric fields, excluding Cheating, grouping by GameID, applying mean/std/min/max/last, flattening columns as name_statistic, filling the one-move standard deviation with 0, and adding move_count.
- [ ] Validate every group has exactly one label and return the first group label as float32.
- [ ] Run all data tests. Expected result: pass.
- [ ] Commit only training_data.py and test_ml_data.py with message feat: aggregate chess features by game.

### Task 2: Save the schema and both model formats

Files:
- Create ml-model/model_artifacts.py
- Modify ml-model/train_torch.py
- Modify ml-model/train_xgboost.py
- Modify tests/test_ml_data.py
- Modify .gitignore

Interfaces:
- save_feature_schema(columns, model_directory) writes feature_schema.json.
- load_feature_schema(model_directory) reads the ordered column list.

- [ ] Add a failing schema round-trip test. Save [move_count, time_delta_mean] in a temporary directory and assert the same order loads.
- [ ] Run the focused test. Expected result: failure because model_artifacts does not exist.
- [ ] Implement schema helpers using JSON and create the model directory when saving.
- [ ] Update both trainers to call prepare_game_training_data and persist features.columns in feature_schema.json.
- [ ] Save XGBoost through model.save_model to xgboost_model.json. Place a comment directly above the line explaining that it creates the reusable XGBoost inference artifact.
- [ ] Save PyTorch state_dict and input_size to pytorch_model.pt. Place a comment directly above the line explaining that it creates the reusable PyTorch inference artifact.
- [ ] Correct ONNX export to use torch.zeros(1, input_size) as its example tensor. Place a comment directly above the line explaining the ONNX export.
- [ ] Add ml-model/models to .gitignore.
- [ ] Run all Python unit tests and py_compile for all four model files. Expected result: pass with no syntax errors.
- [ ] Commit only artifact helpers, model trainers, tests, and .gitignore with message feat: export trained chess models.

### Task 3: Complete game events and unique simulation IDs

Files:
- Modify go-backend/main.go
- Modify python-pipeline/simulator.py

Interfaces:
- The Go backend emits a chess-features record with game_complete true after the final feature event.
- The simulator gives each game a unique ID ending in Clean or Cheating.

- [ ] Add GameComplete bool with JSON name game_complete to FeatureVector.
- [ ] After the normal final feature write, when game.Outcome is final, publish a FeatureVector containing GameID and GameComplete true. Delete the game state after this message.
- [ ] Build simulator IDs with the game-loop counter, for example White vs Black #17 Clean. Use Cheating for AI data. This prevents same-player games from merging.
- [ ] Run gofmt and go test from go-backend. Expected result: formatted source and passing tests.
- [ ] Commit only the Go backend and simulator with message feat: signal completed streamed chess games.

### Task 4: Whole-game live scoring

Files:
- Create python-pipeline/live_scoring.py
- Create python-pipeline/live_game_collector.py
- Create python-pipeline/torch_inference.py
- Create python-pipeline/xgboost_inference.py
- Create python-pipeline/prediction_reporter.py
- Modify tests/test_ml_data.py

Interfaces:
- GameScoreTracker.add_feature(event) stores one normal event.
- GameScoreTracker.complete_game(game_id) returns result JSON or None for an unknown game.
- The collector publishes completed feature rows to chess-game-features.
- The isolated model workers publish one score each to chess-model-predictions.
- The reporter publishes joined result JSON to chess-predictions.

- [ ] Add a failing test that sends two move records to a GameScoreTracker configured with fake PyTorch score 0.8 and fake XGBoost score 0.2. Complete a Cheating game and assert the scores, PyTorch accuracy 1.0, and XGBoost accuracy 0.0.
- [ ] Add a failing test where an expected numeric feature is missing in the live input and assert the aligned model input contains 0.
- [ ] Run the focused test. Expected result: failure because live_scoring does not exist.
- [ ] Implement the tracker using aggregate_game_features over buffered event records. Reindex the final row to the saved schema with fill value 0.
- [ ] Determine a simulator label after prediction from clean or cheating in GameID. Keep correct and total counters independently for each model. Drop buffered records immediately after completion.
- [ ] Implement the collector with its own consumer group. Add normal messages to the tracker. On game_complete, publish the game-level feature row to chess-game-features.
- [ ] Implement a PyTorch-only worker and an XGBoost-only worker. Each loads its own artifact and the shared schema, then publishes its score to chess-model-predictions.
- [ ] Implement a reporter that joins scores by GameID, prints Game ID, both score percentages, and both running accuracies, then publishes game_id, scores, binary predictions, and available accuracies to chess-predictions.
- [ ] Run all Python tests and py_compile for live_scoring.py and live_inference.py. Expected result: pass with no syntax errors.
- [ ] Commit only the two new inference modules and tests with message feat: score completed chess games live.

### Task 5: End-to-end validation

Files: no source changes.

- [ ] Train both models using the launcher. Expected result: model files and feature_schema.json appear in ml-model/models.
- [ ] Start the Go backend, the inference consumer, then the simulator using the existing pipeline environment.
- [ ] Confirm one printed result per completed game includes GameID, PyTorch score, XGBoost score, and both cumulative accuracy percentages.
- [ ] Consume one chess-predictions message. Expected result: JSON with game_id, pytorch_score, xgboost_score, pytorch_prediction, and xgboost_prediction.
