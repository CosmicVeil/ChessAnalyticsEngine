from pathlib import Path

import pandas as pd
from xgboost import XGBClassifier

from model_artifacts import model_artifact_path, save_feature_schema
from model_preprocessing import (
    prepare_min_max_splits,
    save_xgboost_preprocessing,
)
from training_data import group_train_test_indices, prepare_game_training_data

from sklearn.metrics import confusion_matrix


def main() -> None:

    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    # Combine every game's moves before training so one prediction represents one whole game.
    features, labels = prepare_game_training_data(dataframe)
    game_ids = pd.Series(features.index, index=features.index, dtype="string")
    train_index, test_index = group_train_test_indices(features, labels, game_ids)
    X = features.to_numpy(dtype="float32", copy=True)
    y = labels.to_numpy(dtype="float32", copy=True)
    X_train, X_test, x_min, x_range_safe = prepare_min_max_splits(
        X, train_index, test_index
    )
    y_train, y_test = y[train_index], y[test_index]

    model = XGBClassifier(
        n_estimators=5000,
        max_depth=12,
        subsample=0.8,
        colsample_bytree=0.8,
        learning_rate=0.01,
        objective="binary:logistic",
    )
    print("Starting XGBoost model training...")
    model.fit(X_train, y_train)
    accuracy = (model.predict(X_test) == y_test).mean()
    print("XGBoost model accuracy: %.2f%%" % (accuracy * 100))

    save_feature_schema(features.columns.tolist())
    preprocessing_path = save_xgboost_preprocessing(
        features.columns.tolist(), x_min, x_range_safe
    )
    xgboost_path = model_artifact_path("xgboost_model.json")
    # Save the native XGBoost model for the live inference consumer.
    model.save_model(xgboost_path)

    tn, fp, fn, tp = confusion_matrix(y_test, model.predict(X_test).round()).ravel()

    false_positive_rate = fp / (fp + tn) if fp + tn else 0.0
    print(f"Saved XGBoost model to {xgboost_path}")
    print(f"Saved XGBoost preprocessing to {preprocessing_path}")
    print(f"False Positive rate: {false_positive_rate}")


if __name__ == "__main__":
    main()
