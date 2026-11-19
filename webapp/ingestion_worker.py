"""Lease-based worker that streams spooled PGNs into the core pipeline."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from webapp.config import Settings
from webapp.database import create_session_factory
from webapp.domain import AnalysisStore, InvalidGame
from webapp.kafka import KafkaMovePublisher, MovePublisher
from webapp.pgn import build_move_events, iter_pgn_games
from webapp.repository import SqlAlchemyAnalysisStore


LOGGER = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(
        self,
        store: AnalysisStore,
        publisher: MovePublisher,
        *,
        worker_id: str | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        self.store = store
        self.publisher = publisher
        self.worker_id = worker_id or f"web-ingest-{uuid.uuid4()}"
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay

    def process_one(self, now: datetime | None = None) -> bool:
        started_at = now or datetime.now(UTC)
        claim = self.store.claim_next_job(
            self.worker_id, started_at, self.lease_duration
        )
        if claim is None:
            return False

        try:
            with claim.spool_path.open(encoding="utf-8") as stream:
                for parsed in iter_pgn_games(stream):
                    boundary = now or datetime.now(UTC)
                    if not self.store.renew_claim(
                        claim.id,
                        self.worker_id,
                        boundary,
                        self.lease_duration,
                    ):
                        raise RuntimeError("ingestion lease was lost")
                    if isinstance(parsed, InvalidGame):
                        self.store.record_invalid_game(claim.id, parsed)
                        continue
                    game = self.store.upsert_parsed_game(claim.id, parsed)
                    self.publisher.publish_game(
                        game.pipeline_game_id,
                        build_move_events(parsed, game.pipeline_game_id, claim.engine),
                    )
                    self.store.mark_game_published(game.id)

            self.store.finish_ingestion(claim.id, self.worker_id)
            claim.spool_path.unlink(missing_ok=True)
        except Exception as error:
            retry_at = (now or datetime.now(UTC)) + self.retry_delay
            self.store.schedule_retry(
                claim.id, self.worker_id, str(error), retry_at
            )
            LOGGER.exception("analysis ingestion failed for job %s", claim.id)
        return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    store = SqlAlchemyAnalysisStore(
        create_session_factory(settings.database_url)
    )
    publisher = KafkaMovePublisher(settings.kafka_bootstrap_servers)
    worker = IngestionWorker(store, publisher)
    try:
        while True:
            if not worker.process_one():
                time.sleep(0.5)
    except KeyboardInterrupt:
        LOGGER.info("ingestion worker stopped")
    finally:
        publisher.close()


if __name__ == "__main__":
    main()
