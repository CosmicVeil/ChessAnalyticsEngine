"""Flask submission, status, query, and health API contracts."""

import io
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import WebDependencies
from webapp.lichess import LichessUnavailable, StudyForbidden, StudyNotFound
from webapp.spool import SpoolService


PGN = '[White "Ada"]\n[Black "Grace"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'


class HealthProbe:
    def __init__(self, database="ok", kafka="ok"):
        self.database = database
        self.kafka = kafka

    def check(self):
        return {"database": self.database, "kafka": self.kafka}


class FakeLichessClient:
    def __init__(self, body: bytes = PGN.encode(), error: Exception | None = None):
        self.body = body
        self.error = error
        self.requested = []

    def open_pgn(self, ref):
        self.requested.append(ref)
        if self.error is not None:
            raise self.error
        return io.BytesIO(self.body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FakeAnalysisStore()
        self.lichess = FakeLichessClient()
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
            WebDependencies(self.store, self.spool, HealthProbe(), lichess_client=self.lichess),
        )
        self.client = self.app.test_client()
        self.client.get("/create-account")
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        self.client.post("/create-account", data={
            "csrf_token": csrf,
            "display_name": "API Tester",
            "email": "api@example.com",
            "password": "correct horse battery staple",
            "preferred_engine": "nnue",
        })
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def tearDown(self):
        self.temporary.cleanup()

    def test_file_and_paste_create_jobs_with_navigation_links(self):
        file_response = self.client.post(
            "/api/analyses",
            data={"csrf_token": self.csrf, "engine": "nnue", "pgn_file": (io.BytesIO(PGN.encode()), "round.pgn")},
        )
        paste_response = self.client.post(
            "/api/analyses", data={"csrf_token": self.csrf, "engine": "xgboost", "pgn_text": PGN}
        )

        self.assertEqual(file_response.status_code, 202)
        self.assertEqual(paste_response.status_code, 202)
        body = file_response.get_json()
        self.assertEqual(body["job"]["source_name"], "round.pgn")
        self.assertEqual(set(body["links"]), {"page", "status", "games", "events"})
        self.assertNotIn("spool_path", str(body))

    def test_source_selection_lichess_size_and_empty_errors_are_structured(self):
        self.assertEqual(self.client.post("/api/analyses", data={"csrf_token": self.csrf, "engine": "nnue"}).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/analyses",
                data={"csrf_token": self.csrf, "engine": "nnue", "pgn_text": PGN, "lichess_url": "https://lichess.org/study/x"},
            ).status_code,
            400,
        )
        lichess = self.client.post(
            "/api/analyses", data={"csrf_token": self.csrf, "engine": "nnue", "lichess_url": "https://lichess.org/study/x"}
        )
        self.assertEqual(lichess.status_code, 400)
        self.assertEqual(lichess.get_json()["error"]["code"], "invalid_lichess_url")
        self.assertEqual(self.lichess.requested, [])
        self.assertEqual(
            self.client.post("/api/analyses", data={"csrf_token": self.csrf, "engine": "nnue", "pgn_text": "x" * 129}).status_code,
            413,
        )
        self.assertEqual(
            self.client.post("/api/analyses", data={"csrf_token": self.csrf, "engine": "nnue", "pgn_text": "   \n"}).status_code,
            422,
        )

    def _post_file(self, content: bytes):
        return self.client.post(
            "/api/analyses",
            data={"csrf_token": self.csrf, "engine": "nnue", "pgn_file": (io.BytesIO(content), "big.pgn")},
        )

    def test_file_exactly_at_upload_limit_is_accepted(self):
        content = PGN.encode().ljust(1024, b"\n")
        self.assertEqual(len(content), 1024)

        response = self._post_file(content)

        self.assertEqual(response.status_code, 202)

    def test_file_just_over_upload_limit_is_a_structured_error(self):
        # Fits inside MAX_CONTENT_LENGTH's multipart allowance, so the spool's
        # exact per-file check is what rejects it.
        response = self._post_file(PGN.encode().ljust(1025, b"\n"))

        self.assertEqual(response.status_code, 413)
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "source_too_large")
        self.assertEqual(error["limit"], 1024)
        self.assertIn("1,024 bytes", error["message"])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_request_over_max_content_length_is_json_not_html(self):
        # Far over the limit: Werkzeug rejects it before the view runs.
        response = self._post_file(b"x" * (200 * 1024))

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(
            response.get_json()["error"],
            {
                "code": "source_too_large",
                "message": "PGN files can be at most 1,024 bytes.",
                "limit": 1024,
            },
        )
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def _post_lichess(self, url="https://lichess.org/study/abcdEFGH/chap1234#12", engine="nnue"):
        return self.client.post(
            "/api/analyses", data={"csrf_token": self.csrf, "engine": engine, "lichess_url": url}
        )

    def test_lichess_study_is_fetched_spooled_and_queued(self):
        response = self._post_lichess()
        self.assertEqual(response.status_code, 202)
        job = response.get_json()["job"]
        self.assertEqual(job["source_type"], "lichess")
        self.assertEqual(job["source_name"], "lichess-study-abcdEFGH-chap1234.pgn")
        (ref,) = self.lichess.requested
        self.assertEqual(ref.export_url, "https://lichess.org/api/study/abcdEFGH/chap1234.pgn")

    def test_lichess_invalid_engine_is_rejected_before_any_fetch(self):
        self.assertEqual(self._post_lichess(engine="stockfish").status_code, 400)
        self.assertEqual(self.lichess.requested, [])

    def test_lichess_connector_failures_map_to_structured_errors(self):
        cases = [
            (StudyNotFound("missing"), 404, "lichess_study_not_found"),
            (StudyForbidden("private"), 403, "lichess_study_private"),
            (LichessUnavailable("down"), 502, "lichess_unavailable"),
        ]
        for error, status, code in cases:
            with self.subTest(code=code):
                self.lichess.error = error
                response = self._post_lichess()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json()["error"]["code"], code)
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_lichess_oversized_and_empty_exports_are_rejected_without_leftovers(self):
        self.lichess.body = PGN.encode() * 100
        self.assertEqual(self._post_lichess().status_code, 413)
        self.lichess.body = b""
        self.assertEqual(self._post_lichess().get_json()["error"]["code"], "empty_pgn")
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_status_games_filters_pagination_and_missing_job(self):
        created = self.client.post("/api/analyses", data={"csrf_token": self.csrf, "engine": "nnue", "pgn_text": PGN}).get_json()
        job_id = created["job"]["id"]

        status_response = self.client.get(f"/api/analyses/{job_id}")
        self.assertEqual(status_response.status_code, 200)
        status_body = status_response.get_json()
        self.assertEqual(set(status_body), {"job", "event_cursor"})
        self.assertEqual(
            status_body["event_cursor"],
            self.store.latest_event_id(uuid.UUID(job_id)),
        )
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
