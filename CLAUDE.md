# Clipping: project handoff (read this first)

This file holds the full context of the project so far. It was built in a cloud Claude Code session that couldn't reach the user's PC, so the work continues in a local session on the PC.

---

## 1. End goal
A **clipping side hustle** for campaigns on **contentrewards.com**: streamers pay per view for short clips of their streams.

The tool should:
1. **Find hype moments** in the campaign streamers' streams (Twitch, Kick, YouTube), using chat spikes, viewer-made clips and audio loudness.
2. **Download only those moments** and **edit each into a vertical Short** (1080x1920):
   - facecam on top and game below, or another layout when there's no webcam;
   - big word-by-word live captions;
   - a hook line at the start;
   - zooms, SFX and loudness at -14 LUFS;
   - the campaign's logo, intro/outro, required text and hashtags.
3. Use a **"director brain"** to decide the edit for each clip, so **no two clips look the same** (layout, caption style, hook, zoom timing).
4. **File clips by campaign**. The user **approves each clip on a dashboard**, then **uploads by hand** to TikTok, Shorts and Reels.

**Hard constraints (right now):**
- **Free.** The user has **no credit card**, so no Oracle, no paid VPS and **no paid Anthropic API** until the clips earn money.
- **The laptop can't run 24/7.** It runs only while the user is using it.
- Windows PC: an Asus ROG.

**Later, once earning:** turn on the Claude director (paid API) for better hooks, and add a 24/7 server (Oracle or a ~$5 VPS) for live clipping.

---

## 2. Repo
- GitHub: **`saad-designs/Clipping`**
- Branch: **`ccr-8c9d8d98-0303s9`**. It is the only branch and holds all the work, so commit and push here.
- User's local folder: `D:\Start Design\Clipping`
- Python 3.11+, ffmpeg required.

Commit history, oldest first:
1. Watcher step 1: Twitch chat hype detection
2. Watcher step 2: rolling recorder, clip cutter, audio signal
3. Watcher steps 3+5: review dashboard, API, Kick and YouTube Live
4. Studio (laptop): sync, editor, VOD mode
5. Step 7: Oracle deploy, full guide, dashboard polish
6. Testing guide, then fixes from real-world testing, Windows double-click setup and the Oracle guide
7. Tuning from the live test, plus the test report
8. Studio 2.0: campaign dashboard, AI director, hype editing
9. Free built-in director as the default; Claude becomes an optional upgrade
10. One-PC mode: run the stream watcher on the Windows PC, free
11. Clip from finished streams: free, no 24/7 computer, no card
12. Setup survives a shaky connection (the latest)

---

## 3. How it finds moments: three modes
Settings → "How should Studio find hype moments?" (`find_mode` in `studio/store.py`):

| Mode | What it does | Status |
|---|---|---|
| **`vods` (default, free)** | Whenever Studio is open, checks every `sweep_hours` (2) for streams that **ended** since last time. The first time, it looks back `sweep_days` (3). | **Use this now** |
| `local` | Runs the live stream watcher on the PC. The PC must stay on while the streamers are live. | Built, optional |
| `server` | The watcher runs on Oracle or a VPS around the clock; Studio pulls the moments from it. | Built, needs a card |

How the `vods` sweep works (`studio/sweep.py`):
- **Twitch:**
  - The GQL API (public client id `kimne78kx3ncx6brgo4mv6wki5h1ko`) lists past broadcasts plus viewer clips.
  - The chat replay is paged **by offset**, because cursor paging fails Twitch's integrity check.
  - The scorer finds chat spikes, and viewer clips (`videoOffsetSeconds`) boost moments.
  - If there's no chat, it falls back to audio loudness.
- **YouTube / Kick:** `youtube_vods`, `kick_vods`.
- **Download:** `hls_section()` fetches only the `.ts` segments for each moment from the VOD m3u8 and joins them. It replaced yt-dlp `--download-sections`, which failed behind proxies.
- **Bookkeeping:** VODs already done go into the `seen_vods` table; the last check per streamer goes into `sweeps`.
- **Clips per stream:** `clips_per_stream`, set per campaign (default 5).

---

