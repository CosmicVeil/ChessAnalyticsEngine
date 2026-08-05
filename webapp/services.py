"""Application services shared by Flask endpoints."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from confluent_kafka.admin import AdminClient
from sqlalchemy.orm import Session, sessionmaker


class AdapterHealthProbe:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        kafka_bootstrap_servers: str,
    ) -> None:
        self._sessions = sessions
        self._kafka_bootstrap_servers = kafka_bootstrap_servers

    def check(self) -> dict[str, str]:
        database = "ok"
        kafka = "ok"
        try:
            with self._sessions() as session:
                session.execute(sa.text("SELECT 1"))
        except Exception:
            database = "down"
        try:
            AdminClient(
                {"bootstrap.servers": self._kafka_bootstrap_servers}
            ).list_topics(timeout=1.0)
        except Exception:
            kafka = "down"
        return {"database": database, "kafka": kafka}


def health_view(probe: Any) -> tuple[dict[str, str], int]:
    states = probe.check()
    database = states.get("database", "down")
    kafka = states.get("kafka", "down")
    if database != "ok":
        status, code = "unavailable", 503
    elif kafka != "ok":
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    return {
        "status": status,
        "web": "ok",
        "database": database,
        "kafka": kafka,
    }, code


__all__ = ["AdapterHealthProbe", "health_view"]
