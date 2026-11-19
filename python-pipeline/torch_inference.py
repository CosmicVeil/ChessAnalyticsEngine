"""Score completed games in a process that imports PyTorch only."""

import json
from pathlib import Path
import sys

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
import torch


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
if str(PIPELINE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIRECTORY))

MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from model_artifacts import MODELS_DIRECTORY, build_pytorch_model, load_feature_schema
from kafka_delivery import publish_and_commit
from kafka_config import get_kafka_bootstrap_servers
from torch_scoring import score_completed_game, validate_checkpoint_metadata


BOOTSTRAP_SERVERS = get_kafka_bootstrap_servers()


def load_model(feature_schema):
    checkpoint = torch.load(MODELS_DIRECTORY / "pytorch_model.pt", map_location="cpu")
    validate_checkpoint_metadata(checkpoint, feature_schema)
    model = build_pytorch_model(checkpoint["input_size"])
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    return model


def main() -> None:
    feature_schema = load_feature_schema()
    model = load_model(feature_schema)
    consumer = None
    producer = None
    primary_error = None
    try:
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
                score = score_completed_game(
                    model, completed_game["features"], feature_schema
                )
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
                        "engine": completed_game.get("engine", "xgboost"),
                    }),
                )
        except KeyboardInterrupt:
            print("\nStopped PyTorch game inference.")
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
