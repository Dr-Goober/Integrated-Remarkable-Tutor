# -*- coding: utf-8 -*-
"""
reMarkable study-feedback watcher.

Circle something on a workbook page and get feedback pushed to your phone:

    RED  ink  -> mark what is circled against the workbook's tutor brief
    BLUE ink  -> explain what is circled; blue handwriting is read as a question

Run it when you sit down, Ctrl-C when you finish:

    py -3.13 rm_feedback.py                 # normal run
    py -3.13 rm_feedback.py --once          # single pass, useful for testing
    py -3.13 rm_feedback.py --dry-run       # detect + render, but do not call
                                            # Claude or push to the phone

Design notes
------------
* The 5-second poll NEVER invokes a model. It is one `stat` over a reused SSH
  connection; detection is pure geometry. Tokens are only spent when a coloured
  circle actually appears, which is why sitting idle costs nothing.
* Trigger identity is the hash of the coloured strokes themselves, not "question
  N answered". Leave a circle on the page and it stays silent; rub it out and
  draw a new one and it fires again. Several circles at once are several
  independent requests.
* The tablet is treated as READ-ONLY. Nothing is ever written to it.
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import traceback

import logging

import pymupdf
from PIL import Image, ImageDraw
from rmscene import read_blocks

# rmscene logs "Some data has not been read..." to stderr for every v6 page,
# because firmware 3.27 writes fields it does not know about. Everything we
# need - strokes, colours, points - parses fine, so this is pure noise.
for _n in [n for n in logging.root.manager.loggerDict if n.startswith("rmscene")]:
    logging.getLogger(_n).setLevel(logging.ERROR)
logging.getLogger("rmscene").setLevel(logging.ERROR)

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
SSH_HOST = "remarkable"          # falls back to remarkable-usb automatically
SSH_FALLBACK = "remarkable-usb"
XOCHITL = "/home/root/.local/share/remarkable/xochitl"

NTFY_TOPIC = os.environ.get("RM_NTFY_TOPIC", "CHANGE-ME-long-random-string")
NTFY_URL = "https://ntfy.sh/" + NTFY_TOPIC

HERE = os.path.dirname(os.path.abspath(__file__))
STUDY = os.environ.get("RM_STUDY_ROOT") or os.path.dirname(os.path.dirname(HERE))
STATE_PATH = os.path.join(HERE, ".rm_feedback_state.json")
WORK_DIR = os.path.join(HERE, ".rm_feedback_tmp")
LOG_PATH = os.path.join(HERE, "rm_feedback.log")

POLL_SECONDS = 5
HEARTBEAT_SECONDS = 300          # proof-of-life, instead of a line per stroke
CLAUDE_TIMEOUT = 300

# rM stores annotations as page pixels at the panel's 226 DPI, x centred on the
# page. Calibrated against a traced page outline: see the project notes.
RM_DOC_HEIGHT = 2655.0

TRIGGER_COLOURS = {"RED": "mark", "BLUE": "explain", "GRAY": "command"}

# Marking is the slow path, so it gets a switchable model. Grey commands are
# always read by the fastest model - otherwise "switch to something faster"
# would itself be slow, which would be daft.
# Per-task defaults. Marking compares against a brief that already holds the
# answer, so Sonnet is enough. Explaining has to genuinely understand the thing
# and say it differently from the workbook, which is where Opus earns its keep.
# Commands are pure transcription.
PROFILE_DEFAULT = {
    "mark":    {"model": "sonnet", "effort": "medium"},
    "explain": {"model": "opus",   "effort": "medium"},
    "command": {"model": "haiku",  "effort": "low"},
}
MODEL_DEFAULT = PROFILE_DEFAULT["mark"]["model"]
EFFORT_DEFAULT = PROFILE_DEFAULT["mark"]["effort"]
COMMAND_MODEL = PROFILE_DEFAULT["command"]["model"]
COMMAND_EFFORT = PROFILE_DEFAULT["command"]["effort"]
MODELS_OK = ("haiku", "sonnet", "opus", "fable")
EFFORTS_OK = ("low", "medium", "high", "xhigh", "max")

# DEEP explain: grey "deep explain" toggles it on; while on, every BLUE circle
# is handled by a fresh standalone Fable agent at max effort with Read/Grep/Glob
# over the full converted corpus - for when Isaac is stuck on something complex
# and wants perspective, not speed. Slow and expensive by design.
DEEP_MODEL, DEEP_EFFORT = "fable", "max"
DEEP_TIMEOUT = 1200
DEEP_TOOLS = "Read Grep Glob"


def profile(state, kind):
    p = state.setdefault("profile", {})
    d = PROFILE_DEFAULT.get(kind, PROFILE_DEFAULT["mark"])
    got = p.get(kind, {})
    return got.get("model", d["model"]), got.get("effort", d["effort"])
INK_RGB = {"BLACK": (15, 15, 15), "GRAY": (130, 130, 130), "WHITE": (255, 255, 255),
           "RED": (205, 25, 25), "BLUE": (25, 55, 205), "YELLOW": (235, 195, 40),
           "GREEN": (40, 155, 60)}

# EXAMPLE MAP - REPLACE WITH YOUR OWN MODULES.
# tablet document name -> (workbook pdf, marking-notes md), both relative to RM_STUDY_ROOT
WORKBOOKS = {
    "AIPS-workbook-1": ("AIPS/workbooks/AIPS-workbook-day01-weeks1-3.pdf",
                        "AIPS/workbooks/tutor-day01-weeks1-3.md"),
    "AIPS-workbook-2": ("AIPS/workbooks/AIPS-workbook-days2-3-weeks4-5.pdf",
                        "AIPS/workbooks/tutor-days2-3-weeks4-5.md"),
    "AIPS-workbook-3": ("AIPS/workbooks/AIPS-workbook-days4-6-cp-modelling.pdf",
                        "AIPS/workbooks/tutor-days4-6-cp-modelling.md"),
    "AIPS-workbook-4": ("AIPS/workbooks/AIPS-workbook-days7-9-cp-solving-1.pdf",
                        "AIPS/workbooks/tutor-days7-9-cp-solving-1.md"),
    "AIPS-workbook-5": ("AIPS/workbooks/AIPS-workbook-days10-12-ac4-and-mock.pdf",
                        "AIPS/workbooks/tutor-days10-12-ac4-and-mock.md"),
    "VICO-workbook-1": ("VICO/workbooks/VICO-workbook-days2-3.pdf",
                        "VICO/workbooks/tutor-days2-3.md"),
    "VICO-workbook-2": ("VICO/workbooks/VICO-workbook-days4-6.pdf",
                        "VICO/workbooks/tutor-days4-6.md"),
    "VICO-workbook-3": ("VICO/workbooks/VICO-workbook-days7-9.pdf",
                        "VICO/workbooks/tutor-days7-9.md"),
    "VICO-workbook-4": ("VICO/workbooks/VICO-workbook-days10-12.pdf",
                        "VICO/workbooks/tutor-days10-12.md"),
    "VICO-workbook-5": ("VICO/workbooks/VICO-workbook-days13-16.pdf",
                        "VICO/workbooks/tutor-days13-16.md"),
}


# Documents are named inconsistently on the tablet - some carry the ".pdf"
# extension, some don't, and older copies use the original long filenames.
ALIASES = {
    "VICO-workbook-days2-3": "VICO-workbook-1",
    "AIPS-workbook-day01-weeks1-3": "AIPS-workbook-1",
    "AIPS-workbook-days2-3-weeks4-5": "AIPS-workbook-2",
    "AIPS-workbook-days4-6-cp-modelling": "AIPS-workbook-3",
    "AIPS-workbook-days7-9-cp-solving-1": "AIPS-workbook-4",
    "AIPS-workbook-days10-12-ac4-and-mock": "AIPS-workbook-5",
}


def lookup_key(visible_name):
    n = visible_name.strip()
    if n.lower().endswith(".pdf"):
        n = n[:-4]
    n = ALIASES.get(n, n)
    return n if n in WORKBOOKS else None


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def log(msg):
    line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


_host = None


def ssh_host():
    """Pick whichever interface answers, preferring wifi so he can roam."""
    global _host
    if _host:
        return _host
    for h in (SSH_HOST, SSH_FALLBACK):
        try:
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=4", h, "echo ok"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and "ok" in r.stdout:
                _host = h
                log("ssh host: %s" % h)
                return h
        except Exception:
            pass
    raise RuntimeError("tablet unreachable on %s or %s" % (SSH_HOST, SSH_FALLBACK))


def ssh(cmd, timeout=30):
    r = subprocess.run(["ssh", ssh_host(), cmd],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("ssh failed: %s" % (r.stderr.strip()[:200],))
    return r.stdout


def load_state():
    try:
        return json.load(io.open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {"mtimes": {}, "answered": []}


def save_state(st):
    st["answered"] = st["answered"][-400:]        # keep the file small
    with io.open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)


# --------------------------------------------------------------------------- #
# tablet reads (the only SSH traffic)
# --------------------------------------------------------------------------- #
def poll_mtimes():
    """{'<doc>/<page>.rm': mtime} - one stat call, no file transfer."""
    out = ssh("stat -c '%%Y %%n' %s/*/*.rm 2>/dev/null" % XOCHITL)
    res = {}
    for line in out.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            res["/".join(parts[1].split("/")[-2:])] = int(parts[0])
    return res


def pull(rel, dest):
    r = subprocess.run(["scp", "-q", "%s:%s/%s" % (ssh_host(), XOCHITL, rel), dest],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("scp failed: %s" % r.stderr.strip()[:200])
    return dest


def doc_info(doc_uuid):
    """visibleName and the page-uuid -> index ordering, read from the tablet."""
    meta = json.loads(ssh("cat %s/%s.metadata" % (XOCHITL, doc_uuid)))
    content = json.loads(ssh("cat %s/%s.content" % (XOCHITL, doc_uuid)))
    order = []
    pages = content.get("cPages", {}).get("pages")
    if pages:
        order = [p.get("id") for p in pages
                 if not p.get("deleted", {}).get("value")]
    elif isinstance(content.get("pages"), list):
        order = content["pages"]
    return meta.get("visibleName", "?"), order


# --------------------------------------------------------------------------- #
# strokes
# --------------------------------------------------------------------------- #
def parse_strokes(path):
    out = []
    with open(path, "rb") as fh:
        for block in read_blocks(fh):
            item = getattr(block, "item", None)
            val = getattr(item, "value", None) if item is not None else None
            col = getattr(val, "color", None) if val is not None else None
            if col is None:
                continue
            pts = [(p.x, p.y) for p in (getattr(val, "points", None) or [])]
            if len(pts) >= 2:
                out.append((getattr(col, "name", str(col)), pts))
    return out


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def triggers(strokes):
    """Group coloured strokes into requests. Strokes whose boxes are close get
    grouped, so a circle plus a written question count as one request."""
    groups = []
    for cname, pts in strokes:
        if cname not in TRIGGER_COLOURS:
            continue
        b = bbox(pts)
        for g in groups:
            if g["colour"] == cname and _near(g["bbox"], b, 300):
                g["bbox"] = _union(g["bbox"], b)
                g["pts"].append(pts)
                break
        else:
            groups.append({"colour": cname, "bbox": b, "pts": [pts]})
    for g in groups:
        flat = [("%.0f,%.0f" % p) for pts in g["pts"] for p in pts[::4]]
        g["hash"] = hashlib.sha1((g["colour"] + "|" + ";".join(flat))
                                 .encode()).hexdigest()[:16]
        g["kind"] = TRIGGER_COLOURS[g["colour"]]
    return groups


def _near(a, b, pad):
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or
                a[3] + pad < b[1] or b[3] + pad < a[1])


def _union(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def render(pdf_path, page_idx, strokes, crop=None, zoom=2.4):
    doc = pymupdf.open(pdf_path)
    page = doc[page_idx]
    pw, ph = page.rect.width, page.rect.height
    s = ph / RM_DOC_HEIGHT

    def tf(x, y):
        return (s * x + pw / 2.0, s * y)

    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGB")
    d = ImageDraw.Draw(img)
    for cname, pts in strokes:
        xy = [(tf(x, y)[0] * zoom, tf(x, y)[1] * zoom) for x, y in pts]
        w = 5 if cname in TRIGGER_COLOURS else 3
        d.line(xy, fill=INK_RGB.get(cname, (15, 15, 15)), width=w, joint="curve")
    doc.close()

    if crop:
        x0, y0, x1, y1 = crop
        pad = 90
        box = (max(0, (tf(x0, y0)[0] * zoom) - pad),
               max(0, (tf(x0, y0)[1] * zoom) - pad),
               min(img.width, (tf(x1, y1)[0] * zoom) + pad),
               min(img.height, (tf(x1, y1)[1] * zoom) + pad))
        if box[2] - box[0] > 80 and box[3] - box[1] > 80:
            return img, img.crop([int(v) for v in box])
    return img, None


# --------------------------------------------------------------------------- #
# persistent memory - survives sessions, restarts and days
# --------------------------------------------------------------------------- #
MEM_DIR = os.path.join(HERE, ".rm_memory")
MEM_MAX_LINES = 90               # oldest are dropped; patterns should recur anyway


def mem_path(module):
    return os.path.join(MEM_DIR, "%s.md" % module)


def load_memory(module):
    try:
        return io.open(mem_path(module), encoding="utf-8").read().strip()
    except Exception:
        return ""


def append_memory(module, note, wb, pageno):
    """One line per observation. The prompt asks for PATTERNS rather than events
    ('drops frontier nodes when tracing', not 'got D3 2/5'), because a pattern
    stays true next week whereas a score does not."""
    note = " ".join(note.split())[:240]
    if not note:
        return
    os.makedirs(MEM_DIR, exist_ok=True)
    line = "- [%s, %s p.%s] %s" % (time.strftime("%Y-%m-%d"), wb, pageno, note)
    old = load_memory(module)
    lines = [l for l in old.splitlines() if l.strip().startswith("-")]
    if any(note.lower() == l.split("] ", 1)[-1].lower() for l in lines):
        return                                    # already recorded verbatim
    lines.append(line)
    lines = lines[-MEM_MAX_LINES:]
    header = ("# Tutor memory - %s\n\nWhat Isaac keeps getting right and wrong, "
              "accumulated across sessions. Newest last.\n\n" % module)
    with io.open(mem_path(module), "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(lines) + "\n")
    log("     memory += %s" % note[:80])


MEMORY_RE = re.compile(r"^\s*MEMORY\s*:\s*(.+)$", re.I | re.M)


def split_memory(reply):
    """-> (reply without MEMORY lines, joined memory text)"""
    notes = [m.group(1).strip() for m in MEMORY_RE.finditer(reply)]
    return MEMORY_RE.sub("", reply).strip(), " ".join(notes).strip()


# --------------------------------------------------------------------------- #
# retrieval - done here, not by the agent
# --------------------------------------------------------------------------- #
def page_text(pdf_path, idx, limit=4000):
    """The printed text of the page. Cheaper and more reliable than making the
    model OCR the question out of a full-page render."""
    doc = pymupdf.open(pdf_path)
    t = doc[idx].get_text()
    doc.close()
    return " ".join(t.split())[:limit]


SECTION_RE = re.compile(r"(?:§\s*(\d+)|\b([DSME]\d{1,2})\b)")


def location_tag(ptext):
    """A short 'where am I' tag worked out here rather than left for the model
    to infer: the section and drill IDs printed on the page itself."""
    secs, drills = [], []
    for m in SECTION_RE.finditer(ptext):
        if m.group(1) and ("§" + m.group(1)) not in secs:
            secs.append("§" + m.group(1))
        elif m.group(2) and m.group(2).upper() not in drills:
            drills.append(m.group(2).upper())
    bits = []
    if secs:
        bits.append(" ".join(secs[:3]))
    if drills:
        bits.append(" ".join(drills[:6]))
    return " · ".join(bits) or "section not detected"


def brief_excerpt(brief_path, ptext, limit=14000):
    """Pull just the brief sections this page refers to.

    The briefs are 30-58 KB and indexed by section, not page number, so asking
    the agent to find the right part costs several tool round-trips. The page's
    own printed text names its sections (§N, D4, S1 ...) - match on those here
    and inline the result, so the agent makes zero tool calls for the brief.
    """
    try:
        lines = io.open(brief_path, encoding="utf-8").read().splitlines()
    except Exception:
        return ""
    wanted = set()
    for m in SECTION_RE.finditer(ptext):
        wanted.add(("S", m.group(1)) if m.group(1) else ("T", m.group(2).upper()))
    if not wanted:
        return ""

    # index headings -> line ranges
    heads = [(i, l) for i, l in enumerate(lines) if l.startswith("#")]
    picked, out = set(), []
    for n, (i, head) in enumerate(heads):
        h = head.upper()
        hit = False
        for kind, tok in wanted:
            if kind == "S" and re.search(r"§\s*%s\b" % re.escape(tok), head):
                hit = True
            elif kind == "T" and re.search(r"\b%s\b" % re.escape(tok), h):
                hit = True
        if hit and i not in picked:
            end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            picked.add(i)
            out.append("\n".join(lines[i:end]))
    text = "\n\n".join(out)
    if len(text) > limit:
        text = text[:limit] + "\n...[truncated]"
    return text


# --------------------------------------------------------------------------- #
# dispatch + notify
# --------------------------------------------------------------------------- #
HEADER = u"[{workbook} | page {pageno} | {tag}] {colour} ink -> {action}"

PROMPT = u"""You are tutoring Isaac for the {module} module through a workbook.
Exam: {exam}. It is OPEN BOOK with internet access, no generative AI.
You will get several requests from this workbook in a row; keep the context.

