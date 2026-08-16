#!/bin/sh
# Linux/macOS launcher - the counterpart of START-WATCHER.bat. Grey 'restart'
# makes the watcher exit with code 42 and this loop relaunches it, so code
# updates deploy without walking to the computer.
#
# First run:  chmod +x START-WATCHER.sh   (or just:  sh START-WATCHER.sh)
cd "$(dirname "$0")" || exit 1
export RM_LAUNCHER=1
while :; do
    python3 rm_feedback.py "$@"
    code=$?
    [ "$code" -eq 42 ] || exit "$code"
    echo "grey restart - relaunching with the current code..."
done
