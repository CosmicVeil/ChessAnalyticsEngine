"""Score completed games in a process that imports PyTorch only."""

import json
from pathlib import Path
import sys

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
import pandas as pd
import torch


MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from model_artifacts import MODELS_DIRECTORY, build_pytorch_model, load_feature_schema
from kafka_delivery import publish_and_commit


BOOTSTRAP_SERVERS = "localhost:19092"


def load_model():
    checkpoint = torch.load(MODELS_DIRECTORY / "pytorch_model.pt", map_location="cpu")
    model = build_pytorch_model(checkpoint["input_size"])
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    return model


def main() -> None:
    feature_schema = load_feature_schema()
    model = load_model()
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": "chess-pytorch-game-inference",
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
            # This row already summarizes all moves; PyTorch predicts once for the whole game.
            features = pd.DataFrame([completed_game["features"]]).reindex(
                columns=feature_schema, fill_value=0.0
            )
            with torch.no_grad():
                score = float(model(torch.tensor(features.values, dtype=torch.float32)).item())
            publish_and_commit(
                producer,
                consumer,
                message,
                "chess-model-predictions",
                completed_game["game_id"],
                json.dumps({
                    "game_id": completed_game["game_id"],
                    "model": "pytorch",
                    "score": score,
                }),
            )
    except KeyboardInterrupt:
        print("\nStopped PyTorch game inference.")
    finally:
        producer.flush()
        consumer.close()


if __name__ == "__main__":
    main()