--- WHAT YOU ALREADY KNOW ABOUT HIM (earlier sessions) ---
{memory}
--- END ---
Use this: chase patterns you have seen before, and say so when he repeats a
mistake you have already flagged. Do not re-teach things he has demonstrably got.

{header}

Read this image - it shows what he circled, with his handwriting:
{image}

Everything else you need is below; you should not need any other tool call.

--- PRINTED TEXT OF THAT PAGE (the questions) ---
{ptext}

--- RELEVANT TUTOR-BRIEF SECTIONS (answers + marking logic) ---
{excerpt}
--- END ---

Task: {task}

Rules:
- Use the brief's own answer and marking logic. Do not invent a different mark
  scheme - his workbook's answer section must agree with what you tell him.
- If marking, give the mark as "n/m" and say precisely where marks were lost or
  would be lost in the exam. Be strict; a generous mark is worthless to him.
- Hold him to exam discipline: justify claims, show working, answer the question
  actually asked.
- Reply with the feedback ONLY - no preamble, no "here is my feedback". Under
  200 words. Plain text, no markdown headers.
- Start with a single line of the form: VERDICT: <short summary, max 8 words>
  (for marking include the mark, e.g. "VERDICT: D4(a) 3/5 - method right, arithmetic slip")
{memrule}"""

MEMRULE = u"""- Finally, if and only if you learned something durable about how he works,
  add ONE last line: MEMORY: <observation>. Record PATTERNS, not events -
  "drops nodes from the frontier when tracing by hand" or "solid on admissibility
  arguments", never "scored 2/5 on D3". A pattern is still true next week; a score
  is not. Skip the line entirely if nothing durable came up - most requests should
  not produce one. This line is stripped before he sees the reply.
