# Clipping

Two tools for Content Rewards clipping campaigns:

- **Watcher**: runs 24/7 on an Oracle VM, watches streams, and flags hype moments.
- **Studio**: runs on your laptop, pulls those moments, and turns them into vertical clips with captions, filed by campaign. You upload manually.

Full plan: see "Build order" below. Each step can be used on its own before the next one starts.

| Step | What | Status |
|---|---|---|
| 1 | Watcher core: Twitch chat → hype score → moments in SQLite | ✅ built |
| 2 | Rolling stream recorder + clip cutter + audio loudness signal | ✅ built |
| 3 | Review dashboard (approve/reject, assign campaign) + Twitch viewer-clip badge | next |
| 4 | Studio: download, captions, 9:16, encode, campaign folders | |
| 5 | Kick + YouTube Live | |
| 6 | VOD mode | |
| 7 | Oracle deploy scripts (systemd, cleanup, health) | |

## Try it

Requires Python 3.11+ and ffmpeg (`sudo apt install ffmpeg` on the Oracle VM, `brew install ffmpeg` on Mac).

```bash
pip install -r requirements-watcher.txt     # pyyaml + streamlink
cp settings.example.yaml settings.yaml
cp campaigns.example.yaml campaigns.yaml     # add your campaign's streamers

# Quick test with no config: watch two big live channels
python -m watcher.app --channels kaicenat xqc

# Real run (uses campaigns.yaml)
python -m watcher.app

# Chat only, no recording (uses almost no bandwidth)
python -m watcher.app --no-record

# Test recording/clipping with a local video instead of a live stream
python -m watcher.app --channels anyname --fake-source some_video.ts

# Tests
python -m unittest -v
```

When a moment fires, it's printed and saved to `data/watcher.db`:
```
19:41:07 INFO watcher: 🔥 MOMENT #3  twitch:xqc  at 19:40:54  score=11.2  {'messages': 61, 'hype': 38, 'unique': 52, ...}
```
Open the VOD at that time and check whether it really was a moment. Doing this is how you tune the detector.

### How detection works
Chat is grouped into 5-second buckets. Each bucket is compared to the same channel's **last 10 minutes** (z-scores), so big and small streamers are judged fairly.

| Signal | Why |
|---|---|
| Messages per bucket (max 2 per user) | Chat speeds up when something happens. The per-user cap stops one spammer from faking a surge. |
| Hype messages (`KEKW`, `LMAO`, `W`, `?????`, `NOOOO`, `CLIP`, 💀…) | Shows chat is *reacting*, not just busy. A rise here is **required**, so raids, giveaways and bot spam alone don't fire. |
| Unique chatters | Many people reacting is better than a few people reacting a lot. |
| Audio loudness (recorded channels) | Boost only. Never fires a moment by itself. |

The moment time is shifted back about 8 seconds because chat reacts after the event. Each 5-second bucket is scored 15 seconds after it ends, so that moment's audio has been recorded and can be included.

## Recording and clips (step 2)

While a channel is live, the watcher keeps its last **10 minutes** on disk as 10-second files in `data/buffer/<channel>/`. Nothing is re-encoded, so each stream uses almost no CPU. When a moment fires, it waits ~20 seconds for the footage after the moment, then saves a clip:

```
data/clips/twitch_xqc/14_20261125_194054.mp4    ← about 55–65s: ~35s before the moment, ~20s after
```
`clip_path` in the DB points to it. Clips are cut on 10-second boundaries, so they run a bit long on purpose. The laptop app (step 4) trims each one to the campaign's length when it edits.

**Audio loudness** from the recording is added to the hype score: screaming, a big reaction, music dropping. It only **boosts** a moment chat is already reacting to. It can never fire one on its own, because loud game sounds with a silent chat are almost never a good clip.

**Resource use per recorded stream:** ~6 Mbps download, ~450 MB disk for the buffer, ~2–5% of one CPU core. On Oracle's free tier (4 ARM cores, 200 GB), 15–25 streams should fit. Above `max_concurrent` (default 20), extra channels stay chat-only. Use `quality: "720p60,best"` to save bandwidth, but 1080p gives a sharper 9:16 crop.

**Twitch API keys are strongly recommended now.** Without them, the watcher doesn't know who's live, so it retries streamlink for every channel every 2 minutes. With keys, it records only live channels and stops when they go offline.

**Cleanup:** buffer files older than 10 minutes are deleted automatically, unless a pending clip still needs them. Clips are deleted after `clip_retention_days` (default 3), by which point the laptop should have downloaded them.

### Tuning (`settings.yaml → scorer`)
- Too many weak moments → raise `threshold` (try 7–9) or `min_hype_z`.
- Missing obvious moments → lower `threshold` (try 4–5).
- A streamer's chat has its own emotes → add them to that campaign's `extra_hype_tokens`.

### Optional: Twitch API keys
Create an app at dev.twitch.tv/console (free) and put `client_id`/`client_secret` in `settings.yaml`. This adds live/offline detection. Recording starts and stops with the stream, the baseline resets between streams, and offline chat is ignored.
