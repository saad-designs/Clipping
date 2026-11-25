"""Step 2 tests. Use synthetic ffmpeg video, so no internet or Twitch needed (ffmpeg required)."""
import asyncio
import json
import random
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from shared.config import ScorerConfig
from watcher import audio
from watcher.cutter import Cutter, cut, list_segments, segments_for
from watcher.db import DB
from watcher.recorder import Recorder
from watcher.scorer import ChannelScorer

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def make_segment(path: Path, seconds: int, loud_at: float | None = None) -> None:
    vol = f"if(between(t,{loud_at},{loud_at + 1}),1.0,0.02)" if loud_at is not None else "0.02"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                    "-f", "lavfi", "-i", f"sine=f=440:r=48000,volume='{vol}':eval=frame", "-t", str(seconds),
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", "-f", "mpegts", str(path)],
                   check=True)


def duration(path: Path) -> tuple[float, set[str]]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return float(data["format"]["duration"]), {s["codec_type"] for s in data["streams"]}


def live_source(_channel: str) -> list[str]:
    """Fake live stream: generated in real time, like a Twitch stream would arrive."""
    return ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=f=440:r=48000", "-c:v", "libx264", "-preset", "ultrafast",
            "-g", "30", "-c:a", "aac", "-f", "mpegts", "-"]


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class AudioTests(unittest.TestCase):
    def test_finds_loud_second(self):
        with tempfile.TemporaryDirectory() as d:
            seg = Path(d) / "1000.ts"
            make_segment(seg, 10, loud_at=6)
            readings = dict(audio.analyze(seg, 1000))
            loudest = max(readings, key=readings.get)
            self.assertIn(loudest, (1006, 1007))
            self.assertGreater(readings[loudest] - readings[1002], 15)  # clearly louder than baseline


class AudioScoringTests(unittest.TestCase):
    def _run(self, spike_hype: int, loud: bool, seconds: int = 900):
        rng = random.Random(3)
        sc = ChannelScorer("twitch:t", ScorerConfig())
        out = []
        for t in range(seconds):
            sc.add_audio(t, rng.uniform(-30, -26) if not (loud and 594 <= t < 597) else -8)
            for _ in range(rng.randint(1, 5)):
                out += sc.add_message(t + rng.random(), f"u{rng.randint(1, 300)}", "chatting normally")
            if 600 <= t < 610:
                for _ in range(spike_hype):
                    out += sc.add_message(t + rng.random(), f"u{rng.randint(1, 2000)}", "KEKW")
            out += sc.tick(t + 1)
        return out

    def test_audio_boosts_score(self):
        quiet = self._run(15, loud=False)
        loud = self._run(15, loud=True)
        self.assertEqual(len(quiet), 1)
        self.assertEqual(len(loud), 1)
        self.assertGreater(loud[0].score, quiet[0].score + 2)
        self.assertGreater(loud[0].signals["z_audio"], 3)

    def test_audio_alone_never_fires(self):
        self.assertEqual(self._run(0, loud=True), [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class CutterTests(unittest.TestCase):
    def test_segments_for_selects_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            for s in (100, 110, 120, 130, 140):
                (Path(d) / f"{s}.ts").touch()
            chosen = [s for s, _ in segments_for(Path(d), 115, 131)]
            self.assertEqual(chosen, [110, 120, 130])

    def test_cut_moment_into_mp4(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            seg_dir = d / "buffer" / "twitch_test"
            seg_dir.mkdir(parents=True)
            base = 1_700_000_000
            for i in range(8):
                make_segment(seg_dir / f"{base + i * 10}.ts", 10)
            db = DB(d / "w.db")
            mid = db.add_moment("twitch:test", ["c"], base + 40, 9.0, {})
            cutter = Cutter(db, d / "buffer", d / "clips", pre_roll=35, post_roll=20, segment_seconds=10)
            cutter.enqueue(mid, "twitch:test", base + 40)
            self.assertEqual(cutter.oldest_needed("twitch:test"), base + 5)
            self.assertTrue(cutter._ready(cutter.jobs[0], time.time()))
            asyncio.run(cutter._do(cutter.jobs.pop()))
            row = db.get(mid)
            self.assertIsNotNone(row["clip_path"])
            dur, kinds = duration(Path(row["clip_path"]))
            self.assertTrue(55 <= dur <= 70, dur)
            self.assertEqual(kinds, {"video", "audio"})
            self.assertEqual(row["clip_start"], base)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RecorderTests(unittest.TestCase):
    def test_records_segments_and_cleans_up(self):
        seen = []

        async def go(seg_dir: Path):
            rec = Recorder("twitch:test", seg_dir, live_source, segment_seconds=2,
                           on_segment=lambda ch, p, s: seen.append(s), offline_retry=60)
            t0 = time.time()
            rec.start()
            await asyncio.sleep(9)
            await rec.stop()
            return t0

        with tempfile.TemporaryDirectory() as d:
            seg_dir = Path(d) / "seg"
            t0 = asyncio.run(go(seg_dir))
            segs = list_segments(seg_dir)
            self.assertGreaterEqual(len(segs), 3)
            for start, _ in segs:   # names are real wall-clock times
                self.assertTrue(t0 - 1 <= start <= t0 + 10, (t0, start))
            self.assertEqual(sorted(seen), [s for s, _ in segs])   # every segment reported once
            # cut a real clip from the live-recorded buffer
            out = Path(d) / "clip.mp4"
            asyncio.run(cut([p for _, p in segs], out))
            self.assertGreater(duration(out)[0], 5)
            # rolling cleanup
            rec = Recorder("twitch:test", seg_dir, live_source, 2, lambda *a: None, 60)
            removed = rec.cleanup(keep_after=segs[-1][0])
            self.assertEqual(removed, len(segs) - 1)


if __name__ == "__main__":
    unittest.main()
