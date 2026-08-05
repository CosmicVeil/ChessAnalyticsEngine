"""Private, bounded storage for submitted PGN sources."""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import BinaryIO

from webapp.domain import SpooledSource


class SourceTooLarge(ValueError):
    """Raised after a source exceeds its configured byte limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"PGN source exceeds the {limit}-byte limit")
        self.limit = limit


class SpoolService:
    """Writes uploads in bounded chunks to opaque, private paths."""

    def __init__(
        self,
        root: Path,
        *,
        max_pgn_upload_bytes: int,
        max_pasted_pgn_bytes: int,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if min(max_pgn_upload_bytes, max_pasted_pgn_bytes, chunk_size) <= 0:
            raise ValueError("spool limits and chunk size must be positive")
        self.root = Path(root)
        self.max_pgn_upload_bytes = max_pgn_upload_bytes
        self.max_pasted_pgn_bytes = max_pasted_pgn_bytes
        self.chunk_size = chunk_size

    def spool_file(
        self, stream: BinaryIO, source_name: str
    ) -> SpooledSource:
        return self._spool(
            stream,
            source_type="file",
            source_name=source_name,
            limit=self.max_pgn_upload_bytes,
        )

    def spool_text(
        self, text: str, source_name: str = "pasted-pgn"
    ) -> SpooledSource:
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_pasted_pgn_bytes:
            raise SourceTooLarge(self.max_pasted_pgn_bytes)
        return self._spool(
            io.BytesIO(encoded),
            source_type="paste",
            source_name=source_name,
            limit=self.max_pasted_pgn_bytes,
        )

    def _spool(
        self,
        stream: BinaryIO,
        *,
        source_type: str,
        source_name: str,
        limit: int,
    ) -> SpooledSource:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        destination = self.root / str(uuid.uuid4())
        written = 0
        try:
            with destination.open("xb") as output:
                while True:
                    chunk = stream.read(self.chunk_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("PGN upload stream must be binary")
                    written += len(chunk)
                    if written > limit:
                        raise SourceTooLarge(limit)
                    output.write(chunk)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

        return SpooledSource(
            source_type=source_type,
            source_name=_display_name(source_name),
            spool_path=destination,
        )


def _display_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].strip()
    return (name or "uploaded.pgn")[:255]


__all__ = ["SourceTooLarge", "SpoolService"]
