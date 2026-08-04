# Live Inference and Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both live model workers reproduce training preprocessing, publish bounded scores, accept explicit PGN completion, and read Kafka brokers from configuration.

**Architecture:** Shared scoring modules contain testable numerical behavior and keep the PyTorch and XGBoost processes isolated. A small Go protocol extension accepts the final-move flag that the web ingestion worker will publish. Python and Go Kafka entry points use one environment variable while retaining the existing development default.

**Tech Stack:** Python 3.14, NumPy, pandas, PyTorch, XGBoost, confluent-kafka, Go 1.26, kafka-go, `unittest`, Go testing.

## Global Constraints

- Keep PyTorch and XGBoost in separate processes.
- Keep all existing Kafka topic names and joined prediction fields.
- Use the XGBoost sidecar produced by commit `57a8d4a`; never fit or clip live features.
- Apply per-row L2 normalization and sigmoid in PyTorch live scoring.
- Accept the current legacy PyTorch checkpoint without retraining.
- Preserve batch size `128` and epoch count `300` in the user's uncommitted `ml-model/train_torch.py` change.
- Default `KAFKA_BOOTSTRAP_SERVERS` to `localhost:19092`.

---

### Task 1: Centralize Python Kafka broker configuration

**Files:**
- Create: `python-pipeline/kafka_config.py`
- Create: `tests/test_kafka_config.py`
- Modify: `python-pipeline/data_collector.py`
- Modify: `python-pipeline/simulator.py`
- Modify: `python-pipeline/live_game_collector.py`
- Modify: `python-pipeline/torch_inference.py`
- Modify: `python-pipeline/xgboost_inference.py`
- Modify: `python-pipeline/prediction_reporter.py`
- Modify: `python-pipeline/producer_test.py`

**Interfaces:**
- Produces: `get_kafka_bootstrap_servers(environ: Mapping[str, str] | None = None) -> str`.
- Preserves: every existing topic and consumer group.

- [ ] **Step 1: Write failing configuration tests**

```python
class KafkaConfigTests(unittest.TestCase):
    def test_defaults_to_local_redpanda(self):
        module = load_pipeline_module("kafka_config")
        self.assertEqual(module.get_kafka_bootstrap_servers({}), "localhost:19092")

    def test_uses_trimmed_environment_value(self):
        module = load_pipeline_module("kafka_config")
        self.assertEqual(
            module.get_kafka_bootstrap_servers(
                {"KAFKA_BOOTSTRAP_SERVERS": " broker-a:9092,broker-b:9092 "}
            ),
            "broker-a:9092,broker-b:9092",
        )

    def test_blank_value_uses_default(self):
        module = load_pipeline_module("kafka_config")
        self.assertEqual(
            module.get_kafka_bootstrap_servers({"KAFKA_BOOTSTRAP_SERVERS": "  "}),
            "localhost:19092",
        )
```

- [ ] **Step 2: Run `tests.test_kafka_config` and confirm import failure**

Run `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest tests.test_kafka_config -v`.

- [ ] **Step 3: Implement the helper and replace hard-coded broker strings**

```python
DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:19092"


def get_kafka_bootstrap_servers(environ=None):
    source = os.environ if environ is None else environ
    value = source.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    return value or DEFAULT_KAFKA_BOOTSTRAP_SERVERS
```

Each entry point imports the helper after inserting `PIPELINE_DIRECTORY` into `sys.path`, then uses one module-level `BOOTSTRAP_SERVERS` value.

- [ ] **Step 4: Run the focused test and the existing import tests**

Run the focused command plus `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest tests.test_ml_data.TrainingDataTests.test_live_entry_scripts_import_without_a_pipeline_working_directory -v`.

- [ ] **Step 5: Commit only Task 1 files**

Commit with `refactor: configure Python Kafka brokers`.

### Task 2: Validate and apply the XGBoost preprocessing sidecar

**Files:**
- Modify: `ml-model/model_preprocessing.py`
- Create: `tests/test_model_preprocessing.py`

**Interfaces:**
- Produces: `ModelArtifactError(ValueError)`.
- Produces: `feature_schema_fingerprint(feature_names: Sequence[str]) -> str`.
- Produces: `load_xgboost_preprocessing(model_directory=MODELS_DIRECTORY, expected_feature_names=None) -> dict`.
- Produces: `transform_xgboost_features(feature_values, feature_names, preprocessing) -> np.ndarray` with shape `(1, n_features)` and dtype `float32`.

