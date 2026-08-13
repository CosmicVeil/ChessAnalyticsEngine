"""HTML page routes; templates are supplied by the frontend layer."""

import uuid

from flask import Blueprint, redirect, render_template, url_for


pages = Blueprint("pages", __name__)


@pages.get("/")
def landing():
    return render_template("landing.html", job_id=None)


@pages.get("/analyze")
def analyze():
    return redirect(url_for("pages.landing"))


@pages.get("/analyses/<uuid:job_id>")
def analysis(job_id: uuid.UUID):
    return render_template("landing.html", job_id=job_id)


@pages.get("/system")
def system():
    return render_template("system.html")


__all__ = ["pages"]
