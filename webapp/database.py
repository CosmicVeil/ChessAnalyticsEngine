"""SQLAlchemy construction helpers shared by web-side processes."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    """Create an unconnected session factory for the configured PostgreSQL URL."""

    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
