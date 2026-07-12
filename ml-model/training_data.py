import pandas as pd


LABEL_COLUMN = "Cheating"


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
