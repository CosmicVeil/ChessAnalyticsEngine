"""Flask application factory for the analysis adapter."""

from flask import Flask

from webapp.config import Settings
from webapp.domain import WebDependencies
from webapp.spool import MULTIPART_OVERHEAD_BYTES


def create_app(
    settings: Settings | None = None,
    dependencies: WebDependencies | None = None,
) -> Flask:
    """Build an isolated Flask application with injectable collaborators."""

    resolved_settings = settings if settings is not None else Settings.from_env()
    supplied_dependencies = (
        dependencies if dependencies is not None else WebDependencies()
    )

    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE_URL=resolved_settings.database_url,
        KAFKA_BOOTSTRAP_SERVERS=resolved_settings.kafka_bootstrap_servers,
        KAFKA_WEB_RESULT_GROUP=resolved_settings.kafka_web_result_group,
        PGN_SPOOL_DIRECTORY=resolved_settings.pgn_spool_directory,
        # Whole-request cap, checked before the body is read. SpoolService
        # enforces the exact per-file limit; the overhead keeps an at-limit
        # file's multipart envelope from tripping this first.
        MAX_CONTENT_LENGTH=resolved_settings.max_pgn_upload_bytes
        + MULTIPART_OVERHEAD_BYTES,
        MAX_PGN_UPLOAD_BYTES=resolved_settings.max_pgn_upload_bytes,
        MAX_PASTED_PGN_BYTES=resolved_settings.max_pasted_pgn_bytes,
        SECRET_KEY=resolved_settings.secret_key,
        # Secure keeps the session cookie off plain HTTP; it must stay off for
        # local http://localhost development, so production opts in.
        SESSION_COOKIE_SECURE=resolved_settings.session_cookie_secure,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    from webapp.database import create_session_factory
    from webapp.lichess import LichessStudyClient
    from webapp.repository import SqlAlchemyAnalysisStore
    from webapp.services import AdapterHealthProbe
    from webapp.spool import SpoolService

    sessions = None
    if supplied_dependencies.store is None or supplied_dependencies.health_probe is None:
        sessions = create_session_factory(resolved_settings.database_url)
    resolved_dependencies = WebDependencies(
        store=supplied_dependencies.store
        or SqlAlchemyAnalysisStore(sessions),
        spool_service=supplied_dependencies.spool_service
        or SpoolService(
            resolved_settings.pgn_spool_directory,
            max_pgn_upload_bytes=resolved_settings.max_pgn_upload_bytes,
            max_pasted_pgn_bytes=resolved_settings.max_pasted_pgn_bytes,
        ),
        health_probe=supplied_dependencies.health_probe
        or AdapterHealthProbe(sessions, resolved_settings.kafka_bootstrap_servers),
        account_store=supplied_dependencies.account_store,
        lichess_client=supplied_dependencies.lichess_client
        or LichessStudyClient(resolved_settings.lichess_api_token),
    )
    app.extensions["web_settings"] = resolved_settings
    app.extensions["web_dependencies"] = resolved_dependencies

    from webapp.auth import MemoryAccountStore, auth, csrf_token

    app.extensions["account_store"] = (
        supplied_dependencies.account_store
        or (resolved_dependencies.store if hasattr(resolved_dependencies.store, "create_account") else MemoryAccountStore())
    )
    app.jinja_env.globals["csrf_token"] = csrf_token

    from webapp.api import api
    from webapp.pages import pages

    app.register_blueprint(api)
    app.register_blueprint(auth)
    app.register_blueprint(pages)
    return app


__all__ = ["Settings", "WebDependencies", "create_app"]
