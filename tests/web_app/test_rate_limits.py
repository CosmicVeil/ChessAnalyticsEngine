"""Rate-limit contracts: auth by client IP, analyses by account, ProxyFix opt-in."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import AnalysisQuotaUsage, SpooledSource, WebDependencies
from webapp.ratelimit import MemoryRateLimiter, check_analysis_quota
from webapp.spool import SpoolService


PGN = '[White "Ada"]\n[Black "Grace"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'
NOW = datetime(2026, 11, 19, 12, 7, 30, tzinfo=UTC)
PASSWORD = "correct horse battery staple"


class Probe:
    def check(self):
        return {"database": "ok", "kafka": "ok"}


class RateLimitSettingsTests(unittest.TestCase):
    def test_defaults_keep_proxy_trust_off_and_limits_on(self):
        settings = Settings.from_env({"DATABASE_URL": "postgresql+psycopg://x"})

        self.assertFalse(settings.trust_proxy_headers)
        self.assertTrue(settings.rate_limits_enabled)

    def test_environment_overrides_every_limit(self):
        settings = Settings.from_env({
            "DATABASE_URL": "postgresql+psycopg://x",
            "TRUST_PROXY_HEADERS": "true",
            "RATE_LIMITS_ENABLED": "false",
            "SIGN_IN_ATTEMPTS_PER_15_MIN": "7",
            "ACCOUNT_CREATIONS_PER_HOUR": "3",
            "ANALYSES_PER_HOUR": "4",
            "MAX_ACTIVE_ANALYSES": "1",
        })

        self.assertTrue(settings.trust_proxy_headers)
        self.assertFalse(settings.rate_limits_enabled)
        self.assertEqual(
            (settings.sign_in_attempts_per_15_min, settings.account_creations_per_hour,
             settings.analyses_per_hour, settings.max_active_analyses),
            (7, 3, 4, 1),
        )

    def test_non_positive_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ANALYSES_PER_HOUR"):
            Settings.from_env({"DATABASE_URL": "postgresql+psycopg://x", "ANALYSES_PER_HOUR": "0"})


class MemoryRateLimiterTests(unittest.TestCase):
    def test_hits_up_to_the_limit_are_allowed_and_the_next_is_rejected(self):
        limiter = MemoryRateLimiter()
        decisions = [limiter.hit("k", 3, 900, now=NOW) for _ in range(4)]

        self.assertEqual([item.allowed for item in decisions], [True, True, True, False])
        # 12:07:30 sits in the 12:00-12:15 window, which resets in 450 s.
        self.assertEqual(decisions[-1].retry_after, 450)

    def test_keys_are_isolated_and_a_new_window_starts_fresh(self):
        limiter = MemoryRateLimiter()
        limiter.hit("a", 1, 900, now=NOW)

        self.assertFalse(limiter.hit("a", 1, 900, now=NOW).allowed)
        self.assertTrue(limiter.hit("b", 1, 900, now=NOW).allowed)
        self.assertTrue(limiter.hit("a", 1, 900, now=NOW + timedelta(minutes=8)).allowed)


class AnalysisQuotaTests(unittest.TestCase):
    def test_under_both_limits_is_allowed(self):
        usage = AnalysisQuotaUsage(recent_jobs=1, active_jobs=1, oldest_recent_created_at=NOW)
        self.assertTrue(check_analysis_quota(usage, per_hour=2, max_active=2, now=NOW).allowed)

    def test_hourly_limit_retries_when_the_oldest_recent_job_ages_out(self):
        usage = AnalysisQuotaUsage(2, 0, NOW - timedelta(minutes=50))
        decision = check_analysis_quota(usage, per_hour=2, max_active=5, now=NOW)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_after, 600)
        self.assertIn("10 minutes", decision.message)

    def test_active_limit_is_reported_even_under_the_hourly_limit(self):
        decision = check_analysis_quota(AnalysisQuotaUsage(0, 3, None), per_hour=10, max_active=3, now=NOW)

        self.assertFalse(decision.allowed)
        self.assertGreater(decision.retry_after, 0)
        self.assertIn("running", decision.message)


class RateLimitedAppTestCase(unittest.TestCase):
    def make_app(self, **overrides):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = FakeAnalysisStore()
        settings = Settings(
            database_url="postgresql+psycopg://unused",
            pgn_spool_directory=Path(self.temporary.name),
            secret_key="test-secret",
            **overrides,
        )
        spool = SpoolService(Path(self.temporary.name), max_pgn_upload_bytes=1024, max_pasted_pgn_bytes=1024)
        self.app = create_app(settings, WebDependencies(self.store, spool, Probe()))
        return self.app

    @staticmethod
    def csrf(client):
        client.get("/sign-in")
        with client.session_transaction() as session:
            return session["csrf_token"]

    def register(self, client, email, **kwargs):
        return client.post(
            "/create-account",
            data={"csrf_token": self.csrf(client), "display_name": "Tester", "email": email, "password": PASSWORD},
            **kwargs,
        )

    def sign_in(self, client, password="wrong password!", **kwargs):
        return client.post(
            "/sign-in",
            data={"csrf_token": self.csrf(client), "email": "who@example.com", "password": password},
            **kwargs,
        )


class AuthRateLimitTests(RateLimitedAppTestCase):
    def test_sign_in_over_the_limit_returns_429_with_retry_after_and_message(self):
        self.make_app(sign_in_attempts_per_15_min=2)
        client = self.app.test_client()

        self.assertEqual(self.sign_in(client).status_code, 200)
        self.assertEqual(self.sign_in(client).status_code, 200)
        blocked = self.sign_in(client)

        self.assertEqual(blocked.status_code, 429)
        self.assertTrue(1 <= int(blocked.headers["Retry-After"]) <= 900)
        self.assertIn("Too many sign-in attempts", blocked.get_data(as_text=True))

    def test_sign_in_limit_is_per_client_ip(self):
        self.make_app(sign_in_attempts_per_15_min=1)
        attacker = self.app.test_client()
        bystander = self.app.test_client()
        self.sign_in(attacker, environ_base={"REMOTE_ADDR": "203.0.113.9"})

        self.assertEqual(self.sign_in(attacker, environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code, 429)
        self.assertEqual(self.sign_in(bystander, environ_base={"REMOTE_ADDR": "198.51.100.4"}).status_code, 200)

    def test_account_creation_is_limited_separately_from_sign_in(self):
        self.make_app(account_creations_per_hour=1, sign_in_attempts_per_15_min=5)
        client = self.app.test_client()

        self.assertEqual(self.register(client, "first@example.com").status_code, 302)
        # A fresh session from the same IP shares the IP's budget.
        second = self.register(self.app.test_client(), "second@example.com")

        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second.headers)
        self.assertIn("Too many account creation attempts", second.get_data(as_text=True))
        self.assertEqual(self.sign_in(self.app.test_client()).status_code, 200)

    def test_limits_can_be_disabled(self):
        self.make_app(rate_limits_enabled=False, sign_in_attempts_per_15_min=1)
        client = self.app.test_client()
        for _ in range(3):
            self.assertEqual(self.sign_in(client).status_code, 200)


class ProxyFixTests(RateLimitedAppTestCase):
    def test_trusted_proxy_header_gives_each_forwarded_client_its_own_bucket(self):
        self.make_app(sign_in_attempts_per_15_min=1, trust_proxy_headers=True)
        client = self.app.test_client()
        via_proxy = {"REMOTE_ADDR": "172.18.0.5"}

        first = self.sign_in(client, environ_base=via_proxy, headers={"X-Forwarded-For": "203.0.113.1"})
        other = self.sign_in(client, environ_base=via_proxy, headers={"X-Forwarded-For": "203.0.113.2"})
        repeat = self.sign_in(client, environ_base=via_proxy, headers={"X-Forwarded-For": "203.0.113.1"})

        self.assertEqual((first.status_code, other.status_code, repeat.status_code), (200, 200, 429))

    def test_trusted_proxy_uses_only_the_hop_the_proxy_appended(self):
        self.make_app(sign_in_attempts_per_15_min=1, trust_proxy_headers=True)
        client = self.app.test_client()
        # A client-forged first entry must not mint a fresh identity.
        self.sign_in(client, headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.1"})
        forged = self.sign_in(client, headers={"X-Forwarded-For": "2.2.2.2, 203.0.113.1"})

        self.assertEqual(forged.status_code, 429)

    def test_forwarded_header_is_ignored_unless_proxy_trust_is_enabled(self):
        self.make_app(sign_in_attempts_per_15_min=1)
        client = self.app.test_client()

        self.sign_in(client, headers={"X-Forwarded-For": "203.0.113.1"})
        spoofed = self.sign_in(client, headers={"X-Forwarded-For": "203.0.113.2"})

        self.assertEqual(spoofed.status_code, 429)


class AnalysisRateLimitTests(RateLimitedAppTestCase):
    def signed_in_client(self, email):
        client = self.app.test_client()
        self.assertEqual(self.register(client, email).status_code, 302)
        with client.session_transaction() as session:
            return client, session["csrf_token"]

    @staticmethod
    def submit(client, csrf):
        return client.post("/api/analyses", data={"csrf_token": csrf, "engine": "nnue", "pgn_text": PGN})

    def test_hourly_limit_returns_json_429_with_retry_after(self):
        self.make_app(analyses_per_hour=2, max_active_analyses=10)
        client, csrf = self.signed_in_client("hourly@example.com")

        self.assertEqual(self.submit(client, csrf).status_code, 202)
        self.assertEqual(self.submit(client, csrf).status_code, 202)
        blocked = self.submit(client, csrf)

        self.assertEqual(blocked.status_code, 429)
        error = blocked.get_json()["error"]
        self.assertEqual(error["code"], "rate_limited")
        self.assertIn("2 analyses per hour", error["message"])
        self.assertEqual(int(blocked.headers["Retry-After"]), error["retry_after"])
        self.assertTrue(3500 <= error["retry_after"] <= 3600)
        self.assertEqual(len(self.store.jobs), 2)
        self.assertEqual(len(list(Path(self.temporary.name).iterdir())), 2)

    def test_limits_are_per_account(self):
        self.make_app(analyses_per_hour=1, max_active_analyses=10)
        first, first_csrf = self.signed_in_client("one@example.com")
        second, second_csrf = self.signed_in_client("two@example.com")

        self.assertEqual(self.submit(first, first_csrf).status_code, 202)
        self.assertEqual(self.submit(first, first_csrf).status_code, 429)
        self.assertEqual(self.submit(second, second_csrf).status_code, 202)

    def test_active_limit_frees_up_when_a_job_finishes(self):
        self.make_app(analyses_per_hour=10, max_active_analyses=1)
        client, csrf = self.signed_in_client("active@example.com")
        job_id = uuid.UUID(self.submit(client, csrf).get_json()["job"]["id"])

        blocked = self.submit(client, csrf)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("running", blocked.get_json()["error"]["message"])

        self.store.jobs[job_id] = replace(self.store.jobs[job_id], status="completed")
        self.assertEqual(self.submit(client, csrf).status_code, 202)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "PostgreSQL integration test requires TEST_DATABASE_URL")
class PostgresRateLimitTests(unittest.TestCase):
    """The shared counters and quota query against real PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        import sqlalchemy as sa
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.orm import Session, sessionmaker

        from webapp.ratelimit import PostgresRateLimiter
        from webapp.repository import SqlAlchemyAnalysisStore

        url = os.environ["TEST_DATABASE_URL"]
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        cls.engine = sa.create_engine(url)
        cls.sessions = sessionmaker(bind=cls.engine, class_=Session, expire_on_commit=False)
        cls.limiter = PostgresRateLimiter(cls.sessions)
        cls.store = SqlAlchemyAnalysisStore(cls.sessions)
        cls.sa = sa

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(self.sa.text("TRUNCATE rate_limit_counters, users, analysis_jobs CASCADE"))

    def test_counter_is_shared_by_separate_limiter_instances(self):
        from webapp.ratelimit import PostgresRateLimiter

        other_worker = PostgresRateLimiter(self.sessions)
        self.assertTrue(self.limiter.hit("sign-in:ip:1", 2, 900, now=NOW).allowed)
        self.assertTrue(other_worker.hit("sign-in:ip:1", 2, 900, now=NOW).allowed)
        blocked = self.limiter.hit("sign-in:ip:1", 2, 900, now=NOW)

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.retry_after, 450)
        self.assertTrue(self.limiter.hit("sign-in:ip:2", 2, 900, now=NOW).allowed)

    def test_quota_usage_counts_recent_and_unfinished_jobs_for_one_owner(self):
        owner = self.store.create_account(
            email="quota@example.com", display_name="Quota", password_hash="x", preferred_engine="nnue"
        )
        other = self.store.create_account(
            email="other@example.com", display_name="Other", password_hash="x", preferred_engine="nnue"
        )
        source = SpooledSource("paste", "pasted.pgn", Path("/spool/x"))
        old = self.store.create_queued_job(source, owner.id)
        self.store.create_queued_job(source, owner.id)
        self.store.create_queued_job(source, other.id)
        with self.engine.begin() as connection:
            connection.execute(
                self.sa.text(
                    "UPDATE analysis_jobs SET created_at = now() - interval '2 hours', status = 'completed' "
                    "WHERE id = :id"
                ),
                {"id": old.id},
            )

        usage = self.store.analysis_quota_usage(owner.id, datetime.now(UTC) - timedelta(hours=1))

        self.assertEqual((usage.recent_jobs, usage.active_jobs), (1, 1))
        self.assertIsNotNone(usage.oldest_recent_created_at)


if __name__ == "__main__":
    unittest.main()
