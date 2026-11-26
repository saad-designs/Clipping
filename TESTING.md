# Testing guide

> **Just want to get it running?** Use [ORACLE_SETUP.md](ORACLE_SETUP.md) instead. This page is the detailed checklist for checking each part separately.

Each phase below adds one new thing, so when something breaks we know exactly where.

**Why test on your laptop first, before Oracle:** if a stream fails on Oracle, it could be broken code *or* Oracle's IP being blocked. Running the same thing first from your home internet separates the two.

Throughout: on **Windows** use `python` and `copy`; on **Mac/Linux** use `python3` and `cp`. When something fails, send me **the command, what you expected, and the last ~30 lines of output**.


## Phase 1: Laptop basics (15 min)
**Install:** Python 3.11+ (Windows: python.org, tick "Add to PATH"), ffmpeg (Windows: `winget install Gyan.FFmpeg`, Mac: `brew install ffmpeg`), Git.
```bash
git clone -b ccr-8c9d8d98-0303s9 https://github.com/saad-designs/Clipping.git
cd Clipping
pip install -r requirements-watcher.txt -r requirements-studio.txt
python -m unittest
```
✅ **Pass:** the last line says `OK` (38 tests).
❌ If git asks for a password: the repo is private, so use a GitHub Personal Access Token as the password (GitHub → Settings → Developer settings → Tokens).

