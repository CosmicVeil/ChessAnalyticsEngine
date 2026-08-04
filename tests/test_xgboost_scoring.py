import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "python-pipeline" / "xgboost_scoring.py"
INFERENCE_MODULE_PATH = (
    REPOSITORY_ROOT / "python-pipeline" / "xgboost_inference.py"
)
MODEL_DIRECTORY = REPOSITORY_ROOT / "ml-model"
VALID_SCHEMA_FINGERPRINT = (
    "0473ef2dc0d324ab659d3580c1134e9d812035905c4781fdd6d529b0c6860e13"
)


def load_xgboost_scoring_module():
    spec = importlib.util.spec_from_file_location(
        "xgboost_scoring_task_3_tests", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(MODEL_DIRECTORY))
    try:
        with patch.dict(sys.modules, {"torch": None, "xgboost": None}):
            spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def load_xgboost_inference_module():
    fake_kafka = types.ModuleType("confluent_kafka")
    fake_kafka.Consumer = object
    fake_kafka.KafkaError = type("KafkaError", (), {"_PARTITION_EOF": -1})
    fake_kafka.KafkaException = type("KafkaException", (Exception,), {})
    fake_kafka.Producer = object

    fake_xgboost = types.ModuleType("xgboost")
    fake_xgboost.XGBClassifier = type("XGBClassifier", (), {})

    spec = importlib.util.spec_from_file_location(
        "xgboost_inference_task_3_tests", INFERENCE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    original_sys_path = sys.path.copy()
    try:
        with patch.dict(
            sys.modules,
            {
                "confluent_kafka": fake_kafka,
                "torch": None,
                "xgboost": fake_xgboost,
            },
        ):
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_sys_path
    return module


module = load_xgboost_scoring_module()


def preprocessing_fixture():
    return {
        "version": 1,
        "transform": "min_max",
        "feature_names": ["a", "b"],
        "feature_schema_sha256": VALID_SCHEMA_FINGERPRINT,
        "minimum": [10.0, 2.0],
        "range": [5.0, 2.0],
    }


def configure_valid_worker_artifacts(worker):
    worker.load_feature_schema = lambda: ["a", "b"]
    worker.load_xgboost_preprocessing = (
        lambda model_directory, expected_feature_names: preprocessing_fixture()
    )
    worker.load_model = lambda: object()


class CapturingModel:
    def predict_proba(self, values):
        self.values = values.copy()
        return np.array([[0.25, 0.75]], dtype="float32")


class ReturningModel:
    def __init__(self, output):
        self.output = output

    def predict_proba(self, values):
        return self.output


class XGBoostScoringTests(unittest.TestCase):
    def assert_rejects_output(self, output):
        with self.assertRaises(ValueError):
            module.score_completed_game(
                ReturningModel(output),
                {"a": 15.0, "b": 8.0},
                ["a", "b"],
                preprocessing_fixture(),
            )

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

    def test_score_rejects_malformed_model_output(self):
        cases = {
            "rank one": np.array([0.25, 0.75], dtype="float32"),
            "one class": np.array([[0.75]], dtype="float32"),
            "three classes": np.array([[0.2, 0.7, 0.1]], dtype="float32"),
            "multiple rows": np.array(
                [[0.25, 0.75], [0.6, 0.4]], dtype="float32"
            ),
            "not numeric": np.array([["negative", "positive"]]),
        }

        for case_name, output in cases.items():
            with self.subTest(case=case_name):
                self.assert_rejects_output(output)

    def test_score_rejects_non_finite_model_output(self):
        cases = {
            "negative NaN": np.array([[np.nan, 0.75]], dtype="float32"),
            "positive NaN": np.array([[0.25, np.nan]], dtype="float32"),
            "negative infinity": np.array([[np.inf, 0.75]], dtype="float32"),
            "positive infinity": np.array([[0.25, -np.inf]], dtype="float32"),
        }

        for case_name, output in cases.items():
            with self.subTest(case=case_name):
                self.assert_rejects_output(output)

    def test_score_rejects_model_output_outside_zero_to_one(self):
        cases = {
            "negative class below zero": np.array(
                [[-0.01, 0.75]], dtype="float32"
            ),
            "negative class above one": np.array(
                [[1.01, 0.75]], dtype="float32"
            ),
            "positive class below zero": np.array(
                [[0.25, -0.01]], dtype="float32"
            ),
            "positive class above one": np.array(
                [[0.25, 1.01]], dtype="float32"
            ),
        }

        for case_name, output in cases.items():
            with self.subTest(case=case_name):
                self.assert_rejects_output(output)


class XGBoostIsolationTests(unittest.TestCase):
    def test_scoring_and_worker_modules_import_without_torch(self):
        load_xgboost_scoring_module()
        load_xgboost_inference_module()


class XGBoostInferenceTests(unittest.TestCase):
    def test_worker_rejects_preprocessing_before_model_or_kafka_clients(self):
        worker = load_xgboost_inference_module()
        events = []
        feature_schema = ["a", "b"]

        class InvalidSidecar(ValueError):
            pass

        def load_feature_schema():
            events.append("schema")
            return feature_schema

        def reject_preprocessing(model_directory, expected_feature_names):
            events.append("preprocessing")
            self.assertEqual(model_directory, worker.MODELS_DIRECTORY)
            self.assertEqual(expected_feature_names, feature_schema)
            raise InvalidSidecar("invalid preprocessing sidecar")

        def load_model():
            events.append("model")
            raise AssertionError("model loaded before preprocessing validation")

        def construct_consumer(config):
            events.append("consumer")
            return object()

        worker.load_feature_schema = load_feature_schema
        worker.load_xgboost_preprocessing = reject_preprocessing
        worker.load_model = load_model
        worker.Consumer = construct_consumer

        with self.assertRaisesRegex(InvalidSidecar, "invalid preprocessing"):
            worker.main()

        self.assertEqual(events, ["schema", "preprocessing"])

    def test_worker_scores_raw_completed_game_features_after_ordered_startup(self):
        worker = load_xgboost_inference_module()
        events = []
        feature_schema = ["a", "b"]
        preprocessing = preprocessing_fixture()
        completed_features = {"b": 8.0, "a": 15.0}
        model = ReturningModel(
            np.array([[0.25, 0.75]], dtype="float32")
        )
        scoring_calls = []

        class StopWorker(Exception):
            pass

        class Message:
            def value(self):
                return json.dumps(
                    {"game_id": "game-3", "features": completed_features}
                ).encode("utf-8")

            def error(self):
                return None

        class Consumer:
            def __init__(self):
                self.messages = [Message()]

            def subscribe(self, topics):
                self.topics = topics

            def poll(self, timeout):
                if self.messages:
                    return self.messages.pop(0)
                raise StopWorker

            def close(self):
                self.closed = True

        class Producer:
            def flush(self):
                return 0

        consumer = Consumer()
        producer = Producer()

        def load_feature_schema():
            events.append("schema")
            return feature_schema

        def load_preprocessing(model_directory, expected_feature_names):
            events.append("preprocessing")
            self.assertEqual(model_directory, worker.MODELS_DIRECTORY)
            self.assertEqual(expected_feature_names, feature_schema)
            return preprocessing

        def load_model():
            events.append("model")
            return model

        def construct_consumer(config):
            events.append("consumer")
            consumer.config = config
            return consumer

        def construct_producer(config):
            events.append("producer")
            producer.config = config
            return producer

        def score_completed_game(
            received_model,
            feature_values,
            received_schema,
            received_preprocessing,
        ):
            scoring_calls.append(
                (
                    received_model,
                    feature_values,
                    received_schema,
                    received_preprocessing,
                )
            )
            return 0.75

        worker.load_feature_schema = load_feature_schema
        worker.load_xgboost_preprocessing = load_preprocessing
        worker.load_model = load_model
        worker.Consumer = construct_consumer
        worker.Producer = construct_producer
        worker.score_completed_game = score_completed_game
        worker.publish_and_commit = lambda *args: None

        with self.assertRaises(StopWorker):
            worker.main()

        self.assertEqual(
            events[:5],
            ["schema", "preprocessing", "model", "consumer", "producer"],
        )
        self.assertEqual(
            scoring_calls,
            [(model, completed_features, feature_schema, preprocessing)],
        )
        self.assertEqual(consumer.topics, ["chess-game-features"])
        self.assertEqual(
            consumer.config["group.id"],
            "chess-xgboost-game-inference",
        )
        self.assertEqual(
            consumer.config["bootstrap.servers"],
            worker.BOOTSTRAP_SERVERS,
        )
        self.assertEqual(
            producer.config["bootstrap.servers"],
            worker.BOOTSTRAP_SERVERS,
        )
        self.assertTrue(consumer.closed)

    def test_worker_closes_consumer_when_producer_construction_fails(self):
        worker = load_xgboost_inference_module()
        configure_valid_worker_artifacts(worker)

        class ProducerConstructionError(RuntimeError):
            pass

        class CleanupError(RuntimeError):
            pass

        class Consumer:
            close_calls = 0

            def close(self):
                self.close_calls += 1
                raise CleanupError("consumer close failed")

        consumer = Consumer()
        worker.Consumer = lambda config: consumer

        def fail_producer_construction(config):
            raise ProducerConstructionError("producer construction failed")

        worker.Producer = fail_producer_construction

        with self.assertRaisesRegex(
            ProducerConstructionError,
            "producer construction failed",
        ):
            worker.main()

        self.assertEqual(consumer.close_calls, 1)

    def test_worker_cleans_up_without_masking_subscribe_failure(self):
        worker = load_xgboost_inference_module()
        configure_valid_worker_artifacts(worker)

        class SubscribeError(RuntimeError):
            pass

        class CleanupError(RuntimeError):
            pass

        class Consumer:
            close_calls = 0

            def subscribe(self, topics):
                raise SubscribeError("subscription failed")

            def close(self):
                self.close_calls += 1
                raise CleanupError("consumer close failed")

        class Producer:
            flush_calls = 0

            def flush(self):
                self.flush_calls += 1
                raise CleanupError("producer flush failed")

        consumer = Consumer()
        producer = Producer()
        worker.Consumer = lambda config: consumer
        worker.Producer = lambda config: producer

        with self.assertRaisesRegex(SubscribeError, "subscription failed"):
            worker.main()

        self.assertEqual(producer.flush_calls, 1)
        self.assertEqual(consumer.close_calls, 1)

    def test_worker_cleans_up_without_masking_loop_failure(self):
        worker = load_xgboost_inference_module()
        configure_valid_worker_artifacts(worker)

        class LoopError(RuntimeError):
            pass

        class CleanupError(RuntimeError):
            pass

        class Consumer:
            close_calls = 0

            def subscribe(self, topics):
                self.topics = topics

            def poll(self, timeout):
                raise LoopError("poll failed")

            def close(self):
                self.close_calls += 1
                raise CleanupError("consumer close failed")

        class Producer:
            flush_calls = 0

            def flush(self):
                self.flush_calls += 1
                raise CleanupError("producer flush failed")

        consumer = Consumer()
        producer = Producer()
        worker.Consumer = lambda config: consumer
        worker.Producer = lambda config: producer

        try:
            worker.main()
        except Exception as error:  # noqa: BLE001 - inspect the surfaced error.
            self.assertIsInstance(error, LoopError)
            self.assertIn("poll failed", str(error))
        else:
            self.fail("expected poll failure")

        self.assertEqual(producer.flush_calls, 1)
        self.assertEqual(consumer.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
