# One-time setup

Twenty-five minutes, five parts. Everything here survives reboots; only firmware updates undo parts 2–3.

Parts 1 and 5 are two different ways to read your feedback, and you do not need both. The **dashboard** (part 5) renders maths properly, shows progress and lets you manage responses, but only works while you are on the same network as the watcher. **ntfy** (part 1) is a push notification that reaches you anywhere. Start with the dashboard; add ntfy if you want alerts away from your desk.

## 1 · Phone notifications (ntfy) — 2 min · optional

Push is **off by default**, because the dashboard already shows every reply and the ntfy round-trip is a second copy of it. Turn it on only if you want alerts when you are away from the dashboard.

1. Install **ntfy** (free, iOS/Android).
2. Invent a topic name. It is the ONLY secret protecting your feedback — make it long and random, e.g. `rt-9f2a71c4b8e3`. Tap **+** in the app and subscribe to it.
3. On the PC: `setx RM_NTFY_TOPIC "rt-9f2a71c4b8e3"` and `setx RM_NTFY 1` (on Linux/macOS, export both before launching). Without `RM_NTFY=1` the watcher stays silent no matter what topic is set.
4. Test from PowerShell — it should pop on your phone in seconds:
   ```powershell
   Invoke-RestMethod -Uri "https://ntfy.sh/rt-9f2a71c4b8e3" -Method Post -Body "hello"
   ```

## 2 · SSH onto the tablet — 10 min

The root password lives at **Settings → Help → Copyrights and licenses**, scrolled to the very bottom (GPLv3 block). The IP is shown there too. The unlock PIN is unrelated to SSH.

**USB first (always works):** plug the tablet in, then:
```powershell
ssh root@10.11.99.1
```
If Windows shows no `10.11.99.x` interface at all: try the reMarkable's own cable (many USB-C cables are charge-only) and a rear motherboard port.

**WLAN (so you can roam):** there is no Settings toggle for this, and no live status indicator — the only mention in the UI is static small print on the same Copyrights page as the password, which also names the official enable command. Note that firmware updates silently disable it again. Over the USB connection, run:
```bash
rm-ssh-over-wlan on
```
If your firmware lacks that binary, the manual equivalent is:
```bash
touch /home/root/.config/remarkable/rm_enable_ssh_wifi_marker
systemctl start dropbear-wlan.socket
```
To check the current state later (there is no UI indicator): `ls ~/.config/remarkable/rm_enable_ssh_wifi_marker` over any SSH connection — the file existing means enabled. Or simply: if SSH over Wi-Fi connects, it's on.
Then from the PC: `Test-NetConnection <tablet-ip> -Port 22` should say `TcpTestSucceeded: True`.

**Give the tablet a static DHCP lease on your router** (its MAC is on the same About screen) or you'll be editing IPs after every lease renewal.

## 3 · Key auth (required — the watcher can't type passwords) — 5 min

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\remarkable" -N '""'
type "$env:USERPROFILE\.ssh\remarkable.pub" | ssh root@<tablet-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo OK"
```
Type the root password once. Then add to `~/.ssh/config` on the PC:
```
Host remarkable
    HostName <tablet-wifi-ip>
    User root
    IdentityFile ~/.ssh/remarkable
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    ServerAliveInterval 15
    ServerAliveCountMax 3