"""

TASKS = {
    "mark": "Mark what he has written inside or beside the red circle.",
    "explain": ("Explain what is circled in blue. If there is blue handwriting, "
                "treat it as his question and answer that specifically. Explain it "
                "DIFFERENTLY from how the workbook words it, and end with one short "
                "check question."),
}

DEEP_PROMPT = u"""You are Isaac's deep-dive tutor for the {module} module (exam {exam},
open book for AIPS, closed book for VICO). He has enabled DEEP explain because he is
STRUGGLING with something complex and wants as much perspective as possible. Take your
time - depth is the point.

{header}

The image shows what he circled in blue (blue handwriting = his own question): {image}

--- PRINTED TEXT OF THE PAGE ---
{ptext}

--- TUTOR-BRIEF SECTIONS FOR THIS PAGE ---
{excerpt}
--- END ---

RESEARCH FIRST - you have Read, Grep and Glob. The converted corpus:
  {module_root}\\md\\lectures\\week-*\\        the lecturer's own slides, one .md per lecture
  {module_root}\\md\\practice\\                every practice paper and answer set
  {module_root}\\workbooks\\tutor-*.md         drill answers and marking logic
Chase the circled concept through it: the lecture that introduces it, every worked
example of it anywhere, every past-paper appearance, and what the marking logic
actually rewards. Read what you find, not just the excerpt above.

