# Setup: everything on your Windows PC (free, no card)

Clipping runs on your PC, free, with no server and no card. It works from **your streamers' finished streams**:
- Whenever Studio is open, it checks for streams that ended since last time (Twitch, YouTube, Kick).
- It reads the chat replay and the clips viewers already made to find the best moments.
- It downloads **only those moments** and edits each into a vertical Short for you to approve.

**Your laptop only needs to be on while you're using it.** Studio catches up on everything it missed when you open it again.

The trade-offs: clips come from streams that already ended (a few hours old), and streamers who hide or delete their past streams can't be clipped. Twitch keeps past streams for 7–60 days depending on the streamer.

Time needed: about 15 minutes, plus downloads.

---

## 1. Get the code (2 min)
1. On github.com (logged in), open **saad-designs/Clipping**.
2. Click the branch button (top left of the file list) → choose **ccr-8c9d8d98-0303s9**.
3. Green **Code** button → **Download ZIP**.
4. Unzip it to a simple place, e.g. `C:\Clipping`.

## 2. Install (5–10 min, one double-click)
Open `C:\Clipping\windows` and double-click **`setup.bat`**.
- If Windows says "Windows protected your PC" → **More info** → **Run anyway**.
- If it says Python was just installed → close the window and double-click `setup.bat` again.
- ✅ It ends with **"Setup finished."**

## 3. Start (every day)
Double-click **`Start Clipping.bat`**. Your browser opens **Clipping Studio** at http://localhost:8790. Keep the black window open.

## 4. Settings (1 min)
In Clipping Studio → **Settings**:
- **How should Studio find hype moments?** → **From finished streams** (already selected).
- **Check every:** 2 hours is fine. **First time, look back:** 3 days.
- **Director:** **Free (built-in)**.
- **Save settings.**

No Twitch keys are needed in this mode.

## 5. Your campaign (5 min)
**Campaigns → + New campaign:**
- Name, the Content Rewards link, and paste **the whole campaign brief**.
- **Streamers:** platform + channel name for each one in the campaign.
- Length rules and required hashtags from the brief.
- **Clips per finished stream:** how many moments to take from each stream (5 is a good start).
- **Save campaign.**
- If the campaign gives you a logo, intro/outro or font, upload it under **Files**.

✅ Check: open **Streamers** in the left menu → **Check finished streams now**. After a few minutes, clips start appearing in **Queue** and then in **Review**. The first time, captions take longer because the speech model downloads once (~500 MB).

## 6. Daily use
1. Open Clipping when you want to work (e.g. once or twice a day). It checks for new finished streams by itself.
2. The best moments become finished vertical clips in **Review**:
   - Watch, then **A** to approve or **R** to reject.
   - Edit the hook or caption, or type what to change and press **Re-edit**.
3. **Ready to upload:** upload the clip, then **Copy caption**, paste it into TikTok/Shorts/Reels, and **Mark posted**.
4. **Queue → Add a video:** clip a VOD or any long video by hand.

## Good to know
- **Internet use** is small: chat replays are text, and only the picked moments are downloaded (about 50 MB per clip at 1080p).
- **Faster, live clipping later:** Settings → "Live, on this PC" (the PC must stay on while they stream) or "Live, on a rented server" (24/7, see `ORACLE_SETUP.md`).
- **Webcam layout wrong for a streamer?** Campaigns → **Draw box** next to that streamer, and drag a box around their webcam once.
- **Something not working?** Open **Stream watcher** → **Watcher log** and send me the last lines.

## Later, when the clips earn
- **Better edits:** Settings → Director → **Claude (paid)** with an Anthropic API key.
- **Clip around the clock without your PC:** rent a small server (~$4–6/month) and follow `ORACLE_SETUP.md`. In Settings, untick "Run the stream watcher on this PC" and enter the server's address.
