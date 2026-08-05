"""Kafka adapter contracts without a broker."""

import json
import unittest

from webapp.kafka import KafkaMovePublisher


class FakeProducer:
    def __init__(self, remaining=0):
        self.records = []
        self.remaining = remaining
        self.polls = []

    def produce(self, **kwargs):
        self.records.append(kwargs)

    def poll(self, timeout):
        self.polls.append(timeout)

    def flush(self, timeout=None):
        return self.remaining


class KafkaMovePublisherTests(unittest.TestCase):
    def test_publishes_ordered_json_under_one_key_and_flushes(self):
        producer = FakeProducer()
        publisher = KafkaMovePublisher("unused:9092", producer=producer)
        events = [
            {"game_id": "web:j:1", "move": "e4", "move_number": 1},
            {"game_id": "web:j:1", "move": "e5", "move_number": 2},
        ]

        publisher.publish_game("web:j:1", events)

        self.assertEqual([row["topic"] for row in producer.records], ["chess-moves"] * 2)
        self.assertEqual([row["key"] for row in producer.records], ["web:j:1"] * 2)
        self.assertEqual([json.loads(row["value"]) for row in producer.records], events)
        self.assertEqual(producer.polls, [0, 0])

    def test_incomplete_flush_raises(self):
        publisher = KafkaMovePublisher("unused:9092", producer=FakeProducer(1))
        with self.assertRaises(TimeoutError):
            publisher.publish_game("web:j:1", [{"move": "e4"}])


if __name__ == "__main__":
    unittest.main()