Then write the explanation, structured as:
1. The core idea in one short paragraph, in DIFFERENT words from the workbook.
2. Two or three genuinely different angles - an analogy, a worked micro-example with
   fresh small numbers, or the design problem this idea was invented to solve.
3. Where this shows up in HIS exam and what the marks are actually awarded for.
4. The misconception most likely causing his confusion, named plainly.
5. One check question.

Under 600 words, plain text, no markdown headers.
First line exactly: "VERDICT: deep dive - <topic, max 6 words>".
{memrule}"""


def claude_exe():
    """On Windows npm installs claude as .cmd/.ps1, not .exe - subprocess needs
    the .cmd explicitly or it cannot launch it."""
    import shutil
    for cand in ("claude.cmd", "claude.exe", "claude"):
        p = shutil.which(cand)
        if p and not p.lower().endswith(".ps1"):
            return p
    raise RuntimeError("claude CLI not found on PATH")


def session_args(state, wb_key):
    """One live conversation per workbook.

    Switching workbook ends the previous conversation (his stated preference),
    which also stops context growing without bound. Staying in one workbook
    resumes, so follow-ups keep the tutoring context AND hit the prompt cache -
    the second question on a page is markedly faster than the first.
    Returns (cli_args, is_new_session).
    """
    import uuid as _uuid
    if state.get("active_wb") != wb_key:
        if state.get("active_wb"):
            log("  workbook changed %s -> %s; starting a fresh conversation"
                % (state.get("active_wb"), wb_key))
        state["active_wb"] = wb_key
        state["sessions"] = {}
        state["last_page"] = None
    sid = state.setdefault("sessions", {}).get(wb_key)
    if sid:
        return ["--resume", sid], False
    sid = str(_uuid.uuid4())
    state["sessions"][wb_key] = sid
    return ["--session-id", sid], True


def run_claude(prompt, model, effort=EFFORT_DEFAULT, tools="Read Grep",
               timeout=CLAUDE_TIMEOUT, extra=None):
    # The prompt goes in on STDIN, not as an argument. claude is a .cmd shim, so
    # argv passes through cmd.exe and hits the 8191-char Windows command-line
    # limit - which anything with an inlined brief excerpt comfortably exceeds.
    cmd = [claude_exe(), "-p", "--model", model, "--effort", effort]
    if tools:
        cmd += ["--allowedTools", tools]
    if extra:
        cmd += list(extra)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        # the default message dumps the entire command line, which is useless
        # in a phone notification
        raise RuntimeError("timed out after %ds (%s/%s)" % (timeout, model, effort))
    out = (r.stdout or "").strip()
    log("     (%s/%s, %.1fs)" % (model, effort, time.time() - t0))
    if r.returncode != 0 or not out:
        msg = (r.stderr or out or "no output").strip()[:300]
        if "authenticate" in msg.lower() or "oauth" in msg.lower():
            msg += "  ->  run `claude` in a terminal and sign in, then retry"
        raise RuntimeError(msg)
    return out


# Resumed sessions already hold the protocol and the brief, so a follow-up only
# needs what is new. Same page -> almost nothing; new page -> its text and
# sections. This is what makes the second question on a page quick.
FOLLOWUP_SAME_PAGE = u"""{header}

