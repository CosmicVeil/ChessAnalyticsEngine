"""Session authentication for private PGN uploads and owned analyses."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps

from flask import Blueprint, current_app, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from webapp.domain import AccountSnapshot
from webapp.models import ANALYSIS_ENGINES
from webapp.ratelimit import rate_limited_message


auth = Blueprint("auth", __name__)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MemoryAccountStore:
    """Small fallback used only with injected test collaborators."""

    def __init__(self) -> None:
        self.accounts: dict[uuid.UUID, AccountSnapshot] = {}

    def create_account(self, *, email, display_name, password_hash, preferred_engine):
        if self.get_account_by_email(email):
            raise ValueError("email already exists")
        account = AccountSnapshot(
            uuid.uuid4(), email, display_name, password_hash, preferred_engine, datetime.now(UTC)
        )
        self.accounts[account.id] = account
        return account

    def get_account_by_email(self, email):
        return next((item for item in self.accounts.values() if item.email == email), None)

    def get_account(self, user_id):
        return self.accounts.get(user_id)

    def update_preferred_engine(self, user_id, engine):
        account = replace(self.accounts[user_id], preferred_engine=engine)
        self.accounts[user_id] = account
        return account


def account_store():
    return current_app.extensions["account_store"]


def csrf_token() -> str:
    return session.setdefault("csrf_token", secrets.token_urlsafe(32))


def valid_csrf() -> bool:
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(supplied and secrets.compare_digest(supplied, session.get("csrf_token", "")))


@auth.before_app_request
def load_current_user() -> None:
    g.current_user = None
    raw = session.get("user_id")
    if not raw:
        return
    try:
        g.current_user = account_store().get_account(uuid.UUID(raw))
    except (ValueError, TypeError):
        session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.current_user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": {"code": "authentication_required", "message": "Sign in to analyze private PGNs."}}), 401
            return redirect(url_for("auth.sign_in", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def owns_job(job_id: uuid.UUID):
    job = current_app.extensions["web_dependencies"].store.get_job(job_id)
    if job is None or g.current_user is None or job.owner_id != g.current_user.id:
        return None
    return job


def throttle_client_ip(scope: str, limit: int, window_seconds: int, mode: str, action: str):
    """Count one form POST for the client IP; return a 429 page once over the limit.

    Every attempt counts, successful or not, and before the password is
    hashed, so a flood costs the server one counter upsert per request.
    """
    if not current_app.extensions["web_settings"].rate_limits_enabled:
        return None
    key = f"{scope}:ip:{request.remote_addr or 'unknown'}"
    try:
        decision = current_app.extensions["web_dependencies"].rate_limiter.hit(key, limit, window_seconds)
    except Exception:
        return render_template("auth.html", mode=mode, error="The service is temporarily unavailable. Please try again shortly."), 503
    if decision.allowed:
        return None
    response = make_response(
        render_template("auth.html", mode=mode, error=rate_limited_message(action, decision.retry_after)), 429
    )
    response.headers["Retry-After"] = str(decision.retry_after)
    return response


@auth.route("/create-account", methods=["GET", "POST"])
def create_account():
    if g.current_user is not None:
        return redirect(url_for("pages.analyze"))
    error = None
    if request.method == "POST":
        limited = throttle_client_ip(
            "create-account",
            current_app.extensions["web_settings"].account_creations_per_hour,
            3600,
            "create",
            "account creation attempts",
        )
        if limited is not None:
            return limited
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        preferred = request.form.get("preferred_engine", "nnue")
        if not valid_csrf():
            error = "Your form expired. Please try again."
        elif not EMAIL.match(email):
            error = "Enter a valid email address."
        elif not 2 <= len(display_name) <= 100:
            error = "Display name must be between 2 and 100 characters."
        elif len(password) < 10:
            error = "Use at least 10 characters for your password."
        elif preferred not in ANALYSIS_ENGINES:
            error = "Choose a supported model."
        else:
            try:
                account = account_store().create_account(
                    email=email,
                    display_name=display_name,
                    password_hash=generate_password_hash(password),
                    preferred_engine=preferred,
                )
            except Exception:
                error = "An account with that email already exists."
            else:
                session.clear()
                session["user_id"] = str(account.id)
                session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("pages.analyze"))
    return render_template("auth.html", mode="create", error=error)


@auth.route("/sign-in", methods=["GET", "POST"])
def sign_in():
    if g.current_user is not None:
        return redirect(url_for("pages.analyze"))
    error = None
    if request.method == "POST":
        limited = throttle_client_ip(
            "sign-in",
            current_app.extensions["web_settings"].sign_in_attempts_per_15_min,
            15 * 60,
            "signin",
            "sign-in attempts",
        )
        if limited is not None:
            return limited
        account = account_store().get_account_by_email(request.form.get("email", "").strip().lower())
        if not valid_csrf():
            error = "Your form expired. Please try again."
        elif account is None or not check_password_hash(account.password_hash, request.form.get("password", "")):
            error = "Email or password is incorrect."
        else:
            session.clear()
            session["user_id"] = str(account.id)
            session["csrf_token"] = secrets.token_urlsafe(32)
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("pages.analyze"))
    return render_template("auth.html", mode="signin", error=error)


@auth.post("/sign-out")
@login_required
def sign_out():
    if valid_csrf():
        session.clear()
    return redirect(url_for("pages.landing"))


__all__ = ["MemoryAccountStore", "auth", "csrf_token", "login_required", "owns_job", "throttle_client_ip", "valid_csrf"]
