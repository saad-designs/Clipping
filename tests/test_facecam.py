"""Finding the webcam overlay's real border (not a guess from the face size)."""
import unittest

import numpy as np

from studio.editor import facecam as fc

W, H = fc.SAMPLE_W, fc.SAMPLE_H
CAM = (int(0.70 * W), 0, int(0.98 * W), int(0.40 * H))          # x0, y0, x1, y1: top-right, touching the top


def frames(n=16, seed=1):
    """Moving 'game' everywhere, a still 'room' inside the overlay with a headboard line, a
    slightly different face each frame, and a thin border around the overlay like OBS draws."""
    rng = np.random.default_rng(seed)
    room = np.clip(rng.normal(70, 25, (H, W, 3)), 0, 255).astype(np.uint8)
    x0, y0, x1, y1 = CAM
    room[y0 + 120:y0 + 124, x0 + 20:x1 - 20] = 220                  # headboard: a long line INSIDE the room
    out = []
    for i in range(n):
        f = np.clip(rng.normal(120, 60, (H, W, 3)), 0, 255).astype(np.uint8)   # the game changes every frame
        f = np.ascontiguousarray(np.roll(f, i * 37, axis=1))
        f[y0:y1, x0:x1] = room[y0:y1, x0:x1]
        f[y0 + 40 + i % 3:y0 + 100, x0 + 110:x0 + 170] = 200 - i              # the streamer moves a little
        f[y1:y1 + 2, x0:x1] = 255                                              # overlay border
        f[y0:y1, x0 - 2:x0] = 255
        f[y0:y1, x1:x1 + 2] = 255
        out.append(f)
    return out


class OverlayTests(unittest.TestCase):
    def test_finds_the_real_border_not_the_headboard(self):
        face = (CAM[0] + 110, CAM[1] + 40, 60, 60)
        ov = fc.find_overlay(frames(), face)
        self.assertIsNotNone(ov)
        x, y, w, h = ov
        self.assertAlmostEqual(x, 0.70, delta=0.012)
        self.assertAlmostEqual(y, 0.0, delta=0.012)
        self.assertAlmostEqual(x + w, 0.98, delta=0.012)
        self.assertAlmostEqual(y + h, 0.40, delta=0.015)      # the bottom border, not the headboard at ~0.23

    def test_too_few_frames_gives_up(self):
        self.assertIsNone(fc.find_overlay(frames(3), (CAM[0] + 110, 40, 60, 60)))

    def test_estimate_is_still_the_fallback(self):
        cam = fc.expand_to_cam((800, 50, 80, 80))
        self.assertTrue(0 <= cam[0] <= 1 and 0 < cam[2] <= 0.42)


class BorrowTests(unittest.TestCase):
    def test_borrows_the_nearest_overlay_that_contains_the_face(self):
        import tempfile
        from pathlib import Path
        from studio.store import Store
        from studio.worker import Worker
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "sd")
            for sec, cam in ((100, [0.70, 0, 0.28, 0.40]), (5000, [0.76, 0, 0.24, 0.42]), (9000, [0.0, 0.6, 0.3, 0.4])):
                cid = st.add_clip("C", str(Path(d) / f"twitch_77_{sec}.mp4"), "twitch:x", "vod", None, {})
                st.update_clip(cid, facecam={"kind": "webcam", "cam": cam, "overlay": True})
            w = Worker(st)
            found = {"face": [0.79, 0.10, 0.05, 0.08]}            # inside both top-right overlays
            self.assertEqual(w._overlay_from_same_stream(Path(d) / "twitch_77_4000.mp4", "twitch:x", found), [0.76, 0, 0.24, 0.42])
            self.assertEqual(w._overlay_from_same_stream(Path(d) / "twitch_77_300.mp4", "twitch:x", found), [0.70, 0, 0.28, 0.40])
            self.assertIsNone(w._overlay_from_same_stream(Path(d) / "twitch_99_300.mp4", "twitch:x", found))   # other stream


if __name__ == "__main__":
    unittest.main()
