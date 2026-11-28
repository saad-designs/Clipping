"""The scout: the director watches whole streams to pick moments."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio import scan, sweep
from studio.store import Store


class PieceTests(unittest.TestCase):
    def test_pieces_overlap_and_cover_the_ranges(self):
        p = scan.chunks([(0.0, 3000.0)])
        self.assertEqual(p[0], (0.0, 1200.0))
        self.assertEqual(p[1][0], 1200.0 - scan.OVERLAP)
        self.assertGreaterEqual(p[-1][1], 3000.0)
        self.assertEqual(scan.chunks([(100.0, 110.0)]), [])          # too short to bother

    def test_duplicates_from_overlap_keep_the_best(self):
        m = scan.dedupe([{"at": 100, "score": 6}, {"at": 110, "score": 8}, {"at": 500, "score": 7}])
        self.assertEqual([(x["at"], x["score"]) for x in m], [(110, 8), (500, 7)])

    def test_hints_are_in_piece_time(self):
        chat = [(1000 + i, "u", "x") for i in range(5)] + [(1065, "u", "x")]
        h = scan.hints(1000, 1200, chat, [{"videoOffsetSeconds": 1100, "viewCount": 42}])
        self.assertIn("100s (42 views)", h)
        self.assertIn("0s: 5 msgs", h)


class WindowTests(unittest.TestCase):
    def test_scout_window_covers_setup_to_reaction(self):
        self.assertEqual(sweep.window({"at": 500, "start": 470, "end": 520}), (464, 526))
        a, b = sweep.window({"at": 500, "start": 300, "end": 700})       # capped at 100 s around the peak
        self.assertLessEqual(b - a, 100)
        self.assertTrue(a <= 500 <= b)
        a, b = sweep.window({"at": 500, "start": 498, "end": 502})       # at least 30 s
        self.assertGreaterEqual(b - a, 30)
        self.assertEqual(sweep.window({"at": 500}), (500 - sweep.PRE, 500 + sweep.POST))


class ScanVodTests(unittest.TestCase):
    def test_results_are_cached_per_piece_and_in_vod_time(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "sd")
            v = {"id": "9", "url": "u", "duration": 1500.0}
            calls = []

            def fake_piece(key, video, camp, streamer, hint):
                calls.append(1)
                return {"summary": "s", "moments": [{"start": 10, "peak": 20, "end": 30, "what": "w", "why": "y", "score": 8}],
                        "_model": "m"}

            def fake_section(url, a, b, out):
                out.write_bytes(b"x")
                return a
            with mock.patch.object(scan, "low_url", return_value="m3u8"), \
                    mock.patch("studio.sweep.hls_section", side_effect=fake_section), \
                    mock.patch.object(scan, "tiny", return_value=b"v"), \
                    mock.patch.object(scan, "scan_piece", side_effect=fake_piece):
                found = scan.scan_vod(st, "k", v, [(0.0, 1500.0)], {}, "x", [], [])
                again = scan.scan_vod(st, "k", v, [(0.0, 1500.0)], {}, "x", [], [])
            self.assertEqual(len(calls), 2)                     # 2 pieces, second run fully from cache
            self.assertEqual([m["at"] for m in found], [20.0, 1175.0])
            self.assertEqual(found, again)

    def test_sweep_uses_the_scout_when_gemini_is_on(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "sd")
            st.save_settings({"director": "gemini", "gemini_api_key": "k"})
            st.save_campaign({"name": "C", "channels": [{"platform": "twitch", "login": "x"}], "clips_per_stream": 1})
            vods = [{"id": "77", "title": "t", "duration": 3600.0, "created": 1e10, "url": "u"}]
            found = [{"start": 100, "at": 120, "end": 130, "score": 5, "what": "meh", "why": ""},
                     {"start": 900, "at": 915, "end": 930, "score": 8, "what": "she falls in", "why": "irony"},
                     {"start": 2000, "at": 2010, "end": 2020, "score": 7, "what": "ok", "why": ""}]

            def fake_section(url, start, end, out):
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"x")
                return start
            with mock.patch.object(sweep, "twitch_vods", return_value=(vods, [])), \
                    mock.patch.object(sweep.vod, "twitch_chat", return_value=[]), \
                    mock.patch.object(scan, "scan_vod", return_value=found), \
                    mock.patch.object(sweep, "media_url", return_value="m3u8"), \
                    mock.patch.object(sweep, "hls_section", side_effect=fake_section):
                r = sweep.sweep(st)
            self.assertEqual((r["new_clips"], r["errors"]), (1, []))
            c = st.clips()[0]
            self.assertEqual(c["meta"]["vod_time"], 915)                   # the best one, not the first
            self.assertEqual(c["meta"]["scout"]["what"], "she falls in")
            self.assertEqual(c["meta"]["scout"]["peak"], c["meta"]["moment_offset"])


if __name__ == "__main__":
    unittest.main()
