"""One-PC mode: segment names that work on Windows, the watcher supervisor, settings written for the watcher."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from studio import local_watcher
from studio.local_watcher import LocalWatcher
from studio.store import Store
from watcher.cutter import list_segments, segment_start


class SegmentNameTests(unittest.TestCase):
    def test_new_and_old_names(self):
        t = time.mktime(time.strptime("20261126-181502", "%Y%m%d-%H%M%S"))
        self.assertEqual(segment_start("20261126-181502"), t)
        self.assertEqual(segment_start("1795716902"), 1795716902.0)
        self.assertIsNone(segment_start("notes"))
        with tempfile.TemporaryDirectory() as d:
            for n in ("20261126-181512", "20261126-181502", "junk"):
                (Path(d) / f"{n}.ts").touch()
            self.assertEqual([p.stem for _, p in list_segments(Path(d))], ["20261126-181502", "20261126-181512"])


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "studio_data")
        self.lw = LocalWatcher(self.store)
        self.launches = []
        script = Path(self.tmp.name) / "fake.py"
        # a stand-in for the watcher: exits with the code written in a file (or keeps running)
        script.write_text("import sys, time, pathlib\n"
                          "f = pathlib.Path(sys.argv[1])\n"
                          "code = f.read_text().strip() if f.exists() else ''\n"
                          "f.unlink(missing_ok=True)\n"
                          "sys.exit(int(code)) if code else time.sleep(60)\n")
        self.codefile = Path(self.tmp.name) / "code.txt"
        import subprocess

        def launch():
            self.launches.append(time.time())
            return subprocess.Popen([sys.executable, str(script), str(self.codefile)])
        self.lw._launch = launch

    def tearDown(self):
        self.lw.stop()
        self.tmp.cleanup()

    def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def test_waits_for_a_campaign_then_starts(self):
        self.lw.start()
        self.assertTrue(self.wait_for(lambda: self.lw.waiting))
        self.assertEqual(self.launches, [])
        self.store.save_campaign({"name": "C", "channels": [{"platform": "twitch", "login": "x"}]})
        self.lw.restart()
        self.assertTrue(self.wait_for(self.lw.running))
        cfg = yaml.safe_load((self.lw.root / "campaigns.yaml").read_text())
        self.assertEqual(cfg["campaigns"][0]["channels"][0]["login"], "x")

    def test_relaunches_after_restart_request_and_on_restart(self):
        self.store.save_campaign({"name": "C", "channels": [{"platform": "twitch", "login": "x"}]})
        self.codefile.write_text(str(local_watcher.RESTART_EXIT_CODE))
        self.lw.start()
        self.assertTrue(self.wait_for(lambda: len(self.launches) >= 2 and self.lw.running()))   # exit 3 -> relaunched
        n = len(self.launches)
        self.lw.restart()
        self.assertTrue(self.wait_for(lambda: len(self.launches) > n and self.lw.running()))
        self.lw.stop()
        self.assertFalse(self.lw.running())

    def test_settings_written_for_the_watcher(self):
        self.store.save_settings({"twitch_client_id": "id1", "twitch_client_secret": "sec", "max_recording": 2, "quality": "1080p"})
        cfg = yaml.safe_load(self.lw.write_settings().read_text())
        self.assertEqual(cfg["twitch"], {"client_id": "id1", "client_secret": "sec"})
        self.assertEqual(cfg["recorder"]["max_concurrent"], 2)
        self.assertEqual(cfg["recorder"]["quality"], "best")
        self.assertEqual(cfg["api"]["host"], "127.0.0.1")
        s = self.store.settings()
        self.assertEqual(s["server_url"], "http://127.0.0.1:8787")       # Studio talks to the local watcher
        self.assertEqual(s["token"], cfg["api"]["token"])


if __name__ == "__main__":
    unittest.main()