Host remarkable-usb
    HostName 10.11.99.1
    User root
    IdentityFile ~/.ssh/remarkable
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
```
`ssh remarkable "echo OK"` must print OK with **no password prompt**. The watcher tries `remarkable` first, then falls back to `remarkable-usb` automatically.

> **After every firmware update:** redo the `authorized_keys` line and the WLAN marker file. The update wipes both; nothing else breaks.

**Optional but recommended — raise the SSH connection cap.** Dropbear on the tablet is socket-activated with MaxConnections=64, and orphaned sessions (from sleep/wake cycles) count against it; when full, the tablet accepts TCP but never answers ("banner exchange timeout" while demonstrably awake). The watcher holds one persistent connection precisely to avoid this, but the belt-and-braces fix:
```bash
ssh remarkable 'for s in dropbear-wlan dropbear-usb0 dropbear-usb1; do mkdir -p /etc/systemd/system/$s.socket.d; printf "[Socket]\nMaxConnections=256\n" > /etc/systemd/system/$s.socket.d/rm-tutor-cap.conf; done; systemctl daemon-reload'
```
(Also wiped by firmware updates. If the tablet ever shows this symptom anyway, a reboot clears the wedged connections.)

## 4 · PC side — 3 min

```powershell
pip install -r requirements.txt    # rmscene, pymupdf, pillow
npm install -g @anthropic-ai/claude-code
claude          # sign in once; the watcher shells out to `claude -p`
setx RM_STUDY_ROOT "C:\path\to\your\Study"
```
Python must be **3.10+** (`rmscene` requires it). If you have several Pythons, the launcher pins `py -3.13` — edit it to match yours.

Finally, edit the `WORKBOOKS` map at the top of `rm_feedback.py` (beside this file): for each PDF you study on the tablet, map its tablet document name to `(source PDF path, marking-notes markdown path)`, both relative to `RM_STUDY_ROOT` — keep one folder per module, since the first path segment of the source PDF is treated as the module name. Fill in the `EXAM_DATES` dict beside the map so the tutor knows each module's exam. The marking notes are what answers get marked *against* — see `../workbook-pipeline/BUILD-YOUR-MODULE.md` for how to produce good ones.

## 5 · The dashboard — on the computer and on your phone — 5 min

The watcher serves a small read-only web page: the live response feed with maths rendered properly, per-channel activity lights, a progress bar per workbook, the study tracker and a countdown timer. It is a mirror, not a control surface — ink on the tablet remains the only way to drive the watcher.

### On the computer running the watcher

Nothing to configure. Start the watcher and open:

```
http://localhost:8477
```

The page needs the watcher running; it holds no state of its own and the feed starts empty on each launch.

### On your phone (same Wi-Fi)

By default the server binds to loopback only, so nothing else on the network can reach it. To open it to your LAN:

**Linux/macOS** — uncomment the `RM_WEB_HOST` line in `START-WATCHER.sh`, or export it yourself before launching:
```sh
export RM_WEB_HOST=0.0.0.0        # 127.0.0.1 puts it back to this machine only
export RM_WEB_PORT=8477           # optional
```

**Windows** — `setx RM_WEB_HOST 0.0.0.0`, then open a new terminal so the variable is picked up.

Then find the computer's LAN address:

```sh
ip -4 -brief addr           # Linux   -> e.g. 192.168.1.42/24
ipconfig                    # Windows -> "IPv4 Address"
ipconfig getifaddr en0      # macOS
```

Open `http://<that-address>:8477` on the phone. If it times out, the firewall is the usual cause — open the port to your subnet only, never the whole world:

```sh
sudo ufw allow from 192.168.1.0/24 to any port 8477 proto tcp    # Linux (ufw)
```
```powershell
New-NetFirewallRule -DisplayName "reMarkable dashboard" -Direction Inbound `
  -LocalPort 8477 -Protocol TCP -Action Allow -Profile Private
```

### Make it a home-screen app

The page ships a web app manifest and an icon, so it installs without a store:

- **iOS/Safari** — Share → *Add to Home Screen*. It launches full screen with no browser chrome.
- **Android/Chrome** — ⋮ → *Install app* / *Add to Home screen*.

The layout has a dedicated phone breakpoint: the response feed, timer and controls are all reachable one-handed, and the workbook name is abbreviated (`MODULE-A WB2`) to fit.

### Two limits worth knowing up front

- **It is LAN-only.** Leave the house and the page stops loading. A mesh VPN such as Tailscale on both devices fixes this and gives you an HTTPS hostname as a side effect.
- **No push, and no authentication.** Web push on iOS requires HTTPS, which plain-HTTP-over-LAN cannot provide — that is what ntfy (part 1) is for. There is also no login: anyone on your network who finds the port can read your feedback and delete cards. That is a reasonable trade on a home network and a bad one on a shared or public one, where you should leave `RM_WEB_HOST` at its default.

## Daily use

Double-click `START-WATCHER.bat` (Windows) or run `sh START-WATCHER.sh` (Linux/macOS). Circle in red to get marked, blue to get explained, grey for commands. Erase circles once answered. Turn the page after circling — it forces the tablet to flush the stroke file, which is what fires the trigger. Ctrl-C or close the window when done.

**Out and about:** put the tablet and the computer on the same phone hotspot and start the watcher as normal. When the configured ssh aliases don't answer, it scans the hotspot subnet by itself (iPhone's range and the USB address are built in; `export RM_HOTSPOT_NET="192.168.43."` before launching adds an Android-style range). Notifications still arrive over the phone's own connection. Two rules: if your study folder lives in a synced drive, keep the sync client running; and never run two watchers against the same folder at once — they share a state file.
