"""API, platform parsers and viewer-clip matching. No network needed."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from shared import config
from watcher.api import create_app
from watcher.app import Watcher
from watcher.platforms import kick, youtube
from watcher.platforms.twitch import clip_center

TOKEN = "test-token"


def make_settings(root: Path) -> config.Settings:
    s = config.load(root)
    s.data_dir = root / "data"
    s.campaigns = [
        config.Campaign("alpha", True, [config.ChannelRef("twitch", "streamer")], required_text=["#ad"]),
        config.Campaign("beta", True, [config.ChannelRef("kick", "kicker")]),
    ]
    return s


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.w = Watcher(make_settings(root), record=False)
        self.client = TestClient(create_app(self.w, TOKEN))
        self.h = {"X-Token": TOKEN}
        self.mid = self.w.db.add_moment("twitch:streamer", ["alpha"], 1000.0, 9.5, {"messages": 40})
        if shutil.which("ffmpeg"):
            clip = root / "clip.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                            "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
            self.w.db.set_clip(self.mid, str(clip), 990.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_requires_token(self):
        self.assertEqual(self.client.get("/api/moments").status_code, 401)
        self.assertEqual(self.client.get("/api/moments", headers={"X-Token": "nope"}).status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_list_and_approve(self):
        ms = self.client.get("/api/moments?status=new", headers=self.h).json()
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0]["moment_offset"], 10.0)
        self.assertNotIn("clip_path", ms[0])     # server paths stay private
        r = self.client.post(f"/api/moments/{self.mid}", headers=self.h, json={"status": "approved", "campaigns": ["alpha", "beta"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["campaigns"], ["alpha", "beta"])
        self.assertEqual(len(self.client.get("/api/moments?status=approved", headers=self.h).json()), 1)
        self.assertEqual(self.client.post(f"/api/moments/{self.mid}", headers=self.h, json={"campaigns": ["zzz"]}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/moments/{self.mid}", headers=self.h, json={"status": "lol"}).status_code, 400)
        self.client.post(f"/api/moments/{self.mid}/downloaded", headers=self.h)
        self.assertEqual(self.w.db.get(self.mid)["status"], "downloaded")

    def test_campaigns_and_health(self):
        camps = self.client.get("/api/campaigns", headers=self.h).json()
        self.assertEqual(camps[0]["required_text"], ["#ad"])
        h = self.client.get("/api/health", headers=self.h).json()
        self.assertEqual({c["channel"] for c in h["channels"]}, {"twitch:streamer", "kick:kicker"})

    def test_dashboard_token_cookie(self):
        r = self.client.get(f"/?token={TOKEN}", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("token=", r.headers["set-cookie"])
        self.client.cookies.set("token", TOKEN)
        page = self.client.get("/")
        self.assertIn("Clip Review", page.text)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_clip_download_with_range(self):
        full = self.client.get(f"/api/clips/{self.mid}", headers=self.h)
        self.assertEqual(full.status_code, 200)
        part = self.client.get(f"/api/clips/{self.mid}", headers={**self.h, "Range": "bytes=0-99"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(len(part.content), 100)


class ViewerClipTests(unittest.TestCase):
    def test_clip_matches_nearest_moment(self):
        with tempfile.TemporaryDirectory() as d:
            w = Watcher(make_settings(Path(d)), record=False)
            center = clip_center({"created_at": "2026-01-01T12:00:30Z", "duration": 30})
            a = w.db.add_moment("twitch:streamer", ["alpha"], center - 20, 8, {})
            b = w.db.add_moment("twitch:streamer", ["alpha"], center + 200, 8, {})
            self.assertEqual(w.db.add_viewer_clips("twitch:streamer", center, 45), a)
            self.assertEqual(w.db.get(a)["viewer_clips"], 1)
            self.assertEqual(w.db.get(b)["viewer_clips"], 0)
            self.assertEqual(w.db.add_viewer_clips("twitch:streamer", center + 1000, 45), 0)


class KickParserTests(unittest.TestCase):
    def test_channel(self):
        room, live = kick.parse_channel({"id": 1, "chatroom": {"id": 668}, "livestream": {
            "is_live": True, "session_title": "chill", "viewer_count": 900}})
        self.assertEqual(room, 668)
        self.assertEqual(live["viewers"], 900)
        self.assertEqual(kick.parse_channel({"chatroom": {"id": 5}, "livestream": None}), (5, None))

    def test_pusher_chat(self):
        frame = json.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.668.v2",
                            "data": json.dumps({"chatroom_id": 668, "content": "W [emote:37226:KEKW]",
                                                "sender": {"username": "bob"}})})
        self.assertEqual(kick.parse_pusher(frame), (668, "bob", "W KEKW"))
        self.assertIsNone(kick.parse_pusher('{"event":"pusher:connection_established","data":"{}"}'))


class YouTubeParserTests(unittest.TestCase):
    def test_live_page(self):
        html = ('<link rel="canonical" href="https://www.youtube.com/watch?v=abcdefghijk">'
                '<meta name="title" content="Big stream"> ... "isLiveNow":true ...')
        self.assertEqual(youtube.parse_live_page(html), {"video_id": "abcdefghijk", "title": "Big stream"})
        self.assertIsNone(youtube.parse_live_page('<link rel="canonical" href="https://www.youtube.com/@x">'))

    def test_chat_page(self):
        html = ('"INNERTUBE_API_KEY":"KEY123","INNERTUBE_CONTEXT_CLIENT_VERSION":"2.2026","continuations":[{'
                '"invalidationContinuationData":{"invalidationId":{},"timeoutMs":10000,"continuation":"CONT1"}}]')
        self.assertEqual(youtube.parse_chat_page(html), ("KEY123", "2.2026", "CONT1"))

    def test_chat_response(self):
        data = {"continuationContents": {"liveChatContinuation": {
            "continuations": [{"timedContinuationData": {"continuation": "NEXT", "timeoutMs": 4000}}],
            "actions": [
                {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                    "timestampUsec": "1700000000000000", "authorName": {"simpleText": "amy"},
                    "message": {"runs": [{"text": "no way "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}},
                                         {"emoji": {"emojiId": "UCx/abc", "isCustomEmoji": True, "shortcuts": [":kekw:"]}}]}}}}},
                {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {}}}},
            ]}}}
        msgs, nxt, wait = youtube.parse_chat_response(data)
        self.assertEqual(msgs, [(1700000000.0, "amy", "no way 😂:kekw:")])
        self.assertEqual((nxt, wait), ("NEXT", 4000))

    def test_record_cmd_uses_cookies(self):
        yt = youtube.YouTube(["somehandle"], lambda *a: None, cookies_file="")
        cmd = yt.record_cmd("somehandle", {"video_id": "abcdefghijk"}, "720p60,best")
        self.assertIn("best[height<=720]/best", cmd)
        self.assertEqual(cmd[-1], "https://www.youtube.com/watch?v=abcdefghijk")


class ConfigTests(unittest.TestCase):
    def test_youtube_channel_id_case_kept(self):
        self.assertEqual(config._channel({"platform": "YouTube", "login": "UCabcDEF"}).login, "UCabcDEF")
        self.assertEqual(config._channel({"platform": "youtube", "login": "@MrBeast"}).login, "mrbeast")
        self.assertEqual(config._channel({"platform": "twitch", "login": "XQC"}).login, "xqc")


if __name__ == "__main__":
    unittest.main()
