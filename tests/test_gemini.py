"""The watching director (Gemini): response parsing, quota fallback, plan conversion, jump-cut render."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio import director, gemini_director as gd
from studio.editor.transcribe import Word

HAS_FFMPEG = shutil.which("ffmpeg") is not None

DECISION = {
    "watched": {"summary": "She crashes a plane and screams.", "timeline": [{"t": 10, "game": "plane dives", "streamer": "screams"}]},
    "speech": [{"from": 0, "to": 8, "who": "game"}, {"from": 8, "to": 30, "who": "streamer"}],
    "verdict": {"post": True, "score": 7, "reason": "clear payoff and a big reaction"},
    "story": {"setup_at": 4, "payoff_at": 12, "reaction_at": 13, "beats": "takes off -> crashes -> screams"},
    "keep": [{"from": 3, "to": 9, "why": "setup"}, {"from": 11, "to": 25, "why": "crash + reaction"}],
    "layout": "facecam_split", "layout_why": "her reaction is the point", "game_x": 0.5,
    "zooms": [{"at": 13, "target": "face", "strength": 0.15, "why": "she screams"},
              {"at": 14, "target": "game", "strength": 0.1, "why": "too close to the last one"},
              {"at": 20, "target": "game", "strength": 0.1, "why": ""}],
    "hook": {"text": "she should not be flying", "why": "sets up the crash"},
    "caption_style": "pop_yellow", "emphasis_words": ["plane"], "post_caption": "pilot license revoked ✈️",
}


def ctx(duration=60.0):
    return {"duration": duration, "moment": 12.0, "channel": "twitch:laurenhamdenn", "signals": {}, "words": [],
            "facecam": {"kind": "webcam", "cam": [0.75, 0, 0.23, 0.31]}, "recent_styles": [], "notes": "",
            "previous_plan": None, "loudness": [], "language": "en", "recent_hooks": [], "viewer_clips": 1,
            "campaign": {"name": "C", "min_seconds": 15, "max_seconds": 60, "required_text": ["#LaurenHamden"],
                         "pronoun": "she", "hook": True}}


class ParsingTests(unittest.TestCase):
    def test_text_from_steps_skips_thoughts(self):
        resp = {"steps": [{"type": "thought", "content": [{"type": "text", "text": "thinking..."}]},
                          {"type": "model_output", "content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}]}]}
        self.assertEqual(gd.parse_json(gd.response_text(resp)), {"a": 1})
        self.assertEqual(gd.response_text({"output_text": '{"b": 2}'}), '{"b": 2}')

    def test_falls_back_to_next_model_on_quota(self):
        calls = []

        def fake_post(body, key, timeout=300):
            calls.append(body["model"])
            if body["model"] == "m1":
                raise gd.QuotaError("RESOURCE_EXHAUSTED")
            return {"output_text": '{"ok": true}'}
        gd._exhausted.clear()
        with mock.patch.object(gd, "_post", side_effect=fake_post):
            d, model = gd.ask("k", b"video", "prompt", ["m1", "m2"])
            self.assertEqual((d, model), ({"ok": True}, "m2"))
            gd.ask("k", b"video", "prompt", ["m1", "m2"])
        self.assertEqual(calls, ["m1", "m2", "m2"])          # m1 is skipped while it's out of quota
        gd._exhausted.clear()

    def test_bad_option_retries_without_thinking_settings(self):
        bodies = []

        def fake_post(body, key, timeout=300):
            bodies.append(dict(body))
            if "generation_config" in body:
                raise RuntimeError("HTTP 400: unknown field thinking_level")
            return {"output_text": "{}"}
        gd._exhausted.clear()
        with mock.patch.object(gd, "_post", side_effect=fake_post):
            gd.ask("k", b"v", "p", ["m"])
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("generation_config", bodies[1])


class PlanTests(unittest.TestCase):
    def test_decision_becomes_a_valid_plan_with_reasons(self):
        p = director.validate(gd.to_raw(DECISION, "gemini-4-hydrogen", ctx()), ctx())
        self.assertEqual(p["segments"], [[3.0, 9.0], [11.0, 25.0]])
        self.assertEqual(p["keep"], [3.0, 25.0])
        self.assertEqual([(z["at"], z["target"]) for z in p["zooms"]], [(13.0, "face")])   # no reason / too close: dropped
        self.assertEqual(p["hook"]["text"], "SHE SHOULD NOT BE FLYING")
        self.assertIn("#LaurenHamden", p["post"]["hashtags"])
        self.assertEqual(p["source"], "gemini:gemini-4-hydrogen")
        self.assertFalse(p["skip"])

    def test_skip_verdict(self):
        d = {**DECISION, "verdict": {"post": False, "score": 2, "reason": "silent cutscene"}}
        p = director.validate(gd.to_raw(d, "m", ctx()), ctx())
        self.assertTrue(p["skip"])
        self.assertEqual(p["skip_reason"], "silent cutscene")

    def test_segments_respect_campaign_length(self):
        segs = director.clean_segments([{"from": 0, "to": 40}, {"from": 41, "to": 41.3}, {"from": 45, "to": 80}], 90, 15, 60)
        self.assertEqual(sum(b - a for a, b in segs), 60)
        self.assertEqual(director.clean_segments([{"from": 5, "to": 10}], 90, 15, 60), [[5.0, 20.0]])

    def test_captions_only_show_the_streamer(self):
        words = [Word(2, 2.4, "pilot"), Word(9, 9.3, "oh"), Word(10, 10.4, "no")]
        plan = {"speech": [{"from": 0, "to": 8, "who": "game"}]}
        self.assertEqual([w.text for w in director.streamer_words(words, plan)], ["oh", "no"])

    def test_missing_key_falls_back_to_rules(self):
        p = director.plan({**ctx(), "words": []}, Path("x.mp4"), {"director": "gemini", "gemini_api_key": ""})
        self.assertIn("no key", p["why"])
        self.assertEqual(p["source"], "free")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class JumpCutRenderTests(unittest.TestCase):
    def test_render_with_cuts_and_face_punch_in(self):
        from studio.editor import render
        from tests.test_studio_app import make_video, probe
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "clip.mp4"
            make_video(src, 20)
            plan = {"keep": [1.0, 15.0], "segments": [[1.0, 5.0], [8.0, 15.0]], "layout": "facecam_split", "game_x": 0.5,
                    "hook": {"text": "TEST", "seconds": 2}, "caption_style": "pop_yellow", "emphasis_words": [],
                    "zooms": [{"at": 9.0, "strength": 0.15, "target": "face"}, {"at": 6.0, "strength": 0.15}], "sfx": [], "logo": None}
            words = [Word(2.0, 2.4, "kept"), Word(6.0, 6.4, "cut"), Word(9.0, 9.4, "kept")]
            out = render.render(src, plan, d / "out.mp4", {}, words, {"kind": "webcam", "cam": [0.7, 0, 0.3, 0.3]},
                                {"size": 104}, "libx264")
            info = probe(out)
            self.assertAlmostEqual(float(info["format"]["duration"]), 11.0, delta=0.3)   # 4 s + 7 s
            tl = render.Timeline(plan["segments"])
            self.assertEqual([w.text for w in tl.words(words)], ["kept", "kept"])
            self.assertAlmostEqual(tl.out(9.0), 5.0)
            self.assertIsNone(tl.out(6.0))


if __name__ == "__main__":
    unittest.main()
