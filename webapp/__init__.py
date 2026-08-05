"""Flask application factory for the analysis adapter."""

from flask import Flask

from webapp.config import Settings
from webapp.domain import WebDependencies


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
        MAX_CONTENT_LENGTH=resolved_settings.max_pgn_upload_bytes,
        MAX_PASTED_PGN_BYTES=resolved_settings.max_pasted_pgn_bytes,
        SECRET_KEY=resolved_settings.secret_key,
    )
    from webapp.database import create_session_factory
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
    )
    app.extensions["web_settings"] = resolved_settings
    app.extensions["web_dependencies"] = resolved_dependencies

    from webapp.api import api
    from webapp.pages import pages

    app.register_blueprint(api)
    app.register_blueprint(pages)
    return app


__all__ = ["Settings", "WebDependencies", "create_app"]
