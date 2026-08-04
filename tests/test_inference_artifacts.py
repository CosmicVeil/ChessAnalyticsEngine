import json
import math
import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

XGBOOST_SMOKE_SCRIPT = r"""
import json
from pathlib import Path
import sys

repository_root = Path(sys.argv[1])
sys.path[:0] = [
    str(repository_root / "ml-model"),
    str(repository_root / "python-pipeline"),
]

from model_artifacts import MODELS_DIRECTORY, load_feature_schema
from model_preprocessing import load_xgboost_preprocessing
from xgboost import XGBClassifier
from xgboost_scoring import score_completed_game

feature_schema = load_feature_schema(MODELS_DIRECTORY)
preprocessing = load_xgboost_preprocessing(
    MODELS_DIRECTORY,
    feature_schema,
)
model = XGBClassifier()
model.load_model(MODELS_DIRECTORY / "xgboost_model.json")
score = score_completed_game(
    model,
    {feature_name: 0.0 for feature_name in feature_schema},
    feature_schema,
    preprocessing,
)

if "torch" in sys.modules:
    raise RuntimeError("XGBoost smoke process imported PyTorch")
print(json.dumps(score))
"""

PYTORCH_SMOKE_SCRIPT = r"""
import json
from pathlib import Path
import sys

repository_root = Path(sys.argv[1])
sys.path[:0] = [
    str(repository_root / "ml-model"),
    str(repository_root / "python-pipeline"),
]

import torch

from model_artifacts import (
    MODELS_DIRECTORY,
    build_pytorch_model,
    load_feature_schema,
)
from torch_scoring import score_completed_game, validate_checkpoint_metadata

feature_schema = load_feature_schema(MODELS_DIRECTORY)
checkpoint = torch.load(
    MODELS_DIRECTORY / "pytorch_model.pt",
    map_location="cpu",
)
validate_checkpoint_metadata(checkpoint, feature_schema)
model = build_pytorch_model(checkpoint["input_size"])
model.load_state_dict(checkpoint["state_dict"])
model.eval()
score = score_completed_game(
    model,
    {feature_name: 0.0 for feature_name in feature_schema},
    feature_schema,
)

if "xgboost" in sys.modules:
    raise RuntimeError("PyTorch smoke process imported XGBoost")
print(json.dumps(score))
"""


class InferenceArtifactSmokeTests(unittest.TestCase):
    def run_isolated_score(self, script):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                str(REPOSITORY_ROOT),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "isolated inference failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}",
        )
        try:
            score = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                "isolated inference must print exactly one JSON score; "
                f"stdout was {completed.stdout!r}: {error}"
            )
        self.assertIs(type(score), float)
        self.assertTrue(math.isfinite(score))
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        return score

    def test_real_xgboost_artifacts_score_in_xgboost_only_process(self):
        self.run_isolated_score(XGBOOST_SMOKE_SCRIPT)

    def test_real_pytorch_artifact_scores_in_pytorch_only_process(self):
        self.run_isolated_score(PYTORCH_SMOKE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
