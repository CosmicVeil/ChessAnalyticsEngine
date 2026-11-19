"""HTML page routes; templates are supplied by the frontend layer."""

import uuid

from flask import Blueprint, abort, current_app, g, render_template, request

from webapp.auth import login_required, owns_job, valid_csrf
from webapp.models import ANALYSIS_ENGINES


pages = Blueprint("pages", __name__)


@pages.get("/")
def landing():
    return render_template("landing.html")


@pages.get("/analyze")
@login_required
def analyze():
    return render_template("analyze.html", job_id=None)


@pages.get("/evaluation")
def evaluation():
    return render_template("evaluation.html")


@pages.get("/analyses/<uuid:job_id>")
@login_required
def analysis(job_id: uuid.UUID):
    job = owns_job(job_id)
    if job is None:
        abort(404)
    return render_template("analyze.html", job_id=job_id, selected_job=job)


@pages.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        engine = request.form.get("preferred_engine", "")
        if not valid_csrf() or engine not in ANALYSIS_ENGINES:
            abort(400)
        g.current_user = current_app.extensions["account_store"].update_preferred_engine(g.current_user.id, engine)
    jobs = current_app.extensions["web_dependencies"].store.list_jobs_for_user(g.current_user.id)
    summary = {
        "analyses": len(jobs),
        "games": sum(job.total_games for job in jobs),
        "completed": sum(job.completed_games for job in jobs),
        "failed": sum(job.failed_games for job in jobs),
    }
    return render_template("profile.html", jobs=jobs, summary=summary)


@pages.get("/system")
def system():
    return render_template("system.html")


__all__ = ["pages"]
