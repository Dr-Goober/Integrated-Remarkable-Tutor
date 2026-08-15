# Integrated reMarkable Tutor

A Python watcher connects to a reMarkable 2 over SSH and monitors its stroke files for ink in trigger colours: circle handwritten work in **red** to have it marked, **blue** to have it explained, or write a command in **grey**, and the matching Claude agent spins up, reads your page grounded in your own study materials, and pushes its reply to your phone via ntfy. You never leave the page — the phone is just a notification display. This repository holds everything needed to rebuild the workflow: the watcher and helper scripts, one-time setup instructions, and the Claude skill files that turn raw course PDFs into a study corpus, write-on workbooks, and the tutor briefs the marking is grounded in.

Turn a reMarkable tablet into a handwriting-first AI study loop:

- **Circle your handwritten work in RED** → it gets marked, strictly, against a real mark scheme, on your phone within ~30 s
- **Circle anything in BLUE** → it gets explained differently than your notes explain it; blue *handwriting* is read as your question and answered directly
- **Write in GREY** → commands: `model opus`, `effort high`, `deep explain`, `tutor` (blue becomes a free-form question to a full tutor), `screenshot q12` (fetches that question's image from the source exam PDF), `status`, `restart`, `help`
- You keep writing on paper-like e-ink the whole time. No app switching, no typing, no chat window.

Built during a real two-exam sprint (and battle-tested on it). Everything here is the generalised version of that setup.

```
 reMarkable ──SSH (read-only)──> watcher (PC) ──"claude -p"──> Claude
     ^                              │  colour-coded stroke detection,      │
     │ you write with               │  dedup, page→PDF mapping,            │
     │ coloured pens                │  render, per-workbook sessions       v
     └── you erase circles          └────────── ntfy push ─────────> your phone
```

## What's in this folder

| Path | What it is |
|---|---|
| `scripts/rm_feedback.py` | The watcher — the whole live loop in one file. Read its docstring first. |
| `scripts/capture.ps1` | Manual fallback: screenshots the reMarkable desktop app (works even when occluded) for ad-hoc "mark my screen" requests. |
| `scripts/extract_lectures.py` | PDF→markdown converter for lecture decks, with the four extraction traps already solved (see below). |
| `scripts/START-WATCHER.bat` | Double-click launcher (also makes grey `restart` work cleanly). |
| `scripts/START-WATCHER.sh` | The same launcher for Linux/macOS (`sh START-WATCHER.sh`). |
| `docs/SETUP.md` | One-time setup: tablet SSH, keys, phone notifications, Python. |
| `docs/BUILD-YOUR-MODULE.md` | The full pipeline: raw module PDFs → study corpus → plan → write-on workbooks → tutor briefs. |
| `skills/` | The workflow packaged as Claude Code skills — drop into `.claude/skills/` and the agent runs the pipeline for you. |

## Prerequisites

- **reMarkable 2** (rM1 likely works; Paper Pro untested) with SSH enabled — see `docs/SETUP.md`
- **Windows PC** on the same network (the watcher is Windows-flavoured; the SSH/parsing core is portable)
- **Python 3.10+** with `rmscene`, `pymupdf`, `pillow` (`pip install rmscene pymupdf pillow`)
- **Claude Code CLI** installed and signed in (`npm i -g @anthropic-ai/claude-code`, then `claude` once to authenticate)
- **ntfy** app on your phone (free, iOS/Android) subscribed to a topic you choose

## Quickstart

1. Do `docs/SETUP.md` once (~20 min: SSH key onto the tablet, pick an ntfy topic).
2. Set two environment variables (or edit the constants at the top of `rm_feedback.py`):
   - `RM_NTFY_TOPIC` — your ntfy topic. **Treat it as a password**: anyone who knows it can read your feedback. Use a long random string.
   - `RM_STUDY_ROOT` — the folder holding your module folders.
3. Edit the `WORKBOOKS` map in `rm_feedback.py`: tablet document name → (source PDF, tutor brief). The shipped map is the author's — replace it.
4. Put the PDFs you study from on the tablet with the **same names** as in the map.
5. Double-click `START-WATCHER.bat`. Write, circle, keep working.

## The four hard-won technical facts (so you don't rediscover them)

1. **The rM2 screen is monochrome but the *stroke files* store pen colour.** Never try to detect colour from screenshots or the framebuffer — parse `.rm` files (format v6, `rmscene`). RED=7, BLUE=6, GRAY=1.
2. **The desktop app's local cache is useless as a trigger** — sync lag was measured at 26 *minutes*. SSH straight to the tablet; strokes hit its disk within seconds, and turning the page forces a flush.
3. **The coordinate transform**: annotations are stored as page pixels at the panel's 226 DPI with x centred on the page. For an A4 PDF: `x_pdf = s·x_rm + page_width/2`, `y_pdf = s·y_rm`, where `s = page_height_pt / 2655`. Every panel-resolution-based guess is ~42% off.
4. **The 5-second poll must never touch a model.** It's one `stat` over SSH (~300 ms); triggers are detected geometrically and deduped by hashing the coloured strokes. Idle time costs zero tokens. Circles left on the page stay silent; erase and redraw to re-fire.

## Cost honesty

Marking one circled answer ≈ 20–40k tokens (page image + the relevant slice of your marking notes). A 2-hour session with ~10 questions is comparable to a moderate coding session. `deep explain` (Fable, max effort, reads your whole corpus) is deliberately expensive — treat it as break-glass. Building a full module's workbooks with verification (see `docs/BUILD-YOUR-MODULE.md`) is the big-ticket item: expect tens of dollars of API-equivalent usage per module if you want the QA gates that make the output trustworthy.

## Failure modes to expect

- **Firmware updates wipe `~/.ssh/authorized_keys` AND the WLAN-SSH marker file.** If the watcher can't connect after an update, redo those two steps from `docs/SETUP.md`. Nothing else will have broken.
- The tablet sleeps → SSH drops → the watcher re-probes (wifi, then USB at `10.11.99.1`) on the next poll. Failed model calls stay queued and retry; nothing is lost.
- Marks on borderline answers vary between runs. The *substance* of the feedback is stable; treat the number as indicative and mark real mocks against your own answer key.

## Legal note

This processes *your* course materials for *your* private study — keep it that way. Don't redistribute generated workbooks containing your university's figures or questions, and keep AI use inside your institution's rules (preparation ≠ exam-time assistance).