## Phase 2: Real Twitch chat detection on the laptop (30–60 min, mostly waiting)
Pick 1–2 **big streamers who are live right now** (busy chat = faster results).
```bash
python -m watcher.app --channels kaicenat xqc --no-record
```
✅ **Pass, within a minute:** `joined 2 twitch chats`. Then, after the ~2-minute warm-up, `🔥 MOMENT` lines appear when chat explodes.
- Watch the stream at the same time. When you see a MOMENT line, check whether something actually happened ~10s before it.
- Write down: moments per hour, and roughly how many were real. This is our tuning data.
❌ No `joined` line: send the output. Joined but zero moments in 45 min on a busy stream: send me the output (I'll lower the threshold).

## Phase 3: Recording, clips, and dashboard on the laptop (20 min)
```bash
python -m watcher.app --channels <one live streamer>
```
1. Note the line `dashboard: http://<this-server>:8787/?token=XXXX`. Open `http://localhost:8787/?token=XXXX` in your browser.
2. ✅ **Pass:**
   - a `data/buffer/twitch_<name>/` folder fills with 10-second `.ts` files, and old ones disappear after 10 minutes;
   - after a moment, `✂️ clip ready` appears and the card on the dashboard plays the clip;
   - **J/K/A/R** keys work, and approved cards move to the "Approved" tab.
3. Check CPU in Task Manager / Activity Monitor: ffmpeg + streamlink should be a few percent, not high.
❌ No `.ts` files appear: run `streamlink https://twitch.tv/<name> best -o test.ts` by itself and send me the error.

## Phase 4: Studio editing on the laptop, against the local watcher (20 min)
Keep Phase 3 running and approve 1–2 moments with a campaign selected (the `adhoc` chip).
```bash
copy studio.example.yaml studio.yaml        (Mac: cp)
```
Edit `studio.yaml`: `server_url: "http://localhost:8787"`, `token: "<token from Phase 3>"`.
```bash
python -m studio check
python -m studio sync
python -m studio edit
```
✅ **Pass:**
- `check` shows ffmpeg ok, an encoder (ideally `h264_nvenc`, `h264_qsv`, `h264_videotoolbox` or `h264_amf`, not `libx264`), whisper ok, and server ok.
- `Campaigns/adhoc/ready/` has an `.mp4` + `.caption.txt`. The first edit downloads the whisper model (~500 MB, once).
- **Watch the clip on your phone** (send it to yourself). Check:
  - captions match the speech and aren't late;
  - the text isn't hidden under the TikTok/Reels buttons;
  - the moment isn't cut off at the start or end;
  - the volume is sensible.
- Try `python -m studio edit <file> --layout crop` on the same clip (move it back from `inbox/done/` to `inbox/` first) and compare it with blur.
❌ Note anything that looks wrong (caption size/position, cut points). These are settings in `studio.yaml`, and I'll tell you which to change.

## Phase 5: Oracle server (45–60 min)
1. **Oracle account & VM:** Create a compute instance → Ubuntu 24.04 → shape **VM.Standard.A1.Flex, 4 OCPU / 24 GB** → boot volume 150 GB → add your SSH key.
   - "Out of host capacity" is common for free ARM. Try another Availability Domain, or upgrade the account to *Pay As You Go*. Free-tier usage stays free, and ARM becomes much easier to get.
2. SSH in, then:
   ```bash
   git clone -b ccr-8c9d8d98-0303s9 https://github.com/saad-designs/Clipping.git
   cd Clipping && sudo bash deploy/oracle-setup.sh
   ```
   ✅ The status shows `active (running)` and it prints a dashboard link + token.
3. **Tailscale:** `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up` (opens a login link). Install the Tailscale app on your laptop and phone with the same account. Get the IP with `tailscale ip -4`.
4. **Twitch keys:** dev.twitch.tv/console → Register Your Application (redirect `http://localhost`, category Other) → put the Client ID and Secret in `sudo nano /opt/clipping/settings.yaml`.
5. **Campaign:** `sudo nano /opt/clipping/campaigns.yaml` → your real Content Rewards campaign + its streamers → `sudo systemctl restart clipping-watcher`.
6. Watch it work: `journalctl -u clipping-watcher -f`
   ✅ **Pass:** `joined N twitch chats`, then `LIVE: twitch:<name>` and `recording` for live streamers.
7. Open `http://<tailscale-ip>:8787/?token=...` **on your phone** → the header shows "X live / Y channels" (not "unknown").
❌ Works on laptop but not on Oracle = an IP block. Send me the log lines.

## Phase 6: Laptop Studio → Oracle (10 min, then leave it running)
Change `studio.yaml` → `server_url: "http://<tailscale-ip>:8787"` and set the Oracle token. Then:
```bash
python -m studio check     # server: ok, N campaigns
python -m studio run       # leave running
```
✅ **Pass:** approve on your phone → within ~3 minutes the finished clip appears in `Campaigns/<campaign>/ready/` on the laptop, and the card moves to "On laptop" on the dashboard.

## Phase 7: Kick and YouTube Live (30 min)
Add one live Kick streamer and one live YouTube channel to `campaigns.yaml` and restart the service. Then in the logs:
- **Kick ✅:** `joined 1 kick chats`, `LIVE: kick:<name>`, `recording`.
  ❌ `kick API failed`: Cloudflare is blocking Oracle. Send me the line.
- **YouTube ✅:** `LIVE: youtube:<handle>`, `youtube chat connected`, `recording`.
  ❌ "confirm you're not a bot" or no recording: export cookies (browser extension "Get cookies.txt LOCALLY", while logged in to YouTube) → copy to the VM (`scp cookies.txt ubuntu@<ip>:` then `sudo mv ~/cookies.txt /opt/clipping/ && sudo chown clipper /opt/clipping/cookies.txt`) → set `youtube.cookies_file: /opt/clipping/cookies.txt` → restart.

## Phase 8: VOD mode on the laptop (20–40 min)
```bash
python -m studio vod "https://www.twitch.tv/videos/<id>" --campaign test --top 5
python -m studio vod "https://www.youtube.com/watch?v=<past livestream>" --campaign test --top 5
python -m studio vod "https://www.youtube.com/watch?v=<podcast>" --campaign test --top 5
```
✅ **Pass:** the log says how many chat messages it found (Twitch/YouTube streams), then `picked 1/5 …` lines, then finished clips in `Campaigns/test/ready/`. Watch them: are these really the best parts?
- `twitch chat replay unavailable` = Twitch blocked the method; it falls back to audio. Tell me.
- Podcasts (no chat) transcribe the whole video first, so expect a long first run.

## Phase 9: A real day (24 h)
Leave everything running for one stream day, then send me:
1. Total moments, how many you approved vs rejected (or a dashboard screenshot).
2. 2–3 examples of bad moments it flagged, and anything great it missed (with time).
3. On the VM, `top` and `df -h` while several streams record: CPU %, free disk.
4. Which clips you'd actually post vs. not, and why (length, captions, layout).

From that, I'll tune the threshold, the hype words for your streamers, the clip length and the caption style. Then you start posting.

## Quick reference
| Thing | Command |
|---|---|
| Server logs | `journalctl -u clipping-watcher -f` |
| Restart server | `sudo systemctl restart clipping-watcher` |
| Update server | `cd ~/Clipping && git pull && sudo bash deploy/oracle-setup.sh` |
| Server token | `sudo cat /opt/clipping/data/api_token.txt` |
| Laptop health check | `python -m studio check` |
| Mark clip uploaded | `python -m studio posted Campaigns/<c>/ready/<file>.mp4` |
