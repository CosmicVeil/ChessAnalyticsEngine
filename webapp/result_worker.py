"""Consume joined model scores and durably advance browser-visible jobs."""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

from webapp.config import Settings
from webapp.database import create_session_factory
from webapp.domain import AnalysisStore, JoinedResult, StoreResultOutcome
from webapp.kafka import KafkaResultConsumer, ResultConsumer
from webapp.repository import SqlAlchemyAnalysisStore


LOGGER = logging.getLogger(__name__)
TYPED_FIELDS = {
    "game_id",
    "pytorch_score",
    "xgboost_score",
    "pytorch_prediction",
    "xgboost_prediction",
}


class ResultDisposition(str, Enum):
    STORED = "stored"
    REPLAYED = "replayed"
    UNMATCHED = "unmatched"
    IGNORED = "ignored"


class ResultWorker:
    def __init__(self, store: AnalysisStore, consumer: ResultConsumer) -> None:
        self.store = store
        self.consumer = consumer

    def process(self, message: object) -> ResultDisposition:
        payload = _payload(message)
        game_id = str(payload.get("game_id", ""))
        if not game_id.startswith("web:"):
            self.consumer.commit(message)
            return ResultDisposition.IGNORED

        joined = JoinedResult(
            pipeline_game_id=game_id,
            pytorch_score=_score(payload, "pytorch_score"),
            xgboost_score=_score(payload, "xgboost_score"),
            pytorch_prediction=_prediction(payload, "pytorch_prediction"),
            xgboost_prediction=_prediction(payload, "xgboost_prediction"),
            model_payload={
                key: value for key, value in payload.items()
                if key not in TYPED_FIELDS
            },
        )
        outcome = self.store.store_joined_result(joined)
        if outcome is StoreResultOutcome.UNMATCHED:
            self.store.store_unmatched_result(message, payload)
            disposition = ResultDisposition.UNMATCHED
        elif outcome is StoreResultOutcome.REPLAYED:
            disposition = ResultDisposition.REPLAYED
        else:
            disposition = ResultDisposition.STORED
        self.consumer.commit(message)
        return disposition

    def run_forever(self) -> None:
        while True:
            message = self.consumer.poll(1.0)
            if message is None:
                continue
            error = getattr(message, "error", lambda: None)()
            if error:
                raise RuntimeError(str(error))
            self.process(message)


def _payload(message: object) -> dict[str, Any]:
    raw = getattr(message, "value")()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("joined prediction must be a JSON object")
    return value


def _score(payload: dict[str, Any], field: str) -> float:
    value = float(payload[field])
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return value


def _prediction(payload: dict[str, Any], field: str) -> bool:
    value = payload[field]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{field} must be boolean or zero/one")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    store = SqlAlchemyAnalysisStore(
        create_session_factory(settings.database_url)
    )
    consumer = KafkaResultConsumer(
        settings.kafka_bootstrap_servers,
        settings.kafka_web_result_group,
    )
    worker = ResultWorker(store, consumer)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        LOGGER.info("result worker stopped")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
