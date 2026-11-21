"""Environment-backed configuration for the Flask analysis adapter."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:19092"
DEFAULT_KAFKA_WEB_RESULT_GROUP = "chess-web-results-v1"
DEFAULT_PGN_SPOOL_DIRECTORY = Path(".runtime/pgn-spool")
DEFAULT_MAX_PGN_UPLOAD_BYTES = 20 * 1024**2
DEFAULT_MAX_PASTED_PGN_BYTES = 10 * 1024**2
DEFAULT_SECRET_KEY = "development-only-change-me"
DEFAULT_SIGN_IN_ATTEMPTS_PER_15_MIN = 20
DEFAULT_ACCOUNT_CREATIONS_PER_HOUR = 10
DEFAULT_ANALYSES_PER_HOUR = 20
DEFAULT_MAX_ACTIVE_ANALYSES = 3


def _read_text(
    environ: Mapping[str, str], variable: str, default: str | None = None
) -> str:
    value = environ.get(variable, "").strip()
    if value:
        return value
    if default is not None:
        return default
    raise ValueError(f"{variable} is required")


def _read_boolean(
    environ: Mapping[str, str], variable: str, default: bool
) -> bool:
    raw_value = environ.get(variable, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{variable} must be true or false")


def _read_positive_integer(
    environ: Mapping[str, str], variable: str, default: int
) -> int:
    raw_value = environ.get(variable)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{variable} must be a positive integer") from error

    if value <= 0:
        raise ValueError(f"{variable} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable process configuration for web and worker entry points."""

    database_url: str
    kafka_bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS
    kafka_web_result_group: str = DEFAULT_KAFKA_WEB_RESULT_GROUP
    pgn_spool_directory: Path = DEFAULT_PGN_SPOOL_DIRECTORY
    max_pgn_upload_bytes: int = DEFAULT_MAX_PGN_UPLOAD_BYTES
    max_pasted_pgn_bytes: int = DEFAULT_MAX_PASTED_PGN_BYTES
    secret_key: str = DEFAULT_SECRET_KEY
    lichess_api_token: str = ""
    session_cookie_secure: bool = False
    # Only safe behind a proxy that overwrites X-Forwarded-For (Caddy does);
    # otherwise any client could pick its own rate-limit identity.
    trust_proxy_headers: bool = False
    rate_limits_enabled: bool = True
    sign_in_attempts_per_15_min: int = DEFAULT_SIGN_IN_ATTEMPTS_PER_15_MIN
    account_creations_per_hour: int = DEFAULT_ACCOUNT_CREATIONS_PER_HOUR
    analyses_per_hour: int = DEFAULT_ANALYSES_PER_HOUR
    max_active_analyses: int = DEFAULT_MAX_ACTIVE_ANALYSES

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "Settings":
        source = os.environ if environ is None else environ
        return cls(
            database_url=_read_text(source, "DATABASE_URL"),
            kafka_bootstrap_servers=_read_text(
                source,
                "KAFKA_BOOTSTRAP_SERVERS",
                DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
            ),
            kafka_web_result_group=_read_text(
                source,
                "KAFKA_WEB_RESULT_GROUP",
                DEFAULT_KAFKA_WEB_RESULT_GROUP,
            ),
            pgn_spool_directory=Path(
                _read_text(
                    source,
                    "PGN_SPOOL_DIRECTORY",
                    str(DEFAULT_PGN_SPOOL_DIRECTORY),
                )
            ),
            max_pgn_upload_bytes=_read_positive_integer(
                source, "MAX_PGN_UPLOAD_BYTES", DEFAULT_MAX_PGN_UPLOAD_BYTES
            ),
            max_pasted_pgn_bytes=_read_positive_integer(
                source, "MAX_PASTED_PGN_BYTES", DEFAULT_MAX_PASTED_PGN_BYTES
            ),
            secret_key=_read_text(source, "SECRET_KEY", DEFAULT_SECRET_KEY),
            lichess_api_token=_read_text(source, "LICHESS_API_TOKEN", ""),
            session_cookie_secure=_read_boolean(
                source, "SESSION_COOKIE_SECURE", False
            ),
            trust_proxy_headers=_read_boolean(
                source, "TRUST_PROXY_HEADERS", False
            ),
            rate_limits_enabled=_read_boolean(
                source, "RATE_LIMITS_ENABLED", True
            ),
            sign_in_attempts_per_15_min=_read_positive_integer(
                source,
                "SIGN_IN_ATTEMPTS_PER_15_MIN",
                DEFAULT_SIGN_IN_ATTEMPTS_PER_15_MIN,
            ),
            account_creations_per_hour=_read_positive_integer(
                source,
                "ACCOUNT_CREATIONS_PER_HOUR",
                DEFAULT_ACCOUNT_CREATIONS_PER_HOUR,
            ),
            analyses_per_hour=_read_positive_integer(
                source, "ANALYSES_PER_HOUR", DEFAULT_ANALYSES_PER_HOUR
            ),
            max_active_analyses=_read_positive_integer(
                source, "MAX_ACTIVE_ANALYSES", DEFAULT_MAX_ACTIVE_ANALYSES
            ),
        )