A new request on the SAME page. Image: {image}

IMPORTANT: work only from what is actually circled in this image. It may well be
the SAME work you looked at a moment ago - re-circling something to get a second
opinion is normal and expected. Do NOT assume this must be a different question,
and do NOT move on to the next drill because you already covered the last one.
If you cannot tell what is circled, say so and describe what you can see; never
guess at a drill that is not visibly circled.

Task: {task}
Same rules as before. Under 200 words, first line "VERDICT: ...".
{memrule}"""

FOLLOWUP_NEW_PAGE = u"""{header}

He has moved to a new page in the same workbook. Image: {image}

--- PRINTED TEXT OF PAGE {pageno} ---
{ptext}

--- TUTOR-BRIEF SECTIONS FOR THIS PAGE ---
{excerpt}
--- END ---

Task: {task}
Same rules as before. Under 200 words, first line "VERDICT: ...".
{memrule}"""


def ask_claude(ctx, model, effort, state, wb_key, dry=False):
    args, is_new = session_args(state, wb_key)
    same_page = (not is_new) and state.get("last_page") == (wb_key, ctx["pageno"])

    if is_new:
        tpl, why = PROMPT, "new session"
    elif same_page:
        tpl, why = FOLLOWUP_SAME_PAGE, "resume, same page"
    else:
        tpl, why = FOLLOWUP_NEW_PAGE, "resume, new page"

    prompt = tpl.format(task=TASKS[ctx["kind"]], **ctx)
    log("     %s | prompt %d chars (excerpt %d)" % (why, len(prompt), len(ctx["excerpt"])))
    if dry:
        log("DRY-RUN prompt:\n" + prompt[:2000])
        return "VERDICT: dry run\n(no model called)"
    try:
        out = run_claude(prompt, model, effort, tools="Read", extra=args)
    except RuntimeError as e:
        # a resume can fail if the transcript was cleaned up - start over once
        if not is_new and "resume" in (" ".join(args) + str(e)).lower():
            log("     resume failed, starting a fresh conversation")
            state["sessions"].pop(wb_key, None)
            args, _ = session_args(state, wb_key)
            prompt = PROMPT.format(task=TASKS[ctx["kind"]], **ctx)
            out = run_claude(prompt, model, effort, tools="Read", extra=args)
        else:
            raise
    state["last_page"] = (wb_key, ctx["pageno"])
    return out


CMD_PROMPT = u"""Transcribe the handwriting in this image: {png}

