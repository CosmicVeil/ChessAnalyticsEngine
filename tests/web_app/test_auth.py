"""Authentication, ownership, and per-job engine contracts."""

import tempfile
import unittest
import uuid
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import WebDependencies
from webapp.spool import SpoolService


PGN = '[White "Ada"]\n[Black "Grace"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'


class Probe:
    def check(self):
        return {"database": "ok", "kafka": "ok"}


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FakeAnalysisStore()
        self.app = create_app(
            Settings(
                database_url="postgresql+psycopg://unused",
                pgn_spool_directory=Path(self.temporary.name),
                secret_key="test-secret",
            ),
            WebDependencies(
                self.store,
                SpoolService(
                    Path(self.temporary.name),
                    max_pgn_upload_bytes=1024,
                    max_pasted_pgn_bytes=1024,
                ),
                Probe(),
            ),
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def csrf(client):
        with client.session_transaction() as session:
            return session["csrf_token"]

    def register(self, client, email):
        client.get("/create-account")
        return client.post(
            "/create-account",
            data={
                "csrf_token": self.csrf(client),
                "display_name": email.split("@")[0],
                "email": email,
                "password": "correct horse battery staple",
                "preferred_engine": "nnue",
            },
        )

    def test_public_home_hides_ingestion_and_private_page_requires_login(self):
        client = self.app.test_client()
        home = client.get("/")

        self.assertEqual(home.status_code, 200)
        self.assertNotIn(b'name="pgn_file"', home.data)
        self.assertNotIn(b'name="pgn_text"', home.data)
        self.assertEqual(client.get("/analyze").status_code, 302)
        self.assertEqual(client.post("/api/analyses").status_code, 401)

    def test_registration_unlocks_submission_and_persists_engine_and_owner(self):
        client = self.app.test_client()
        self.assertEqual(self.register(client, "mohan@example.com").status_code, 302)
        workspace = client.get("/analyze")
        self.assertIn(b'name="pgn_file"', workspace.data)
        self.assertIn(b'value="consensus"', workspace.data)

        response = client.post(
            "/api/analyses",
            data={"csrf_token": self.csrf(client), "engine": "consensus", "pgn_text": PGN},
        )
        self.assertEqual(response.status_code, 202)
        job = self.store.get_job(uuid.UUID(response.get_json()["job"]["id"]))
        self.assertEqual(job.engine, "consensus")
        self.assertIsNotNone(job.owner_id)

    def test_one_account_cannot_open_another_accounts_job(self):
        first = self.app.test_client()
        second = self.app.test_client()
        self.register(first, "first@example.com")
        created = first.post(
            "/api/analyses",
            data={"csrf_token": self.csrf(first), "engine": "nnue", "pgn_text": PGN},
        ).get_json()
        self.register(second, "second@example.com")

        job_id = created["job"]["id"]
        self.assertEqual(second.get(f"/api/analyses/{job_id}").status_code, 404)
        self.assertEqual(second.get(f"/analyses/{job_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