### The scout (Gemini on): the director picks the moments itself
Chat spikes don't work for small streamers (Lauren: ~7 msgs/min, viewer clips with 10-200 views): they handed over ordinary gameplay. With Director = Gemini, `studio/scan.py` downloads a tiny 160p copy of the campaign-game parts of each finished stream, cuts it into 20-min pieces and has Gemini list moments worth a Short (MM:SS times, what, why, strict 1-10 score). Moments scoring 6+ are downloaded in full quality (setup → reaction, 30-100 s, `sweep.window`). The two most-viewed viewer clips the scout passed over are added as a safety net. Each piece is cached in `studio_data/scans/<vod>.json`, so interrupted runs resume. Campaign field `games` restricts clipping to Twitch chapters showing those games (`sweep.allowed_ranges`).

## 4. Architecture

```
watcher/            live hype detection (runs on a server, or on the PC in "local" mode)
  app.py            main loop per platform, API server, viewer-clips loop, disk guard,
                    restart (Windows: exit code 3; POSIX: execv)
  scorer.py         ChannelScorer: z-score per 5 s bucket, audio boost, hype gate,
                    per-user cap, bot filter, max moments/hour, top_chat phrases
  recorder.py       streamlink -> ffmpeg segment muxer, files %Y%m%d-%H%M%S.ts
  cutter.py         cuts moments from segments
  audio.py          ebur128 loudness analysis
  api.py            token-auth API: moments, clips (Range), campaigns (PUT writes yaml + restarts), health
  db.py             SQLite
  platforms/        twitch (IRC over websocket :443, Helix, clips), kick (Pusher), youtube (innertube chat)
  dashboard/        watcher's own web page
shared/
  config.py         settings + campaigns yaml, write_campaigns()
  hype_tokens.py    is_hype, is_noise, BOTS
studio/             the PC app (the part the user uses)
  __main__.py/cli.py  python -m studio app | check | sync | run | edit | vod | posted
  app.py            FastAPI dashboard on http://localhost:8790 (settings, campaigns + assets,
                    clips approve/reject/edit/reedit/retry/posted, ingest, /api/sweep, /api/local-watcher)
  store.py          SQLite: settings, campaigns (JSON + asset files), clips, seen_vods, sweeps
  worker.py         background loop: vods -> sweep_once() every sweep_hours; other modes -> sync_once();
                    process(): transcribe -> loudness -> facecam -> director -> render -> review/
                    approve -> ready/ + caption .txt
  sweep.py          finished-streams sweep (see section 3)
  local_watcher.py  supervises the watcher in "local" mode; keep_awake (SetThreadExecutionState)
  director.py       plan schema, validation (campaign rules enforced), Claude director (optional)
  free_director.py  free rule-based director (DEFAULT): classify, payoff, hook candidates (en/de/es), quality
  sync.py, client.py  pull moments from the watcher API (server/local mode)
  vod.py            manual: clip any long video / VOD URL
  editor/
    render.py       layouts facecam_split | face_crop | blur_fit | game_only, zooms, logo, SFX, intro/outro
    reframe.py      ffmpeg filter graphs 16:9 -> 1080x1920
    captions.py     4 caption presets, pop animation, emphasis words, hook, emoji stripped, Inter Black font
    facecam.py      OpenCV Haar (upscaled, CLAHE, profile + flipped, skin check)
    transcribe.py   faster-whisper, audio decoded via ffmpeg (not PyAV); returns words + language
    encode.py       hardware encoder auto-detect: NVENC, QSV, AMF, VideoToolbox, else libx264
    trim.py, pipeline.py
  assets/           sfx/*.wav, fonts/Inter-Black.otf (+ OFL.txt)
  web/index.html    dashboard UI
windows/
  setup.bat         winget installs Python + ffmpeg, adds %LOCALAPPDATA%\Microsoft\WinGet\Links to PATH,
                    runs install_packages.py (no pip self-upgrade, no Git needed)
  install_packages.py  one package at a time, --retries 10 --timeout 90, 3 attempts each,
                    offers the mirror pypi.tuna.tsinghua.edu.cn if PyPI keeps failing
  Start Clipping.bat   python -m studio app
  Check setup.bat      python -m studio check
deploy/             oracle-setup.sh, clipping-watcher.service (for the later server)
tests/              pytest: scorer, recorder, api, studio, director, studio_app, local_mode, sweep
requirements-studio.txt   pyyaml, faster-whisper, yt-dlp, anthropic, opencv-python-headless>=4.8,<5,
                          fastapi, uvicorn, python-multipart
requirements-watcher.txt  pyyaml, streamlink, yt-dlp, fastapi, uvicorn, websockets, curl_cffi
```

