"""Preprocessing contracts shared by model training and live inference."""

import hashlib
import json
from pathlib import Path

import numpy as np


MODELS_DIRECTORY = Path(__file__).with_name("models")
XGBOOST_PREPROCESSING_FILENAME = "xgboost_preprocessing.json"
XGBOOST_PREPROCESSING_VERSION = 1


def prepare_min_max_splits(features, train_index, test_index):
    values = np.asarray(features, dtype="float32")
    X_train_raw = values[train_index]
    X_test_raw = values[test_index]
    minimum = X_train_raw.min(axis=0)
    observed_range = X_train_raw.max(axis=0) - minimum
    safe_range = np.where(observed_range == 0, 1.0, observed_range).astype(
        "float32"
    )
    X_train = (X_train_raw - minimum) / safe_range
    X_test = (X_test_raw - minimum) / safe_range
    return X_train, X_test, minimum.astype("float32"), safe_range


def _schema_fingerprint(feature_names):
    encoded = json.dumps(list(feature_names), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_xgboost_preprocessing(
    feature_names,
    minimum,
    safe_range,
    model_directory=MODELS_DIRECTORY,
):
    names = list(feature_names)
    minimum_values = np.asarray(minimum, dtype="float32")
    range_values = np.asarray(safe_range, dtype="float32")
    if len(names) != len(minimum_values) or len(names) != len(range_values):
        raise ValueError("Feature names, minimums, and ranges must have equal length.")
    model_directory.mkdir(parents=True, exist_ok=True)
    path = model_directory / XGBOOST_PREPROCESSING_FILENAME
    payload = {
        "version": XGBOOST_PREPROCESSING_VERSION,
        "transform": "min_max",
        "feature_names": names,
        "feature_schema_sha256": _schema_fingerprint(names),
        "minimum": minimum_values.tolist(),
        "range": range_values.tolist(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load_xgboost_preprocessing(model_directory=MODELS_DIRECTORY):
    path = model_directory / XGBOOST_PREPROCESSING_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))
