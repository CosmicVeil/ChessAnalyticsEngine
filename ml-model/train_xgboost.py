from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from training_data import prepare_training_data


def main() -> None:
    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    features, labels = prepare_training_data(dataframe)
    X_train, X_test, y_train, y_test = train_test_split(
        features.to_numpy(dtype="float32", copy=True),
        labels.to_numpy(dtype="float32", copy=True),
        train_size=0.7,
        shuffle=True,
    )

    model = XGBClassifier(
        n_estimators=2,
        max_depth=2,
        learning_rate=0.01,
        objective="binary:logistic",
    )
    print("Starting XGBoost model training...")
    model.fit(X_train, y_train)
    accuracy = (model.predict(X_test) == y_test).mean()
    print("XGBoost model accuracy: %.2f%%" % (accuracy * 100))


if __name__ == "__main__":
    main()
