# XGBoost Retraining Preprocessing Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fit XGBoost min-max parameters from training games only and save those parameters beside the retrained model for live inference.

**Architecture:** A focused `model_preprocessing.py` module owns split transformation and the JSON sidecar contract. The XGBoost trainer selects raw train and test rows, asks that module to fit and transform them, then saves the same parameters used by `model.fit`.

**Tech Stack:** Python 3.14, NumPy, XGBoost, `unittest`, JSON model artifacts.

## Global Constraints

- Preserve the existing grouped game split and XGBoost hyperparameters.
- Derive minimum and range from `X_train_raw` only.
- Use `1.0` as the safe range for a feature that has no variation in training data.
- Save `xgboost_preprocessing.json` in the same ignored model directory as `xgboost_model.json`.
- Preserve the user's uncommitted batch-size and epoch changes in `ml-model/train_torch.py`.
- Do not change live inference in this prerequisite task.

---

### Task 1: Correct and persist XGBoost preprocessing

**Files:**
- Create: `ml-model/model_preprocessing.py`
- Modify: `ml-model/train_xgboost.py:6-32,47-55`
- Modify: `tests/test_ml_data.py`

**Interfaces:**
- Consumes: aggregated numeric features, grouped train/test index arrays, and ordered feature names.
- Produces: `prepare_min_max_splits(features, train_index, test_index) -> tuple[X_train, X_test, minimum, safe_range]`.
- Produces: `save_xgboost_preprocessing(feature_names, minimum, safe_range, model_directory=MODELS_DIRECTORY) -> Path`.
- Produces: `load_xgboost_preprocessing(model_directory=MODELS_DIRECTORY) -> dict` for the later live-inference task.

- [ ] **Step 1: Add a regression test that puts an outlier only in the held-out partition**

Add NumPy to the test imports and add this test to `TrainingDataTests`:

```python
def test_xgboost_preprocessing_fits_min_and_range_on_training_rows_only(self):
    preprocessing = load_module(
        "model_preprocessing", "ml-model/model_preprocessing.py"
    )
    features = np.array(
        [[0.0, 5.0], [10.0, 5.0], [100.0, 50.0]], dtype="float32"
    )

    X_train, X_test, minimum, safe_range = preprocessing.prepare_min_max_splits(
        features,
        np.array([0, 1]),
        np.array([2]),
    )

    np.testing.assert_allclose(minimum, [0.0, 5.0])
    np.testing.assert_allclose(safe_range, [10.0, 1.0])
    np.testing.assert_allclose(X_train, [[0.0, 0.0], [1.0, 0.0]])
    np.testing.assert_allclose(X_test, [[10.0, 45.0]])
```

The held-out outlier must not change the training scale. A constant training feature receives a safe range of `1.0`.

- [ ] **Step 2: Run the regression test and verify the RED state**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest \
  tests.test_ml_data.TrainingDataTests.test_xgboost_preprocessing_fits_min_and_range_on_training_rows_only -v
```

Expected: ERROR because `ml-model/model_preprocessing.py` does not exist.

- [ ] **Step 3: Implement split fitting and transformation**

Create `ml-model/model_preprocessing.py` with:

```python
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
```

- [ ] **Step 4: Run the regression test and verify the GREEN state**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Add a failing sidecar round-trip test**

Add this test to `TrainingDataTests`:

```python
def test_xgboost_preprocessing_sidecar_preserves_schema_and_parameters(self):
    preprocessing = load_module(
        "model_preprocessing", "ml-model/model_preprocessing.py"
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        model_directory = Path(temporary_directory)
        saved_path = preprocessing.save_xgboost_preprocessing(
            ["move_count", "rating_diff_mean"],
            np.array([1.0, -20.0], dtype="float32"),
            np.array([9.0, 40.0], dtype="float32"),
            model_directory,
        )
        artifact = preprocessing.load_xgboost_preprocessing(model_directory)

    self.assertEqual(saved_path.name, "xgboost_preprocessing.json")
    self.assertEqual(artifact["version"], 1)
    self.assertEqual(artifact["transform"], "min_max")
    self.assertEqual(
        artifact["feature_names"], ["move_count", "rating_diff_mean"]
    )
    self.assertEqual(artifact["minimum"], [1.0, -20.0])
    self.assertEqual(artifact["range"], [9.0, 40.0])
    self.assertEqual(len(artifact["feature_schema_sha256"]), 64)
```

- [ ] **Step 6: Run the sidecar test and verify the RED state**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest \
  tests.test_ml_data.TrainingDataTests.test_xgboost_preprocessing_sidecar_preserves_schema_and_parameters -v
```

Expected: FAIL because the save and load functions do not exist.

- [ ] **Step 7: Implement the versioned sidecar contract**

Add these functions to `ml-model/model_preprocessing.py`:

```python
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
```

- [ ] **Step 8: Run both focused tests and verify the GREEN state**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest \
  tests.test_ml_data.TrainingDataTests.test_xgboost_preprocessing_fits_min_and_range_on_training_rows_only \
  tests.test_ml_data.TrainingDataTests.test_xgboost_preprocessing_sidecar_preserves_schema_and_parameters -v
```

Expected: two passing tests.

- [ ] **Step 9: Make the trainer use and save the fitted parameters**

Update `ml-model/train_xgboost.py` to import both new helpers, remove the full-dataset min/range block, and split labels without transforming them:

```python
from model_preprocessing import (
    prepare_min_max_splits,
    save_xgboost_preprocessing,
)

# ...

X = features.to_numpy(dtype="float32", copy=True)
y = labels.to_numpy(dtype="float32", copy=True)
X_train, X_test, x_min, x_range_safe = prepare_min_max_splits(
    X, train_index, test_index
)
y_train, y_test = y[train_index], y[test_index]
```

After the trainer saves the feature schema, save the exact fitted values:

```python
save_feature_schema(features.columns.tolist())
preprocessing_path = save_xgboost_preprocessing(
    features.columns.tolist(), x_min, x_range_safe
)
```

Print `preprocessing_path` beside the model path after training.

- [ ] **Step 10: Run the full verification suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest discover -s tests -v
```

Expected: 21 passing Python tests.

Run:

```bash
./.venv/bin/python -m py_compile \
  ml-model/model_preprocessing.py ml-model/train_xgboost.py
```

Expected: exit code zero.

- [ ] **Step 11: Commit the retraining prerequisite without the user's PyTorch edits**

Stage only:

```bash
git add \
  ml-model/model_preprocessing.py \
  ml-model/train_xgboost.py \
  tests/test_ml_data.py \
  docs/superpowers/plans/2026-08-04-xgboost-retraining-preprocessing-fix.md
```

Confirm `ml-model/train_torch.py` is not staged, then commit:

```bash
git commit -m "fix: persist XGBoost training preprocessing"
```
