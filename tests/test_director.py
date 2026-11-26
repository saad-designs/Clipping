"""Director: plan validation, campaign rules, rule-based variety, re-edit notes, AI response handling."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from studio import director
from studio.editor.transcribe import Word

WORDS = [Word(5 + i * 0.4, 5.3 + i * 0.4, t) for i, t in enumerate("no way he actually hit that shot chat what".split())]
CAMP = {"name": "Test", "min_seconds": 15, "max_seconds": 30, "required_text": ["#ad", "@brand"], "hook": True}


def ctx(**kw):
    base = director.build_context({"moment_offset": 20, "channel": "twitch:x", "signals": {"z_hype": 6}},
                                  {**CAMP, **kw.pop("campaign", {})}, WORDS, kw.pop("facecam", {"kind": "none"}), 60.0,
                                  kw.pop("recent", []), kw.pop("notes", ""), kw.pop("previous_plan", None))
    return base


def raw(**kw):
    r = {"keep_start": 10, "keep_end": 35, "layout": "facecam_split", "game_x": 0.5,
         "webcam_box": {"present": False, "x": 0, "y": 0, "w": 0, "h": 0}, "hook_text": "HE DID NOT SEE IT", "hook_seconds": 2.5,
         "caption_style": "pop_yellow", "emphasis_words": ["way"], "zooms": [{"at": 21, "strength": 0.5}, {"at": 99, "strength": 0.1}],
         "sfx": [{"at": 21, "sound": "boom"}, {"at": 22, "sound": "laser"}], "logo_corner": "none",
         "post_title": "t", "post_caption": "c", "hashtags": ["gaming"], "why": "w", "quality": 14, "skip": False, "skip_reason": ""}
    r.update(kw)
    return r


class ValidateTests(unittest.TestCase):
    def test_clamps_length_zooms_sfx_quality(self):
        p = director.validate(raw(), ctx())
        self.assertEqual(p["keep"], [10.0, 35.0])
        self.assertLessEqual(director.validate(raw(keep_end=55), ctx())["keep"][1], 40.0)   # campaign max 30 s
        self.assertEqual([z["at"] for z in p["zooms"]], [21])                  # outside the kept range dropped
        self.assertEqual(p["zooms"][0]["strength"], 0.25)                      # strength capped
        self.assertEqual([s["sound"] for s in p["sfx"]], ["boom"])             # unknown sound dropped
        self.assertEqual(p["quality"], 10)

    def test_required_hashtags_always_added(self):
        p = director.validate(raw(hashtags=["gaming", "#ad"]), ctx())
        self.assertEqual(p["post"]["hashtags"], ["#gaming", "#ad", "@brand"])

    def test_min_length_enforced(self):
        p = director.validate(raw(keep_start=20, keep_end=24), ctx())
        self.assertGreaterEqual(p["keep"][1] - p["keep"][0], 15)

    def test_facecam_layout_needs_a_cam(self):
        self.assertEqual(director.validate(raw(), ctx())["layout"], "blur_fit")
        p = director.validate(raw(webcam_box={"present": True, "x": 0.8, "y": 0.3, "w": 0.18, "h": 0.2}), ctx())
        self.assertEqual(p["layout"], "facecam_split")                         # AI-located webcam used
        self.assertEqual(p["facecam"]["source"], "ai")
        p = director.validate(raw(), ctx(facecam={"kind": "webcam", "cam": [0, 0, 0.2, 0.2]}))
        self.assertEqual(p["layout"], "facecam_split")

    def test_campaign_overrides(self):
        p = director.validate(raw(), ctx(campaign={"layout": "game_only", "caption_style": "punch_red", "hook": False,
                                                   "logo_path": "/x/logo.png"}))
        self.assertEqual((p["layout"], p["caption_style"], p["hook"]), ("game_only", "punch_red", None))
        self.assertEqual(p["logo"]["corner"], "top_right")


class RulesTests(unittest.TestCase):
    def test_rule_plan_is_valid_and_varies_styles(self):
        styles = []
        for i in range(4):
            p = director.validate(director.rule_plan(ctx(recent=styles[-3:]), seed=i), ctx())
            styles.append(p["caption_style"])
            self.assertTrue(p["hook"]["text"])
            self.assertIn("#ad", p["post"]["hashtags"])
        self.assertEqual(len(set(styles)), 4)       # never repeats a recently used style

    def test_layout_follows_webcam_detection(self):
        p = director.validate(director.rule_plan(ctx(facecam={"kind": "webcam", "cam": [0, 0.6, 0.25, 0.3]})),
                              ctx(facecam={"kind": "webcam", "cam": [0, 0.6, 0.25, 0.3]}))
        self.assertEqual(p["layout"], "facecam_split")

    def test_reedit_notes_without_ai(self):
        first = director.validate(director.rule_plan(ctx(), seed=1), ctx())
        c = ctx(notes='no sound effects, punch style, start 2s later, hook "BRO WHAT"', previous_plan=first)
        p = director.plan(c, "unused.mp4", {})
        self.assertEqual(p["sfx"], [])
        self.assertEqual(p["caption_style"], "punch_red")
        self.assertAlmostEqual(p["keep"][0], first["keep"][0] + 2, places=1)
        self.assertEqual(p["hook"]["text"], "BRO WHAT")


class AITests(unittest.TestCase):
    def _fake_client(self, payload, stop="end_turn"):
        resp = SimpleNamespace(stop_reason=stop, model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                               usage=SimpleNamespace(input_tokens=4000, output_tokens=900))
        client = mock.MagicMock()
        client.beta.messages.create.return_value = resp
        return client

    def test_ai_plan_used_and_validated(self):
        client = self._fake_client(raw(hook_text="CHAT PREDICTED THIS"))
        with mock.patch("anthropic.Anthropic", return_value=client), \
                mock.patch.object(director, "frame_jpegs", return_value=[b"jpg"] * 4):
            p = director.plan(ctx(), "clip.mp4", {"anthropic_api_key": "sk-test", "director": "ai"})
        self.assertEqual(p["hook"]["text"], "CHAT PREDICTED THIS")
        self.assertTrue(p["source"].startswith("ai:"))
        kwargs = client.beta.messages.create.call_args.kwargs
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["fallbacks"], "default")
        images = [b for b in kwargs["messages"][0]["content"] if b["type"] == "image"]
        self.assertEqual(len(images), 4)

    def test_refusal_or_error_falls_back_to_rules(self):
        for client in (self._fake_client(raw(), stop="refusal"),):
            with mock.patch("anthropic.Anthropic", return_value=client), \
                    mock.patch.object(director, "frame_jpegs", return_value=[]):
                p = director.plan(ctx(), "clip.mp4", {"anthropic_api_key": "sk-test", "director": "ai"})
            self.assertEqual(p["source"], "rules")
            self.assertIn("AI director unavailable", p["why"])

    def test_schema_lists_every_required_field(self):
        props = set(director.PLAN_SCHEMA["properties"])
        self.assertEqual(props, set(director.PLAN_SCHEMA["required"]))


if __name__ == "__main__":
    unittest.main()
