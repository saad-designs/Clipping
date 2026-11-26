# Setup: everything on your Windows PC (free, no card)

Clipping runs entirely on your PC. It follows your streamers' chats, records their streams to catch hype moments, edits each moment into a vertical Short, and shows it to you for approval. No server to rent and no credit card needed.

**The one rule:** keep the black Clipping window open while your streamers are live. If the PC is off or Clipping is closed, those moments are missed. Clipping stops the PC from going to sleep while it's open (the screen can still turn off).

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

## 4. Twitch keys (5 min, free, one time)
These let Clipping know when your streamers go live, so it only records live streams.
1. Go to **dev.twitch.tv/console** and log in with your Twitch account. Twitch asks you to turn on two-factor authentication first if you haven't.
2. **Register Your Application:**
   - Name: anything unique, e.g. `clipper-yourname`.
   - OAuth Redirect URL: `http://localhost` → **Add**.
   - Category: **Other**. Client type: **Confidential**.
   - Click **Create**.
3. Click **Manage** → copy the **Client ID** → click **New Secret** → copy the secret.
4. In Clipping Studio → **Settings** → paste both into **Twitch Client ID** and **Twitch Client Secret** → **Save settings**.

While you're there:
- **Record at most** sets how many streams are recorded at the same time. **Video quality** sets the sharpness. The page shows how much internet this uses. **720p** with **3 streams** is a good start. If your internet has a monthly data cap, pick **480p**.
- **Director:** leave it on **Free (built-in)**.

## 5. Your campaign (5 min)
**Campaigns → + New campaign:**
- Name, the Content Rewards link, and paste **the whole campaign brief**.
- **Streamers:** platform + channel name for each one in the campaign.
- Length rules and required hashtags from the brief.
- **Save campaign.** The watcher starts following those streamers right away.
- If the campaign gives you a logo, intro/outro or font, upload it under **Files**.

✅ Check: open **Stream watcher** in the left menu. You should see **Running on this PC** and your streamers with **Chat: Connected**. When a streamer is live, **Recording: Yes**.

## 6. Daily use
1. Keep Clipping open while your streamers are live.
2. Hype moments become finished vertical clips in **Review**:
   - Watch, then **A** to approve or **R** to reject.
   - Edit the hook or caption, or type what to change and press **Re-edit**.
3. **Ready to upload:** upload the clip, then **Copy caption**, paste it into TikTok/Shorts/Reels, and **Mark posted**.
4. **Queue → Add a video:** clip a VOD or any long video by hand.

## Good to know
- **Laptop?** Keep it plugged in. In Windows *Settings → System → Power*, set **"When I close the lid" → Do nothing** if you want to close it while Clipping runs.
- **Internet use** only happens while a followed streamer is live and being recorded. Following chat alone uses almost nothing.
- **Webcam layout wrong for a streamer?** Campaigns → **Draw box** next to that streamer, and drag a box around their webcam once.
- **Something not working?** Open **Stream watcher** → **Watcher log** and send me the last lines.

## Later, when the clips earn
- **Better edits:** Settings → Director → **Claude (paid)** with an Anthropic API key.
- **Clip around the clock without your PC:** rent a small server (~$4–6/month) and follow `ORACLE_SETUP.md`. In Settings, untick "Run the stream watcher on this PC" and enter the server's address.
