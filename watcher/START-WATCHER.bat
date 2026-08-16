@echo off
rem ============================================================
rem  reMarkable study watcher - double-click to start a session
rem  Red circle = mark - Blue = explain - Grey = commands
rem  Requires: RM_NTFY_TOPIC and RM_STUDY_ROOT env vars set
rem  (see SETUP.md beside this file), or the constants edited in the script.
rem ============================================================
title reMarkable study watcher
set RM_LAUNCHER=1
cd /d "%~dp0"

:run
py -3.13 rm_feedback.py %*
if %errorlevel%==42 (
    echo.
    echo --- grey RESTART received: relaunching with current code ---
    echo.
    goto run
)

echo.
echo watcher exited - press any key to close this window
pause >nul
