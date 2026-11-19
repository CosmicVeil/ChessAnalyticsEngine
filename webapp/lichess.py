"""Connector that exports a Lichess Study (or one chapter) as PGN."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import urlsplit


LICHESS_HOSTS = frozenset({"lichess.org", "www.lichess.org"})
LICHESS_API_ROOT = "https://lichess.org/api/study"
_ID = re.compile(r"[A-Za-z0-9]{8}")


class LichessError(Exception):
    """Base class for connector failures surfaced to API callers."""


class InvalidStudyUrl(LichessError):
    pass


class StudyNotFound(LichessError):
    pass


class StudyForbidden(LichessError):
    pass


class LichessUnavailable(LichessError):
    pass


@dataclass(frozen=True, slots=True)
class StudyRef:
    study_id: str
    chapter_id: str | None = None

    @property
    def export_url(self) -> str:
        if self.chapter_id is None:
            return f"{LICHESS_API_ROOT}/{self.study_id}.pgn"
        return f"{LICHESS_API_ROOT}/{self.study_id}/{self.chapter_id}.pgn"

    @property
    def source_name(self) -> str:
        suffix = f"-{self.chapter_id}" if self.chapter_id else ""
        return f"lichess-study-{self.study_id}{suffix}.pgn"


def parse_study_url(raw_url: str) -> StudyRef:
    """Accept only lichess.org/study/<id>[/<chapter>] and extract the IDs.

    The export URL is rebuilt from the IDs, so user input never chooses
    which host the server talks to.
    """

    value = raw_url.strip()
    if "://" not in value:
        value = f"https://{value}"
    try:
        parts = urlsplit(value)
        parts.port  # urlsplit validates the port lazily
    except ValueError as error:
        raise InvalidStudyUrl("Enter a lichess.org/study/… URL.") from error
    if parts.scheme not in {"http", "https"} or (parts.hostname or "") not in LICHESS_HOSTS:
        raise InvalidStudyUrl("Only lichess.org Study URLs are supported.")
    segments = [segment for segment in parts.path.split("/") if segment]
    if not 2 <= len(segments) <= 3 or segments[0] != "study":
        raise InvalidStudyUrl("Enter a lichess.org/study/… URL.")
    ids = segments[1:]
    if not all(_ID.fullmatch(segment) for segment in ids):
        raise InvalidStudyUrl("That Study URL has an unrecognized study or chapter ID.")
    return StudyRef(ids[0], ids[1] if len(ids) == 2 else None)


class LichessStudyClient:
    """Opens the PGN export as a binary stream for the spool to consume."""

    def __init__(
        self,
        api_token: str = "",
        *,
        timeout: float = 15.0,
        opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    ) -> None:
        self._api_token = api_token
        self._timeout = timeout
        self._opener = opener

    def open_pgn(self, ref: StudyRef) -> BinaryIO:
        headers = {"Accept": "application/x-chess-pgn"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        request = urllib.request.Request(ref.export_url, headers=headers)
        try:
            return self._opener(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                raise StudyNotFound("That Lichess Study was not found.") from error
            if error.code in {401, 403}:
                raise StudyForbidden(
                    "That Lichess Study is private. Make it public or unlisted and try again."
                ) from error
            raise LichessUnavailable(f"Lichess returned HTTP {error.code}.") from error
        except (urllib.error.URLError, OSError) as error:
            raise LichessUnavailable("Lichess could not be reached.") from error


__all__ = [
    "InvalidStudyUrl",
    "LichessError",
    "LichessStudyClient",
    "LichessUnavailable",
    "StudyForbidden",
    "StudyNotFound",
    "StudyRef",
    "parse_study_url",
]
