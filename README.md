An analytical chess engine was used to predict whether a game was clean, or a chess model was used.

This architecture uses a Kafka-based streaming pipeline to process 1500 games per second into a PyTorch ReLu and an XGBoost model.

Predictions are ~ 78% accurate for PyTorch, and ~ 82% accuracy for XGBoost.

The rate of false positives is <5%, a key analytic to prevent real games from being falsely reported as AI.
