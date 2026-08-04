"""Shared Kafka broker configuration for Python pipeline entry points."""

import os
from collections.abc import Mapping


DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:19092"


def get_kafka_bootstrap_servers(
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    value = source.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    return value or DEFAULT_KAFKA_BOOTSTRAP_SERVERS
