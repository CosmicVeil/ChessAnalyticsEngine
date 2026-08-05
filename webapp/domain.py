"""Dependency boundaries for the Flask adapter."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebDependencies:
    """Runtime collaborators, replaceable independently in tests."""

    store: object | None = None
    spool_service: object | None = None
    health_probe: object | None = None
