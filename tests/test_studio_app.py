"""Renderer, PC dashboard API (store + worker), and the server's campaign push. Needs ffmpeg."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from studio import director
from studio.app import create_app
from studio.editor import render, transcribe
from studio.editor.transcribe import Word
from studio.store import Store
from studio.worker import Worker

HAS_FFMPEG = shutil.which("ffmpeg") is not None
WORDS = [Word(2 + i * 0.4, 2.3 + i * 0.4, t) for i, t in enumerate("no way he actually hit that shot".split())]


def make_video(path: Path, seconds: int = 12):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine=f=300:r=48000,volume=0.05", "-t", str(seconds),
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def low_freq_db(path, start, dur=0.6):
    r = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", str(path), "-vn",
                        "-af", "lowpass=f=120,volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    line = [l for l in r.stderr.splitlines() if "mean_volume" in l][0]
    return float(line.split("mean_volume:")[1].split("dB")[0])


class AvoidCamTests(unittest.TestCase):
    def test_game_window_slides_off_the_webcam(self):
        win = 0.51                                              # 1080 of a 2116-wide game strip
        cam = [0.748, 0.0, 0.233, 0.31]                         # top-right webcam, like a real stream
        gx = render.avoid_cam(0.5, cam, win)
        self.assertLessEqual(gx * (1 - win) + win, cam[0] - 0.06 + 1e-9)    # right edge clears the cam
        left_cam = [0.02, 0.6, 0.2, 0.3]
        self.assertGreaterEqual(render.avoid_cam(0.0, left_cam, win) * (1 - win), 0.28 - 1e-9)
        self.assertEqual(render.avoid_cam(0.5, [0.0, 0.0, 0.1, 0.1], win), 0.5)   # no overlap: unchanged


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RenderTests(unittest.TestCase):
    def test_full_hype_render(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "clip.mp4"
            make_video(src)
            logo = d / "logo.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=300x100", "-frames:v", "1", str(logo)], check=True)
            plan = {"keep": [1.0, 10.0], "layout": "facecam_split", "game_x": 0.5,
                    "hook": {"text": "WAIT FOR IT 😳", "seconds": 2.0}, "caption_style": "punch_red",
                    "emphasis_words": ["way"], "zooms": [{"at": 5.0, "strength": 0.15}],
                    "sfx": [{"at": 6.0, "sound": "boom"}], "logo": {"corner": "top_right", "scale": 0.16}}
            out = render.render(src, plan, d / "out.mp4", {"logo_path": str(logo)}, WORDS,
                                {"kind": "webcam", "cam": [0.0, 0.0, 0.3, 0.3]}, {"size": 104}, "libx264")
            info = probe(out)
            v = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (1080, 1920))
            self.assertAlmostEqual(float(info["format"]["duration"]), 9.0, delta=0.3)
            # the boom at 6.0 s in the source = 5.0 s in the output: a clear jump in low frequencies
            self.assertGreater(low_freq_db(out, 5.0) - low_freq_db(out, 2.5), 8)

    def test_every_layout_and_style_renders(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "clip.mp4"
            make_video(src, 6)
            for layout, style in zip(director.LAYOUTS, director.STYLES):
                plan = {"keep": [0.5, 5.0], "layout": layout, "caption_style": style, "hook": {"text": "HI", "seconds": 1.5}}
                out = render.render(src, plan, d / f"{layout}.mp4", {}, WORDS,
                                    {"kind": "center_face", "face": [0.4, 0.3, 0.2, 0.3], "cam": [0, 0, 0.3, 0.3]}, {}, "libx264")
                v = next(s for s in probe(out)["streams"] if s["codec_type"] == "video")
                self.assertEqual((v["width"], v["height"]), (1080, 1920), layout)

    def test_intro_outro_joined(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, intro = d / "clip.mp4", d / "intro.mp4"
            make_video(src, 6)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:r=30", "-t", "2",
                            "-c:v", "libx264", "-preset", "ultrafast", str(intro)], check=True)   # silent intro
            out = render.render(src, {"keep": [0, 5], "layout": "blur_fit", "caption_style": "pop_yellow"}, d / "o.mp4",
                                {"intro_path": str(intro)}, [], None, {}, "libx264")
            self.assertAlmostEqual(float(probe(out)["format"]["duration"]), 7.0, delta=0.35)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "studio_data")
        self.store.save_settings({"output_dir": str(self.root / "Campaigns"), "encoder": "libx264"})
        self.worker = Worker(self.store)
        app = create_app(self.store, self.worker, start_worker=False)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = "Bearer " + app.state.auth.new_token("test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_login_is_required_from_anywhere(self):
        anon = TestClient(self.client.app)
        self.assertEqual(anon.get("/api/clips").status_code, 401)
        self.assertEqual(anon.get("/api/ping").status_code, 200)
        self.assertEqual(anon.post("/api/login", json={"password": "x"}).status_code, 401)     # none set yet
        self.assertEqual(self.client.post("/api/password", json={"password": "short"}).status_code, 400)
        self.client.post("/api/password", json={"password": "correct horse battery"})
        self.assertEqual(anon.post("/api/login", json={"password": "wrong password!"}).status_code, 401)
        token = anon.post("/api/login", json={"password": "correct horse battery"}).json()["token"]
        self.assertEqual(anon.get("/api/clips", headers={"Authorization": f"Bearer {token}"}).status_code, 200)
        r = anon.options("/api/clips", headers={"Origin": "https://x.vercel.app", "Access-Control-Request-Method": "GET"})
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        anon.post("/api/logout", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(anon.get("/api/clips", headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_wrong_passwords_lock_the_login(self):
        anon = TestClient(self.client.app)
        self.client.post("/api/password", json={"password": "correct horse battery"})
        for _ in range(5):
            anon.post("/api/login", json={"password": "nope nope nope"})
        self.assertEqual(anon.post("/api/login", json={"password": "correct horse battery"}).status_code, 429)

    def test_settings_mask_secrets(self):
        self.client.put("/api/settings", json={"anthropic_api_key": "sk-ant-api03-abcdefghijklmnop", "token": "t" * 20})
        s = self.client.get("/api/settings").json()
        self.assertNotIn("abcdefghijklmnop", s["anthropic_api_key"])
        self.client.put("/api/settings", json={"anthropic_api_key": s["anthropic_api_key"]})   # masked value: unchanged
        self.assertEqual(self.store.settings()["anthropic_api_key"], "sk-ant-api03-abcdefghijklmnop")

    def test_campaign_with_logo_then_clip_review_approve(self):
        c = self.client.post("/api/campaigns", json={"name": "Energy Drink", "brief": "Hype clips. Must include #ad.",
                             "channels": [{"platform": "twitch", "login": "XQC"}], "required_text": ["#ad"],
                             "max_seconds": 8, "min_seconds": 3}).json()
        self.assertEqual(c["channels"][0]["login"], "xqc")
        logo = self.root / "l.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x80", "-frames:v", "1", str(logo)], check=True)
        r = self.client.post(f"/api/campaigns/{c['slug']}/asset", data={"kind": "logo"}, files={"file": ("logo.png", logo.read_bytes())})
        self.assertTrue(Path(r.json()["logo_path"]).exists())

        clip_file = self.root / "upload.mp4"
        make_video(clip_file, 10)
        r = self.client.post("/api/ingest", data={"campaign": "Energy Drink", "whole": "true"},
                             files={"file": ("upload.mp4", clip_file.read_bytes())})
        cid = r.json()["id"]
        with mock.patch.object(transcribe, "transcribe", return_value=WORDS):
            self.worker.process(self.store.next_queued())
        clip = self.client.get("/api/clips?status=review").json()[0]
        self.assertEqual(clip["id"], cid)
        self.assertLessEqual(clip["plan"]["keep"][1] - clip["plan"]["keep"][0], 8)
        self.assertIn("#ad", clip["plan"]["post"]["hashtags"])
        self.assertEqual(self.client.get(f"/api/clips/{cid}/video").status_code, 200)
        # <video> tags can't send the login: the signed link works on its own, a forged one doesn't
        anon = TestClient(self.client.app)
        self.assertEqual(anon.get(clip["video_url"]).status_code, 200)
        r = anon.get(clip["video_url"] + "&dl=1")
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(anon.get(f"/api/clips/{cid}/video?sig=9999999999.deadbeef").status_code, 401)
        self.assertEqual(anon.get(clip["video_url"].replace(f"/clips/{cid}/", f"/clips/{cid + 1}/")).status_code, 401)
        camp = self.client.get("/api/campaigns").json()[0]
        self.assertEqual(anon.get(camp["logo_url"]).status_code, 200)

        # change the hook -> re-render with exactly that plan (no director)
        self.client.post(f"/api/clips/{cid}/edit", json={"hook_text": "MY OWN HOOK"})
        q = self.store.next_queued()
        self.assertTrue(q["lock_plan"])
        self.worker.process(q)
        self.assertEqual(self.store.clip(cid)["plan"]["hook"]["text"], "MY OWN HOOK")

        r = self.client.post(f"/api/clips/{cid}/approve").json()
        ready = Path(r["out_path"])
        self.assertEqual(ready.parent.name, "ready")
        self.assertIn("#ad", ready.with_suffix(".caption.txt").read_text())
        r = self.client.post(f"/api/clips/{cid}/posted").json()
        self.assertEqual(Path(r["out_path"]).parent.name, "posted")

    def test_failed_clip_reports_error_and_retries(self):
        self.client.post("/api/campaigns", json={"name": "C"})
        cid = self.store.add_clip("C", str(self.root / "missing.mp4"))
        self.worker.process(self.store.next_queued())
        self.assertEqual(self.store.clip(cid)["status"], "failed")
        self.assertTrue(self.store.clip(cid)["error"])
        self.client.post(f"/api/clips/{cid}/retry")
        self.assertEqual(self.store.clip(cid)["status"], "queued")


class ServerPushTests(unittest.TestCase):
    def test_put_campaigns_writes_yaml_and_restarts(self):
        from shared import config
        from watcher.api import create_app as server_app
        from watcher.app import Watcher
        with tempfile.TemporaryDirectory() as d:
            s = config.load(d)
            s.data_dir = Path(d) / "data"
            s.campaigns = [config.Campaign("old", True, [config.ChannelRef("twitch", "a")])]
            w = Watcher(s, record=False)
            client = TestClient(server_app(w, "tok"))
            r = client.put("/api/campaigns", headers={"X-Token": "tok"}, json={"campaigns": [
                {"name": "Energy Drink", "active": True, "min_seconds": 10, "max_seconds": 40, "required_text": ["#ad"],
                 "channels": [{"platform": "kick", "login": "adinross"}, {"platform": "twitch", "login": "xqc"}]}]})
            self.assertEqual(r.json()["channels"], 2)
            self.assertTrue(w.restart)
            loaded = config.load(d)
            self.assertEqual([c.key for c in loaded.campaigns[0].channels], ["kick:adinross", "twitch:xqc"])
            self.assertEqual(loaded.campaigns[0].max_seconds, 40)
            self.assertEqual(client.put("/api/campaigns", json={"campaigns": []}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
