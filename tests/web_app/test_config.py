"""Settings, database, and app-factory contract tests."""

import sys
import subprocess
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from webapp import create_app
from webapp.config import Settings
from webapp.database import create_session_factory
from webapp.domain import WebDependencies


DATABASE_URL = "postgresql+psycopg://chess:chess@db/chess_analytics"


class SettingsTests(unittest.TestCase):
    def test_settings_parse_limits_and_brokers(self):
        settings = Settings.from_env(
            {
                "DATABASE_URL": DATABASE_URL,
                "KAFKA_BOOTSTRAP_SERVERS": "one:9092,two:9092",
                "KAFKA_WEB_RESULT_GROUP": "web-results-test",
                "PGN_SPOOL_DIRECTORY": "/tmp/chess-pgn-tests",
                "MAX_PGN_UPLOAD_BYTES": "4294967296",
                "MAX_PASTED_PGN_BYTES": "10485760",
                "SECRET_KEY": "test-secret",
            }
        )

        self.assertEqual(settings.database_url, DATABASE_URL)
        self.assertEqual(
            settings.kafka_bootstrap_servers, "one:9092,two:9092"
        )
        self.assertEqual(settings.kafka_web_result_group, "web-results-test")
        self.assertEqual(settings.pgn_spool_directory, Path("/tmp/chess-pgn-tests"))
        self.assertEqual(settings.max_pgn_upload_bytes, 4_294_967_296)
        self.assertEqual(settings.max_pasted_pgn_bytes, 10_485_760)
        self.assertEqual(settings.secret_key, "test-secret")

    def test_settings_use_documented_defaults(self):
        settings = Settings.from_env({"DATABASE_URL": DATABASE_URL})

        self.assertEqual(settings.kafka_bootstrap_servers, "localhost:19092")
        self.assertEqual(settings.kafka_web_result_group, "chess-web-results-v1")
        self.assertEqual(settings.pgn_spool_directory, Path(".runtime/pgn-spool"))
        self.assertEqual(settings.max_pgn_upload_bytes, 4 * 1024**3)
        self.assertEqual(settings.max_pasted_pgn_bytes, 10 * 1024**2)
        self.assertEqual(settings.secret_key, "development-only-change-me")
        self.assertFalse(settings.session_cookie_secure)

    def test_settings_parse_session_cookie_secure(self):
        for value, expected in (
            ("true", True), ("1", True), ("YES", True), ("on", True),
            ("false", False), ("0", False), ("no", False), ("off", False),
        ):
            with self.subTest(value=value):
                settings = Settings.from_env(
                    {"DATABASE_URL": DATABASE_URL, "SESSION_COOKIE_SECURE": value}
                )
                self.assertIs(settings.session_cookie_secure, expected)

    def test_settings_reject_invalid_session_cookie_secure(self):
        with self.assertRaisesRegex(
            ValueError, r"^SESSION_COOKIE_SECURE must be true or false$"
        ):
            Settings.from_env(
                {"DATABASE_URL": DATABASE_URL, "SESSION_COOKIE_SECURE": "maybe"}
            )

    def test_settings_reject_non_integer_limits(self):
        for variable in ("MAX_PGN_UPLOAD_BYTES", "MAX_PASTED_PGN_BYTES"):
            with self.subTest(variable=variable):
                with self.assertRaisesRegex(
                    ValueError, rf"^{variable} must be a positive integer$"
                ):
                    Settings.from_env(
                        {"DATABASE_URL": DATABASE_URL, variable: "not-an-integer"}
                    )

    def test_settings_reject_non_positive_limits(self):
        for variable in ("MAX_PGN_UPLOAD_BYTES", "MAX_PASTED_PGN_BYTES"):
            for value in ("0", "-1"):
                with self.subTest(variable=variable, value=value):
                    with self.assertRaisesRegex(
                        ValueError, rf"^{variable} must be a positive integer$"
                    ):
                        Settings.from_env(
                            {"DATABASE_URL": DATABASE_URL, variable: value}
                        )

    def test_settings_are_immutable(self):
        settings = Settings.from_env({"DATABASE_URL": DATABASE_URL})

        with self.assertRaises(FrozenInstanceError):
            settings.secret_key = "replacement"  # type: ignore[misc]


class AppFactoryTests(unittest.TestCase):
    def test_app_uses_injected_dependencies(self):
        store = object()
        spool_service = object()
        health_probe = object()
        dependencies = WebDependencies(
            store=store,
            spool_service=spool_service,
            health_probe=health_probe,
        )
        settings = Settings(database_url=DATABASE_URL, secret_key="test-secret")

        app = create_app(settings=settings, dependencies=dependencies)

        self.assertEqual(app.config["SECRET_KEY"], "test-secret")
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertIs(app.extensions["web_dependencies"].store, store)
        self.assertIs(
            app.extensions["web_dependencies"].spool_service, spool_service
        )
        self.assertIs(app.extensions["web_dependencies"].health_probe, health_probe)

    def test_app_factory_does_not_load_model_libraries(self):
        script = """
import sys

from webapp import create_app
from webapp.config import Settings
from webapp.domain import WebDependencies

create_app(
    settings=Settings(database_url="postgresql+psycopg://chess:chess@db/chess"),
    dependencies=WebDependencies(
        store=object(),
        spool_service=object(),
        health_probe=object(),
    ),
)
assert "torch" not in sys.modules
assert "xgboost" not in sys.modules
"""

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


class SessionCookieTests(unittest.TestCase):
    def test_secure_setting_marks_session_cookie_secure(self):
        app = create_app(
            settings=Settings(
                database_url=DATABASE_URL,
                secret_key="test-secret",
                session_cookie_secure=True,
            ),
            dependencies=WebDependencies(
                store=object(), spool_service=object(), health_probe=object()
            ),
        )

        @app.get("/_set-session")
        def set_session():
            from flask import session

            session["probe"] = "1"
            return "ok"

        response = app.test_client().get("/_set-session")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)


class DatabaseTests(unittest.TestCase):
    def test_session_factory_creates_sessions_bound_to_database_url(self):
        session_factory = create_session_factory(DATABASE_URL)

        session = session_factory()
        try:
            self.assertEqual(
                session.get_bind().url.render_as_string(hide_password=False),
                DATABASE_URL,
            )
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
