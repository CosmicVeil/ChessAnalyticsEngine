"""Score completed games in a process that imports XGBoost only."""

import json
from pathlib import Path
import sys

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from xgboost import XGBClassifier


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
if str(PIPELINE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIRECTORY))

MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from model_artifacts import MODELS_DIRECTORY, load_feature_schema
from model_preprocessing import load_xgboost_preprocessing
from kafka_delivery import publish_and_commit
from kafka_config import get_kafka_bootstrap_servers
from xgboost_scoring import score_completed_game


BOOTSTRAP_SERVERS = get_kafka_bootstrap_servers()


def load_model() -> XGBClassifier:
    model = XGBClassifier()
    model.load_model(MODELS_DIRECTORY / "xgboost_model.json")
    return model


def main() -> None:
    feature_schema = load_feature_schema()
    preprocessing = load_xgboost_preprocessing(
        MODELS_DIRECTORY,
        feature_schema,
    )
    model = load_model()
    consumer = None
    producer = None
    primary_error = None
    try:
        consumer = Consumer(
            {
                "bootstrap.servers": BOOTSTRAP_SERVERS,
                "group.id": "chess-xgboost-game-inference",
                "auto.offset.reset": "latest",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
            }
        )
        producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
        consumer.subscribe(["chess-game-features"])

        try:
            while True:
                message = consumer.poll(timeout=1.0)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(message.error())

                completed_game = json.loads(message.value().decode("utf-8"))
                # This row already summarizes all moves; XGBoost predicts once for the whole game.
                score = score_completed_game(
                    model,
                    completed_game["features"],
                    feature_schema,
                    preprocessing,
                )
                publish_and_commit(
                    producer,
                    consumer,
                    message,
                    "chess-model-predictions",
                    completed_game["game_id"],
                    json.dumps({
                        "game_id": completed_game["game_id"],
                        "model": "xgboost",
                        "score": score,
                    }),
                )
        except KeyboardInterrupt:
            print("\nStopped XGBoost game inference.")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = None
        if producer is not None:
            try:
                producer.flush()
            except BaseException as error:
                cleanup_error = error
        if consumer is not None:
            try:
                consumer.close()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


if __name__ == "__main__":
    main()
