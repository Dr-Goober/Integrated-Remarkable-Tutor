#!/bin/sh
# Linux/macOS launcher - the counterpart of START-WATCHER.bat. Grey 'restart'
# makes the watcher exit with code 42 and this loop relaunches it, so code
# updates deploy without walking to the computer.
#
# First run:  chmod +x START-WATCHER.sh   (or just:  sh START-WATCHER.sh)
cd "$(dirname "$0")" || exit 1
export RM_LAUNCHER=1

# Serve the dashboard to the whole LAN so a phone can open it. Off by default:
# the page has no authentication, so only do this on a network you trust.
# See SETUP.md part 5 - you will also need to open the port in your firewall.
# export RM_WEB_HOST=0.0.0.0

# Tablet discovery falls back to scanning phone-hotspot ranges. If you are on a
# home network instead, name its /24 here so the tablet is found without an
# ssh-config alias, e.g. 192.168.1. for 192.168.1.x
# export RM_HOTSPOT_NET="192.168.1."
while :; do
    python3 rm_feedback.py "$@"
    code=$?
    [ "$code" -eq 42 ] || exit "$code"
    echo "grey restart - relaunching with the current code..."
done