Output ONLY the transcription - the words as written, nothing else. Do not
answer it, explain it, comment on it, or add any preamble. If it is a question,
transcribe the question; do not respond to it. If nothing is legible, output the
single word ILLEGIBLE.
"""


def classify(text):
    """Classify the transcribed command in code rather than asking the model to
    do it. Transcription is something models are reliable at; format compliance
    is not, and a mis-followed format silently breaks the control channel."""
    t = " ".join(text.lower().split())
    # an explicit "mark ..." / "explain ..." scopes the change to one task
    scope = None
    if re.search(r"\bexplain\w*\b", t):
        scope = "explain"
    elif re.search(r"\bmark\w*\b", t):
        scope = "mark"

    named = next((m for m in MODELS_OK if re.search(r"\b%s\b" % m, t)), None)
    if named:
        return "set-model", (named, scope)
    # "think harder" / "effort high" / "max thinking"
    lvl = next((e for e in EFFORTS_OK if re.search(r"\b%s\b" % e, t)), None)
    if lvl and re.search(r"\beffort\b|\bthink\w*\b|\breason\w*\b", t):
        return "set-effort", (lvl, scope)
    if re.search(r"\bthink harder\b|\bmore thinking\b|\bthink more\b", t):
        return "set-effort", ("high", scope)
    if re.search(r"\bthink less\b|\bless thinking\b|\bfaster\b|\bquicker\b", t):
        return "set-effort", ("low", scope)
    if re.search(r"\brestart\b|\breboot\b|\breload\b", t):
        return "restart", None
    if re.search(r"\bdeep\b", t):
        off = bool(re.search(r"\boff\b|\bdisable\b|\bstop\b|\bnormal\b", t))
        return "deep", ("off" if off else "on")
    if re.search(r"\beffort\b|\bthinking\b", t):
        return "get-model", None          # asking about effort -> report both
    if re.search(r"\bmodel\b|\bmodle\b", t):
        return "get-model", None
    if re.search(r"\bstatus\b|\bhow many\b", t):
        return "status", None
    return "unknown", text.strip()


def handle_command(crop_png, state, dry=False):
    """Grey ink is a control channel, not a question. Always read with the
    fastest model so switching away from a slow one is itself quick."""
    if dry:
        log("DRY-RUN command read of %s" % crop_png)
        return "(dry run)", "cmd"
    raw = run_claude(CMD_PROMPT.format(png=crop_png), COMMAND_MODEL,
                     COMMAND_EFFORT, tools="Read", timeout=120)
    # the transcription is normally the whole reply; if the model added chatter,
    # the shortest non-empty line is almost always the transcription itself
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    said = min(lines, key=len) if lines else ""
    kind, arg = classify(said)
    log("     transcribed %r -> %s %s" % (said[:80], kind, arg or ""))

    def describe():
        mm, me = profile(state, "mark")
        em, ee = profile(state, "explain")
        return "mark %s/%s · explain %s/%s · commands %s/%s" % (
            mm, me, em, ee, COMMAND_MODEL, COMMAND_EFFORT)

    def apply(field, value, scope):
        targets = [scope] if scope else ["mark", "explain"]
        for t in targets:
            state.setdefault("profile", {}).setdefault(t, {})[field] = value
        return ", ".join(targets)

    if kind in ("set-model", "set-effort"):
        value, scope = arg
        field = "model" if kind == "set-model" else "effort"
        where = apply(field, value, scope)
        return ("%s %s -> %s\n\nNow: %s\n\nScope it by saying 'explain opus' or "
                "'mark haiku'; unscoped changes both."
                % (where, field, value, describe())), "%s %s=%s" % (where, field, value)
    if kind == "get-model":
        return (describe() + "\n\nIn grey: 'model opus', 'explain opus', "
                "'mark sonnet', 'effort high', 'think harder', 'faster'."), describe()
    if kind == "status":
        return ("%s\nDEEP explain: %s. %d requests answered. Watching %d pages. "
                "Active workbook: %s."
                % (describe(), "ON" if state.get("deep") else "off",
                   len(state.get("answered", [])),
                   len(state.get("mtimes", {})),
                   state.get("active_wb") or "none")), "status"
    if kind == "restart":
        # actual respawn happens in the main loop AFTER this trigger is recorded
        # as answered and state is saved - otherwise the new process re-fires on
        # the same grey ink and restarts itself forever
        state["_restart"] = True
        return ("Restarting the watcher now. Back in ~15 s with the current code. "
                "Conversation and model defaults reset; memory file kept."), "restarting"
    if kind == "deep":
        if arg == "on":
            state["deep"] = True
            return ("DEEP explain enabled. Blue circles now go to a standalone %s "
                    "agent at %s effort that reads the whole converted corpus - "
                    "expect several minutes per answer. Red marking is unaffected. "
                    "Write 'deep off' in grey to return to quick explains."
                    % (DEEP_MODEL, DEEP_EFFORT)), "DEEP explain enabled"
        state["deep"] = False
        return ("DEEP explain off - blue circles back to %s/%s quick explains."
                % profile(state, "explain")), "DEEP explain off"
    return ("Read that as: \"%s\"\nNot a command I know. Try: 'model haiku' / "
            "'model sonnet' / 'model opus', 'which model?', or 'status'."
            % (said[:120] or "nothing legible")), "unknown command"


def notify(title, body, image=None, priority="default", tags=None, dry=False):
    if dry:
        log("DRY-RUN notify: [%s] %s" % (title, body[:120]))
        return
    import urllib.request
    hdrs = {"Title": title.encode("utf-8"), "Priority": priority}
    if tags:
        hdrs["Tags"] = tags
    req = urllib.request.Request(NTFY_URL, data=body.encode("utf-8"),
                                headers=hdrs, method="POST")
    urllib.request.urlopen(req, timeout=25).read()
    if image and os.path.exists(image):
        with open(image, "rb") as fh:
            data = fh.read()
        req = urllib.request.Request(
            NTFY_URL, data=data,
            headers={"Filename": os.path.basename(image),
                     "Title": ("img - " + title).encode("utf-8")},
            method="PUT")
        urllib.request.urlopen(req, timeout=60).read()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def handle(rel, state, dry=False):
    """-> (answered, failed). A non-zero `failed` tells the caller to leave this
    page looking un-seen so the pending circle is retried on the next poll
    instead of being silently lost."""
    doc_uuid, page_file = rel.split("/")
    page_uuid = os.path.splitext(page_file)[0]

    local = os.path.join(WORK_DIR, page_uuid + ".rm")
    pull(rel, local)
    strokes = parse_strokes(local)
    trg = [g for g in triggers(strokes) if g["hash"] not in state["answered"]]
    if not trg:
        return 0, 0

    name, order = doc_info(doc_uuid)
    key = lookup_key(name)
    if key is None:
        log("  page changed in '%s' - not a tracked workbook, ignoring" % name)
        return 0, 0
    pdf_rel, brief_rel = WORKBOOKS[key]
    pdf_path = os.path.join(STUDY, pdf_rel.replace("/", os.sep))
    brief_path = os.path.join(STUDY, brief_rel.replace("/", os.sep))
    if not os.path.exists(pdf_path):
        log("  !! workbook pdf missing: %s" % pdf_path)
        return 0, 0          # config problem - retrying will not help
    idx = order.index(page_uuid) if page_uuid in order else 0
    module = "AIPS" if name.startswith("AIPS") else "VICO"
    exam = "Wed 19 Aug 2026" if module == "AIPS" else "Thu 20 Aug 2026"

    done = failed = 0
    for g in trg:
        log("  %s trigger on %s p.%d (hash %s)" % (g["colour"], name, idx + 1, g["hash"]))
        notify("%s - %s p.%d - working..." % (g["kind"].upper(), name, idx + 1),
               "Seen your %s circle. Working on it." % g["colour"].lower(),
               priority="low", tags="eyes", dry=dry)

        full, crop = render(pdf_path, idx, strokes, crop=g["bbox"])
        full_png = os.path.join(WORK_DIR, "%s_p%d_full.png" % (name, idx + 1))
        full.save(full_png)
        crop_png = None
        if crop is not None:
            crop_png = os.path.join(WORK_DIR, "%s_p%d_crop.png" % (name, idx + 1))
            crop.save(crop_png)

        # grey = control channel; no marking, no brief, always the fast model
        if g["kind"] == "command":
            try:
                body, short = handle_command(crop_png or full_png, state, dry=dry)
            except Exception as e:
                log("  !! command failed: %s" % e)
                notify("CMD ERROR", str(e)[:400], priority="high",
                       tags="warning", dry=dry)
                failed += 1
                continue
            notify("CMD - %s" % short, body, tags="gear", dry=dry)
            state["answered"].append(g["hash"])
            done += 1
            continue

        ptext = page_text(pdf_path, idx)
        ctx = {"module": module, "exam": exam, "workbook": key, "pageno": idx + 1,
               "colour": g["colour"].lower(), "action": g["kind"],
               "kind": g["kind"], "image": (crop_png or full_png),
               "ptext": ptext, "excerpt": brief_excerpt(brief_path, ptext),
               "tag": location_tag(ptext), "memrule": MEMRULE,
               "memory": load_memory(module) or "(nothing recorded yet)"}
        ctx["header"] = HEADER.format(**ctx)
        log("  -> %s" % ctx["header"])
        # DEEP explain: standalone agent, fresh every time, full-corpus tools.
        # Deliberately NOT part of the workbook conversation - it is a specialist
        # brought in for one question, and its long transcript would bloat the
        # tutoring session's context for every later request.
        deep = (g["kind"] == "explain") and state.get("deep")
        if deep:
            notify("DEEP - %s p.%d" % (key, idx + 1),
                   "Fable at max effort is reading the corpus for this one - "
                   "expect several minutes.", priority="low", tags="brain", dry=dry)
        mdl, eff = (DEEP_MODEL, DEEP_EFFORT) if deep else profile(state, g["kind"])
        try:
            if deep:
                dctx = dict(ctx, module_root=os.path.join(
                    STUDY, "AIPS" if module == "AIPS" else "VICO"))
                prompt = DEEP_PROMPT.format(**dctx)
                log("     DEEP dispatch (%s/%s, timeout %ds)"
                    % (DEEP_MODEL, DEEP_EFFORT, DEEP_TIMEOUT))
                reply = ("VERDICT: dry run\n(no model called)" if dry else
                         run_claude(prompt, DEEP_MODEL, DEEP_EFFORT,
                                    tools=DEEP_TOOLS, timeout=DEEP_TIMEOUT))
            else:
                reply = ask_claude(ctx, mdl, eff, state, key, dry=dry)
        except Exception as e:
            log("  !! claude failed: %s" % e)
            log("     -> leaving this page pending; it will retry each poll")
            notify("ERROR - %s p.%d" % (name, idx + 1), str(e)[:400],
                   priority="high", tags="warning", dry=dry)
            failed += 1
            continue

        reply, note = split_memory(reply)
        if note:
            append_memory(module, note, key, idx + 1)
        # The VERDICT line is not always first - Fable in particular narrates
        # before it. Find it anywhere; the title must be the verdict, never prose.
        mv = re.search(r"^\s*VERDICT\s*:\s*(.+)$", reply, re.M)
        if mv:
            verdict = mv.group(1).strip()[:120]
            rest = (reply[:mv.start()].strip() + "\n\n" + reply[mv.end():].strip()).strip()
        else:
            first, _, rest = reply.partition("\n")
            verdict = first.strip()[:120] or g["kind"]
        kind_label = "DEEP" if deep else g["kind"].upper()
        title = "%s - %s p.%d - %s" % (kind_label, name, idx + 1, verdict)
        notify(title[:200], (rest.strip() or reply),
               image=(crop_png if g["kind"] == "mark" else None),
               tags=("white_check_mark" if g["kind"] == "mark" else "bulb"), dry=dry)
        state["answered"].append(g["hash"])
        done += 1
    return done, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="forget answered triggers and current mtimes")
    ap.add_argument("--test-page", metavar="DOC/PAGE.rm",
                    help="process one page now regardless of mtime or answered state")
    ap.add_argument("--model", choices=MODELS_OK,
                    help="model for marking/explaining (persisted; grey ink can change it)")
    ap.add_argument("--effort", choices=EFFORTS_OK,
                    help="reasoning effort for marking/explaining (persisted)")
    args = ap.parse_args()

    os.makedirs(WORK_DIR, exist_ok=True)
    state = {"mtimes": {}, "answered": []} if args.reset else load_state()
    # Launch defaults are exactly that: every launch starts from PROFILE_DEFAULT.
    # A --model/--effort flag overrides for this run, and grey commands override
    # for the rest of the session - but neither leaks into the next launch.
    # (Long-term continuity is the memory file's job, not the profile's.)
    state["profile"] = {}
    for t in ("mark", "explain"):
        if args.model:
            state["profile"].setdefault(t, {})["model"] = args.model
        if args.effort:
            state["profile"].setdefault(t, {})["effort"] = args.effort

    # Likewise, don't resume a conversation from a previous launch: it may be
    # hours or days stale. The memory file carries what actually matters across
    # sessions; the transcript would only carry bulk.
    if state.pop("sessions", None):
        log("cleared conversation(s) from the previous launch")
    state["active_wb"] = None
    state["last_page"] = None

    if args.test_page:
        state["answered"] = []          # so an already-answered circle still fires
        done, failed = handle(args.test_page, state, dry=args.dry_run)
        log("test-page: %d answered, %d failed" % (done, failed))
        return

    log("watcher starting (poll %ds, topic %s)" % (POLL_SECONDS, NTFY_TOPIC))
    log("  mark %s/%s | explain %s/%s | commands %s/%s"
        % (profile(state, "mark") + profile(state, "explain")
           + (COMMAND_MODEL, COMMAND_EFFORT)))
    first_pass = not state["mtimes"]
    if first_pass:
        log("no previous state - baselining, will not fire on existing ink")
    last_beat, seen_changes = time.time(), 0

    while True:
        try:
            now = poll_mtimes()
            # Oldest write first, so circling page 11 then page 12 answers in
            # that order. Filesystem glob order is by UUID, which would deliver
            # them in an arbitrary sequence.
            changed = sorted((r for r, mt in now.items()
                              if state["mtimes"].get(r) != mt),
                             key=lambda r: now[r])
            pending = set()
            if first_pass:
                log("baselined %d pages" % len(now))
            elif changed:
                # Every stroke you write changes an mtime, so logging each one
                # buries the lines that matter. handle() logs when a coloured
                # trigger actually fires; a heartbeat below shows it is alive.
                seen_changes += len(changed)
                for rel in changed:
                    try:
                        _done, failed = handle(rel, state, dry=args.dry_run)
                        if failed:
                            pending.add(rel)
                    except Exception as e:
                        log("  !! %s: %s" % (rel, e))
                        pending.add(rel)
                        if os.environ.get("RM_DEBUG"):
                            traceback.print_exc()
            state["mtimes"] = now
            # anything that failed stays "unseen" so the next poll retries it
            for rel in pending:
                state["mtimes"].pop(rel, None)
            if pending:
                log("%d page(s) pending retry" % len(pending))
            first_pass = False
            restart = state.pop("_restart", False)   # must never persist to disk
            save_state(state)
            if restart:
                if os.environ.get("RM_LAUNCHER"):
                    # started via START-STUDY-WATCHER.bat: exit 42 and let the
                    # batch loop relaunch us in the SAME console - respawning a
                    # child here would die when that console window is closed
                    log("grey RESTART command - handing back to the launcher")
                    os._exit(42)
                log("grey RESTART command - respawning with current code")
                subprocess.Popen([sys.executable] + sys.argv)
                time.sleep(1)
                os._exit(0)
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                log("...watching (%d writes seen, %d answered) - %s"
                    % (seen_changes, len(state.get("answered", [])),
                       state.get("active_wb") or "no workbook active"))
                seen_changes = 0
                last_beat = time.time()
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as e:
            log("poll error: %s" % e)
            global _host
            _host = None          # force interface re-probe (tablet may have slept)
        if args.once:
            return
        try:
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("stopped")
            return


if __name__ == "__main__":
    main()



