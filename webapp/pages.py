"""HTML page routes; templates are supplied by the frontend layer."""

import uuid

from flask import Blueprint, render_template


pages = Blueprint("pages", __name__)


@pages.get("/")
def landing():
    return render_template("landing.html")


@pages.get("/analyze")
def analyze():
    return render_template("analyze.html")


@pages.get("/analyses/<uuid:job_id>")
def analysis(job_id: uuid.UUID):
    return render_template("analysis.html", job_id=job_id)


@pages.get("/system")
def system():
    return render_template("system.html")


__all__ = ["pages"]
