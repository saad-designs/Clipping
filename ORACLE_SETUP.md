# Optional: a 24/7 server (Oracle or any VPS)

> **Start with [SETUP.md](SETUP.md).** It runs everything on your PC for free. Use this guide later only if you want clipping to run while your PC is off. Oracle needs a card for verification; any small Ubuntu VPS works the same way.

There are three parts:
- **A.** the server on Oracle (watches streams 24/7)
- **B.** your private connection (Tailscale)
- **C.** the Studio app on your Windows PC (edits the clips)

Do them in order. Each step says what you should see. If something looks different, copy the text on your screen and send it to Claude.

You'll need: your Oracle account, a Twitch account, a GitHub login, and ~1 hour.

---

## A. The server (Oracle Cloud)

### A1. Create the server (10 min)
1. Log in at **cloud.oracle.com** → menu (☰) → **Compute** → **Instances** → **Create instance**.
2. **Name:** `clipper`.
3. **Image and shape** → click **Edit**:
   - **Change image** → **Canonical Ubuntu** → version **24.04** → Select.
   - **Change shape** → **Ampere** → tick **VM.Standard.A1.Flex** → set **OCPUs: 4**, **Memory: 24 GB** → Select.
4. **Networking:** leave the defaults. Make sure **"Assign a public IPv4 address"** is on.
5. **Add SSH keys:** choose **"Generate a key pair for me"** → click **Save private key**. A file like `ssh-key-2026-11-26.key` downloads. Keep it, because you can't download it again.
6. **Boot volume:** tick **"Specify a custom boot volume size"** → **150** GB.
7. Click **Create**. After 1–2 minutes the status turns green: **RUNNING**. Copy the **Public IP address** shown on that page.

