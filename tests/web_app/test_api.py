"""Flask submission, status, query, and health API contracts."""

import io
import tempfile
import unittest
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import WebDependencies
from webapp.spool import SpoolService


PGN = '[White "Ada"]\n[Black "Grace"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'


class HealthProbe:
    def __init__(self, database="ok", kafka="ok"):
        self.database = database
        self.kafka = kafka

    def check(self):
        return {"database": self.database, "kafka": self.kafka}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FakeAnalysisStore()
        self.spool = SpoolService(
            Path(self.temporary.name),
            max_pgn_upload_bytes=1024,
            max_pasted_pgn_bytes=128,
            chunk_size=8,
        )
        self.app = create_app(
            Settings(
                database_url="postgresql+psycopg://unused",
                pgn_spool_directory=Path(self.temporary.name),
                max_pgn_upload_bytes=1024,
                max_pasted_pgn_bytes=128,
                secret_key="test",
            ),
            WebDependencies(self.store, self.spool, HealthProbe()),
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def test_file_and_paste_create_jobs_with_navigation_links(self):
        file_response = self.client.post(
            "/api/analyses",
            data={"pgn_file": (io.BytesIO(PGN.encode()), "round.pgn")},
        )
        paste_response = self.client.post(
            "/api/analyses", data={"pgn_text": PGN}
        )

        self.assertEqual(file_response.status_code, 202)
        self.assertEqual(paste_response.status_code, 202)
        body = file_response.get_json()
        self.assertEqual(body["job"]["source_name"], "round.pgn")
        self.assertEqual(set(body["links"]), {"page", "status", "games", "events"})
        self.assertNotIn("spool_path", str(body))

    def test_source_selection_lichess_size_and_empty_errors_are_structured(self):
        self.assertEqual(self.client.post("/api/analyses", data={}).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/analyses",
                data={"pgn_text": PGN, "lichess_url": "https://lichess.org/study/x"},
            ).status_code,
            400,
        )
        lichess = self.client.post(
            "/api/analyses", data={"lichess_url": "https://lichess.org/study/x"}
        )
        self.assertEqual(lichess.status_code, 501)
        self.assertEqual(lichess.get_json()["error"]["code"], "connector_unavailable")
        self.assertEqual(
            self.client.post("/api/analyses", data={"pgn_text": "x" * 129}).status_code,
            413,
        )
        self.assertEqual(
            self.client.post("/api/analyses", data={"pgn_text": "   \n"}).status_code,
            422,
        )

    def test_status_games_filters_pagination_and_missing_job(self):
        created = self.client.post("/api/analyses", data={"pgn_text": PGN}).get_json()
        job_id = created["job"]["id"]

        self.assertEqual(self.client.get(f"/api/analyses/{job_id}").status_code, 200)
        games = self.client.get(
            f"/api/analyses/{job_id}/games?status=queued&agreement=all&limit=500"
        )
        self.assertEqual(games.status_code, 200)
        self.assertEqual(set(games.get_json()), {"items", "next_cursor", "has_more"})
        self.assertEqual(
            self.client.get(f"/api/analyses/{job_id}/games?score_min=1.1").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").status_code,
            404,
        )

    def test_health_distinguishes_degraded_and_unavailable(self):
        self.assertEqual(self.client.get("/api/health").get_json()["status"], "ok")
        self.app.extensions["web_dependencies"] = WebDependencies(
            self.store, self.spool, HealthProbe(kafka="down")
        )
        self.assertEqual(self.client.get("/api/health").get_json()["status"], "degraded")
        self.app.extensions["web_dependencies"] = WebDependencies(
            self.store, self.spool, HealthProbe(database="down")
        )
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
