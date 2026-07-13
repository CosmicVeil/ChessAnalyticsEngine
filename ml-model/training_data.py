import pandas as pd
from sklearn.model_selection import GroupShuffleSplit


LABEL_COLUMN = "Cheating"


def get_game_ids(dataframe: pd.DataFrame) -> pd.Series:
    for column in dataframe.columns:
        if column.lower().replace("_", "") == "gameid":
            return dataframe[column].astype("string")

    raise ValueError("Dataset must contain a GameID or game_id column for grouped splitting.")


def group_train_test_indices(
    features: pd.DataFrame,
    labels: pd.Series,
    game_ids: pd.Series,
    train_size: float = 0.7,
    random_state: int = 42,
) -> tuple[object, object]:
    splitter = GroupShuffleSplit(n_splits=1, train_size=train_size, random_state=random_state)
    return next(splitter.split(features, labels, groups=game_ids))


def prepare_training_data(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    if LABEL_COLUMN not in dataframe.columns:
        raise ValueError(f"Dataset must contain a '{LABEL_COLUMN}' column.")

    labels = pd.to_numeric(dataframe[LABEL_COLUMN], errors="raise").astype("float32")
    features = dataframe.drop(columns=[LABEL_COLUMN]).copy()

    game_id_columns = [
        column for column in features.columns if column.lower().replace("_", "") == "gameid"
    ]
    features = features.drop(columns=game_id_columns)

    return pd.get_dummies(features, dtype="float32").astype("float32"), labels
