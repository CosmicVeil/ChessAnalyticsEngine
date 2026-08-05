from contextlib import redirect_stdout
import importlib.util
import io
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import torch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "python-pipeline" / "torch_scoring.py"
VALID_SCHEMA_FINGERPRINT = (
    "0473ef2dc0d324ab659d3580c1134e9d812035905c4781fdd6d529b0c6860e13"
)


class CapturingModel(torch.nn.Module):
    def __init__(self, output):
        super().__init__()
        self.output = output
        self.values = None
        self.inference_mode_enabled = None

    def forward(self, values):
        self.values = values.detach().clone()
        self.inference_mode_enabled = torch.is_inference_mode_enabled()
        return self.output.clone()


class CountingModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def forward(self, _values):
        self.calls += 1
        return torch.tensor([[0.0]])


class TorchScoringTests(unittest.TestCase):
    def load_module(self):
        if not MODULE_PATH.exists():
            self.fail("python-pipeline/torch_scoring.py must exist")
        spec = importlib.util.spec_from_file_location(
            "torch_scoring_task_4_tests", MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def scoring_function(self):
        module = self.load_module()
        scoring_function = getattr(module, "score_completed_game", None)
        self.assertIsNotNone(
            scoring_function, "score_completed_game must be public"
        )
        return scoring_function

    def metadata_validator(self):
        module = self.load_module()
        validator = getattr(module, "validate_checkpoint_metadata", None)
        self.assertIsNotNone(
            validator, "validate_checkpoint_metadata must be public"
        )
        return validator

    def load_repository_module(self, module_name, relative_path):
        module_path = REPOSITORY_ROOT / relative_path
        module_directory = str(module_path.parent)
        added_path = module_directory not in sys.path
        if added_path:
            sys.path.insert(0, module_directory)
        try:
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            if added_path:
                sys.path.remove(module_directory)

    def configure_valid_worker_artifacts(self, worker):
        worker.load_feature_schema = lambda: ["a", "b"]
        worker.load_model = lambda feature_schema: object()

    def test_score_normalizes_in_schema_order_and_applies_sigmoid(self):
        model = CapturingModel(torch.log(torch.tensor([[3.0]])))

        score = self.scoring_function()(
            model,
            {"b": 4.0, "a": 3.0},
            ["a", "b"],
        )

        torch.testing.assert_close(model.values, torch.tensor([[0.6, 0.8]]))
        self.assertTrue(model.inference_mode_enabled)
        self.assertAlmostEqual(score, 0.75, delta=1e-6)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_score_keeps_a_zero_vector_finite(self):
        model = CapturingModel(torch.tensor([[0.0]]))

        score = self.scoring_function()(model, {}, ["a", "b"])

        torch.testing.assert_close(model.values, torch.tensor([[0.0, 0.0]]))
        self.assertTrue(math.isfinite(score))
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_score_rejects_output_that_is_not_one_finite_value(self):
        cases = [
            ("multiple values", torch.tensor([[0.0, 1.0]]), "one"),
            ("not finite", torch.tensor([[float("nan")]]), "finite"),
        ]

        for case_name, output, expected_message in cases:
            with self.subTest(case=case_name):
                model = CapturingModel(output)
                with self.assertRaisesRegex(ValueError, expected_message):
                    self.scoring_function()(model, {"a": 1.0}, ["a"])

    def test_score_rejects_non_finite_or_float32_overflowing_inputs(self):
        cases = [
            ("NaN", float("nan")),
            ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")),
            ("float32 overflow", 1e100),
        ]

        for case_name, invalid_value in cases:
            with self.subTest(case=case_name):
                model = CountingModel()
                try:
                    with self.assertRaisesRegex(ValueError, "feature"):
                        self.scoring_function()(
                            model, {"a": invalid_value}, ["a"]
                        )
                except (OverflowError, RuntimeError, TypeError) as error:
                    self.fail(
                        "invalid aligned input must raise ValueError before "
                        f"tensor execution: {error}"
                    )
                self.assertEqual(model.calls, 0)

    def test_scoring_and_worker_modules_import_without_xgboost(self):
        with patch.dict(sys.modules, {"xgboost": None}):
            try:
                self.load_module()
                self.load_repository_module(
                    "torch_inference_isolation_task_4_tests",
                    "python-pipeline/torch_inference.py",
                )
            except ModuleNotFoundError as error:
                if "xgboost" not in str(error):
                    raise
                self.fail(f"Torch modules imported XGBoost: {error}")

    def test_metadata_accepts_legacy_and_complete_current_checkpoints(self):
        validate = self.metadata_validator()

        validate({"input_size": 2}, ["a", "b"])
        validate(
            {
                "input_size": 2,
                "preprocessing": "l2",
                "feature_schema_sha256": VALID_SCHEMA_FINGERPRINT,
            },
            ["a", "b"],
        )

    def test_metadata_rejects_wrong_input_size(self):
        with self.assertRaisesRegex(ValueError, "input_size"):
            self.metadata_validator()({"input_size": 1}, ["a", "b"])

    def test_metadata_requires_both_current_fields_and_matching_values(self):
        cases = [
            (
                "missing fingerprint",
                {"input_size": 2, "preprocessing": "l2"},
                "feature_schema_sha256",
            ),
            (
                "missing preprocessing",
                {
                    "input_size": 2,
                    "feature_schema_sha256": VALID_SCHEMA_FINGERPRINT,
                },
                "preprocessing",
            ),
            (
                "wrong preprocessing",
                {
                    "input_size": 2,
                    "preprocessing": "min_max",
                    "feature_schema_sha256": VALID_SCHEMA_FINGERPRINT,
                },
                "preprocessing",
            ),
            (
                "wrong fingerprint",
                {
                    "input_size": 2,
                    "preprocessing": "l2",
                    "feature_schema_sha256": "0" * 64,
                },
                "feature_schema_sha256",
            ),
        ]

        for case_name, checkpoint, expected_field in cases:
            with self.subTest(case=case_name):
                with self.assertRaisesRegex(ValueError, expected_field):
                    self.metadata_validator()(checkpoint, ["a", "b"])

    def test_inference_validates_metadata_before_loading_weights(self):
        torch_inference = self.load_repository_module(
            "torch_inference_task_4_tests",
            "python-pipeline/torch_inference.py",
        )
        checkpoint = {
            "state_dict": {},
            "input_size": 2,
            "preprocessing": "l2",
            "feature_schema_sha256": "0" * 64,
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            torch.save(checkpoint, model_directory / "pytorch_model.pt")
            with patch.object(
                torch_inference, "MODELS_DIRECTORY", model_directory
            ):
                try:
                    with self.assertRaisesRegex(
                        ValueError, "feature_schema_sha256"
                    ):
                        torch_inference.load_model(["a", "b"])
                except TypeError as error:
                    self.fail(
                        "load_model must accept the feature schema for validation: "
                        f"{error}"
                    )

    def test_main_builds_kafka_clients_only_after_a_validated_model(self):
        torch_inference = self.load_repository_module(
            "torch_inference_valid_startup_task_4_tests",
            "python-pipeline/torch_inference.py",
        )
        schema = ["a", "b"]
        events = []
        original_validator = torch_inference.validate_checkpoint_metadata
        original_builder = torch_inference.build_pytorch_model

        class FakeConsumer:
            def __init__(self, _configuration):
                events.append("consumer")

            def subscribe(self, _topics):
                events.append("subscribe")

            def poll(self, timeout):
                self.timeout = timeout
                raise KeyboardInterrupt

            def close(self):
                events.append("close")

        class FakeProducer:
            def __init__(self, _configuration):
                events.append("producer")

            def flush(self):
                events.append("flush")
                return 0

        def load_schema():
            events.append("schema")
            return schema

        def validate_metadata(checkpoint, feature_schema):
            events.append("metadata")
            return original_validator(checkpoint, feature_schema)

        def build_model(input_size):
            events.append("model")
            return original_builder(input_size)

        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            torch.save(
                {
                    "state_dict": original_builder(2).state_dict(),
                    "input_size": 2,
                    "preprocessing": "l2",
                    "feature_schema_sha256": VALID_SCHEMA_FINGERPRINT,
                },
                model_directory / "pytorch_model.pt",
            )
            with (
                patch.object(
                    torch_inference, "MODELS_DIRECTORY", model_directory
                ),
                patch.object(
                    torch_inference,
                    "load_feature_schema",
                    side_effect=load_schema,
                ),
                patch.object(
                    torch_inference,
                    "validate_checkpoint_metadata",
                    side_effect=validate_metadata,
                ),
                patch.object(
                    torch_inference,
                    "build_pytorch_model",
                    side_effect=build_model,
                ),
                patch.object(torch_inference, "Consumer", FakeConsumer),
                patch.object(torch_inference, "Producer", FakeProducer),
                redirect_stdout(io.StringIO()),
            ):
                torch_inference.main()

        self.assertEqual(
            events,
            [
                "schema",
                "metadata",
                "model",
                "consumer",
                "producer",
                "subscribe",
                "flush",
                "close",
            ],
        )

    def test_main_rejects_invalid_metadata_before_building_kafka_clients(self):
        torch_inference = self.load_repository_module(
            "torch_inference_invalid_startup_task_4_tests",
            "python-pipeline/torch_inference.py",
        )
        schema = ["a", "b"]
        events = []
        original_validator = torch_inference.validate_checkpoint_metadata

        def load_schema():
            events.append("schema")
            return schema

        def validate_metadata(checkpoint, feature_schema):
            events.append("metadata")
            return original_validator(checkpoint, feature_schema)

        def build_model(_input_size):
            events.append("model")
            return object()

        def build_consumer(_configuration):
            events.append("consumer")
            return object()

        def build_producer(_configuration):
            events.append("producer")
            return object()

        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            torch.save(
                {
                    "state_dict": {},
                    "input_size": 2,
                    "preprocessing": "l2",
                    "feature_schema_sha256": "0" * 64,
                },
                model_directory / "pytorch_model.pt",
            )
            with (
                patch.object(
                    torch_inference, "MODELS_DIRECTORY", model_directory
                ),
                patch.object(
                    torch_inference,
                    "load_feature_schema",
                    side_effect=load_schema,
                ),
                patch.object(
                    torch_inference,
                    "validate_checkpoint_metadata",
                    side_effect=validate_metadata,
                ),
                patch.object(
                    torch_inference,
                    "build_pytorch_model",
                    side_effect=build_model,
                ),
                patch.object(
                    torch_inference, "Consumer", side_effect=build_consumer
                ),
                patch.object(
                    torch_inference, "Producer", side_effect=build_producer
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError, "feature_schema_sha256"
                ):
                    torch_inference.main()

        self.assertEqual(events, ["schema", "metadata"])

    def test_worker_closes_consumer_when_producer_construction_fails(self):
        worker = self.load_repository_module(
            "torch_inference_producer_failure_task_4_tests",
            "python-pipeline/torch_inference.py",
        )
        self.configure_valid_worker_artifacts(worker)

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
        worker.Consumer = lambda configuration: consumer

        def fail_producer_construction(_configuration):
            raise ProducerConstructionError("producer construction failed")

        worker.Producer = fail_producer_construction

        with self.assertRaisesRegex(
            ProducerConstructionError,
            "producer construction failed",
        ):
            worker.main()

        self.assertEqual(consumer.close_calls, 1)

    def test_worker_cleans_up_without_masking_subscribe_failure(self):
        worker = self.load_repository_module(
            "torch_inference_subscribe_failure_task_4_tests",
            "python-pipeline/torch_inference.py",
        )
        self.configure_valid_worker_artifacts(worker)

        class SubscribeError(RuntimeError):
            pass

        class CleanupError(RuntimeError):
            pass

        class Consumer:
            close_calls = 0

            def subscribe(self, _topics):
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
        worker.Consumer = lambda configuration: consumer
        worker.Producer = lambda configuration: producer

        with self.assertRaisesRegex(SubscribeError, "subscription failed"):
            worker.main()

        self.assertEqual(producer.flush_calls, 1)
        self.assertEqual(consumer.close_calls, 1)

    def test_worker_cleans_up_without_masking_loop_failure(self):
        worker = self.load_repository_module(
            "torch_inference_loop_failure_task_4_tests",
            "python-pipeline/torch_inference.py",
        )
        self.configure_valid_worker_artifacts(worker)

        class LoopError(RuntimeError):
            pass

        class CleanupError(RuntimeError):
            pass

        class Consumer:
            close_calls = 0

            def subscribe(self, topics):
                self.topics = topics

            def poll(self, timeout):
                self.timeout = timeout
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
        worker.Consumer = lambda configuration: consumer
        worker.Producer = lambda configuration: producer

        try:
            worker.main()
        except Exception as error:  # noqa: BLE001 - inspect surfaced error.
            self.assertIsInstance(error, LoopError)
            self.assertIn("poll failed", str(error))
        else:
            self.fail("expected poll failure")

        self.assertEqual(producer.flush_calls, 1)
        self.assertEqual(consumer.close_calls, 1)

    def test_training_checkpoint_records_preprocessing_and_schema(self):
        train_torch = self.load_repository_module(
            "train_torch_task_4_tests", "ml-model/train_torch.py"
        )
        features = pd.DataFrame(
            [[3.0, 4.0], [1.0, 2.0], [2.0, 1.0], [4.0, 3.0]],
            columns=["a", "b"],
        )
        labels = pd.Series([0.0, 1.0, 0.0, 1.0])
        saved_checkpoints = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "pytorch_model.pt"
            with (
                patch.object(train_torch.pd, "read_csv", return_value=pd.DataFrame()),
                patch.object(
                    train_torch,
                    "prepare_game_training_data",
                    return_value=(features, labels),
                ),
                patch.object(
                    train_torch,
                    "group_train_test_indices",
                    return_value=([0, 1], [2, 3]),
                ),
                patch.object(train_torch, "save_feature_schema"),
                patch.object(
                    train_torch,
                    "model_artifact_path",
                    return_value=checkpoint_path,
                ),
                patch.object(
                    train_torch.torch,
                    "save",
                    side_effect=lambda payload, _path: saved_checkpoints.append(
                        payload
                    ),
                ),
                patch.object(
                    train_torch.torch.mps, "is_available", return_value=False
                ),
                redirect_stdout(io.StringIO()),
            ):
                train_torch.main()

        self.assertEqual(len(saved_checkpoints), 1)
        self.assertEqual(saved_checkpoints[0].get("preprocessing"), "l2")
        self.assertEqual(
            saved_checkpoints[0].get("feature_schema_sha256"),
            VALID_SCHEMA_FINGERPRINT,
        )


if __name__ == "__main__":
    unittest.main()