**Data flow:**
1. A sweep (or the watcher) finds a moment, and the clip is `queued`.
2. The worker processes it: transcribe, loudness, facecam, director plan, render.
3. The clip shows in **Review** (`editing` → `review`).
4. The user approves it. The clip moves to `Campaigns/<campaign>/ready/` along with a caption `.txt`, and the user uploads by hand and clicks **Mark posted**.

Studio data lives in `studio_data/` (SQLite `studio.db`, campaigns, clips). Finished clips go to `Campaigns/`. **Never delete these two folders when updating the code.**

---

## 5. The director
- **Gemini director (in use, free key from AI Studio, no card):** `studio/gemini_director.py`. Sends a 480p copy of the clip *with sound* plus transcript, chat, campaign brief and the scout's notes. Returns what it saw, who speaks when (streamer/game), a verdict (post or skip), story beats, kept segments (jump cuts), punch-ins with target face/game and a reason each, hook, caption style. Skipped clips go to rejected with the reason. Captions drop game dialogue (`director.streamer_words`).
  - Interactions API (`POST v1beta/interactions`, header `Api-Revision: 2026-05-20`), `response_format` JSON schema, `generation_config.thinking_level`.
  - Model chain: gemini-4-hydrogen → gemini-3.8-flash → gemini-3.6-flash → gemini-3.5-flash-lite. **Free tier = 20 requests/day per top model**; 503 "high demand" is common (retry 10 s/30 s, then skip that model 10 min); 429 skips it until Google's "retry in …".
  - Busy / out of quota / no internet → `DirectorWait`: the clip waits in the queue, it is NOT handed to the rules director.
  - Free tier: Google may use the clips sent to improve its products.
- **Rules director (no key):** `studio/free_director.py`, rule-based. It picks the layout, caption style, hook text (en/de/es templates plus top chat phrases), payoff point (the loudest moment near the chat spike), zooms and trim. It avoids repeating recent styles and hooks (`store.recent_styles`, `store.recent_hooks`).
- **Claude director (optional, paid):** `studio/director.py`. Turn it on with Settings → Director = Claude plus an API key.
  - Model: default `claude-opus-5-5`, with a dropdown for Haiku 5.5, Sonnet 5.5 or Opus 5.5.
  - Call: `client.beta.messages.create` with `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`, and `output_config={"effort":..., "format":{"type":"json_schema","schema":PLAN_SCHEMA}}`.
  - Input: 4 JPEG frames sent as images, plus the transcript and campaign brief.
  - `validate()` always enforces the campaign rules (length, required text, banned words).
  - **Don't make it the default.** The user doesn't pay for the API until the clips earn.

---

## 6. Dashboard (http://localhost:8790)
Start it with `python -m studio app` or `windows\Start Clipping.bat`. Pages:
- **Review:** watch each clip; **A** approves, **R** rejects. Edit the hook or caption, or type notes and press **Re-edit**.
- **Ready to upload:** **Copy caption**, then **Mark posted**.
- **Queue:** clips being processed. **Add a video** clips any VOD or URL by hand.
- **Campaigns:** name, Content Rewards link, the whole brief, and streamers (platform + channel). Also length rules, required text and hashtags, clips per stream, and **Files** (logo, intro/outro, font). **Draw box** marks a streamer's webcam by hand.
- **Streamers:** sweep status and the **Check finished streams now** button.
- **Settings:** finding-moments mode, check interval and look-back, director (Free or Claude), encoder, whisper model, output folder.

---

## 7. Verified so far (in the cloud session)
- **78 tests pass** (`python -m pytest -q`).
- **Live detection:** run on 5 real Twitch streams.
- **Real clip edits:** whisper captions, facecam, hooks.
- **Real VOD sweep on tfue:** 8,773 chat messages, 3 moments found, then downloaded and edited automatically.
- **install_packages.py:** checked.
- **Not yet verified on the user's Windows PC.** The user's first `setup.bat` run failed on a flaky connection (pip resets, hash mismatch, JSONDecodeError). The per-package installer was written for that, but it hasn't run on the PC yet.

