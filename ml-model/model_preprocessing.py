"""Preprocessing contracts shared by model training and live inference."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np


MODELS_DIRECTORY = Path(__file__).with_name("models")
XGBOOST_PREPROCESSING_FILENAME = "xgboost_preprocessing.json"
XGBOOST_PREPROCESSING_VERSION = 1


class ModelArtifactError(ValueError):
    """Raised when a persisted model artifact violates its contract."""


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


def feature_schema_fingerprint(feature_names: Sequence[str]) -> str:
    encoded = json.dumps(list(feature_names), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_xgboost_preprocessing(
    feature_names,
    minimum,
    safe_range,
    model_directory=MODELS_DIRECTORY,
):
    if isinstance(feature_names, (str, bytes)):
        raise ModelArtifactError(
            "feature_names: expected a sequence of non-empty strings"
        )
    try:
        names = list(feature_names)
    except TypeError as error:
        raise ModelArtifactError(
            "feature_names: expected a sequence of non-empty strings"
        ) from error
    if not all(isinstance(name, str) and name for name in names):
        raise ModelArtifactError(
            "feature_names: expected a sequence of non-empty strings"
        )

    try:
        minimum_values = np.asarray(minimum, dtype="float32")
    except (OverflowError, TypeError, ValueError) as error:
        raise ModelArtifactError("minimum: every value must be numeric") from error
    try:
        range_values = np.asarray(safe_range, dtype="float32")
    except (OverflowError, TypeError, ValueError) as error:
        raise ModelArtifactError("range: every value must be numeric") from error

    payload = {
        "version": XGBOOST_PREPROCESSING_VERSION,
        "transform": "min_max",
        "feature_names": names,
        "feature_schema_sha256": feature_schema_fingerprint(names),
        "minimum": minimum_values.tolist(),
        "range": range_values.tolist(),
    }
    _validate_xgboost_preprocessing(payload, names)

    model_directory.mkdir(parents=True, exist_ok=True)
    path = model_directory / XGBOOST_PREPROCESSING_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _validate_numeric_vector(metadata, field, expected_length):
    values = metadata.get(field)
    if not isinstance(values, list):
        raise ModelArtifactError(f"{field}: expected a JSON array")
    if len(values) != expected_length:
        raise ModelArtifactError(
            f"{field}: expected {expected_length} values, found {len(values)}"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise ModelArtifactError(f"{field}: every value must be numeric")

    try:
        array = np.asarray(values, dtype="float32")
    except (OverflowError, TypeError, ValueError) as error:
        raise ModelArtifactError(f"{field}: every value must be numeric") from error
    if array.ndim != 1 or not np.isfinite(array).all():
        raise ModelArtifactError(f"{field}: every value must be finite")
    return array


def _validate_xgboost_preprocessing(metadata, expected_feature_names):
    if not isinstance(metadata, dict):
        raise ModelArtifactError("metadata: expected a JSON object")

    version = metadata.get("version")
    if type(version) is not int or version != XGBOOST_PREPROCESSING_VERSION:
        raise ModelArtifactError(
            f"version: expected {XGBOOST_PREPROCESSING_VERSION}, found {version!r}"
        )

    transform = metadata.get("transform")
    if transform != "min_max":
        raise ModelArtifactError(
            f"transform: expected 'min_max', found {transform!r}"
        )

    feature_names = metadata.get("feature_names")
    if not isinstance(feature_names, list) or not all(
        isinstance(name, str) and name for name in feature_names
    ):
        raise ModelArtifactError(
            "feature_names: expected a JSON array of non-empty strings"
        )

    fingerprint = metadata.get("feature_schema_sha256")
    expected_fingerprint = feature_schema_fingerprint(feature_names)
    if not isinstance(fingerprint, str) or fingerprint != expected_fingerprint:
        raise ModelArtifactError(
            "feature_schema_sha256: does not match feature_names"
        )

    if expected_feature_names is not None:
        if isinstance(expected_feature_names, (str, bytes)):
            raise ModelArtifactError(
                "feature_names: expected schema must be a sequence of names"
            )
        try:
            expected_names = list(expected_feature_names)
        except TypeError as error:
            raise ModelArtifactError(
                "feature_names: expected schema must be a sequence of names"
            ) from error
        if not all(isinstance(name, str) and name for name in expected_names):
            raise ModelArtifactError(
                "feature_names: expected schema contains an invalid name"
            )
        if feature_names != expected_names:
            raise ModelArtifactError(
                "feature_names: artifact order does not match expected schema"
            )

    minimum = _validate_numeric_vector(metadata, "minimum", len(feature_names))
    safe_range = _validate_numeric_vector(metadata, "range", len(feature_names))
    if np.any(safe_range <= 0):
        raise ModelArtifactError("range: every value must be greater than zero")

    return minimum, safe_range


def load_xgboost_preprocessing(
    model_directory=MODELS_DIRECTORY,
    expected_feature_names=None,
) -> dict:
    path = model_directory / XGBOOST_PREPROCESSING_FILENAME
    try:
        serialized = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ModelArtifactError(
            f"{XGBOOST_PREPROCESSING_FILENAME}: file is missing"
        ) from error
    except UnicodeError as error:
        raise ModelArtifactError(
            f"{XGBOOST_PREPROCESSING_FILENAME} JSON: invalid document"
        ) from error
    except OSError as error:
        raise ModelArtifactError(
            f"{XGBOOST_PREPROCESSING_FILENAME}: could not be read"
        ) from error

    try:
        metadata = json.loads(serialized)
    except json.JSONDecodeError as error:
        raise ModelArtifactError(
            f"{XGBOOST_PREPROCESSING_FILENAME} JSON: invalid document"
        ) from error

    _validate_xgboost_preprocessing(metadata, expected_feature_names)
    return metadata


def transform_xgboost_features(
    feature_values: Mapping[str, object],
    feature_names: Sequence[str],
    preprocessing: Mapping[str, object],
) -> np.ndarray:
    minimum, safe_range = _validate_xgboost_preprocessing(
        preprocessing, feature_names
    )
    raw = np.asarray(
        [[float(feature_values.get(name, 0.0)) for name in feature_names]],
        dtype="float32",
    )
    return (raw - minimum) / safe_range
