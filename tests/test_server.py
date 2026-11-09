import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ParserTests(unittest.TestCase):
    def test_release_name(self):
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "Interstellar.2014.2160p.BluRay.x265.mkv"
            video.touch()
            self.assertEqual(server.metadata(video), ("Interstellar", "", 2014))

    def test_plex_style(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "The Matrix (1999)"
            folder.mkdir()
            video = folder / "movie.mkv"
            video.touch()
            self.assertEqual(server.metadata(video), ("The Matrix", "", 1999))

    def test_nfo(self):
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "Localized.Film.mkv"
            video.touch()
            video.with_suffix(".nfo").write_text("<movie><title>Le Voyage</title><originaltitle>The Journey</originaltitle><year>2005</year></movie>")
            self.assertEqual(server.metadata(video), ("Le Voyage", "The Journey", 2005))

    def test_year_check(self):
        self.assertGreaterEqual(server.score_candidate("The Matrix", "", 1999, "The.Matrix.1999.1080p.BluRay"), .89)
        self.assertLess(server.score_candidate("The Matrix", "", 1999, "The.Matrix.2003.1080p.BluRay"), .89)

    def test_directory_sandbox(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(server, "MEDIA_ROOT", Path(root).resolve()):
                self.assertEqual(server.safe_directory("."), Path(root).resolve())
                with self.assertRaises(ValueError):
                    server.safe_directory("../")

    def test_end_to_end_mock_scan(self):
        xml = b'<rss><channel><item><title>My.Movie.2001.1080p</title></item></channel></rss>'
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            media = base / 'media'
            media.mkdir()
            (media / 'My.Movie.2001.mkv').write_bytes(b'fake-video')
            with patch.object(server, 'MEDIA_ROOT', media), patch.object(server, 'DATA_DIR', base / 'config'), \
                 patch.object(server, 'API_KEY', 'fake'), patch.object(server, 'APP_PASSWORD', 'pass'), \
                 patch.object(server, 'REQUEST_INTERVAL', 0), patch.object(server, 'LAST_REQUEST', [0]), \
                 patch('server.urlopen') as fetch:
                class Response:
                    def __enter__(self): return self
                    def __exit__(self,*args): return False
                    def read(self,limit): return xml
                fetch.return_value = Response()
                server.initialize()
                server.STOP_EVENT.clear()
                server.run_scan('.', False)
                with server.db() as conn:
                    result = conn.execute('SELECT status,match_title FROM movies').fetchone()
                    self.assertEqual(result['status'],'found')
                    self.assertEqual(result['match_title'],'My.Movie.2001.1080p')
                self.assertEqual(server.STATE['phase'],'done')
                self.assertEqual(fetch.call_count, 1)

    def test_xml_parser(self):
        xml = b'<rss><channel><item><title>My.Movie.2001.1080p</title></item></channel></rss>'
        with tempfile.TemporaryDirectory() as root:
            with patch.object(server, "DATA_DIR", Path(root)):
                server.initialize()
                with server.db() as conn, patch.object(server, "API_KEY", "fake"), patch.object(server, "LAST_REQUEST", [0]), patch.object(server, "REQUEST_INTERVAL", 0), patch("server.urlopen") as fetch:
                    class Response:
                        def __enter__(self): return self
                        def __exit__(self,*args): return False
                        def read(self, limit): return xml
                    fetch.return_value = Response()
                    self.assertEqual(server.search_c411(conn, "My Movie"), ["My.Movie.2001.1080p"])
                    self.assertEqual(server.search_c411(conn, "My Movie"), ["My.Movie.2001.1080p"])
                    self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
