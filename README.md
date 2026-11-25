# Clipping

Two tools for Content Rewards clipping campaigns:

- **Watcher**: runs 24/7 on an Oracle VM, watches streams, and flags hype moments.
- **Studio**: runs on your laptop, pulls those moments, and turns them into vertical clips with captions, filed by campaign. You upload manually.

Full plan: see "Build order" below. Each step can be used on its own before the next one starts.

| Step | What | Status |
|---|---|---|
| 1 | Watcher core: Twitch chat → hype score → moments in SQLite | ✅ built |
| 2 | Rolling stream recorder + clip cutter + audio loudness signal | next |
| 3 | Review dashboard (approve/reject, assign campaign) | |
| 4 | Studio: download, captions, 9:16, encode, campaign folders | |
| 5 | Kick + YouTube Live | |
| 6 | VOD mode | |
| 7 | Oracle deploy scripts (systemd, cleanup, health) | |

## Step 1: try it

Requires Python 3.11+.

```bash
pip install -r requirements-watcher.txt
cp settings.example.yaml settings.yaml
cp campaigns.example.yaml campaigns.yaml     # add your campaign's streamers

# Quick test with no config: watch two big live channels
python -m watcher.app --channels kaicenat xqc

# Real run (uses campaigns.yaml)
python -m watcher.app

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

The moment time is shifted back about 8 seconds because chat reacts after the event. Clips in step 2 will also include ~35 seconds before the moment for context.

### Tuning (`settings.yaml → scorer`)
- Too many weak moments → raise `threshold` (try 7–9) or `min_hype_z`.
- Missing obvious moments → lower `threshold` (try 4–5).
- A streamer's chat has its own emotes → add them to that campaign's `extra_hype_tokens`.

### Optional: Twitch API keys
Create an app at dev.twitch.tv/console (free) and put `client_id`/`client_secret` in `settings.yaml`. This adds live/offline detection, so the baseline resets between streams and offline chat is ignored. Step 2 needs this to know when to start recording.
