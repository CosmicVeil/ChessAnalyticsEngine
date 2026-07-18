# Chess Analytics Engine

Chess Analytics Engine is a containerized streaming system for identifying anomalous, engine-like chess play from live game events. It turns individual moves into game-level feature vectors, then scores each completed game with independent PyTorch and XGBoost classifiers.

## Results

- Processed approximately 1,500 live games per minute through the streaming pipeline.
- Reached 87% classification accuracy when flagging anomalous, engine-like play.
- Produced live model results in under 550 ms while holding the false-positive rate below 5%.

## Architecture

```text
Chess event producer
        |
        v
  chess-moves (Redpanda/Kafka)
        |
        v
Go state manager
  - validates and applies moves
  - computes move-level game-state features
        |
        v
 chess-features
        |
        v
Python game feature collector
  - aggregates a complete game's move features
        |
        v
 chess-game-features
    |                |
    v                v
PyTorch worker   XGBoost worker
    |                |
    +-------> chess-model-predictions
                         |
                         v
              Prediction reporter and chess-predictions
```

The producer keys messages by game ID, keeping a game's moves together in Kafka. The Go service maintains the legal board state for each active game and emits metrics such as material balance, move complexity, elapsed time, rating difference, and piece counts. The Python collector combines those move-level records into the same game-level schema used during training.

## Repository layout

| Path | Purpose |
| --- | --- |
| `infra/` | Docker Compose configuration for Redpanda and Redpanda Console. |
| `go-backend/` | Kafka consumer that reconstructs game state and publishes move features. |
| `python-pipeline/` | Game aggregation, live inference, prediction reporting, and simulation tools. |
| `ml-model/` | Feature preparation, training scripts, and saved-model helpers. |
| `data/` | PGN-based data generation inputs. |

## Run locally

Prerequisites: Docker, Go, and Python with the dependencies in `ml-model/requirements.txt` installed. Train or place the PyTorch and XGBoost model artifacts before starting the inference workers.

Start Redpanda and its console:

```bash
docker compose -f infra/docker-compose.yaml up -d
```

In separate terminals, start the feature processor and four Python consumers before sending events:

```bash
cd go-backend && go run .
```

```bash
# Terminal 2
python python-pipeline/live_game_collector.py

# Terminal 3
python python-pipeline/torch_inference.py

# Terminal 4
python python-pipeline/xgboost_inference.py

# Terminal 5
python python-pipeline/prediction_reporter.py
```

Run the simulator after the consumers are ready:

```bash
python python-pipeline/simulator.py
```

The Redpanda Console is available at `http://localhost:8080`.

## Train the models

The training scripts load `ml-model/chess_dataset.csv`, aggregate move records by game ID, keep each game in one train/test split, and write the model artifacts used by live inference.

```bash
python ml-model/train_torch.py
python ml-model/train_xgboost.py
```

## Testing

Run the Python test suite from the repository root:

```bash
python -m unittest discover -s tests
```
