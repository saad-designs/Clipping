"""Studio: trim, captions, layouts, full edit, sync against a real local server, VOD picking."""
import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from studio import config as scfg
from studio import vod
from studio.client import Client, ServerError
from studio.editor import captions, pipeline, reframe, transcribe, trim
from studio.editor.transcribe import Word
from studio.sync import sync

HAS_FFMPEG = shutil.which("ffmpeg") is not None
W = [Word(0.5 + i * 0.4, 0.8 + i * 0.4, t) for i, t in enumerate("no way he actually hit that shot chat".split())]


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def make_video(path: Path, seconds: int, size="640x360", loud_at=None):
    vol = f"0.02+0.9*between(t,{loud_at},{loud_at + 4})" if loud_at is not None else "0.05"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                    "-f", "lavfi", "-i", f"sine=f=300:r=48000,volume='{vol}':eval=frame", "-t", str(seconds),
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac", str(path)], check=True)


class TrimTests(unittest.TestCase):
    def test_window_respects_rules(self):
        s, e = trim.choose_window(60, 35, 15, 45, target=35)
        self.assertAlmostEqual(e - s, 35)
        self.assertAlmostEqual(s, 35 - 21)            # 60% before the moment
        s, e = trim.choose_window(60, 35, 15, 20, target=35)
        self.assertAlmostEqual(e - s, 20)             # campaign max wins
        s, e = trim.choose_window(60, 2, None, None, target=35)
        self.assertEqual(s, 0)                        # can't start before the clip

    def test_snaps_to_word_edges(self):
        words = [Word(9.8, 10.4, "hello"), Word(44.6, 45.4, "there")]
        s, e = trim.choose_window(60, 31, None, None, target=35, words=words)
        self.assertLessEqual(s, 9.8)
        self.assertGreaterEqual(e, 45.4)


