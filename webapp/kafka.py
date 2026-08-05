"""Narrow Kafka adapters used by the independent web workers."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from confluent_kafka import Consumer, Producer


@runtime_checkable
class MovePublisher(Protocol):
    def publish_game(
        self, game_id: str, events: Iterable[Mapping[str, object]]
    ) -> None: ...


@runtime_checkable
class ResultConsumer(Protocol):
    def poll(self, timeout: float = 1.0) -> object | None: ...
    def commit(self, message: object) -> None: ...
    def close(self) -> None: ...


class KafkaMovePublisher:
    def __init__(
        self,
        bootstrap_servers: str,
        *,
        producer: Any | None = None,
        topic: str = "chess-moves",
        flush_timeout: float = 30.0,
    ) -> None:
        self._producer = producer or Producer(
            {"bootstrap.servers": bootstrap_servers}
        )
        self._topic = topic
        self._flush_timeout = flush_timeout

    def publish_game(
        self, game_id: str, events: Iterable[Mapping[str, object]]
    ) -> None:
        for event in events:
            self._producer.produce(
                topic=self._topic,
                key=game_id,
                value=json.dumps(dict(event), separators=(",", ":")),
            )
            self._producer.poll(0)
        if self._producer.flush(self._flush_timeout):
            raise TimeoutError(f"Kafka did not flush game {game_id}")

    def close(self) -> None:
        self._producer.flush(self._flush_timeout)


class KafkaResultConsumer:
    def __init__(
        self,
        bootstrap_servers: str,
        group_id: str,
        *,
        consumer: Any | None = None,
        topic: str = "chess-predictions",
    ) -> None:
        self._consumer = consumer or Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
            }
        )
        self._consumer.subscribe([topic])

    def poll(self, timeout: float = 1.0) -> object | None:
        return self._consumer.poll(timeout)

    def commit(self, message: object) -> None:
        self._consumer.commit(message=message, asynchronous=False)

    def close(self) -> None:
        self._consumer.close()


__all__ = [
    "KafkaMovePublisher",
    "KafkaResultConsumer",
    "MovePublisher",
    "ResultConsumer",
]
