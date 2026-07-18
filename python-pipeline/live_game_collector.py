"""Publish one schema-aligned feature vector for each completed streamed game."""

import json
from pathlib import Path
import sys

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
if str(PIPELINE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIRECTORY))

MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from model_artifacts import load_feature_schema
from live_scoring import GameFeatureCollector
from kafka_delivery import commit_after_processing, publish_and_commit


BOOTSTRAP_SERVERS = "localhost:19092"


def main() -> None:
    feature_schema = load_feature_schema()
    # Store move events only until the game-complete event turns them into one game vector.
    collector = GameFeatureCollector(feature_schema)
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": "chess-game-feature-collector",
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
    )
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    consumer.subscribe(["chess-features"])

    try:
        while True:
            message = consumer.poll(timeout=1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(message.error())

            event = json.loads(message.value().decode("utf-8"))
            game_id = event.get("game_id")
            if event.get("game_complete"):
                completed_game = collector.prepare_completed_game(game_id)
                if completed_game is not None:
                    publish_and_commit(
                        producer,
                        consumer,
                        message,
                        "chess-game-features",
                        game_id,
                        json.dumps(completed_game),
                    )
                    collector.discard_game(game_id)
                else:
                    commit_after_processing(consumer, message)
            else:
                collector.add_feature(event)
                commit_after_processing(consumer, message)
    except KeyboardInterrupt:
        print("\nStopped game feature collector.")
    finally:
        producer.flush()
        consumer.close()


if __name__ == "__main__":
    main()