> **"Out of capacity for shape VM.Standard.A1.Flex"?** This is very common on free accounts. Either try again later or pick a different **Availability domain** (in step 3's Placement section). Or upgrade your account to **Pay As You Go** (Billing → Upgrade). The free allowance still applies and you pay nothing as long as you stay within it, and ARM servers become much easier to get.

### A2. Connect to the server from Windows (5 min)
1. Move the key file somewhere safe: create a folder `C:\Users\<you>\.ssh` and move the `.key` file there. Rename it to `oracle.key`.
2. Press **Start**, type **PowerShell**, and open it. Paste these lines one at a time (Enter after each). They lock the key file so only you can read it, which SSH requires:
   ```powershell
   icacls "$env:USERPROFILE\.ssh\oracle.key" /inheritance:r
   icacls "$env:USERPROFILE\.ssh\oracle.key" /grant:r "$($env:USERNAME):(R)"
   ```
3. Connect (put your server's IP in):
   ```powershell
   ssh -i "$env:USERPROFILE\.ssh\oracle.key" ubuntu@YOUR.PUBLIC.IP
   ```
   Type `yes` when asked about the fingerprint. ✅ You'll see `ubuntu@clipper:~$`. You're now typing on the server.

### A3. Get a GitHub token (the code is in a private repo) (3 min)
On your PC: github.com → your photo (top right) → **Settings** → **Developer settings** (bottom left) → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
- Name: `oracle-clipper`. Expiration: 90 days.
- **Repository access:** Only select repositories → **Clipping**.
- **Permissions** → Repository permissions → **Contents: Read-only**.
- **Generate token** → copy it (it starts with `github_pat_`).

### A4. Install everything (10 min, one paste)
In the server window (PowerShell, connected to the server), paste:
```bash
git clone -b ccr-8c9d8d98-0303s9 https://github.com/saad-designs/Clipping.git && cd Clipping && sudo bash deploy/oracle-setup.sh
```
- **Username:** your GitHub username.
- **Password:** paste the token. Nothing shows while you paste; that's normal. Press Enter.

✅ After a few minutes you'll see `Active: active (running)` and a **Done. Next:** message with a dashboard link and a token. **Copy that token** somewhere.

### A5. Twitch keys (5 min)
1. On your PC: **dev.twitch.tv/console** → log in. Twitch requires 2-factor authentication to be on for this page.
2. **Register Your Application:**
   - Name: anything unique, e.g. `clipper-yourname`.
   - OAuth Redirect URL: `http://localhost` (click Add).
   - Category: **Other**. Client type: **Confidential**.
   - Click Create.
3. Click **Manage** → copy the **Client ID** → click **New Secret** → copy the secret.
4. In the server window:
   ```bash
   sudo nano /opt/clipping/settings.yaml
   ```
   Use the arrow keys to go to `client_id: ""` and paste the ID between the quotes, then the same for `client_secret`. Save with **Ctrl+O** then **Enter**, and exit with **Ctrl+X**.

### A6. Campaigns: set them up from your PC
You don't edit campaign files on the server. You add campaigns in the Studio dashboard on your PC (part C), press **Send channels to server**, and the server restarts itself to watch the new streamers.

To check the server is running:
```bash
journalctl -u clipping-watcher -f
```
Press **Ctrl+C** to stop watching the log; the server keeps running.

---

## B. Private connection (Tailscale) (10 min)
Tailscale lets only *your* devices reach the dashboard, with nothing open to the internet.

1. In the server window:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   It prints a link. Open it on your PC and sign in (Google is fine).
2. Get the server's private address:
   ```bash
   tailscale ip -4
   ```
   It looks like `100.101.102.103`. Write it down.
3. Install **Tailscale** on your **Windows PC** (tailscale.com/download) and your **phone** (App Store / Play Store). Sign in with the **same account**.
4. On your phone or PC browser, open:
   `http://100.101.102.103:8787/?token=YOUR-TOKEN`
   ✅ You'll see **Clip Review**, and moments appear there as streams get hype. Bookmark it; after the first visit you don't need the token in the link.

Lost the token? In the server window: `sudo cat /opt/clipping/data/api_token.txt`

---

## C. Studio on your Windows PC (15 min)
Studio is where clips get edited and where you approve them. It runs on your PC and opens in your browser.

1. **Get the code:** on github.com open **saad-designs/Clipping** → the branch button (top left of the file list) → pick **ccr-8c9d8d98-0303s9** → green **Code** button → **Download ZIP**. Unzip it somewhere simple, e.g. `C:\Clipping`.
2. **Install:** open the `windows` folder and double-click **`setup.bat`**.
   - "Windows protected your PC" → **More info** → **Run anyway**.
   - If it says Python was just installed, close the window and double-click `setup.bat` again.
3. **Start:** double-click **`Start Studio.bat`**. Your browser opens **Clipping Studio** (http://localhost:8790). Keep the black window open while you work.
4. **Settings** (left menu):
   - **Server address:** `http://100.101.102.103:8787` (your server's Tailscale address) and the **token**.
   - **Director:** leave it on **Free (built-in)**. It costs nothing. When the clips start earning, you can switch to **Claude (paid)** here with an Anthropic API key: Haiku 5.5 is about 0.2 US cents per clip, Opus 5.5 (best) about 3–6 cents.
   - Save.
5. **Campaigns → + New campaign:**
   - Name, the Content Rewards link, and **paste the whole campaign brief**.
   - Add the streamers (platform + channel name).
   - Set the length rules and required hashtags from the brief.
   - **Save**, then upload the logo, intro/outro or font if the campaign gives you one.
   - Press **Send channels to server**.
6. **Webcam position** (optional): Studio finds the streamer's webcam automatically. If it gets one wrong, open the campaign → **Draw box** next to that streamer → drag a rectangle around the webcam once. Every clip from that streamer uses it.

## Everyday use
1. The server catches hype moments. Studio downloads each one and edits it: facecam on top, game below, big captions, a hook title, zoom and sound on the moment, your logo.
2. Open **Review**. Watch each clip.
   - **A:** approve.
   - **R:** reject.
   - Change the hook or caption text and press **Save changes & re-render**.
   - Or type what you want different ("funnier hook, start 2 seconds later, no sound effects") and press **Re-edit**.
3. **Ready to upload:** upload the clip, press **Copy caption** and paste it into TikTok/Shorts/Reels, then **Mark posted**.
4. **Queue → Add a video:** paste a VOD link or upload a file to clip it by hand.

## Updating later (when Claude changes the code)
- **Server:** connect (A2), then `cd ~/Clipping && git pull && sudo bash deploy/oracle-setup.sh`
- **PC:** download the ZIP again and replace the folder. Keep your `studio.yaml` and the `Campaigns` folder.
