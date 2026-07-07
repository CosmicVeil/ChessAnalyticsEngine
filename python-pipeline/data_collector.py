import json
import os
import sys
from confluent_kafka import Consumer, KafkaException, KafkaError
from pathlib import Path
import csv

folder_path = Path(os.path.dirname(os.getcwd()) + '/ml-model/')
file_path = "chess_dataset.csv"

full_path = folder_path / file_path

def create_consumer():

    config = {
        'bootstrap.servers': 'localhost:19092',
        'group.id': 'chess-features-consumer',
        'auto.offset.reset': 'earliest',
        'enable.auto.commit': True,
        'auto.commit.interval.ms': 5000
    }

    return Consumer(config)

def main():
    consumer = create_consumer()

    topics = ['chess-features']

    consumer.subscribe(topics)

    full_path.unlink(missing_ok=True)

    try:
        while True:

            msg = consumer.poll(timeout=1.0)

            if msg is None:
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    print(f"Reached end of partition: {msg.topic()} [{msg.partition()}]")

                else:
                    raise KafkaException(msg.error)

            else:
                key = msg.key().decode('utf-8') if msg.key() else None
                value = msg.value().decode('utf-8') if msg.value() else None

                if  not full_path.is_file():
                    with open(full_path, "w", newline="") as file:
                        writer = csv.writer(file)
                        writer.writerow(["GameID", "Features", "Cheating"])

                with open(full_path, mode="a", newline="", encoding="utf-8") as file:

                    data_dict = json.loads(msg.value())
                    data_dict["GameID"] = key

                    if "cheating" in key.lower():
                        data_dict["Cheating"] = 1
                    else:
                        data_dict["Cheating"] = 0


                    writer = csv.writer(file)
                    writer.writerows(data_dict)

    except KeyboardInterrupt:
        print("\nAborted by user.")

    finally:
        # 6. Clean up connections and trigger clean rebalancing/final commits
        print("Closing consumer...")
        consumer.close()

if __name__ == '__main__':
    main()