---

## 8. Decisions and why
- **Oracle Free Tier is out for now:** it needs a credit card.
- **Free hosts were all checked and ruled out:**
  - Render, Koyeb, Railway and Hugging Face Spaces sleep, have hour limits, or ban this use in their terms.
  - Cloudflare Durable Objects can't run ffmpeg or streamlink.
  - GitHub Actions bans this kind of continuous use.
  - Recording streams live needs a 24/7 machine.
- **So the free path is finished-streams (VOD) clipping.** Chat replay and viewer clips are available after the stream ends, so nothing has to stay online. Studio catches up whenever the laptop is opened.
- **The trade-off:** clips are a few hours old, and streamers who hide their VODs can't be clipped.
- **The free director is the default; Claude is an upgrade later.**

---

## 9. Bugs already fixed: don't regress these
- **Twitch chat:** IRC port 6697 was blocked, so chat uses the **websocket `wss://irc-ws.chat.twitch.tv:443`**.
- **Twitch GQL VOD chat:** must page **by offset**, not cursor.
- **faster-whisper:** breaks with new PyAV, so audio is **decoded with ffmpeg**.
- **OpenCV:** pinned **`<5`**, because 5 has no Haar cascades. Don't install mediapipe; it pulls in opencv-contrib.
- **Moment picking:**
  - bots and commands are filtered by `is_noise`;
  - moments are capped per hour with a median rule (ohnepixel once gave 16 moments in 43 minutes).
- **Captions:**
  - line breaks by pixel width (long German words made 3-line captions);
  - no overlapping captions;
  - emoji stripped from burned-in text.
- **Windows:**
  - no `os.pipe` (an asyncio copy loop instead);
  - segment filenames without `%s`;
  - restart via `sys.exit(3)` and the supervisor relaunches, instead of `os.execv`;
  - an intentional restart isn't counted as a crash (`_requested` flag).
- **Facecam missed dark or side-on faces:** fixed with upscaling, CLAHE, profile plus flipped profile detection, and a skin check.
- **Dashboard:** a hidden modal was showing; the `[hidden]` CSS rule fixes it.
- **Setup on Windows:**
  - ffmpeg is installed by winget into `%LOCALAPPDATA%\Microsoft\WinGet\Links`, which every .bat adds to PATH;
  - PyPI drops are handled by per-package retries.

---

## 9b. Found and fixed on the user's PC (Nov 28)
- Whisper on the RTX 4060 failed silently (no `cublas64_12.dll`) → clips had no captions. Falls back to CPU; `studio check` runs a real transcription.
- Every clip opened with a white-noise "whoosh" ~20 dB over a quiet stream, and dynamic loudnorm pumped hiss. Now: measured steady gain to -14 LUFS + limiter, no automatic sound effects (the bundled SFX are synthetic placeholders; real ones needed).
- Hooks said "HE" for a female streamer → campaign `pronoun` (she/he/they). Post titles "Didn'T" → fixed.
- Game crop showed a webcam sliver → `render.avoid_cam`.
- Tests: 18 failed only on Windows temp-folder cleanup (open SQLite) → `tests/conftest.py`. `test_recorder` timing test still fails on Windows (live mode only).
- Webcam box was a guess from the face size (often too tight; on a two-person cam it cut Lauren out). `facecam.find_overlay` now finds the real overlay border: long unbroken lines in the per-clip median edge map, each confirmed by "game moves outside, room still inside" (rejects headboards/shelves). A side with no line runs to the frame edge. If a clip can't find it, `Worker._overlay_from_same_stream` borrows the overlay of the nearest-in-time clip of the same stream that contains the face (streamers move overlays mid-stream: Lauren did). 10/11 found directly on her clips, 1 borrowed.

