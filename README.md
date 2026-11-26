# Clipping

A two-part system for Content Rewards clipping campaigns:

- **Watcher** runs 24/7 on an Oracle Cloud VM. It watches Twitch, Kick and YouTube Live streams, detects hype moments from chat and audio, keeps a rolling recording, and cuts a clip of every moment. Its own dashboard shows what it is watching and recording.
- **Studio** runs on your PC as a dashboard in the browser (http://localhost:8790). You set up each campaign there: brief, streamers, logo, rules. It pulls every captured moment, and an **AI director** (Claude) plans each edit: facecam on top / game below, big animated captions, a hook title, zoom and sound hits on the moment, logo, and the post caption, with a different style per clip. You approve the final edit in **Review**, then upload from **Ready to upload**. It can also pick the best moments out of a VOD or any long video.

```
Stream ──► Watcher (Oracle) ──► captured clip ──► Studio (PC): director plans ──► render ──► Review
                                                                                              │ approve
                         Campaigns/<campaign>/ready/clip.mp4 + caption.txt  ◄─────────────────┘ ──► you upload
```

**New here? Follow [ORACLE_SETUP.md](ORACLE_SETUP.md): click-by-click setup for the Oracle server and your Windows PC. On Windows, everything runs from double-click files in the `windows` folder.**

Results of real-world tests: [TEST_REPORT.md](TEST_REPORT.md). Deeper testing checklist: [TESTING.md](TESTING.md).

---

## 1. Server setup (Oracle Cloud, one time)

1. Create an **Ubuntu 22.04 or 24.04** VM. The free-tier *Ampere A1* (ARM, 4 cores, 24 GB RAM) works well. Give it a 100–200 GB boot volume.
2. SSH in and run:
   ```bash
   git clone https://github.com/saad-designs/Clipping.git
   cd Clipping
   sudo bash deploy/oracle-setup.sh
   ```
   This installs ffmpeg and the Python packages, creates a `clipper` service user, installs to `/opt/clipping`, and starts the `clipping-watcher` service. It's safe to run again: to update later, `git pull` and run it again.
3. **Private access with Tailscale.** This means you never open a port to the internet. On the VM:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale ip -4          # note this 100.x.y.z address
   ```
   Install Tailscale on your laptop and phone, signed in to the same account.
4. **Twitch API keys (free, strongly recommended).** Go to [dev.twitch.tv/console](https://dev.twitch.tv/console) → Register Your Application (name: anything, OAuth redirect: `http://localhost`, category: Other) → copy the Client ID and create a secret. Put both in `/opt/clipping/settings.yaml` under `twitch:`.
   Without keys, the watcher can't tell who's live, so it keeps retrying every channel, and the "viewers clipped this" badge is off.
5. Add your campaigns (next section), then `sudo systemctl restart clipping-watcher`.
6. Open the dashboard: `http://<tailscale-ip>:8787/?token=<token>`. The setup script prints the full link. The token is in `/opt/clipping/data/api_token.txt`. After the first visit the token is kept in a cookie, so bookmark `http://<tailscale-ip>:8787/`.

Logs: `journalctl -u clipping-watcher -f`

## 2. Campaigns (`/opt/clipping/campaigns.yaml`)

One entry per Content Rewards campaign. A campaign can cover several streamers on different platforms.

```yaml
campaigns:
  - name: energy-drink-nov          # becomes Campaigns/energy-drink-nov/ on the laptop
    active: true
    channels:
      - {platform: twitch,  login: xqc}
      - {platform: kick,    login: adinross}
      - {platform: youtube, login: "@somecreator"}   # @handle or UC... channel id
    clip: {min_seconds: 15, max_seconds: 60}         # the campaign's length rules
    required_text: ["#ad", "@brand"]                 # added to every caption.txt
    layout: blur                                     # blur | crop | facecam
    extra_hype_tokens: ["OMEGALUL", "ICANT"]         # this streamer's chat's own emotes
    notes: "Paste the campaign's rules here"
```
After editing: `sudo systemctl restart clipping-watcher`.

## 3. Reviewing on the dashboard

Every detected moment appears as a card with its clip, which starts playing 5 seconds before the moment.

| Key | Action |
|---|---|
| `J` / `K` | next / previous card |
| `Space` | play from the moment |
| `A` | approve: goes to the laptop for every highlighted campaign |
| `R` | reject |

Click a campaign chip to add or remove it before approving. **🎬 viewer clips** means Twitch viewers clipped the same moment themselves, a strong sign it's good. If viewers clip something the detector missed, it shows up as its own card with score 0.

Review matters: detection catches when chat and audio spike, not whether the moment is actually funny. Thirty seconds of reviewing saves you posting weak clips.

## 4. Laptop setup (Studio)

Windows, Mac or Linux. Needs Python 3.11+ and ffmpeg.

- **Windows:** install Python from python.org (tick "Add to PATH"), then `winget install Gyan.FFmpeg`
- **Mac:** `brew install python ffmpeg`

```bash
git clone https://github.com/saad-designs/Clipping.git
cd Clipping
pip install -r requirements-studio.txt
cp studio.example.yaml studio.yaml     # set server_url (Tailscale IP) and token
python -m studio check                 # checks ffmpeg, encoder, whisper, server connection
python -m studio run                   # leave running: downloads + edits approved clips every 3 min
```

What you get, per campaign:
```
Campaigns/energy-drink-nov/
  inbox/        downloaded, waiting to be edited (inbox/done/ keeps the originals)
  ready/        twitch_xqc_20261125_194054_14.mp4          ← upload this
                twitch_xqc_20261125_194054_14.caption.txt  ← hook line from the transcript, streamer credit, required tags
                twitch_xqc_20261125_194054_14.json         ← details (kept range, transcript…)
  posted/       move clips here after uploading: python -m studio posted <file>
  vods/         downloaded VODs (VOD mode)
```

**How each clip is edited:**
- **Length:** `target_seconds` (default 35), kept inside the campaign's min/max. About 60% comes before the moment (build-up) and 40% after (reaction). Edges are moved so no word is cut in half.
- **Layout:**
  - `blur` (default): the full frame in the middle with a blurred copy filling the background. Works for anything.
  - `crop`: center crop. Best for just-chatting and IRL streams.
  - `facecam`: the webcam on top and the gameplay below. Set the rectangles per channel in `studio.yaml`.
- **Captions:** faster-whisper transcribes the clip, and captions show 3 words at a time with the spoken word highlighted, placed above the TikTok/Reels/Shorts buttons. The first run downloads the whisper model (~500 MB for `small`).
- **Encoding:** uses your GPU encoder automatically (NVIDIA, Intel, AMD or Apple), otherwise the CPU. Audio is normalized to -14 LUFS, the level short-form apps play at.

Redo one clip with another layout: `python -m studio edit Campaigns/x/inbox/done/<file>.mp4 --layout crop` (move it back to `inbox/` first, or pass the path).

## 5. VOD mode (finished streams, podcasts, any long video)

```bash
python -m studio vod "https://www.twitch.tv/videos/123456789" --campaign energy-drink-nov --top 8
python -m studio vod "https://www.youtube.com/watch?v=..."     --campaign podcast-clips
python -m studio vod ./recording.mp4                            --campaign podcast-clips
```
It downloads the video and scores every 5 seconds using:
- **Chat replay** where it exists (Twitch VODs, YouTube past live streams).
- **Audio loudness.**
- **Hype words in the transcript**, when there's no chat.

It keeps the best N moments at least 90 seconds apart, then edits them exactly like live clips.

## 6. How detection works

Chat is grouped into 5-second buckets. Each bucket is compared against the same channel's last 10 minutes (z-scores), so a 200-viewer stream and a 50k-viewer stream are judged fairly.

| Signal | Why |
|---|---|
| Messages per bucket (max 2 per person) | Chat speeds up when something happens. The cap stops one spammer from faking it. |
| Hype messages (`KEKW`, `LMAO`, `W`, `?????`, `NOOOO`, 💀, "clip it"…) | Shows chat is **reacting**, not just busy. **Required**: raids, giveaways and bot spam alone never fire. |
| Unique chatters | Many people reacting beats a few people reacting a lot. |
| Audio loudness (recorded channels) | Boosts a moment, never fires one alone. Loud game sounds with a quiet chat are rarely a good clip. |

The moment time is shifted back about 8 seconds, because chat reacts after the event. Clips are cut from the rolling buffer with about 35 seconds before and 20 seconds after.

**Tuning** (`settings.yaml → scorer`):
- Too many weak moments → raise `threshold` (try 7–9).
- Missing obvious ones → lower it (try 4–5).
- Each moment's raw numbers are on its card, so compare good and bad ones to see what to change.

## 7. Capacity (Oracle free tier)

Each recorded stream uses about 6 Mbps download, ~450 MB of disk for the 10-minute buffer, and 2–5% of one CPU core, because nothing is re-encoded on the server. Expect **15–25 recorded streams** at once. Channels beyond `recorder.max_concurrent` (default 20) are still watched through chat, but without clips. Chat-only monitoring is nearly free, so you can watch far more channels that way.

If free disk drops below `min_free_gb` (default 15), new recordings pause and old clips are pruned in this order: rejected first, then downloaded, then unreviewed. Approved clips are pruned last. Clips are deleted after 3 days anyway.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard won't load | Is Tailscale connected on both devices? Is the service running? Check `systemctl status clipping-watcher`. |
| "bad or missing token" | Open the link with `?token=` again; the token is in `/opt/clipping/data/api_token.txt`. |
| YouTube: no recording / "confirm you're not a bot" | YouTube blocks many cloud IPs. Export `cookies.txt` from a logged-in browser (e.g. the "Get cookies.txt LOCALLY" extension), copy it to the VM, and set `youtube.cookies_file` in settings.yaml. |
| Kick chat or live status not working | Kick's Cloudflare sometimes blocks cloud IPs. Check the logs for `kick API failed`. If Kick changed its chat key, update `kick.pusher_key`. |
| A platform keeps blocking the VM | Run a second Watcher on your laptop for just those channels: same code, its own `campaigns.yaml`, `python -m watcher.app --root <folder>`. |
| Clips have no captions | `python -m studio check`. Captions need `faster-whisper` and a one-time model download. |
| Captions too small or too low | `studio.yaml → captions.size / position` |

## Development

```bash
pip install -r requirements-watcher.txt -r requirements-studio.txt
python -m unittest -v                  # needs ffmpeg; no internet needed
python -m watcher.app --channels anyname --fake-source some_video.ts   # local test without a live stream
```

Layout: `watcher/` (server), `studio/` (laptop), `shared/` (config, hype words), `deploy/` (Oracle setup), `tests/`.

The Kick and YouTube chat readers and Twitch's VOD chat replay use those sites' unofficial web endpoints. They work today but can break when a site changes. The parsing code for each is isolated in one function per format (`parse_*`) with tests, so a fix is small.
