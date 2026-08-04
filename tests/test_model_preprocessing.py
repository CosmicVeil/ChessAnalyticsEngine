import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "ml-model" / "model_preprocessing.py"
VALID_SCHEMA_FINGERPRINT = (
    "0473ef2dc0d324ab659d3580c1134e9d812035905c4781fdd6d529b0c6860e13"
)


def load_model_preprocessing_module():
    spec = importlib.util.spec_from_file_location(
        "model_preprocessing_task_2_tests", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load_model_preprocessing_module()


def valid_metadata():
    return {
        "version": 1,
        "transform": "min_max",
        "feature_names": ["a", "b"],
        "feature_schema_sha256": VALID_SCHEMA_FINGERPRINT,
        "minimum": [10.0, 2.0],
        "range": [5.0, 2.0],
    }


class ModelPreprocessingTests(unittest.TestCase):
    def assert_artifact_error(self, expected_field, operation):
        try:
            operation()
        except module.ModelArtifactError as error:
            self.assertIn(expected_field, str(error))
        except Exception as error:  # noqa: BLE001 - reports the violated boundary.
            self.fail(
                f"expected ModelArtifactError for {expected_field}, "
                f"got {type(error).__name__}: {error}"
            )
        else:
            self.fail(f"expected ModelArtifactError for {expected_field}")

    def test_model_artifact_error_is_a_value_error(self):
        artifact_error = getattr(module, "ModelArtifactError", None)

        self.assertIsNotNone(artifact_error, "ModelArtifactError must be public")
        self.assertTrue(issubclass(artifact_error, ValueError))

    def test_feature_schema_fingerprint_uses_compact_json_order(self):
        fingerprint = getattr(module, "feature_schema_fingerprint", None)

        self.assertIsNotNone(
            fingerprint, "feature_schema_fingerprint must be public"
        )
        self.assertEqual(fingerprint(("a", "b")), VALID_SCHEMA_FINGERPRINT)

    def test_transform_uses_schema_order_zero_fill_and_no_clipping(self):
        transform = getattr(module, "transform_xgboost_features", None)
        self.assertIsNotNone(transform, "transform_xgboost_features must be public")

        row = transform({"b": 8.0}, ["a", "b"], valid_metadata())

        np.testing.assert_allclose(row, [[-2.0, 3.0]])
        self.assertEqual(row.shape, (1, 2))
        self.assertEqual(row.dtype, np.float32)

    def test_transform_rejects_feature_names_that_do_not_exactly_match_metadata(self):
        cases = [
            ("reordered", ["b", "a"]),
            ("missing name", ["a"]),
            ("extra name", ["a", "b", "c"]),
        ]

        for case_name, feature_names in cases:
            with self.subTest(case=case_name):
                self.assert_artifact_error(
                    "feature_names",
                    lambda: module.transform_xgboost_features(
                        {"a": 15.0, "b": 8.0},
                        feature_names,
                        valid_metadata(),
                    ),
                )

    def test_transform_rejects_vectors_that_could_broadcast_or_scale_badly(self):
        short_minimum = valid_metadata()
        short_minimum["minimum"] = [10.0]
        short_range = valid_metadata()
        short_range["range"] = [5.0]
        zero_range = valid_metadata()
        zero_range["range"] = [5.0, 0.0]
        cases = [
            ("short minimum", short_minimum, "minimum"),
            ("short range", short_range, "range"),
            ("zero range", zero_range, "range"),
        ]

        for case_name, metadata, expected_field in cases:
            with self.subTest(case=case_name):
                self.assert_artifact_error(
                    expected_field,
                    lambda: module.transform_xgboost_features(
                        {"a": 15.0, "b": 8.0},
                        ["a", "b"],
                        metadata,
                    ),
                )

    def test_save_round_trips_without_changing_valid_artifact_json(self):
        metadata = valid_metadata()
        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)

            saved_path = module.save_xgboost_preprocessing(
                metadata["feature_names"],
                np.asarray(metadata["minimum"], dtype="float32"),
                np.asarray(metadata["range"], dtype="float32"),
                model_directory,
            )
            loaded = module.load_xgboost_preprocessing(
                model_directory, metadata["feature_names"]
            )

            self.assertEqual(
                saved_path.read_text(encoding="utf-8"), json.dumps(metadata)
            )
            self.assertEqual(loaded, metadata)

    def test_save_rejects_inputs_that_would_create_an_invalid_artifact(self):
        cases = [
            ("string schema", "ab", [10.0, 2.0], [5.0, 2.0], "feature_names"),
            (
                "empty feature name",
                ["a", ""],
                [10.0, 2.0],
                [5.0, 2.0],
                "feature_names",
            ),
            (
                "non-string feature name",
                ["a", 2],
                [10.0, 2.0],
                [5.0, 2.0],
                "feature_names",
            ),
            ("scalar minimum", ["a"], 10.0, [5.0], "minimum"),
            ("two-dimensional minimum", ["a"], [[10.0]], [5.0], "minimum"),
            ("two-dimensional range", ["a"], [10.0], [[5.0]], "range"),
            (
                "non-finite minimum",
                ["a", "b"],
                [float("nan"), 2.0],
                [5.0, 2.0],
                "minimum",
            ),
            (
                "non-finite range",
                ["a", "b"],
                [10.0, 2.0],
                [5.0, float("inf")],
                "range",
            ),
            ("zero range", ["a", "b"], [10.0, 2.0], [5.0, 0.0], "range"),
            (
                "negative range",
                ["a", "b"],
                [10.0, 2.0],
                [5.0, -2.0],
                "range",
            ),
        ]

        for case_name, feature_names, minimum, safe_range, expected_field in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    model_directory = Path(temporary_directory)
                    artifact_path = (
                        model_directory / module.XGBOOST_PREPROCESSING_FILENAME
                    )

                    self.assert_artifact_error(
                        expected_field,
                        lambda: module.save_xgboost_preprocessing(
                            feature_names,
                            minimum,
                            safe_range,
                            model_directory,
                        ),
                    )
                    self.assertFalse(artifact_path.exists())

    def test_load_accepts_positional_directory_and_expected_schema(self):
        metadata = valid_metadata()
        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            path = model_directory / module.XGBOOST_PREPROCESSING_FILENAME
            path.write_text(json.dumps(metadata), encoding="utf-8")

            try:
                loaded = module.load_xgboost_preprocessing(
                    model_directory, ["a", "b"]
                )
            except TypeError as error:
                self.fail(f"loader positional compatibility was lost: {error}")

        self.assertEqual(loaded, metadata)

    def test_load_rejects_invalid_artifacts_with_contract_field(self):
        artifact_error = getattr(module, "ModelArtifactError", None)
        self.assertIsNotNone(artifact_error, "ModelArtifactError must be public")

        missing_version = valid_metadata()
        del missing_version["version"]
        invalid_feature_names = valid_metadata()
        invalid_feature_names["feature_names"] = "a,b"
        unsupported_version = valid_metadata()
        unsupported_version["version"] = 2
        unsupported_transform = valid_metadata()
        unsupported_transform["transform"] = "standardize"
        tampered_fingerprint = valid_metadata()
        tampered_fingerprint["feature_schema_sha256"] = "0" * 64
        short_minimum = valid_metadata()
        short_minimum["minimum"] = [10.0]
        short_range = valid_metadata()
        short_range["range"] = [5.0]
        non_finite_minimum = valid_metadata()
        non_finite_minimum["minimum"] = [float("nan"), 2.0]
        non_finite_range = valid_metadata()
        non_finite_range["range"] = [5.0, float("inf")]
        zero_range = valid_metadata()
        zero_range["range"] = [5.0, 0.0]
        negative_range = valid_metadata()
        negative_range["range"] = [5.0, -2.0]

        cases = [
            ("missing file", None, None, module.XGBOOST_PREPROCESSING_FILENAME),
            ("invalid JSON", "{not valid json", None, "JSON"),
            ("invalid UTF-8 JSON", b"\xff", None, "JSON"),
            ("non-object JSON", [], None, "metadata"),
            ("missing version", missing_version, None, "version"),
            ("unsupported version", unsupported_version, None, "version"),
            (
                "unsupported transform",
                unsupported_transform,
                None,
                "transform",
            ),
            (
                "invalid feature names",
                invalid_feature_names,
                None,
                "feature_names",
            ),
            ("reordered schema", valid_metadata(), ["b", "a"], "feature_names"),
            (
                "tampered fingerprint",
                tampered_fingerprint,
                None,
                "feature_schema_sha256",
            ),
            ("short minimum", short_minimum, None, "minimum"),
            ("short range", short_range, None, "range"),
            (
                "non-finite minimum",
                non_finite_minimum,
                None,
                "minimum",
            ),
            ("non-finite range", non_finite_range, None, "range"),
            ("zero range", zero_range, None, "range"),
            ("negative range", negative_range, None, "range"),
        ]

        for case_name, payload, expected_feature_names, expected_field in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    model_directory = Path(temporary_directory)
                    if payload is not None:
                        path = (
                            model_directory
                            / module.XGBOOST_PREPROCESSING_FILENAME
                        )
                        if isinstance(payload, bytes):
                            path.write_bytes(payload)
                        elif isinstance(payload, str):
                            path.write_text(payload, encoding="utf-8")
                        else:
                            path.write_text(json.dumps(payload), encoding="utf-8")

                    def load_artifact():
                        if expected_feature_names is None:
                            return module.load_xgboost_preprocessing(model_directory)
                        return module.load_xgboost_preprocessing(
                            model_directory, expected_feature_names
                        )

                    self.assert_artifact_error(expected_field, load_artifact)


if __name__ == "__main__":
    unittest.main()
