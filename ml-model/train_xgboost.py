from pathlib import Path

import pandas as pd
from xgboost import XGBClassifier


from training_data import get_game_ids, group_train_test_indices, prepare_training_data


def main() -> None:

    #device = device("cuda" if torch.cuda.is_available() else "cpu")
    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    features, labels = prepare_training_data(dataframe)
    game_ids = get_game_ids(dataframe)
    train_index, test_index = group_train_test_indices(features, labels, game_ids)
    X = features.to_numpy(dtype="float32", copy=True)
    y = labels.to_numpy(dtype="float32", copy=True)
    X_train, X_test = X[train_index], X[test_index]
    y_train, y_test = y[train_index], y[test_index]

    model = XGBClassifier(
        n_estimators=5000,
        max_depth=10,
        learning_rate=0.01,
        objective="binary:logistic",
    ).to(device)
    print("Starting XGBoost model training...")
    model.fit(X_train, y_train)
    accuracy = (model.predict(X_test) == y_test).mean()
    print("XGBoost model accuracy: %.2f%%" % (accuracy * 100))


if __name__ == "__main__":
    main()
