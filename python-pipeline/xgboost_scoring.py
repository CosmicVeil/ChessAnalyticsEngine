"""Numerical scoring behavior shared by XGBoost inference entry points."""

import numpy as np

from model_preprocessing import transform_xgboost_features


def score_completed_game(
    model,
    feature_values,
    feature_schema,
    preprocessing,
) -> float:
    """Transform one completed game and return its positive-class score."""
    transformed = transform_xgboost_features(
        feature_values,
        feature_schema,
        preprocessing,
    )
    probabilities = np.asarray(model.predict_proba(transformed))

    if probabilities.shape != (1, 2):
        raise ValueError("predict_proba must return one negative/positive pair")
    if not np.issubdtype(probabilities.dtype, np.number) or np.iscomplexobj(
        probabilities
    ):
        raise ValueError("predict_proba probabilities must be real numbers")
    if not np.isfinite(probabilities).all():
        raise ValueError("predict_proba probabilities must be finite")
    if np.any((probabilities < 0.0) | (probabilities > 1.0)):
        raise ValueError("predict_proba probabilities must be between zero and one")

    return float(probabilities[0, 1])
