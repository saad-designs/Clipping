import random
import unittest

from shared.config import ScorerConfig
from shared.hype_tokens import is_hype, is_noise
from watcher.platforms.twitch import parse_privmsg, split_frame
from watcher.scorer import ChannelScorer


def simulate(spikes: dict[int, int], seconds: int = 900, base_rate: int = 3, seed: int = 1):
    """Normal chat at ~base_rate msgs/s, plus `spikes` {second: extra hype msgs/s for 10s}."""
    rng = random.Random(seed)
    sc = ChannelScorer("twitch:test", ScorerConfig())
    moments = []
    for t in range(seconds):
        n = rng.randint(base_rate - 2, base_rate + 2)
        for _ in range(n):
            moments += sc.add_message(t + rng.random(), f"u{rng.randint(1, 300)}", "chatting normally here")
        for start, extra in spikes.items():
            if start <= t < start + 10:
                for _ in range(extra):
                    moments += sc.add_message(t + rng.random(), f"u{rng.randint(1, 2000)}", "KEKW LMAO")
        moments += sc.tick(t + 1)
    return moments


class ScorerTests(unittest.TestCase):
    def test_quiet_chat_fires_nothing(self):
        self.assertEqual(simulate({}), [])

    def test_spike_fires_once_near_spike(self):
        ms = simulate({600: 15})
        self.assertEqual(len(ms), 1)
        self.assertTrue(585 <= ms[0].at <= 605, ms[0].at)  # chat-lag shift applied

    def test_no_fire_during_warmup(self):
        self.assertEqual(simulate({30: 15}, seconds=200), [])

    def test_two_separate_spikes(self):
        self.assertEqual(len(simulate({400: 15, 700: 15})), 2)

    def test_single_spammer_is_weak(self):
        sc = ChannelScorer("twitch:test", ScorerConfig())
        rng = random.Random(2)
        out = []
        for t in range(700):
            for _ in range(3):
                out += sc.add_message(t + rng.random(), f"u{rng.randint(1, 300)}", "hello")
            if 600 <= t < 610:
                for _ in range(6):
                    out += sc.add_message(t + rng.random(), "spammer", "hi")
            out += sc.tick(t + 1)
        self.assertEqual(out, [])


class ParsingTests(unittest.TestCase):
    def test_hype_tokens(self):
        for s in ["KEKW", "lmao 💀", "?????", "NOOOOO", "clip it", "W"]:
            self.assertTrue(is_hype(s), s)
        for s in ["what are we doing today", "Wednesday", "is this live?"]:
            self.assertFalse(is_hype(s), s)

    def test_bots_and_commands_ignored(self):
        self.assertTrue(is_noise("Nightbot", "Check out my YouTube"))
        self.assertTrue(is_noise("viewer", "!uptime"))
        self.assertTrue(is_noise("SomeChannelBot", "hi"))
        self.assertFalse(is_noise("viewer", "KEKW"))

    def test_privmsg(self):
        self.assertEqual(parse_privmsg(":bob!bob@bob.tmi.twitch.tv PRIVMSG #xqc :KEKW that was nuts"),
                         ("xqc", "bob", "KEKW that was nuts"))
        self.assertIsNone(parse_privmsg("PING :tmi.twitch.tv"))

    def test_websocket_frame_with_several_lines(self):
        frame = ":a!a@a.tmi.twitch.tv PRIVMSG #x :W\r\n:b!b@b.tmi.twitch.tv PRIVMSG #x :L\r\n"
        self.assertEqual([parse_privmsg(l) for l in split_frame(frame)], [("x", "a", "W"), ("x", "b", "L")])


if __name__ == "__main__":
    unittest.main()
