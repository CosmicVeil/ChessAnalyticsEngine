"""Score completed games in a process that imports XGBoost only."""

import json
from pathlib import Path
import sys

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
import pandas as pd
from xgboost import XGBClassifier


MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from model_artifacts import MODELS_DIRECTORY, load_feature_schema
from kafka_delivery import publish_and_commit


BOOTSTRAP_SERVERS = "localhost:19092"


def load_model() -> XGBClassifier:
    model = XGBClassifier()
    model.load_model(MODELS_DIRECTORY / "xgboost_model.json")
    return model


def main() -> None:
    feature_schema = load_feature_schema()
    model = load_model()
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
            features = pd.DataFrame([completed_game["features"]]).reindex(
                columns=feature_schema, fill_value=0.0
            )
            score = float(model.predict_proba(features)[0, 1])
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
    finally:
        producer.flush()
        consumer.close()


if __name__ == "__main__":
    main()
