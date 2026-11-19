"""Frontend routes, factual content, design constraints, and auth gating."""

import unittest
import uuid
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import SpooledSource, WebDependencies


ROOT = Path(__file__).resolve().parents[2]


class FrontendPageTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeAnalysisStore()
        self.app = create_app(
            Settings(database_url="postgresql+psycopg://unused", secret_key="test"),
            WebDependencies(store=self.store, spool_service=object(), health_probe=object()),
        )
        self.client = self.app.test_client()

    def login(self):
        self.client.get("/create-account")
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        self.client.post("/create-account", data={
            "csrf_token": csrf, "display_name": "Mohan", "email": "mohan@example.com",
            "password": "correct horse battery staple", "preferred_engine": "nnue",
        })
        with self.client.session_transaction() as session:
            return uuid.UUID(session["user_id"])

    def test_public_pages_render_and_home_hides_pgn_controls(self):
        for path in ("/", "/evaluation", "/system"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Chess Analytics Engine", response.data)
        home = self.client.get("/").data
        self.assertNotIn(b'name="pgn_file"', home)
        self.assertNotIn(b'name="pgn_text"', home)
        self.assertIn(b"Made by Mohan", home)
        self.assertIn(b"data-theme-toggle", home)
        self.assertIn(b"cae-theme", home)

    def test_authenticated_workspace_has_engine_source_and_live_controls(self):
        owner = self.login()
        page = self.client.get("/analyze").data
        for copy in (b'name="pgn_file"', b'name="pgn_text"', b'value="nnue"', b'value="xgboost"', b'value="consensus"', b'data-filter-status', b'aria-live="polite"'):
            self.assertIn(copy, page)
        self.assertNotIn(b"Coming soon", page)
        self.assertIn(b'<input type="url" name="lichess_url" inputmode="url"', page)

        job = self.store.create_queued_job(SpooledSource("paste", "pgn", Path("/tmp/example")), owner, "nnue")
        analysis = self.client.get(f"/analyses/{job.id}")
        self.assertEqual(analysis.status_code, 200)
        self.assertIn(b"Connecting to live analysis", analysis.data)
        self.assertIn(b"PyTorch verdict", analysis.data)
        self.assertIn(b"Selected-engine verdict", analysis.data)

    def test_evaluation_uses_repository_metrics_and_labels_saved_data(self):
        page = self.client.get("/evaluation").data
        for copy in (b"97.58%", b"97.17%", b"1.37%", b"8,090 games", b"Repository data", b"not live production telemetry"):
            self.assertIn(copy, page)

    def test_profile_uses_owned_persisted_history(self):
        owner = self.login()
        job = self.store.create_queued_job(SpooledSource("file", "round.pgn", Path("/tmp/example")), owner, "consensus")
        page = self.client.get("/profile")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"round.pgn", page.data)
        self.assertIn(str(job.id).encode(), page.data)
        self.assertIn(b"Privacy", page.data)

    def test_visual_system_uses_solid_tokens_and_reduced_motion(self):
        css = (ROOT / "webapp/static/css/site.css").read_text(encoding="utf-8").lower()
        for banned in ("linear-gradient", "radial-gradient", "backdrop-filter", "#8b5cf6"):
            self.assertNotIn(banned, css)
        self.assertNotIn("clip-path", css)
        for required in ("--control-radius: 8px", "--card-radius: 12px", "@media (prefers-reduced-motion: reduce)", ".drop-zone:focus-within", "min-height: 44px", 'html[data-theme="light"]', ".theme-toggle"):
            self.assertIn(required, css)


if __name__ == "__main__":
    unittest.main()