## 9c. Remote dashboard (built, NOT switched on yet: do it on the permanent laptop)
The user wants the dashboard reachable from anywhere: backend stays on the PC, the page is hosted on Vercel and talks to the PC through Tailscale Funnel. The laptop used so far is only for building, so Funnel and Vercel were deliberately left off.
- **Built and pushed:** `studio/auth.py`. Every /api call needs `Authorization: Bearer <token>`; dashboard password (pbkdf2, Settings → Dashboard access, min 10 chars); 30-day sessions; 5 wrong passwords lock the login for 10 min. Videos/logos/frames use signed links (`?sig=`, 6 h) because `<video>/<img>` can't send headers; `&dl=1` downloads. CORS is `*` (safe: bearer tokens, no cookies). On start-up Studio opens and prints a one-time `http://localhost:8790/#login=<token>` link, so the PC's browser is signed in. The page (`studio/web/index.html`) works served by Studio (`window.STUDIO_SAME_ORIGIN`) or from another address (asks for the PC's https address and password, shows "PC offline"). `studio/web/vercel.json` adds noindex/no-frame headers.
- **Steps on the permanent laptop**, after the normal setup (section 10):
  1. Run Studio once, then **Settings → Dashboard access → set a password** (the user types it; never in chat).
  2. `winget install -e --id Tailscale.Tailscale` (admin prompt), then sign in with the user's Tailscale account.
  3. `tailscale funnel --bg --yes 8790`. The first time it prints a login.tailscale.com link to **enable Funnel** (and HTTPS certificates) on the tailnet. The user approves it. The address is `https://<machine>.<tailnet>.ts.net`; check it with `tailscale funnel status`. `--bg` keeps it on across reboots.
  4. Vercel: `cd studio\web` → `npx vercel login` (the user signs in) → `npx vercel deploy --prod`. Open the vercel.app address, enter the ts.net address and the password.
  5. Test from a phone on mobile data: sign in, play a clip, Download, approve. Then turn the PC off: the page should say "Your PC is offline".
- The build laptop has Tailscale installed and signed in (machine `asus-rog`) but **no Funnel/serve config**. Remove the device from the tailnet, or uninstall Tailscale, if it's no longer wanted.

## 10. Next steps (for this local session)
1. **Update the folder** to the latest `ccr-8c9d8d98-0303s9`.
   - If it's a git checkout: `git fetch origin ccr-8c9d8d98-0303s9 && git checkout ccr-8c9d8d98-0303s9 && git pull`.
   - If not: download the branch ZIP and unzip it over the folder, **keeping `studio_data/` and `Campaigns/`**.
2. **Install:** run `windows\setup.bat`, or `python windows\install_packages.py` directly.
   - The user's internet drops downloads, so retry failed packages.
   - The mirror (`-i https://pypi.tuna.tsinghua.edu.cn/simple`) is the fallback.
3. **Check:** `python -m studio check` should show **ffmpeg ok**, an **encoder** (NVENC on the ROG if it has an NVIDIA GPU) and **whisper ok**. Fix anything that fails.
4. Optionally run the tests: `python -m pytest -q`.
5. **Start the dashboard** (`python -m studio app`), then walk the user through:
   1. **Settings:** "From finished streams", check every 2 h, look back 3 days, Director = Free.
   2. **Campaigns → New:** the Content Rewards link, the whole brief, the streamers, length rules, hashtags, clips per stream (5), and files if the campaign provides them.
   3. **Streamers → Check finished streams now.**
6. **Confirm clips reach Review.** The first run downloads the whisper model (~500 MB), so it's slower. Then review clip quality with the user and tune it.

**Later, not now:**
- the Claude director once earning;
- live mode on a VPS or Oracle once a card exists (`ORACLE_SETUP.md`, `deploy/`).

---

## 11. Working rules from the user
- **Language:** plain and direct. No corporate tone, buzzwords or filler.
- **Push back:** when the user's idea is wrong, say so and explain why.
- **Build section by section,** and finish one part before the next.
- **Keep the copy honest:** content and hooks must sound human, not generic. Iterate until it's good.
- **Secrets:** never ask the user to paste API keys or passwords into chat, and never commit them. Keys go in Studio's Settings, which masks them.
- **Git:** commit and push to branch `ccr-8c9d8d98-0303s9`.
- **Privacy:** don't publish real streamer clips publicly. Keep demos private.

Other docs in the repo: `SETUP.md` (the free PC guide), `ORACLE_SETUP.md` (later server), `README.md`, `TESTING.md`, `TEST_REPORT.md`.
