import pandas as pd
from sklearn.model_selection import GroupShuffleSplit


LABEL_COLUMN = "Cheating"
GAME_AGGREGATIONS = ("mean", "std", "min", "max", "last")


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


def aggregate_game_features(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Convert each game's numeric move records into one model feature row."""
    game_ids = get_game_ids(dataframe)
    # The model does not see individual moves: it receives one summary row per game.
    numeric_features = dataframe.select_dtypes(include="number").drop(
        columns=[LABEL_COLUMN], errors="ignore"
    )
    numeric_features.index = game_ids
    grouped_features = numeric_features.groupby(level=0, sort=False)
    aggregated_features = grouped_features.agg(GAME_AGGREGATIONS)
    aggregated_features.columns = [
        f"{feature}_{statistic}"
        for feature, statistic in aggregated_features.columns
    ]
    aggregated_features["move_count"] = grouped_features.size().astype("float32")
    return aggregated_features.fillna(0.0).astype("float32")


def prepare_game_training_data(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Prepare one labelled numeric feature vector per complete chess game."""
    if LABEL_COLUMN not in dataframe.columns:
        raise ValueError(f"Dataset must contain a '{LABEL_COLUMN}' column.")

    game_ids = get_game_ids(dataframe)
    labels = pd.to_numeric(dataframe[LABEL_COLUMN], errors="raise")
    grouped_labels = labels.groupby(game_ids, sort=False)
    if (grouped_labels.nunique() != 1).any():
        raise ValueError("Each game must have one Cheating label.")

    # Every move from the same GameID shares one game-level training label.
    return aggregate_game_features(dataframe), grouped_labels.first().astype("float32")