- [ ] **Step 1: Write failing transformation and validation tests**

```python
def test_transform_uses_schema_order_zero_fill_and_no_clipping(self):
    metadata = {
        "version": 1,
        "transform": "min_max",
        "feature_names": ["a", "b"],
        "feature_schema_sha256": module.feature_schema_fingerprint(["a", "b"]),
        "minimum": [10.0, 2.0],
        "range": [5.0, 2.0],
    }
    row = module.transform_xgboost_features(
        {"b": 8.0}, ["a", "b"], metadata
    )
    np.testing.assert_allclose(row, [[-2.0, 3.0]])
    self.assertEqual(row.dtype, np.float32)
```

Add table-driven tests for a missing file, invalid JSON, unsupported version or transform, reordered schema, tampered fingerprint, mismatched vector lengths, non-finite values, and non-positive ranges. Each case must raise `ModelArtifactError` with the failing contract field in its message.

- [ ] **Step 2: Run the focused module and confirm missing interfaces**

Run `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest tests.test_model_preprocessing -v`.

- [ ] **Step 3: Implement strict loading and transformation**

Promote the current private fingerprint helper and validate the JSON before returning it. Build the live row in `feature_names` order, fill absent values with `0.0`, then calculate:

```python
raw = np.asarray(
    [[float(feature_values.get(name, 0.0)) for name in feature_names]],
    dtype="float32",
)
return (raw - minimum) / safe_range
```

Do not call `np.clip`.

- [ ] **Step 4: Run the focused and existing preprocessing tests**

Run `tests.test_model_preprocessing` and both XGBoost preprocessing tests in `tests.test_ml_data`.

- [ ] **Step 5: Commit Task 2**

Commit with `feat: validate XGBoost preprocessing artifacts`.

### Task 3: Correct XGBoost live scoring

**Files:**
- Create: `python-pipeline/xgboost_scoring.py`
- Modify: `python-pipeline/xgboost_inference.py`
- Create: `tests/test_xgboost_scoring.py`

**Interfaces:**
- Produces: `score_completed_game(model, feature_values, feature_schema, preprocessing) -> float`.
- Startup order: schema, preprocessing validation, model, Kafka clients.

- [ ] **Step 1: Write a failing fake-model scoring test**

```python
class CapturingModel:
    def predict_proba(self, values):
        self.values = values.copy()
        return np.array([[0.25, 0.75]], dtype="float32")


def test_score_transforms_values_before_prediction(self):
    model = CapturingModel()
    score = module.score_completed_game(
        model,
        {"b": 8.0, "a": 15.0},
        ["a", "b"],
        preprocessing_fixture(),
    )
    np.testing.assert_allclose(model.values, [[1.0, 3.0]])
    self.assertEqual(score, 0.75)
```

Add cases that reject malformed model output, NaN, and values outside zero to one.

- [ ] **Step 2: Confirm the scoring module import fails**

Run `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest tests.test_xgboost_scoring -v`.

- [ ] **Step 3: Implement the scoring helper and integrate the worker**

The helper imports `transform_xgboost_features`, calls `predict_proba`, verifies an array containing `[negative, positive]`, and returns the positive score. The worker passes `completed_game["features"]` to the helper and refuses to construct a consumer when sidecar validation fails.

- [ ] **Step 4: Run focused tests and compile both modules**

Run the focused suite and `PYTHONPYCACHEPREFIX=/private/tmp/chessanalytics-pycache ./.venv/bin/python -m py_compile python-pipeline/xgboost_scoring.py python-pipeline/xgboost_inference.py`.

- [ ] **Step 5: Commit Task 3**

Commit with `fix: apply XGBoost preprocessing during inference`.

### Task 4: Correct PyTorch live scoring and checkpoint metadata

**Files:**
- Create: `python-pipeline/torch_scoring.py`
- Modify: `python-pipeline/torch_inference.py`
- Modify: `ml-model/train_torch.py`
- Create: `tests/test_torch_scoring.py`

