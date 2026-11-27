"""Clipping from finished streams: section download from an HLS playlist, moment picking, the sweep loop."""
import functools
import http.server
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from studio import sweep
from studio.store import Store

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class HlsSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        # a 60 s "VOD" in 10 s pieces, like Twitch's index-dvr.m3u8
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                        "-f", "lavfi", "-i", "sine=f=300:r=48000", "-t", "60", "-c:v", "libx264", "-preset", "ultrafast",
                        "-g", "30", "-c:a", "aac", "-f", "hls", "-hls_time", "10", "-hls_list_size", "0",
                        "-hls_segment_filename", str(d / "%d.ts"), str(d / "index-dvr.m3u8")], check=True)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
        handler.log_message = lambda *a: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/index-dvr.m3u8"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def test_downloads_only_needed_pieces(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            out = Path(d) / "clip.mp4"
            start = sweep.hls_section(self.url, 23.0, 41.0, out)
            self.assertEqual(start, 20.0)                     # the piece holding 23 s starts at 20 s
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                        str(out)], capture_output=True, text=True).stdout)
            self.assertAlmostEqual(dur, 30.0, delta=1.0)      # pieces 20-30, 30-40, 40-50
            with self.assertRaises(ValueError):
                sweep.hls_section(self.url, 500, 560, Path(d) / "none.mp4")


class PickingTests(unittest.TestCase):
    def test_viewer_clips_are_picked_and_counted(self):
        v = {"id": "123", "duration": 3600.0, "url": "x"}
        clips = [{"video": {"id": "123"}, "videoOffsetSeconds": 1000, "durationSeconds": 30, "viewCount": 4000},
                 {"video": {"id": "999"}, "videoOffsetSeconds": 2000, "durationSeconds": 30, "viewCount": 9000}]
        with mock.patch.object(sweep.vod, "twitch_chat", return_value=[]):
            moments, _ = sweep.pick_moments("twitch", v, clips, 3, Path("."))
        self.assertEqual(len(moments), 1)                     # the other clip belongs to a different VOD
        self.assertAlmostEqual(moments[0]["at"], 1018.0)
        self.assertEqual(moments[0]["viewer_clips"], 1)

    def test_top_chat_ignores_bots(self):
        chat = [(10, f"u{i}", "KEKW") for i in range(5)] + [(11, "Nightbot", "KEKW")] * 9 + [(99, "u", "W")]
        self.assertEqual(sweep.top_chat(chat, 10), [["KEKW", 5]])


class SweepLoopTests(unittest.TestCase):
    def test_new_vod_becomes_clips_once(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "sd")
            st.save_campaign({"name": "C", "channels": [{"platform": "twitch", "login": "x"}], "clips_per_stream": 2})
            vods = [{"id": "77", "title": "t", "duration": 3600.0, "created": 1e10, "url": "u"}]
            moments = [{"at": 500.0, "score": 12.0, "viewer_clips": 1}, {"at": 900.0, "score": 9.0, "viewer_clips": 0}]
            chat = [(505, "a", "LMAO"), (506, "b", "LMAO")]

            def fake_section(url, start, end, out):
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"x")
                return start - 5
            with mock.patch.object(sweep, "twitch_vods", return_value=(vods, [])), \
                    mock.patch.object(sweep, "pick_moments", return_value=(moments, chat)), \
                    mock.patch.object(sweep, "media_url", return_value="m3u8"), \
                    mock.patch.object(sweep, "hls_section", side_effect=fake_section):
                r = sweep.sweep(st)
                self.assertEqual((r["vods"], r["new_clips"], r["errors"]), (1, 2, []))
                c = sorted(st.clips(), key=lambda x: x["id"])[0]
                self.assertEqual(c["meta"]["moment_offset"], 40.0)          # 500 - (465 - 5)
                self.assertEqual(c["meta"]["signals"]["top_chat"], [["LMAO", 2]])
                self.assertEqual(sweep.sweep(st)["vods"], 0)                 # already done: not clipped twice

    def test_errors_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "sd")
            st.save_campaign({"name": "C", "channels": [{"platform": "twitch", "login": "x"}]})
            with mock.patch.object(sweep, "twitch_vods", side_effect=OSError("offline")):
                r = sweep.sweep(st)
            self.assertEqual(r["vods"], 0)
            self.assertIn("twitch:x", r["errors"][0])

    def test_finished_streams_is_the_default_mode(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "sd").settings()
            self.assertEqual(s["find_mode"], "vods")
            self.assertFalse(s["local_watcher"])


if __name__ == "__main__":
    unittest.main()
