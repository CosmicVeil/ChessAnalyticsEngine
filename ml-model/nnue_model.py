"""NNUE-style game classifier shared by training, comparison, and live inference.

The design borrows the parts of a chess-engine NNUE that make it efficient:

* Sparse input features. Every move switches on a handful of one-hot
  features: one quantile bucket per move statistic, plus a "game phase x
  bucket" cross (the analogue of HalfKP's king-square x piece-square cross).
* A linear accumulator. The first layer is a plain weight matrix, so the
  accumulator for a game is just the sum of the weight columns of its active
  features. Each new move adds its columns; nothing is recomputed. That is the
  "efficiently updatable" part, see ``NNUEAccumulator``.
* A small clipped-ReLU head (accumulator -> 32 -> 32 -> 1), the same layer
  stack shape that classic NNUEs use, which keeps activations in [0, 1].

Because the accumulator is linear, the mean-pooled accumulator equals the
normalized per-game histogram of active features multiplied by the weight
matrix. Training uses that histogram form because it is fast to batch;
``NNUEAccumulator`` uses the incremental form and produces the same numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import torch
import torch.nn as nn

from model_preprocessing import feature_schema_fingerprint


NNUE_MODEL_FILENAME = "nnue_model.pt"
NNUE_FORMAT = "chess-nnue"
NNUE_VERSION = 1
PHASE_COLUMN = "move_number"


class ClippedReLU(nn.Module):
    """NNUE activation: clamp to [0, 1] so values stay quantization friendly."""

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return values.clamp(0.0, 1.0)


def signed_log(values: np.ndarray) -> np.ndarray:
    """Compress heavy-tailed counts while keeping sign, e.g. time deltas."""
    return np.sign(values) * np.log1p(np.abs(values))


@dataclass
class NNUEFeatureSpec:
    """Everything needed to turn moves and game features into network inputs."""

    move_columns: list[str]
    bucket_columns: list[str]
    bucket_edges: list[list[float]]
    feature_schema: list[str]
    dense_mean: list[float]
    dense_std: list[float]
    phase_length: int = 10
    phase_count: int = 8
    # 0 disables transitions; 2 pairs each move with the same player's last move.
    transition_lag: int = 0

    @classmethod
    def fit(
        cls,
        moves: np.ndarray,
        move_columns: Sequence[str],
        game_features: np.ndarray,
        feature_schema: Sequence[str],
        bucket_count: int = 16,
        phase_length: int = 10,
        phase_count: int = 8,
        transition_lag: int = 0,
    ) -> "NNUEFeatureSpec":
        """Learn bucket edges and dense scaling from training rows only."""
        move_columns = list(move_columns)
        if PHASE_COLUMN not in move_columns:
            raise ValueError(f"move columns must include {PHASE_COLUMN!r}")
        quantiles = np.linspace(0.0, 1.0, bucket_count + 1)[1:-1]
        bucket_columns, bucket_edges = [], []
        for position, column in enumerate(move_columns):
            values = moves[:, position]
            # Constant columns cannot separate anything; skip them entirely.
            if column == PHASE_COLUMN or values.std() == 0:
                continue
            bucket_columns.append(column)
            bucket_edges.append(np.unique(np.quantile(values, quantiles)).tolist())

        dense = signed_log(np.asarray(game_features, dtype="float64"))
        dense_std = dense.std(axis=0)
        dense_std[dense_std == 0] = 1.0
        return cls(
            move_columns=move_columns,
            bucket_columns=bucket_columns,
            bucket_edges=bucket_edges,
            feature_schema=list(feature_schema),
            dense_mean=dense.mean(axis=0).tolist(),
            dense_std=dense_std.tolist(),
            phase_length=phase_length,
            phase_count=phase_count,
            transition_lag=transition_lag,
        )

    @property
    def bucket_sizes(self) -> list[int]:
        return [len(edges) + 1 for edges in self.bucket_edges]

    @property
    def features_per_move(self) -> int:
        # Plain bucket, phase-crossed bucket, and optional transition per column.
        blocks = 3 if self.transition_lag else 2
        return blocks * len(self.bucket_columns)

    @property
    def sparse_size(self) -> int:
        sizes = self.bucket_sizes
        size = sum(sizes) + self.phase_count * sum(sizes)
        if self.transition_lag:
            # The extra previous-bucket slot means "no earlier move yet".
            size += sum((bucket + 1) * bucket for bucket in sizes)
        return size

    @property
    def dense_size(self) -> int:
        return len(self.feature_schema)

    def active_indices(
        self, moves: np.ndarray, game_lengths: np.ndarray | None = None
    ) -> np.ndarray:
        """Return the active sparse feature indices for each move row.

        ``moves`` may hold several games back to back; ``game_lengths`` marks
        where each game starts so transitions never cross a game boundary.
        Without it, every row is treated as one game.
        """
        moves = np.asarray(moves, dtype="float32")
        if moves.ndim != 2 or moves.shape[1] != len(self.move_columns):
            raise ValueError(
                f"moves: expected shape (n, {len(self.move_columns)}), "
                f"found {moves.shape}"
            )
        phase_position = self.move_columns.index(PHASE_COLUMN)
        phase = np.clip(
            (moves[:, phase_position] - 1) // self.phase_length,
            0,
            self.phase_count - 1,
        ).astype(np.int64)

        buckets = []
        for column, edges in zip(self.bucket_columns, self.bucket_edges):
            values = moves[:, self.move_columns.index(column)]
            buckets.append(np.searchsorted(edges, values, side="right"))

        indices, offset = [], 0
        for bucket, size in zip(buckets, self.bucket_sizes):
            indices.append(offset + bucket)
            offset += size
        for bucket, size in zip(buckets, self.bucket_sizes):
            indices.append(offset + phase * size + bucket)
            offset += self.phase_count * size
        if self.transition_lag:
            lag = self.transition_lag
            lengths = (
                np.asarray([len(moves)])
                if game_lengths is None
                else np.asarray(game_lengths, dtype=np.int64)
            )
            starts = np.repeat(np.cumsum(lengths) - lengths, lengths)
            has_previous = (np.arange(len(moves)) - starts) >= lag
            for bucket, size in zip(buckets, self.bucket_sizes):
                previous = np.full_like(bucket, size)
                previous[lag:] = bucket[:-lag] if len(bucket) > lag else previous[lag:]
                previous = np.where(has_previous, previous, size)
                indices.append(offset + previous * size + bucket)
                offset += (size + 1) * size
        return np.stack(indices, axis=1).astype(np.int64)

    def histograms(self, moves: np.ndarray, game_lengths: np.ndarray) -> np.ndarray:
        """Per-game mean count of each sparse feature (the batched accumulator input)."""
        game_lengths = np.asarray(game_lengths, dtype=np.int64)
        if np.any(game_lengths <= 0):
            raise ValueError("every game must contain at least one move")
        indices = self.active_indices(moves, game_lengths)
        game_of_move = np.repeat(np.arange(len(game_lengths)), game_lengths)
        histogram = np.zeros((len(game_lengths), self.sparse_size), dtype="float32")
        np.add.at(
            histogram,
            (np.repeat(game_of_move, indices.shape[1]), indices.ravel()),
            1.0,
        )
        return histogram / game_lengths[:, None].astype("float32")

    def dense_features(self, game_features: np.ndarray) -> np.ndarray:
        values = signed_log(np.asarray(game_features, dtype="float64"))
        return ((values - self.dense_mean) / self.dense_std).astype("float32")

    def to_dict(self) -> dict:
        return {
            "move_columns": self.move_columns,
            "bucket_columns": self.bucket_columns,
            "bucket_edges": self.bucket_edges,
            "feature_schema": self.feature_schema,
            "dense_mean": self.dense_mean,
            "dense_std": self.dense_std,
            "phase_length": self.phase_length,
            "phase_count": self.phase_count,
            "transition_lag": self.transition_lag,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "NNUEFeatureSpec":
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__})


class NNUENetwork(nn.Module):
    """Sparse accumulator plus a dense game-feature branch feeding a CReLU head."""

    def __init__(
        self,
        sparse_size: int,
        dense_size: int,
        accumulator_size: int = 256,
        hidden_size: int = 32,
        dropout: float = 0.3,
    ) -> None:
        super().__init__()
        # bias=False: the dense branch's bias already plays that role.
        self.accumulator = nn.Linear(sparse_size, accumulator_size, bias=False)
        self.dense = nn.Linear(dense_size, accumulator_size)
        self.head = nn.Sequential(
            ClippedReLU(),
            nn.Dropout(dropout),
            nn.Linear(accumulator_size, hidden_size),
            ClippedReLU(),
            nn.Linear(hidden_size, hidden_size),
            ClippedReLU(),
            nn.Linear(hidden_size, 1),
        )

    def forward(self, histograms: torch.Tensor, dense: torch.Tensor) -> torch.Tensor:
        return self.from_accumulator(self.accumulator(histograms), dense)

    def from_accumulator(
        self, accumulator: torch.Tensor, dense: torch.Tensor
    ) -> torch.Tensor:
        """Finish the forward pass from an already-built accumulator."""
        return self.head(accumulator + self.dense(dense))


@dataclass
class NNUEEnsemble:
    """Several independently seeded networks whose logits are averaged."""

    spec: NNUEFeatureSpec
    members: list[NNUENetwork]
    decision_offset: float = 0.0

    def logits(self, histograms: np.ndarray, dense: np.ndarray) -> np.ndarray:
        histogram_tensor = torch.as_tensor(histograms, dtype=torch.float32)
        dense_tensor = torch.as_tensor(dense, dtype=torch.float32)
        with torch.inference_mode():
            stacked = torch.stack(
                [member(histogram_tensor, dense_tensor) for member in self.members]
            )
        return stacked.mean(dim=0).reshape(-1).numpy() - self.decision_offset

    def scores(self, histograms: np.ndarray, dense: np.ndarray) -> np.ndarray:
        """Bounded scores whose 0.5 cutoff applies the validation-tuned threshold."""
        return 1.0 / (1.0 + np.exp(-self.logits(histograms, dense)))


class NNUEAccumulator:
    """Incrementally updated accumulator for one game, one move at a time."""

    def __init__(self, ensemble: NNUEEnsemble) -> None:
        self.ensemble = ensemble
        width = ensemble.members[0].accumulator.out_features
        self.sums = torch.zeros(len(ensemble.members), width)
        self.move_count = 0
        # Only the last ``transition_lag`` moves are needed for transitions.
        self.recent_moves: list[list[float]] = []

    def push_move(self, move: Sequence[float]) -> None:
        """Add one move's active weight columns; no other work is redone."""
        lag = self.ensemble.spec.transition_lag
        # Early in a game the window is shorter than lag + 1, so the last row
        # correctly sees "no earlier move" for its transition features.
        window = (self.recent_moves + [list(move)])[-(lag + 1):]
        active = self.ensemble.spec.active_indices(np.asarray(window))[-1]
        self.recent_moves = window[-lag:] if lag else []
        active_tensor = torch.as_tensor(active)
        with torch.inference_mode():
            for position, member in enumerate(self.ensemble.members):
                weight = member.accumulator.weight
                self.sums[position] += weight.index_select(1, active_tensor).sum(dim=1)
        self.move_count += 1

    def score(self, game_features: Sequence[float]) -> float:
        if self.move_count == 0:
            raise ValueError("cannot score a game with no moves")
        dense = torch.as_tensor(
            self.ensemble.spec.dense_features(np.asarray([game_features])),
            dtype=torch.float32,
        )
        with torch.inference_mode():
            logits = [
                member.from_accumulator(
                    (self.sums[position] / self.move_count).unsqueeze(0), dense
                )
                for position, member in enumerate(self.ensemble.members)
            ]
        logit = float(torch.stack(logits).mean()) - self.ensemble.decision_offset
        return 1.0 / (1.0 + np.exp(-logit))


