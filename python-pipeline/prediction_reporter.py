"""Print and publish one joined result for each completed chess game."""

import json
from pathlib import Path
import sys
import time

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
if str(PIPELINE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIRECTORY))

from prediction_reporting import PredictionReporter
from kafka_delivery import commit_after_processing, publish_and_commit


BOOTSTRAP_SERVERS = "localhost:19092"



def print_result(result: dict) -> None:
    print(
        f"Game: {result['game_id']} | "
        f"PyTorch: {result['pytorch_score']:.2%} | "
        f"XGBoost: {result['xgboost_score']:.2%}"
    )
    if result["pytorch_accuracy"] is None:
        print("Running accuracy: unavailable until a verified game label is available.")
    else:
        print(
            f"Running accuracy - PyTorch: {result['pytorch_accuracy']:.2%} | "
            f"XGBoost: {result['xgboost_accuracy']:.2%}"
        )


def main() -> None:

    start_time = time.perf_counter()
    end_time = time.perf_counter()

    total_time_taken = 0
    total_games= 0

    reporter = PredictionReporter()
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": "chess-prediction-reporter",
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
    )
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    consumer.subscribe(["chess-model-predictions"])

    try:
        while True:
            message = consumer.poll(timeout=1.0)
            if message is None:
                continue
            if message.error():
                if message.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(message.error())

            result = reporter.add_prediction(json.loads(message.value().decode("utf-8")))
            if result is not None:
                # The reporter prints only after both model workers scored the same whole game.

                end_time = time.perf_counter()

                print_result(result)
                print(f"Time Taken: {end_time-start_time:.6f}")

                total_time_taken += end_time-start_time
                total_games+=1
                start_time = time.perf_counter()


                publish_and_commit(
                    producer,
                    consumer,
                    message,
                    "chess-predictions",
                    result["game_id"],
                    json.dumps(result),
                )
            else:
                commit_after_processing(consumer, message)
    except KeyboardInterrupt:
        print("\nStopped prediction reporter.")
        print(f"Average Time: {total_time_taken/total_games:.6f}")
    finally:
        producer.flush()
        consumer.close()
        print(f"Average Time: {total_time_taken/total_games:.6f}")


if __name__ == "__main__":
    main()
