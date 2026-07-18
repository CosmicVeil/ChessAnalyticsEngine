"""Kafka delivery helpers that acknowledge input only after it is handled."""


def commit_after_processing(consumer, message) -> None:
    """Synchronously acknowledge an input message after its local work succeeds."""
    consumer.commit(message=message, asynchronous=False)


def publish_and_commit(producer, consumer, message, topic: str, key: str, value: str) -> None:
    """Publish an output event, wait for broker delivery, then acknowledge its input."""
    producer.produce(topic, key=key, value=value)
    undelivered_messages = producer.flush()
    if undelivered_messages:
        raise RuntimeError(f"{undelivered_messages} Kafka messages were not delivered")
    commit_after_processing(consumer, message)
