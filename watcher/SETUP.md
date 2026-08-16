# One-time setup

Twenty minutes, four parts. Everything here survives reboots; only firmware updates undo parts 2–3.

## 1 · Phone notifications (ntfy) — 2 min

1. Install **ntfy** (free, iOS/Android).
2. Invent a topic name. It is the ONLY secret protecting your feedback — make it long and random, e.g. `rt-9f2a71c4b8e3`. Tap **+** in the app and subscribe to it.
3. On the PC: `setx RM_NTFY_TOPIC "rt-9f2a71c4b8e3"` (or edit the constant in `rm_feedback.py`).
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

## Daily use

Double-click `START-WATCHER.bat` (Windows) or run `sh START-WATCHER.sh` (Linux/macOS). Circle in red to get marked, blue to get explained, grey for commands. Erase circles once answered. Turn the page after circling — it forces the tablet to flush the stroke file, which is what fires the trigger. Ctrl-C or close the window when done.

**Out and about:** put the tablet and the computer on the same phone hotspot and start the watcher as normal. When the configured ssh aliases don't answer, it scans the hotspot subnet by itself (iPhone's range and the USB address are built in; `export RM_HOTSPOT_NET="192.168.43."` before launching adds an Android-style range). Notifications still arrive over the phone's own connection. Two rules: if your study folder lives in a synced drive, keep the sync client running; and never run two watchers against the same folder at once — they share a state file.
