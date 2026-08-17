# Integrated reMarkable Tutor

A Python watcher connects to a reMarkable 2 over SSH and monitors its stroke files for ink in trigger colours: circle handwritten work in **red** to have it marked, **blue** to have it explained, or write a command in **grey**, and the matching Claude agent spins up, reads your page grounded in your own study materials, and posts its reply to a local dashboard you can open on the computer or on your phone, with optional ntfy push for when you are away from it. You never leave the page — the screen is just a display. This repository holds everything needed to rebuild the workflow: the watcher and helper scripts, one-time setup instructions, and the Claude skill files that turn raw course PDFs into a study corpus, write-on workbooks, and the tutor briefs the marking is grounded in.

Turn a reMarkable tablet into a handwriting-first AI study loop:

- **Circle your handwritten work in RED** → it gets marked, strictly, against a real mark scheme, within ~30 s
- **Circle anything in BLUE** → it gets explained differently than your notes explain it; blue *handwriting* is read as your question and answered directly
- **Write in GREY** → commands: `model opus`, `effort high`, `deep explain`, `tutor` (blue becomes a free-form question to a full tutor), `screenshot q12` (fetches that question's image from the source exam PDF), `wait` … `begin` (hold your ink while writing a long prompt, then fire it all), `start timer 25` (a study countdown on the dashboard), `status`, `restart`, `help`
- **Draw a GREY box** around anything, no words needed → that crop is sent back to the dashboard, rendered from the source PDF
- **The three channels run in parallel.** Circle in blue, red and grey one after another and all three agents work at once, each on its own conversation — you do not queue behind the slowest one
- **A local dashboard** mirrors the loop: live replies with rendered LaTeX, per-workbook and per-module progress bars, a session token/cost meter, a stop button that kills the in-flight agent, and study timers with a built-in break flow. It runs at `http://localhost:8477` on the computer, and installs to your phone's home screen as a full-screen app over Wi-Fi — see [`watcher/SETUP.md`](watcher/SETUP.md) part 5
- You keep writing on paper-like e-ink the whole time. No app switching, no typing, no chat window.

Built during a real two-exam sprint (and battle-tested on it). Everything here is the generalised version of that setup.

```
 reMarkable ──SSH (read-only)──> watcher (PC) ──"claude -p"──> Claude
     ^                              │  colour-coded stroke detection,      │
     │ you write with               │  dedup, page→PDF mapping, render,    │
     │ coloured pens                │  one session per workbook × channel  v
     │                              ├──── dashboard :8477 ────> laptop + phone
     └── you erase circles          └──── ntfy push (optional) ────> phone
```

## Repo layout — two segments

**[`watcher/`](watcher/)** — the live ink loop. Start here.

| File | What it is |
|---|---|
| `SETUP.md` | One-time setup: tablet SSH, keys, phone notifications, Python. |
| `rm_feedback.py` | The watcher — the whole live loop in one file. Read its docstring first. |
| `rm_dashboard.html` | The local dashboard the watcher serves at `http://localhost:8477`: live feed, progress tracking, session stats, stop control, timers. |
| `START-WATCHER.bat` / `.sh` | Double-click (Windows) / `sh` (Linux, macOS) launchers; both make grey `restart` work cleanly. |
| `build_question_map.py` | Builds the question-location table the grey `screenshot qN` command fetches from. |
| `capture.ps1` | Manual fallback: screenshots the reMarkable desktop app (works even when occluded) for ad-hoc "mark my screen" requests. |
| `skills/remarkable-tutor` | The marking/tutoring protocol as a Claude Code skill. |

**[`workbook-pipeline/`](workbook-pipeline/)** — the content factory that makes the marking worth anything: raw module PDFs → study corpus → write-on workbooks → marking notes.

| File | What it is |
|---|---|
| `BUILD-YOUR-MODULE.md` | The full pipeline guide, stage by stage with QA gates. |
| `extract_lectures.py` | PDF→markdown converter for lecture decks, with the four extraction traps already solved (see below). |
| `skills/module-ingest`, `skills/exam-workbook-builder` | The pipeline stages as Claude Code skills. |

Skills are standard Claude Code skills — drop any of them into `.claude/skills/` and the agent runs that stage for you.

## Prerequisites

- **reMarkable 2** (rM1 likely works; Paper Pro untested) with SSH enabled — see `watcher/SETUP.md`
- **A computer on the same network** — Windows, Linux, or macOS (launchers for each are included; away from home, tablet and computer can share a phone hotspot and the watcher finds the tablet by itself)
- **Python 3.10+** — `pip install -r requirements.txt` (rmscene, pymupdf, pillow)
- **Claude Code CLI** installed and signed in (`npm i -g @anthropic-ai/claude-code`, then `claude` once to authenticate)
- **ntfy** app on your phone (free, iOS/Android) subscribed to a topic you choose

## Quickstart

1. Do `watcher/SETUP.md` once (~20 min: SSH key onto the tablet, pick an ntfy topic).
2. Set two environment variables (or edit the constants at the top of `rm_feedback.py`):
   - `RM_NTFY_TOPIC` — your ntfy topic. **Treat it as a password**: anyone who knows it can read your feedback. Use a long random string.
   - `RM_STUDY_ROOT` — the folder holding your module folders.
3. Edit the `WORKBOOKS` map (and `EXAM_DATES`) in `rm_feedback.py`: tablet document name → (source PDF, tutor brief). The shipped map is an example — replace it.
4. Put the PDFs you study from on the tablet with the **same names** as in the map.
5. Double-click `watcher/START-WATCHER.bat` (Windows) or run `sh watcher/START-WATCHER.sh` (Linux/macOS). Write, circle, keep working.
6. Open `http://localhost:8477` on the watcher machine for the live dashboard.

## The four hard-won technical facts (so you don't rediscover them)

1. **The rM2 screen is monochrome but the *stroke files* store pen colour.** Never try to detect colour from screenshots or the framebuffer — parse `.rm` files (format v6, `rmscene`). RED=7, BLUE=6, GRAY=1.
2. **The desktop app's local cache is useless as a trigger** — sync lag was measured at 26 *minutes*. SSH straight to the tablet; strokes hit its disk within seconds, and turning the page forces a flush.
3. **The coordinate transform**: annotations are stored as page pixels at the panel's 226 DPI with x centred on the page. For an A4 PDF: `x_pdf = s·x_rm + page_width/2`, `y_pdf = s·y_rm`, where `s = page_height_pt / 2655`. Every panel-resolution-based guess is ~42% off.
4. **The 5-second poll must never touch a model.** It's one `stat` over SSH (~300 ms); triggers are detected geometrically and deduped by hashing the coloured strokes. Idle time costs zero tokens. Circles left on the page stay silent; erase and redraw to re-fire.

## Cost honesty

Marking one circled answer ≈ 20–40k tokens (page image + the relevant slice of your marking notes). A 2-hour session with ~10 questions is comparable to a moderate coding session. `deep explain` (Fable, max effort, reads your whole corpus) is deliberately expensive — treat it as break-glass. Building a full module's workbooks with verification (see `workbook-pipeline/BUILD-YOUR-MODULE.md`) is the big-ticket item: expect tens of dollars of API-equivalent usage per module if you want the QA gates that make the output trustworthy.

## Failure modes to expect

- **Firmware updates wipe `~/.ssh/authorized_keys` AND the WLAN-SSH marker file.** If the watcher can't connect after an update, redo those two steps from `watcher/SETUP.md`. Nothing else will have broken.
- The tablet sleeps → SSH drops → the watcher re-probes on the next poll (wifi, USB at `10.11.99.1`, then a phone-hotspot subnet scan). Failed model calls stay queued and retry; nothing is lost.
- Marks on borderline answers vary between runs. The *substance* of the feedback is stable; treat the number as indicative and mark real mocks against your own answer key.

## Legal note

This processes *your* course materials for *your* private study — keep it that way. Don't redistribute generated workbooks containing your university's figures or questions, and keep AI use inside your institution's rules (preparation ≠ exam-time assistance).

---

🐇
