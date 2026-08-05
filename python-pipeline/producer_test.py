#!/usr/bin/env python

from pathlib import Path
from random import choice
import sys

from confluent_kafka import Producer


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
if str(PIPELINE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIRECTORY))

from kafka_config import get_kafka_bootstrap_servers


BOOTSTRAP_SERVERS = get_kafka_bootstrap_servers()


if __name__ == '__main__':

    config = {
        # User-specific properties that you must set
        'bootstrap.servers': BOOTSTRAP_SERVERS,

        # Fixed properties
        'acks': 'all'
    }

    # Create Producer instance
    producer = Producer(config)

    # Optional per-message delivery callback (triggered by poll() or flush())
    # when a message has been successfully delivered or permanently
    # failed delivery (after retries).
    def delivery_callback(err, msg):
        if err:
            print('ERROR: Message failed delivery: {}'.format(err))
        else:
            print("Produced event to topic {topic}: key = {key:12} value = {value:12}".format(
                topic=msg.topic(), key=msg.key().decode('utf-8'), value=msg.value().decode('utf-8')))

    producer.produce("chess-moves")

    # Block until the messages are delivered.
    producer.flush()
