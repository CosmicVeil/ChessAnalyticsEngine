"""Contract tests for bounded, private PGN spooling."""

import stat
import tempfile
import unittest
import uuid
from pathlib import Path

from webapp.domain import SpooledSource
from webapp.spool import SourceTooLarge, SpoolService, describe_byte_limit


class ChunkOnlyStream:
    """Binary stream that rejects unbounded or non-positive reads."""

    def __init__(self, data: bytes, *, fail_after_reads: int | None = None):
        self.data = data
        self.fail_after_reads = fail_after_reads
        self.offset = 0
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size <= 0:
            raise AssertionError("spooling must request a positive chunk size")
        if (
            self.fail_after_reads is not None
            and len(self.read_sizes) >= self.fail_after_reads
        ):
            raise OSError("stream failed after a partial write")

        self.read_sizes.append(size)
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def spool_service(
    root: Path,
    *,
    upload_limit: int = 8,
    pasted_limit: int = 8,
    chunk_size: int = 3,
) -> SpoolService:
    return SpoolService(
        root,
        max_pgn_upload_bytes=upload_limit,
        max_pasted_pgn_bytes=pasted_limit,
        chunk_size=chunk_size,
    )


class SpoolServiceTests(unittest.TestCase):
    def test_file_at_exact_limit_is_written_with_bounded_reads(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "private-spool"
            stream = ChunkOnlyStream(b"12345678")

            source = spool_service(root).spool_file(stream, "batch.pgn")

            self.assertIsInstance(source, SpooledSource)
            self.assertEqual(source.source_type, "file")
            self.assertEqual(source.source_name, "batch.pgn")
            self.assertEqual(source.spool_path.read_bytes(), b"12345678")
            self.assertGreater(len(stream.read_sizes), 1)
            self.assertEqual(set(stream.read_sizes), {3})

    def test_file_one_byte_over_limit_is_rejected_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "private-spool"
            stream = ChunkOnlyStream(b"123456789")

            with self.assertRaises(SourceTooLarge):
                spool_service(root).spool_file(stream, "batch.pgn")

            self.assertEqual(list(root.iterdir()), [])

    def test_stream_failure_removes_partially_written_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "private-spool"
            stream = ChunkOnlyStream(b"123456", fail_after_reads=1)

            with self.assertRaisesRegex(OSError, "partial write"):
                spool_service(root).spool_file(stream, "batch.pgn")

            self.assertEqual(list(root.iterdir()), [])

    def test_pasted_text_limit_counts_utf8_bytes_and_uses_default_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "private-spool"
            service = spool_service(root, pasted_limit=4)

            source = service.spool_text("éé")

            self.assertEqual(source.source_type, "paste")
            self.assertEqual(source.source_name, "pasted-pgn")
            self.assertEqual(source.spool_path.read_bytes(), "éé".encode("utf-8"))
            existing_paths = set(root.iterdir())

            with self.assertRaises(SourceTooLarge):
                service.spool_text("ééa", source_name="too-large.pgn")

            self.assertEqual(set(root.iterdir()), existing_paths)

    def test_source_name_is_sanitized_but_spool_path_is_opaque(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "private-spool"

            source = spool_service(root).spool_file(
                ChunkOnlyStream(b"pgn"),
                "../../private/original-name.pgn",
            )

            self.assertEqual(source.source_name, "original-name.pgn")
            self.assertEqual(source.spool_path.parent, root)
            self.assertNotIn("original-name", source.spool_path.name)
            self.assertEqual(uuid.UUID(source.spool_path.name).version, 4)

    def test_spool_directory_is_created_with_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "nested" / "private-spool"

            spool_service(root).spool_text("1. e4 *")

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)


class DescribeByteLimitTests(unittest.TestCase):
    def test_whole_mebibytes_and_odd_sizes(self):
        self.assertEqual(describe_byte_limit(20 * 1024**2), "20 MiB")
        self.assertEqual(describe_byte_limit(10 * 1024**2), "10 MiB")
        self.assertEqual(describe_byte_limit(1024), "1,024 bytes")
        self.assertEqual(describe_byte_limit(20 * 1024**2 + 1), "20,971,521 bytes")


if __name__ == "__main__":
    unittest.main()
