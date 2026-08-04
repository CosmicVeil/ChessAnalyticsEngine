"""Validated PyTorch scoring for one completed game."""

import math
from pathlib import Path
import sys

import torch
import torch.nn.functional as F


MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from model_preprocessing import feature_schema_fingerprint


def validate_checkpoint_metadata(checkpoint, feature_schema) -> None:
    """Reject a checkpoint that cannot consume the ordered feature schema."""
    expected_input_size = len(feature_schema)
    input_size = checkpoint.get("input_size")
    if type(input_size) is not int or input_size != expected_input_size:
        raise ValueError(
            f"input_size: expected {expected_input_size}, found {input_size!r}"
        )

    has_preprocessing = "preprocessing" in checkpoint
    has_fingerprint = "feature_schema_sha256" in checkpoint
    if not has_preprocessing and not has_fingerprint:
        return
    if not has_preprocessing:
        raise ValueError("preprocessing: required with feature_schema_sha256")
    if not has_fingerprint:
        raise ValueError("feature_schema_sha256: required with preprocessing")
    if checkpoint["preprocessing"] != "l2":
        raise ValueError(
            "preprocessing: expected 'l2', "
            f"found {checkpoint['preprocessing']!r}"
        )

    expected_fingerprint = feature_schema_fingerprint(feature_schema)
    if checkpoint["feature_schema_sha256"] != expected_fingerprint:
        raise ValueError("feature_schema_sha256: does not match feature schema")


def score_completed_game(model, feature_values, feature_schema) -> float:
    """Align, normalize, and convert one model logit into a bounded score."""
    try:
        ordered_values = [
            float(feature_values.get(feature_name, 0.0))
            for feature_name in feature_schema
        ]
        raw = torch.tensor([ordered_values], dtype=torch.float32)
    except (OverflowError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError(
            "feature values must be finite float32 numbers"
        ) from error
    if not bool(torch.isfinite(raw).all()):
        raise ValueError("feature values must be finite float32 numbers")
    normalized = F.normalize(raw, p=2.0, dim=1)

    with torch.inference_mode():
        probability = torch.sigmoid(model(normalized)).reshape(-1)

    if probability.numel() != 1:
        raise ValueError("model output must contain exactly one score")
    score = float(probability.item())
    if not math.isfinite(score):
        raise ValueError("model score must be finite")
    if not 0.0 <= score <= 1.0:
        raise ValueError("model score must be between zero and one")
    return score
