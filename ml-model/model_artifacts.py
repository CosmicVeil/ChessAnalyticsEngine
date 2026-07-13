"""Paths and helpers shared by model training and live inference."""

import json
from pathlib import Path


MODELS_DIRECTORY = Path(__file__).with_name("models")
FEATURE_SCHEMA_FILENAME = "feature_schema.json"


def save_feature_schema(
    columns: list[str], model_directory: Path = MODELS_DIRECTORY
) -> Path:
    """Persist the ordered columns that every model prediction must receive."""
    model_directory.mkdir(parents=True, exist_ok=True)
    schema_path = model_directory / FEATURE_SCHEMA_FILENAME
    schema_path.write_text(json.dumps(columns), encoding="utf-8")
    return schema_path


def load_feature_schema(model_directory: Path = MODELS_DIRECTORY) -> list[str]:
    """Load the ordered feature schema saved alongside the trained models."""
    schema_path = model_directory / FEATURE_SCHEMA_FILENAME
    return json.loads(schema_path.read_text(encoding="utf-8"))


def model_artifact_path(
    filename: str, model_directory: Path = MODELS_DIRECTORY
) -> Path:
    """Return a model-file path inside the directory that holds all artifacts."""
    model_directory.mkdir(parents=True, exist_ok=True)
    return model_directory / filename


def build_pytorch_model(input_size: int):
    """Recreate the PyTorch classifier architecture for training or inference."""
    import torch.nn as nn

    return nn.Sequential(
        nn.Linear(input_size, 60),
        nn.ReLU(),
        nn.Linear(60, 30),
        nn.ReLU(),
        nn.Linear(30, 1),
        nn.Sigmoid(),
    )