**Interfaces:**
- Produces: `validate_checkpoint_metadata(checkpoint, feature_schema) -> None`.
- Produces: `score_completed_game(model, feature_values, feature_schema) -> float`.
- Legacy checkpoint rule: accept missing metadata when `input_size` matches.

- [ ] **Step 1: Write failing numerical tests**

Use a capturing `torch.nn.Module` and assert that raw `[3.0, 4.0]` reaches the model as `[0.6, 0.8]`. Return `torch.log(torch.tensor([[3.0]]))` and assert the score is `0.75`. Add a zero-vector case that returns a finite score.

- [ ] **Step 2: Write failing metadata tests**

Accept `{"input_size": 2}`. Reject the wrong input size. When either `preprocessing` or `feature_schema_sha256` exists, require both, require `preprocessing == "l2"`, and require a matching fingerprint.

- [ ] **Step 3: Run `tests.test_torch_scoring` and confirm missing interfaces**

- [ ] **Step 4: Implement scoring and worker integration**

```python
raw = torch.tensor([ordered_values], dtype=torch.float32)
normalized = F.normalize(raw, p=2.0, dim=1)
with torch.inference_mode():
    probability = torch.sigmoid(model(normalized)).reshape(-1)
```

Require one finite element in the inclusive zero-to-one range. Update checkpoint loading to validate metadata before `load_state_dict`.

- [ ] **Step 5: Add metadata to future checkpoints without changing the user's hyperparameters**

Extend the existing `torch.save` payload with `preprocessing: "l2"` and `feature_schema_sha256`. Preserve batch size `128` and epoch count `300`; show their unchanged diff during review.

- [ ] **Step 6: Run focused tests, the full Python suite, and syntax compilation**

- [ ] **Step 7: Commit Task 4 without altering the user's intended training values**

Commit with `fix: normalize PyTorch live inference scores`.

### Task 5: Configure Go Kafka brokers and accept explicit completion

**Files:**
- Modify: `go-backend/main.go`
- Modify: `go-backend/main_test.go`

**Interfaces:**
- Produces: `kafkaBootstrapServers() []string`.
- Produces: `shouldCompleteGame(event ChessMoveEvent, game *chess.Game) bool`.
- Adds: `GameComplete bool \`json:"game_complete,omitempty"\`` to `ChessMoveEvent`.

- [ ] **Step 1: Write failing broker parsing tests**

Use `t.Setenv` to cover absent, blank, and comma-separated broker values. Expect ordered trimmed entries and the local fallback.

- [ ] **Step 2: Write failing completion tests**

Decode a JSON event with `game_complete: true`. Assert an ongoing board plus the flag completes, checkmate without the flag completes, and an ordinary ongoing move does not.

- [ ] **Step 3: Run `GOCACHE=/private/tmp/chessanalytics-go-cache go test ./...` and confirm RED**

- [ ] **Step 4: Implement the helpers and use one completion decision**

```go
complete := event.GameComplete || game.Outcome() != chess.NoOutcome
```

Use `complete` for marker publication, state retention, and state deletion. Feed the configured broker slice to the reader and `kafka.TCP(brokers...)`.

- [ ] **Step 5: Format and test**

Run `gofmt -w main.go main_test.go` and the Go suite from `go-backend` with the temporary cache.

- [ ] **Step 6: Commit Task 5**

Commit with `feat: accept explicit streamed game completion`.

### Task 6: Isolated artifact smoke tests and plan verification

**Files:**
- Create: `tests/test_inference_artifacts.py`

**Interfaces:**
- Verifies each model in a separate subprocess.
- Does not import PyTorch and XGBoost into the same test process.

- [ ] **Step 1: Write subprocess smoke tests**

One subprocess loads the feature schema, XGBoost sidecar, and XGBoost model, then scores one zero-filled aligned row through `xgboost_scoring`. A second subprocess loads the PyTorch checkpoint and scores the same logical row through `torch_scoring`. Each script prints one JSON score.

- [ ] **Step 2: Run the smoke tests and fix only artifact-contract failures**

- [ ] **Step 3: Run the full verification gate**

Run all Python tests, syntax compilation for every changed Python entry point, `git diff --check`, and the full Go suite. Confirm the only score values emitted by smoke tests fall within zero and one.

- [ ] **Step 4: Commit Task 6**

Commit with `test: verify isolated live inference artifacts`.
