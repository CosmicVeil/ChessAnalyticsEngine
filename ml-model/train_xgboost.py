from pathlib import Path

import pandas as pd
from xgboost import XGBClassifier

from model_artifacts import model_artifact_path, save_feature_schema
from training_data import group_train_test_indices, prepare_game_training_data


def main() -> None:

    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    # Combine every game's moves before training so one prediction represents one whole game.
    features, labels = prepare_game_training_data(dataframe)
    game_ids = pd.Series(features.index, index=features.index, dtype="string")
    train_index, test_index = group_train_test_indices(features, labels, game_ids)
    X = features.to_numpy(dtype="float32", copy=True)
    y = labels.to_numpy(dtype="float32", copy=True)
    X_train, X_test = X[train_index], X[test_index]
    y_train, y_test = y[train_index], y[test_index]

    model = XGBClassifier(
        n_estimators=10000,
        max_depth=20,
        learning_rate=0.01,
        objective="binary:logistic",
    )
    print("Starting XGBoost model training...")
    model.fit(X_train, y_train)
    accuracy = (model.predict(X_test) == y_test).mean()
    print("XGBoost model accuracy: %.2f%%" % (accuracy * 100))

    save_feature_schema(features.columns.tolist())
    xgboost_path = model_artifact_path("xgboost_model.json")
    # Save the native XGBoost model for the live inference consumer.
    model.save_model(xgboost_path)
    print(f"Saved XGBoost model to {xgboost_path}")


if __name__ == "__main__":
    main()
