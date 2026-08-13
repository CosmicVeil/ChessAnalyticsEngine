"""Frontend route, design-constraint, and raster asset checks."""

import struct
import unittest
import uuid
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import WebDependencies


ROOT = Path(__file__).resolve().parents[2]


class FrontendPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(
            Settings(database_url="postgresql+psycopg://unused"),
            WebDependencies(
                store=FakeAnalysisStore(),
                spool_service=object(),
                health_probe=object(),
            ),
        )
        cls.client = cls.app.test_client()

    def test_all_creative_pages_render_without_model_imports(self):
        paths = ["/", "/system", f"/analyses/{uuid.uuid4()}"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Chess Analytics Engine", response.data)
                self.assertIn(b"/static/css/site.css", response.data)

    def test_analyze_redirects_to_home(self):
        response = self.client.get("/analyze", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_home_shell_preserves_sources_and_restores_a_selected_job(self):
        job_id = uuid.uuid4()
        home = self.client.get("/")
        analysis = self.client.get(f"/analyses/{job_id}")

        for response in (home, analysis):
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"data-home-shell", response.data)
            self.assertIn(b'name="pgn_file"', response.data)
            self.assertIn(b'name="pgn_text"', response.data)
            self.assertIn(b'name="lichess_url"', response.data)
        self.assertIn(f'data-job-id="{job_id}"'.encode(), analysis.data)

    def test_visual_system_avoids_disallowed_design_shortcuts(self):
        css = (ROOT / "webapp/static/css/site.css").read_text(encoding="utf-8").lower()
        for banned in ("linear-gradient", "radial-gradient", "backdrop-filter", "#8b5cf6"):
            self.assertNotIn(banned, css)
        landing = self.client.get("/").data
        self.assertIn(b"data-depth", landing)
        self.assertIn(b"assets/knight.png", landing)
        self.assertIn(b"live model scores from your", landing.lower())

    def test_piece_assets_are_rgba_pngs_with_isolated_alpha(self):
        for name in ("knight.png", "rook.png", "bishop.png"):
            with self.subTest(name=name):
                data = (ROOT / "webapp/static/assets" / name).read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(data[12:16], b"IHDR")
                width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
                self.assertGreaterEqual(min(width, height), 1000)
                self.assertEqual(bit_depth, 8)
                self.assertEqual(color_type, 6, "PNG must have an alpha channel")

    def test_live_ledger_leads_with_verdicts_instead_of_percentages(self):
        response = self.client.get(f"/analyses/{uuid.uuid4()}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PyTorch verdict", response.data)
        self.assertIn(b"XGBoost verdict", response.data)
        self.assertNotIn(b"PyTorch score</span>", response.data)
        self.assertNotIn(b"XGBoost score</span>", response.data)

    def test_live_ledger_connects_with_live_analysis_copy(self):
        response = self.client.get(f"/analyses/{uuid.uuid4()}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Connecting to live analysis", response.data)


if __name__ == "__main__":
    unittest.main()
