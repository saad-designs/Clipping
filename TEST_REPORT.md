# Test report: 26 Nov 2026

Real-world tests run by Claude on real Twitch data, before any server existed. Try the results yourself in the private demo dashboard: **https://claude.ai/artifact/VHQtyiDtTKw3AeK63DRsxj**

## What was tested, and the result

| Part | Test | Result |
|---|---|---|
| Live chat detection | 5 live Twitch streams (ohnepixel 32k viewers, zarbex, tfue, cohhcarnage, nix), 43 minutes | ✅ Connected to all 5, no disconnects. **17 moments** caught: 16 on ohnepixel, 1 on zarbex. |
| Editing + captions | 5 real Twitch clips → full Studio pipeline | ✅ 1080×1920 vertical, captions in sync with speech, highlighted word, volume levelled. German was auto-detected (97%). |
| VOD mode | Real chat replay of a Tfue VOD, 2 hours (3,381 messages) | ✅ Fetched in 30 s; top 8 moments picked (see below). |
| Automated tests | 43 unit + integration tests | ✅ All pass. |

## Problems found and fixed (these would have hit you on day 1)

1. **Twitch VOD chat stopped after one page.** Twitch now blocks anonymous "next page" requests ("failed integrity check"). Fixed: fetch by time offset instead, which still works.
2. **Captions failed on a fresh install.** The newest PyAV library breaks faster-whisper's audio decoder. Fixed: Studio decodes audio with ffmpeg itself.
3. **Bots and `!commands` were driving picks** (e.g. `!uptime`, "Check out Tfue's YouTube…"). Fixed: bots and commands are ignored by the scorer.
4. **Too many moments from huge chats.** ohnepixel produced one every ~2.7 min. Fixed: `max_moments_per_hour` (default 6). Past that, a new moment must beat the hour's median score. On the real data, this keeps 8 of 16, dropping the weakest.
5. **Long words made 3-line caption blocks** (German). Fixed: caption lines are limited by width, not just word count.
6. **Post-caption text cut mid-word.** Fixed: it stops at a word boundary with "…".
7. **Oracle's firewall would have blocked the dashboard.** Oracle's Ubuntu images reject all incoming traffic except SSH. Fixed: the setup script allows traffic over Tailscale only (nothing opened to the internet).
8. **Twitch chat used a port that firewalls often block.** Fixed: chat now uses Twitch's websocket on port 443.
9. **Windows' fake `python` shortcut** (it opens the Microsoft Store) would have fooled the setup script. Fixed.

## Live moments (check them yourself)
Each link opens the stream VOD 5 seconds before the moment. These are the 9 kept with current settings:

| Channel | Time (UTC) | Score | Watch |
|---|---|---|---|
| ohnepixel | 16:46:47 | 13.8 | [2h46m26s](https://www.twitch.tv/videos/2941322278?t=2h46m26s) |
| ohnepixel | 16:47:57 | 18.0 | [2h47m36s](https://www.twitch.tv/videos/2941322278?t=2h47m36s) |
| ohnepixel | 16:50:12 | 16.4 | [2h49m51s](https://www.twitch.tv/videos/2941322278?t=2h49m51s) |
| ohnepixel | 16:51:12 | 7.7 | [2h50m51s](https://www.twitch.tv/videos/2941322278?t=2h50m51s) |
| ohnepixel | 16:54:37 | 24.3 | [2h54m16s](https://www.twitch.tv/videos/2941322278?t=2h54m16s) |
| ohnepixel | 16:58:52 | 8.0 | [2h58m31s](https://www.twitch.tv/videos/2941322278?t=2h58m31s) |
| zarbex | 17:05:22 | 14.2 | [4h31m23s](https://www.twitch.tv/videos/2941273430?t=4h31m23s) |
| ohnepixel | 17:18:57 | 15.7 | [3h18m36s](https://www.twitch.tv/videos/2941322278?t=3h18m36s) |
| ohnepixel | 17:26:02 | 16.8 | [3h25m41s](https://www.twitch.tv/videos/2941322278?t=3h25m41s) |

## VOD test picks (Tfue, chat only)
Top picks from 1:00:00–3:00:00 of [this VOD](https://www.twitch.tv/videos/2941206024): 2:39:52 ("ohhh | oh no | gg"), 2:18:42 ("bad timing | LMAOOO"), 2:54:22 ("what | KEKW | cheater | LMAO"), 1:11:22, 1:02:47, 2:06:22, 2:23:47, 1:08:57. Tfue's chat was slow (~1 message every 2 s), which is a weak signal, so on your PC the audio track will add to it.

## What these tests could NOT check (needs your server)
- **Recording live video.** Twitch's video servers change address constantly, so the test machine couldn't be allowed to reach them. This is the main thing to watch on the first day on Oracle.
- **Kick and YouTube Live from Oracle's IP.** Both sometimes block cloud servers. See the troubleshooting table in the README.
- **Quiet chats:** tfue, nix and cohhcarnage produced 0 moments in 43 min. That may simply be because nothing big happened, but if your campaign's streamer has a small chat, lower `threshold` to 4–5.
- **Accuracy vs. viewers' own clips:** without Twitch API keys, only 2 viewer clips from the test window were visible, which is too few to score. With keys on your server, the dashboard shows a 🎬 badge whenever viewers clipped the same moment.
- **Your PC's speed:** the test machine took ~2 min per clip on a shared CPU. A PC with a graphics card is much faster.