def build_checkpoint(
    ensemble: NNUEEnsemble,
    accumulator_size: int,
    hidden_size: int,
    metrics: Mapping[str, float] | None = None,
) -> dict:
    return {
        "format": NNUE_FORMAT,
        "version": NNUE_VERSION,
        "feature_schema_sha256": feature_schema_fingerprint(
            ensemble.spec.feature_schema
        ),
        "spec": ensemble.spec.to_dict(),
        "accumulator_size": accumulator_size,
        "hidden_size": hidden_size,
        "decision_offset": float(ensemble.decision_offset),
        "members": [member.state_dict() for member in ensemble.members],
        "metrics": dict(metrics or {}),
    }


def load_ensemble(checkpoint: Mapping, feature_schema: Sequence[str]) -> NNUEEnsemble:
    """Validate a checkpoint against the live feature schema and rebuild it."""
    if checkpoint.get("format") != NNUE_FORMAT:
        raise ValueError(f"format: expected {NNUE_FORMAT!r}")
    if checkpoint.get("version") != NNUE_VERSION:
        raise ValueError(
            f"version: expected {NNUE_VERSION}, found {checkpoint.get('version')!r}"
        )
    if checkpoint.get("feature_schema_sha256") != feature_schema_fingerprint(
        list(feature_schema)
    ):
        raise ValueError("feature_schema_sha256: does not match feature schema")
    members = checkpoint.get("members")
    if not members:
        raise ValueError("members: expected at least one network")

    spec = NNUEFeatureSpec.from_dict(checkpoint["spec"])
    networks = []
    for state_dict in members:
        network = NNUENetwork(
            spec.sparse_size,
            spec.dense_size,
            checkpoint["accumulator_size"],
            checkpoint["hidden_size"],
        )
        network.load_state_dict(state_dict)
        network.eval()
        networks.append(network)
    return NNUEEnsemble(spec, networks, float(checkpoint["decision_offset"]))
