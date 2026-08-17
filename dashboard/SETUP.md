# The dashboard

A read-only mirror of the live loop, served by the watcher itself. It is a
*display*, not a control surface — ink on the tablet remains the only way to
drive the watcher, and nothing here can start, stop or change a job except the
one stop button.

There is nothing to install. The watcher serves the page from
`dashboard/rm_dashboard.html`, so if the watcher runs, the dashboard is already
up. Complete [`../watcher/SETUP.md`](../watcher/SETUP.md) first.

## What it shows

| | |
|---|---|
| **Response feed** | Every mark, explanation and command reply, newest first, with LaTeX rendered properly. Cards expand, pin, export as PNG and delete. |
| **Channel chips** | MARK / EXPLAIN / COMMAND light the moment ink is detected and blink while their agent works. All three can run at once. |
| **Progress** | A bar per workbook and per module, ticked automatically when a drill earns full marks. Click it for the study tracker. |
| **Connection light** | Whether the tablet is reachable, and which address it was found on. |
| **Session meter** | Requests answered, average time per channel, tokens and cost. |
| **Timer** | A study countdown with presets and a break flow, kept out of the way in the corner. |

## On the computer running the watcher

Nothing to configure. Start the watcher and open:

```
http://localhost:8477
```

The page holds no state of its own — it polls `/state` and renders whatever the
watcher currently has. The feed starts empty on each launch, because responses
live in memory rather than on disk.

## On your phone (same Wi-Fi)

By default the server binds to loopback only, so nothing else on the network can
reach it. To open it to your LAN:

**Linux/macOS** — uncomment the `RM_WEB_HOST` line in
[`../watcher/START-WATCHER.sh`](../watcher/START-WATCHER.sh), or export it
yourself before launching:

```sh
export RM_WEB_HOST=0.0.0.0        # 127.0.0.1 puts it back to this machine only
export RM_WEB_PORT=8477           # optional
```

**Windows** — `setx RM_WEB_HOST 0.0.0.0`, then open a new terminal so the
variable is picked up.

Then find the computer's LAN address:

```sh
ip -4 -brief addr           # Linux   -> e.g. 192.168.1.42/24
ipconfig                    # Windows -> "IPv4 Address"
ipconfig getifaddr en0      # macOS
```

Open `http://<that-address>:8477` on the phone. If it times out, the firewall is
the usual cause — open the port to your subnet only, never to the whole world:

```sh
sudo ufw allow from 192.168.1.0/24 to any port 8477 proto tcp    # Linux (ufw)
```
```powershell
New-NetFirewallRule -DisplayName "reMarkable dashboard" -Direction Inbound `
  -LocalPort 8477 -Protocol TCP -Action Allow -Profile Private
```

## Make it a home-screen app

The page ships a web app manifest and a generated icon, so it installs without a
store:

- **iOS/Safari** — Share → *Add to Home Screen*. It launches full screen with no
  browser chrome.
- **Android/Chrome** — ⋮ → *Install app* / *Add to Home screen*.

There is a dedicated phone layout below 700px: the feed, timer and controls are
all reachable one-handed, safe-area insets keep content clear of the notch and
home indicator, workbook names are abbreviated (`MODULE-A WB2`) to fit, and
minimised cards carry their channel as a border tint rather than a badge.

## Two limits worth knowing up front

- **It is LAN-only.** Leave the house and the page stops loading. A mesh VPN such
  as Tailscale on both devices fixes this, and gives you an HTTPS hostname as a
  side effect.
- **No push, and no authentication.** Web push on iOS requires HTTPS, which
  plain-HTTP-over-LAN cannot provide — that is what ntfy (part 1 of the watcher
  setup) is for. There is also no login: anyone on your network who finds the
  port can read your feedback and delete cards. That is a reasonable trade on a
  home network and a bad one on a shared or public one, where you should leave
  `RM_WEB_HOST` at its default.

## Moving or replacing the page

The watcher looks for the page in this order:

1. `RM_DASHBOARD_FILE` — an explicit path, if set
2. `../dashboard/rm_dashboard.html` — this segment, the normal case
3. `rm_dashboard.html` beside `rm_feedback.py` — a flat, everything-in-one-folder
   install

So you can point the watcher at your own build without moving anything, and a
single-folder deployment still works.

## Internals, if you are editing it

One self-contained HTML file: no build step, no framework, no bundler. KaTeX and
html2canvas load from a CDN — everything else, including the icons, is generated
locally. It polls `/state` and re-renders the feed only when a content key
changes, so idle polling is close to free.

Endpoints the watcher exposes: `/` (this page), `/state` (JSON snapshot),
`/img/<name>.png` (response attachments, cached immutably), `/del?id=` (remove a
card), `/stop` (kill the in-flight agent), `/manifest.webmanifest` and
`/icon-*.png` (home-screen assets).

Module accent colours are assigned by a module's position in the sorted module
list, not by its name — add more `--mod-N` variables in the CSS and raise
`MOD_COLOURS` in the script if you run more than four modules.
