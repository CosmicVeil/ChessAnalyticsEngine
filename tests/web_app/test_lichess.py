"""Lichess Study URL parsing and export client behavior."""

import io
import unittest
import urllib.error

from webapp.lichess import (
    InvalidStudyUrl,
    LichessStudyClient,
    LichessUnavailable,
    StudyForbidden,
    StudyNotFound,
    StudyRef,
    parse_study_url,
)


class ParseStudyUrlTests(unittest.TestCase):
    def test_accepts_study_and_chapter_urls_in_common_shapes(self):
        cases = {
            "https://lichess.org/study/abcdEFGH": StudyRef("abcdEFGH"),
            "  lichess.org/study/abcdEFGH/  ": StudyRef("abcdEFGH"),
            "https://www.lichess.org/study/abcdEFGH/Ch4pter1#23": StudyRef("abcdEFGH", "Ch4pter1"),
            "http://lichess.org/study/abcdEFGH?x=1": StudyRef("abcdEFGH"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(parse_study_url(url), expected)

    def test_rejects_other_hosts_paths_and_malformed_ids(self):
        for url in [
            "https://evil.example/study/abcdEFGH",
            "https://lichess.org.evil.example/study/abcdEFGH",
            "https://lichess.org@evil.example/study/abcdEFGH",
            "ftp://lichess.org/study/abcdEFGH",
            "https://lichess.org/abcdEFGH",
            "https://lichess.org/study/short",
            "https://lichess.org/study/abcdEFGH/chap1234/extra",
            "https://lichess.org/study/abcd..GH",
            "https://lichess.org:99999/study/abcdEFGH",
        ]:
            with self.subTest(url=url), self.assertRaises(InvalidStudyUrl):
                parse_study_url(url)

    def test_export_url_is_built_from_ids_only(self):
        self.assertEqual(StudyRef("abcdEFGH").export_url, "https://lichess.org/api/study/abcdEFGH.pgn")
        self.assertEqual(
            StudyRef("abcdEFGH", "chap1234").export_url,
            "https://lichess.org/api/study/abcdEFGH/chap1234.pgn",
        )


class LichessStudyClientTests(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError("https://lichess.org", code, "x", {}, io.BytesIO())

    def test_sends_token_only_when_configured_and_returns_stream(self):
        seen = []

        def opener(request, timeout):
            seen.append((request, timeout))
            return io.BytesIO(b"pgn")

        stream = LichessStudyClient("secret", timeout=3, opener=opener).open_pgn(StudyRef("abcdEFGH"))
        self.assertEqual(stream.read(), b"pgn")
        request, timeout = seen[0]
        self.assertEqual(request.full_url, "https://lichess.org/api/study/abcdEFGH.pgn")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(timeout, 3)

        LichessStudyClient(opener=opener).open_pgn(StudyRef("abcdEFGH"))
        self.assertIsNone(seen[1][0].get_header("Authorization"))

    def test_maps_http_and_network_failures(self):
        cases = [
            (self._http_error(404), StudyNotFound),
            (self._http_error(403), StudyForbidden),
            (self._http_error(401), StudyForbidden),
            (self._http_error(429), LichessUnavailable),
            (urllib.error.URLError("dns"), LichessUnavailable),
            (TimeoutError(), LichessUnavailable),
        ]
        for raised, expected in cases:
            def opener(request, timeout, raised=raised):
                raise raised

            with self.subTest(raised=raised), self.assertRaises(expected):
                LichessStudyClient(opener=opener).open_pgn(StudyRef("abcdEFGH"))


if __name__ == "__main__":
    unittest.main()