class CaptionTests(unittest.TestCase):
    def test_chunks_break_on_pause_and_size(self):
        words = W[:4] + [Word(5.0, 5.3, "later")]
        groups = captions.chunk(words, 3)
        self.assertEqual([len(g) for g in groups], [3, 1, 1])

    def test_long_words_split_into_short_lines(self):
        words = [Word(i * 0.3, i * 0.3 + 0.25, t) for i, t in enumerate(["Bundenzielle", "Probleme,", "Probleme,", "ist"])]
        groups = captions.chunk(words, 3, 16)
        self.assertTrue(all(len(" ".join(w.text for w in g)) <= 16 or len(g) == 1 for g in groups), groups)

    def test_ass_times_relative_and_highlighted(self):
        ass = captions.build_ass(W, clip_start=1.0, clip_end=4.0, style=scfg.DEFAULTS["captions"])
        dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue")]
        self.assertTrue(dialogue)
        # "no" ends before the kept part starts, so the first line begins at "way", at t=0
        self.assertTrue(dialogue[0].startswith("Dialogue: 0,0:00:00.00,"), dialogue[0])
        # the spoken word is coloured (#FFD21F in ASS BGR) and pops; the rest of the line follows
        self.assertIn("{\\c&H001FD2FF&", dialogue[0])
        self.assertIn("}WAY{\\c&H00FFFFFF&\\fscx100\\fscy100} HE ACTUALLY", dialogue[0])
        ends = [l.split(",")[2] for l in dialogue]
        starts = [l.split(",")[1] for l in dialogue]
        self.assertTrue(all(e <= s for e, s in zip(ends, starts[1:])), "caption lines overlap")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class EditTests(unittest.TestCase):
    def _cfg(self, out):
        cfg = scfg._merge(scfg.DEFAULTS, {"editing": {"encoder": "libx264"}})
        cfg["output_dir"] = out
        return cfg

    def test_layouts_and_full_edit(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "Campaigns"
            inbox = out / "camp" / "inbox"
            inbox.mkdir(parents=True)
            cfg = self._cfg(out)
            cfg["facecam"] = {"twitch:x": {"cam": [0, 0, 0.3, 0.3]}}
            for layout in ("blur", "crop", "facecam"):
                src = inbox / f"twitch_x_{layout}.mp4"
                make_video(src, 20)
                src.with_suffix(".json").write_text(json.dumps({"channel": "twitch:x", "moment_offset": 10,
                                                                 "rules": {"max_seconds": 12, "required_text": ["#ad"]}}))
                # stand-in transcript (whisper models can't be downloaded in CI)
                with mock.patch.object(transcribe, "transcribe", return_value=[Word(w.start + 4, w.end + 4, w.text) for w in W]):
                    ready = pipeline.edit(src, cfg, layout)
                info = probe(ready)
                v = next(s for s in info["streams"] if s["codec_type"] == "video")
                self.assertEqual((v["width"], v["height"]), (1080, 1920), layout)
                self.assertTrue(11 <= float(info["format"]["duration"]) <= 13.5, info["format"]["duration"])
                self.assertIn("audio", {s["codec_type"] for s in info["streams"]})
                meta = json.loads(ready.with_suffix(".json").read_text())
                self.assertTrue(meta["captions"])
                self.assertIn("#ad", ready.with_suffix(".caption.txt").read_text())
                self.assertTrue((inbox / "done" / src.name).exists())

    def test_reframe_graph_labels(self):
        for layout in ("blur", "crop"):
            self.assertTrue(reframe.build(layout, 30).endswith("[v]"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class SyncTests(unittest.TestCase):
    def test_sync_downloads_into_each_campaign(self):
        import uvicorn
        from shared import config
        from watcher.api import create_app
        from watcher.app import Watcher
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = config.load(root)
            s.data_dir = root / "data"
            s.campaigns = [config.Campaign("alpha", True, [config.ChannelRef("twitch", "x")], max_seconds=30),
                           config.Campaign("beta", True, [config.ChannelRef("twitch", "x")])]
            w = Watcher(s, record=False)
            clip = root / "clip.mp4"
            make_video(clip, 3, size="160x90")
            mid = w.db.add_moment("twitch:x", ["alpha"], 1000.0, 9, {})
            w.db.set_clip(mid, str(clip), 990.0)
            w.db.update(mid, "approved", ["alpha", "beta"])
            skipped = w.db.add_moment("twitch:x", ["alpha"], 2000.0, 9, {})   # not approved
            port = free_port()
            server = uvicorn.Server(uvicorn.Config(create_app(w, "tok"), host="127.0.0.1", port=port, log_level="error"))
            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            for _ in range(50):
                if server.started:
                    break
                time.sleep(0.1)
            try:
                with self.assertRaises(ServerError):
                    sync(Client(f"http://127.0.0.1:{port}", "wrong"), root / "Campaigns")
                new = sync(Client(f"http://127.0.0.1:{port}", "tok"), root / "Campaigns")
                self.assertEqual(len(new), 2)
                for camp in ("alpha", "beta"):
                    files = list((root / "Campaigns" / camp / "inbox").glob("*.mp4"))
                    self.assertEqual(len(files), 1)
                    side = json.loads(files[0].with_suffix(".json").read_text())
                    self.assertEqual(side["moment_offset"], 10.0)
                    self.assertEqual(side["campaign"], camp)
                self.assertEqual(json.loads((next((root / "Campaigns" / "alpha" / "inbox").glob("*.json"))).read_text())["rules"]["max_seconds"], 30)
                self.assertEqual(w.db.get(mid)["status"], "downloaded")
                self.assertEqual(w.db.get(skipped)["status"], "new")
                self.assertEqual(sync(Client(f"http://127.0.0.1:{port}", "tok"), root / "Campaigns"), [])
            finally:
                server.should_exit = True
                t.join(5)


class VodTests(unittest.TestCase):
    def test_youtube_replay_parse(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.live_chat.json"
            line = {"replayChatItemAction": {"videoOffsetTimeMsec": "61500", "actions": [{"addChatItemAction": {"item": {
                "liveChatTextMessageRenderer": {"authorName": {"simpleText": "z"}, "message": {"runs": [{"text": "LMAO"}]}}}}}]}}
            f.write_text(json.dumps(line) + "\nnot json\n")
            self.assertEqual(vod.parse_youtube_replay(f), [(61.5, "z", "LMAO")])

    def test_twitch_comments_parse(self):
        page = {"data": {"video": {"comments": {"pageInfo": {"hasNextPage": True}, "edges": [
            {"cursor": "c1", "node": {"contentOffsetSeconds": 12, "commenter": {"displayName": "a"},
                                      "message": {"fragments": [{"text": "KEKW "}, {"text": "W"}]}}}]}}}}
        self.assertEqual(vod.parse_twitch_comments(page), ([(12.0, "a", "KEKW W")], "c1"))

    def test_twitch_chat_pages_by_offset(self):
        # fake VOD: one comment every 2s from 0 to 300s; each "page" returns 5 comments from the
        # requested second, starting a little earlier like the real API does
        comments = [(float(t), f"u{t}", "hi") for t in range(0, 300, 2)]
        calls = []

        def fetch(_vid, offset):
            calls.append(offset)
            page = [c for c in comments if c[0] >= offset - 3][:5]
            return {"data": {"video": {"comments": {"pageInfo": {"hasNextPage": True}, "edges": [
                {"cursor": "x", "node": {"contentOffsetSeconds": c[0], "commenter": {"displayName": c[1]},
                                         "message": {"fragments": [{"text": c[2]}]}}} for c in page]}}}}
        got = vod.twitch_chat("1", start=10, end=200, fetch=fetch)
        self.assertEqual(got, [c for c in comments if 10 <= c[0] <= 200])   # complete, no duplicates
        self.assertEqual(calls, sorted(calls))                               # always moves forward

    def test_chat_scoring_finds_spike(self):
        import random
        rng = random.Random(4)
        chat = [(t + rng.random(), f"u{rng.randint(1, 200)}", "hi") for t in range(900) for _ in range(2)]
        chat += [(500 + rng.random() * 8, f"v{i}", "KEKW") for i in range(80)]
        picks = vod.pick(vod.score_with_chat(chat, [], 900), 3, 90, 900)
        self.assertTrue(any(485 <= t <= 510 for t, _ in picks), picks)

    def test_pick_spacing(self):
        scored = [(100, 9), (120, 8), (300, 7), (305, 6), (600, 5)]
        self.assertEqual([t for t, _ in vod.pick(scored, 3, 90, 1000)], [100, 300, 600])

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_local_vod_audio_only(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "podcast.mp4"
            make_video(src, 240, size="160x90", loud_at=150)
            cfg = scfg._merge(scfg.DEFAULTS, {})
            cfg["output_dir"] = Path(d) / "Campaigns"
            clips = vod.run(str(src), "pod", cfg, top=2, transcribe_no_chat=False)
            self.assertTrue(clips)
            first = json.loads(clips[0].with_suffix(".json").read_text())
            times = [json.loads(c.with_suffix(".json").read_text())["vod_time"] for c in clips]
            self.assertTrue(any(148 <= t <= 158 for t in times), times)
            self.assertEqual(first["source"], "vod")
            self.assertGreater(float(probe(clips[0])["format"]["duration"]), 20)


if __name__ == "__main__":
    unittest.main()
