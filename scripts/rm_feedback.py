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
import base64
import glob
import hashlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid

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
OFFLINE_ALERT_AFTER = 4          # consecutive failed polls before phoning it in
SETTLE_POLLS = 2                 # a changed page must sit unchanged this many
                                 # polls (~10 s of pen-up) before its ink is
                                 # acted on - xochitl commits mid-thought at any
                                 # decent pause, and answering a half-written
                                 # question is worse than answering 10 s later
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
# over the full converted corpus - for when the student is stuck on something complex
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


class SshChannel:
    """ONE persistent SSH connection for the whole session.

    The failsafe this exists for: dropbear on the tablet is socket-activated
    with a small concurrent-connection cap (~64). Opening a fresh connection
    every 5-second poll - as the first version did, 720/hour - slowly fills
    that cap with wedged half-dead sessions (every sleep/wake leaves one), and
    once full the tablet accepts TCP but never answers: 'banner exchange
    timeout' while the tablet is demonstrably awake. One long-lived channel
    makes the whole class of failure impossible, and drops per-poll latency
    from ~300 ms to ~30 ms as a bonus.

    Commands run through a remote `sh` via stdin; output is framed by a random
    end marker carrying the exit status. ServerAliveInterval in ~/.ssh/config
    kills the channel within ~45 s of a dead link, and ensure() then reopens
    it - wifi first, USB fallback - on the next use.
    """

    def __init__(self):
        self.p = None
        self.q = None
        self.host = None

    def _reader(self, p, q):
        for raw in p.stdout:                           # binary pipe
            q.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        q.put(None)                                    # EOF sentinel

    def _send(self, text):
        # binary write: text=True would translate \n -> \r\n on Windows, and the
        # stray \r glues onto the last shell token ("file not found: foo\r")
        self.p.stdin.write(text.encode("utf-8"))
        self.p.stdin.flush()

    def close(self):
        if self.p:
            try:
                self.p.kill()
            except Exception:
                pass
        self.p = None
        self.host = None

    def ensure(self):
        if self.p and self.p.poll() is None:
            return True
        self.close()
        for h in (SSH_HOST, SSH_FALLBACK):
            try:
                p = subprocess.Popen(
                    ["ssh", "-T", "-o", "ConnectTimeout=4", h, "sh"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)     # binary pipes, deliberately
            except Exception:
                continue
            q = queue.Queue()
            threading.Thread(target=self._reader, args=(p, q), daemon=True).start()
            try:
                p.stdin.write(b"echo __READY__\n")
                p.stdin.flush()
                deadline = time.time() + 8
                while True:
                    line = q.get(timeout=max(0.1, deadline - time.time()))
                    if line is None:
                        raise RuntimeError("eof")
                    if "__READY__" in line:
                        break
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
                continue
            self.p, self.q, self.host = p, q, h
            log("ssh channel open via %s" % h)
            return True
        return False

    def run(self, cmd, timeout=30):
        if not self.ensure():
            raise RuntimeError("tablet unreachable on %s or %s"
                               % (SSH_HOST, SSH_FALLBACK))
        mark = "__DONE_%s__" % uuid.uuid4().hex[:10]
        try:
            # leading \n before the marker: commands like hexdump emit no final
            # newline, and the marker must never merge onto an output line
            self._send('%s\nprintf \'\\n%s %%s\\n\' "$?"\n' % (cmd, mark))
        except Exception:
            self.close()
            raise RuntimeError("ssh channel write failed")
        out = []
        deadline = time.time() + timeout
        while True:
            try:
                line = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                self.close()
                raise RuntimeError("ssh channel timeout running: %s" % cmd[:60])
            if line is None:
                self.close()
                raise RuntimeError("ssh channel closed by tablet")
            if line.startswith(mark):
                status = line[len(mark):].strip()
                if status not in ("", "0"):
                    raise RuntimeError("remote command failed (%s): %s"
                                       % (status, cmd[:60]))
                return "\n".join(out)
            out.append(line)


_channel = SshChannel()


def ssh_host():
    """The interface the live channel uses (needed for scp fallback)."""
    if not _channel.ensure():
        raise RuntimeError("tablet unreachable on %s or %s"
                           % (SSH_HOST, SSH_FALLBACK))
    return _channel.host


def ssh(cmd, timeout=30):
    return _channel.run(cmd, timeout)


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


HEXDUMP_FMT = "hexdump -ve " + chr(39) + "1/1 " + chr(34) + "%%02x" + chr(34) + chr(39)


def pull(rel, dest):
    """Fetch a file over the persistent channel so page pulls don't open new
    connections either. The tablet's BusyBox has no base64, but hexdump
    round-trips verified (md5-identical). scp remains as a fallback."""
    try:
        out = _channel.run((HEXDUMP_FMT + " '%s/%s'") % (XOCHITL, rel), timeout=60)
        data = bytes.fromhex("".join(out.split()))
        if not data:
            raise RuntimeError("empty transfer")
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest
    except Exception as e:
        log("     channel pull failed (%s) - falling back to scp" % str(e)[:60])
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
    header = ("# Tutor memory - %s\n\nWhat the student keeps getting right and wrong, "
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


# Where each module's question-bearing PDFs live (relative to STUDY), and which
# filenames count as papers. Deliberately excludes textbooks and lecture decks.
QUESTION_DIRS = {"AIPS": ["AIPS/Practice_Questions"], "VICO": ["VICO/pdf"]}
SRC_NAME_RE = re.compile(r"exam|quiz|formative|paper|question", re.I)


def _q_markers(page, q):
    """y-coordinates where question `q` plausibly starts on this page. Handles
    'Question 12' headings, the Blackboard-export style (left-margin number
    followed by ESSAY / MULTIPLE CHOICE / FILL IN THE BLANK), and numbered
    list items ('3.' at the left margin, the formative sheets' style)."""
    tops = [r.y0 for r in (page.search_for("Question %d" % q) or [])]
    words = page.get_text("words")
    for i, w in enumerate(words):
        if w[0] >= 120:
            continue
        if w[4] == str(q) and i + 1 < len(words) and \
                words[i + 1][4].upper() in ("ESSAY", "MULTIPLE", "FILL"):
            tops.append(w[1])
        elif w[4] == "%d." % q:
            tops.append(w[1])
        # "Ques&on 3" - the ti ligature breaks in several practice sheets, so
        # search_for("Question 3") misses them entirely
        elif re.match(r"Ques.{0,3}on$", w[4]) and i + 1 < len(words) \
                and words[i + 1][4] == str(q):
            tops.append(w[1])
        # "3 (25 marks)" - the VICO exam rebuilds head their QUESTIONS section
        # this way; the word "Question" only appears in the ANSWERS section,
        # which is exactly the wrong half to fetch from
        elif w[4] == str(q) and i + 2 < len(words) \
                and re.match(r"\(\d+$", words[i + 1][4]) \
                and words[i + 2][4].startswith("mark"):
            tops.append(w[1])
    return sorted(tops)


# Explicit question-location table: the authority for where each question's PNG
# comes from. Consulted BEFORE any marker-scanning; scan results are written back
# so the table grows with use. Hand-edit the JSON to correct a bad location;
# QUESTION-LOOKUP.md is a generated human-readable view (build_question_map.py).
QMAP_PATH = os.path.join(HERE, "question_locations.json")
_qmap = None


def qmap():
    global _qmap
    if _qmap is None:
        try:
            _qmap = json.load(io.open(QMAP_PATH, encoding="utf-8"))
        except Exception:
            _qmap = {}
    return _qmap


def qmap_save():
    if _qmap is not None:
        with io.open(QMAP_PATH, "w", encoding="utf-8") as fh:
            json.dump(_qmap, fh, indent=1, sort_keys=True)


def _locate(doc, q):
    """Scan a document for question q. -> (page, y0, last_page, y1|None) or None,
    all 0-based pages, y in pt. y1 is None when q+1 was never found (question may
    run to the end of last_page)."""
    for p in range(doc.page_count):
        tops = _q_markers(doc[p], q)
        if not tops:
            continue
        y0 = max(0.0, tops[0] - 10)
        last, end_y = p, None
        for p2 in range(p, min(p + 3, doc.page_count)):
            nxt = [t for t in _q_markers(doc[p2], q + 1)
                   if p2 > p or t > tops[0] + 20]
            if nxt:
                last, end_y = p2, nxt[0] - 6
                break
        else:
            # no q+1 marker found (last question, or a marker-style gap):
            # include one continuation page so multi-page tails arrive whole
            last = min(p + 1, doc.page_count - 1)
        return p, y0, last, end_y
    return None


def _render_span(doc, q, p, y0, last, end_y):
    shots = []
    for pi in range(p, last + 1):
        pg = doc[pi]
        top = y0 if pi == p else 0.0
        bot = end_y if (pi == last and end_y is not None) else pg.rect.height
        clip = pymupdf.Rect(0, top, pg.rect.width, max(bot, top + 60))
        out = os.path.join(WORK_DIR, "q%d_part%d.png" % (q, pi - p + 1))
        pg.get_pixmap(matrix=pymupdf.Matrix(3.5, 3.5), clip=clip).save(out)
        shots.append(out)
    return shots


def find_question(module, q, hint=None):
    """Locate Question q in the module's source papers and render it.
    -> (shots, description) or None. The lookup table wins; scanning is the
    fallback, and successful scans are persisted into the table."""
    cands = []
    for rel in QUESTION_DIRS.get(module, []):
        d = os.path.join(STUDY, rel.replace("/", os.sep))
        for f in sorted(glob.glob(os.path.join(d, "*.pdf"))):
            if SRC_NAME_RE.search(os.path.basename(f)):
                cands.append(f)
    if hint:
        toks = hint.lower().split()
        hinted = [f for f in cands
                  if all(tk in os.path.basename(f).lower() for tk in toks)]
        cands = hinted or cands            # a hint that matches nothing is ignored
    # Default preference: example paper, then questions-only papers, then
    # with-answers rebuilds, and the real-sitting export (Isaac's wrong answers
    # embedded) or cohort feedback only if named by hint or nothing else matches.
    def rank(f):
        n = os.path.basename(f).lower()
        if "example" in n:
            return 0
        if "question" in n and "answer" not in n:
            return 1
        if "real-sitting" in n or "feedback" in n:
            return 4
        return 2 if "exam" in n else 3
    # within a rank, newest paper first (filenames carry years)
    cands.sort(key=lambda f: os.path.basename(f).lower(), reverse=True)
    cands.sort(key=rank)

    for f in cands:
        rel = os.path.relpath(f, STUDY).replace(os.sep, "/")
        entry = qmap().setdefault(module, {}).setdefault(rel, {})
        loc = entry.get(str(q))
        try:
            doc = pymupdf.open(f)
        except Exception:
            continue
        src = "table"
        if loc:
            p, y0 = loc["page"] - 1, loc["y0"]
            last, end_y = loc["end_page"] - 1, loc.get("y1")
        else:
            found = _locate(doc, q)
            if not found:
                doc.close()
                continue
            p, y0, last, end_y = found
            entry[str(q)] = {"page": p + 1, "y0": round(y0, 1),
                             "end_page": last + 1,
                             "y1": (round(end_y, 1) if end_y is not None else None)}
            qmap_save()
            src = "scan, saved to table"
        shots = _render_span(doc, q, p, y0, last, end_y)
        name, pno = os.path.basename(f), p + 1
        doc.close()
        desc = "Question %d from %s, page %d [%s]" % (q, name, pno, src)
        if last > p and end_y is not None:
            desc += " (spans %d pages - sent every part)" % (last - p + 1)
        elif last > p:
            desc += " (sent the following page too in case it continues)"
        if re.search(r"answer", name, re.I):
            desc += (". (Source file also contains model answers in a later "
                     "section - the crop is from the questions half; tell me "
                     "if any answer text leaks in.)")
        return shots, desc
    return None


Q_REF_RE = re.compile(r"\bQ(?:uestion)?\s*\.?\s*(\d{1,2})\b", re.I)


def guess_question(pdf_path, page_idx, bbox=None):
    """Which exam question does this workbook page refer to? Look in the text
    near the grey ink first, then the whole page. Lets 'screenshot from the exam
    paper' work without a number when the page itself names the question."""
    try:
        doc = pymupdf.open(pdf_path)
        page = doc[page_idx]
        pw, ph = page.rect.width, page.rect.height
        s = ph / RM_DOC_HEIGHT
        texts = []
        if bbox:
            x0, y0, x1, y1 = bbox
            pad = 150 * s
            r = pymupdf.Rect(max(0, s * x0 + pw / 2 - pad), max(0, s * y0 - pad),
                             min(pw, s * x1 + pw / 2 + pad), min(ph, s * y1 + pad))
            texts.append(page.get_textbox(r) or "")
        texts.append(page.get_text() or "")
        doc.close()
        for t in texts:
            m = Q_REF_RE.search(t)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def page_shot(pdf_path, page_idx, bbox=None):
    """Clean high-res renders of the source page - the PDF's own pixels, no ink.
    For when the tablet mangles a question's display. Returns [region?, full],
    region first so the thing he asked about is the primary attachment."""
    doc = pymupdf.open(pdf_path)
    page = doc[page_idx]
    pw, ph = page.rect.width, page.rect.height
    s = ph / RM_DOC_HEIGHT
    shots = []
    if bbox:
        x0, y0, x1, y1 = bbox
        # taller than a written word = he circled a region, so crop to it
        if (y1 - y0) > 180:
            r = pymupdf.Rect(max(0, s * x0 + pw / 2 - 15), max(0, s * y0 - 15),
                             min(pw, s * x1 + pw / 2 + 15), min(ph, s * y1 + 15))
            p = os.path.join(WORK_DIR, "shot_p%d_region.png" % (page_idx + 1))
            page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=r).save(p)
            shots.append(p)
    p = os.path.join(WORK_DIR, "shot_p%d_page.png" % (page_idx + 1))
    page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).save(p)
    shots.append(p)
    doc.close()
    return shots


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

# Grey "tutor" swaps the explain task for this. The standard rules still arrive
# with every prompt, so the override has to name which of them stop applying -
# otherwise the 200-word cap and the circled-work-only rule quietly win.
TUTOR_TASK = u"""FULL-TUTOR MODE. This is a question to his personal tutor, and the following
overrides the standard rules where they conflict:
- The blue handwriting is the question; the circle only anchors where he is.
  The question may be about ANYTHING - this module, exam strategy, his past
  performance - so answer the question actually asked, not the circled drill.
- You have Read, Grep and Glob over the whole study tree. Start from:
    {module_root}\\md\\   converted study materials, if present
    the workbook PDFs and marking-notes files named in the WORKBOOKS map
  Go and read what the question needs; never answer a question about his own
  past work without opening the file that holds it.
- The 200-word cap is lifted: up to 600 words when the question deserves it.
- Everything else stands: strict standards, exam discipline, say it differently
  from the workbook, end with one check question, and the first line is still
  "VERDICT: <max 8 words>"."""

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
    # Headless agents may only read below their cwd (here: scripts\). Grant the
    # whole study tree, or any "look at file X" request gets a file-access
    # denial - the tools are read-only, so this is safe.
    cmd += ["--add-dir", STUDY]
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

    task = TASKS[ctx["kind"]]
    if ctx["kind"] == "explain" and state.get("tutor"):
        task = TUTOR_TASK.format(module_root=os.path.join(STUDY, ctx["module"]))
        why += ", full-tutor"
    prompt = tpl.format(task=task, **ctx)
    log("     %s | prompt %d chars (excerpt %d)" % (why, len(prompt), len(ctx["excerpt"])))
    if dry:
        log("DRY-RUN prompt:\n" + prompt[:2000])
        return "VERDICT: dry run\n(no model called)"
    try:
        out = run_claude(prompt, model, effort, tools="Read Grep Glob", extra=args)
    except RuntimeError as e:
        # a resume can fail if the transcript was cleaned up - start over once
        if not is_new and "resume" in (" ".join(args) + str(e)).lower():
            log("     resume failed, starting a fresh conversation")
            state["sessions"].pop(wb_key, None)
            args, _ = session_args(state, wb_key)
            prompt = PROMPT.format(task=task, **ctx)
            out = run_claude(prompt, model, effort, tools="Read Grep Glob", extra=args)
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
    if re.search(r"\bhelp\b|\bcommands?\b|\bwhat can you do\b", t):
        return "help", None
    if re.search(r"\brestart\b|\breboot\b|\breload\b", t):
        return "restart", None
    if re.search(r"\bscreen ?shot\b|\bpng\b|\bphoto\b|\bpicture\b|\bsnap\b"
                 r"|\bsend\b.*\b(page|question)\b", t):
        # "screenshot q12 example exam" -> fetch Question 12 from a source paper.
        # Bare "screenshot" -> render the circled region of the current page.
        mq = re.search(r"\bq(?:uestion)?\s*\.?\s*(\d{1,2})\b", t)
        stop = {"screenshot", "screen", "shot", "png", "photo", "picture", "snap",
                "image", "send", "me", "a", "of", "the", "from", "question", "q",
                "please", "crop", "this", "it", "that", "and", "for", "paper",
                "pdf", "page", "in", "on"}
        toks = [w for w in re.findall(r"[a-z0-9-]+", t)
                if w not in stop and not w.isdigit()
                and not re.fullmatch(r"q\d+", w)]
        return "screenshot", ((int(mq.group(1)) if mq else None),
                              (" ".join(toks) or None))
    if re.search(r"\bdeep\b", t):
        off = bool(re.search(r"\boff\b|\bdisable\b|\bstop\b|\bnormal\b", t))
        return "deep", ("off" if off else "on")
    if re.search(r"\btutor\w*\b", t):
        off = bool(re.search(r"\boff\b|\bdisable\b|\bstop\b|\bnormal\b|\bquick\b", t))
        return "tutor", ("off" if off else "on")
    if re.search(r"\beffort\b|\bthinking\b", t):
        return "get-model", None          # asking about effort -> report both
    if re.search(r"\bmodel\b|\bmodle\b", t):
        return "get-model", None
    if re.search(r"\bstatus\b|\bhow many\b", t):
        return "status", None
    return "unknown", text.strip()


HELP_TEXT = u"""GREY INK COMMANDS (write any of these in grey)

INK COLOURS
red circle = mark the circled work, strictly
blue circle = explain it; blue handwriting = your question
grey = commands (this list). Erase circles once answered.

FETCH A QUESTION IMAGE
'screenshot q12' - Question 12 from the default paper
'screenshot q1 real' / 'q3 game theory' / 'q1 formative' - name the paper
'screenshot from the exam paper' - infers the question from the page you're on
'screenshot' alone + a grey circle - renders the circled region of THIS page
(sources and exact pages: workbooks/QUESTION-LOOKUP.md)

MODELS AND EFFORT
'model opus' / 'model sonnet' / 'model haiku' - both mark and explain
'mark haiku' / 'explain opus' - scope to one task
'effort high' / 'think harder' / 'faster' - reasoning effort
'which model?' - report current setup

DEEP MODE
'deep explain' - blue circles go to a max-effort Fable agent that reads the
whole corpus (several minutes each). 'deep off' - back to quick explains.

FULL TUTOR
'tutor' - blue handwriting becomes a question to your tutor: it reads
anything in the study tree and answers at length - not limited to the
circled work. Stays in the same conversation, so follow-ups build on it.
'tutor off' - quick explains.

SYSTEM
'status' - models, deep on/off, requests answered, active workbook
'restart' - relaunch the watcher with the latest code
'help' - this list"""


def handle_command(crop_png, state, dry=False, page=None):
    """Grey ink is a control channel, not a question. Always read with the
    fastest model so switching away from a slow one is itself quick.
    `page` = (pdf_path, page_idx, grey_bbox) so screenshot requests can render
    from the source PDF. Screenshots are handed back via state["_shots"]."""
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
        return ("%s\nDEEP explain: %s. Full tutor: %s. %d requests answered. "
                "Watching %d pages. Active workbook: %s."
                % (describe(), "ON" if state.get("deep") else "off",
                   "ON" if state.get("tutor") else "off",
                   len(state.get("answered", [])),
                   len(state.get("mtimes", {})),
                   state.get("active_wb") or "none")), "status"
    if kind == "help":
        return HELP_TEXT, "command list"
    if kind == "screenshot":
        q, hint = arg if isinstance(arg, tuple) else (None, None)
        inferred = False
        # The gesture decides what a bare "screenshot" means: a proper circled
        # REGION (taller than a written word) means "render what I circled from
        # this page"; just the written word means "fetch the exam question this
        # page is about". Extra words like "from the exam paper" force a fetch.
        region_gesture = bool(page and page[2] and
                              (page[2][3] - page[2][1]) > 180)
        if not q and page and (hint or not region_gesture):
            q = guess_question(page[0], page[1], page[2])
            inferred = q is not None
        if q:
            module = (page or (None, None, None, "AIPS"))[3]
            found = find_question(module, q, hint)
            if found:
                state["_shots"], desc = found
                pre = ("The page you're on references Question %d, so I fetched "
                       "that. If you meant a different one, write the number: "
                       "'screenshot q3'.\n\n" % q) if inferred else ""
                return (pre + "%s - rendered straight from the paper." % desc), \
                       "Q%d fetched" % q
            names = []
            for rel in QUESTION_DIRS.get(module, []):
                d = os.path.join(STUDY, rel.replace("/", os.sep))
                names += [os.path.basename(f)[:44] for f in
                          sorted(glob.glob(os.path.join(d, "*.pdf")))
                          if SRC_NAME_RE.search(os.path.basename(f))]
            return ("Could not find a 'Question %d' marker%s. Searched:\n%s\n"
                    "Name the paper in the command (e.g. 'screenshot q%d formative') "
                    "or check the question number."
                    % (q, (" matching '%s'" % hint) if hint else "",
                       "\n".join("- " + n for n in names[:10]), q)), "Q%d not found" % q
        if not page:
            return "No page context - circle on a workbook page.", "screenshot failed"
        shots = page_shot(page[0], page[1], page[2])
        state["_shots"] = shots
        return ("Sent %d render%s of the current page from its source PDF. To fetch "
                "a question from an exam paper instead, write e.g. 'screenshot q12' "
                "or 'screenshot q3 game theory quiz'."
                % (len(shots), "" if len(shots) == 1 else "s")), "page screenshot"
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
    if kind == "tutor":
        if arg == "on":
            state["tutor"] = True
            return ("FULL TUTOR enabled (%s/%s). Blue handwriting is now a question "
                    "to your tutor: it reads whatever the question needs from the "
                    "study tree and answers at length instead of only explaining "
                    "the circled work. Red marking is unchanged. 'tutor off' "
                    "returns to quick explains. (While 'deep explain' is on, it "
                    "still takes precedence for blue circles.)"
                    % profile(state, "explain")), "FULL TUTOR on"
        state["tutor"] = False
        return ("Full tutor off - blue circles back to quick, focused explains "
                "of the circled work."), "full tutor off"
    return ("Read that as: \"%s\" - not a command I know.\n"
            "Write 'help' in grey for the full command list."
            % (said[:120] or "nothing legible")), "unknown - write 'help' in grey"


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
                body, short = handle_command(crop_png or full_png, state, dry=dry,
                                             page=(pdf_path, idx, g["bbox"], module))
            except Exception as e:
                log("  !! command failed: %s" % e)
                notify("CMD ERROR", str(e)[:400], priority="high",
                       tags="warning", dry=dry)
                failed += 1
                continue
            shots = state.pop("_shots", [])       # transient - never persisted
            notify("CMD - %s" % short, body, tags="gear", dry=dry,
                   image=(shots[0] if shots else None))
            for extra in shots[1:]:
                notify("CMD - %s" % short, "full page for context",
                       image=extra, dry=dry)
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
    offline_polls = 0
    settling = {}                 # rel -> [mtime, stable_poll_count]

    while True:
        try:
            now = poll_mtimes()
            if offline_polls >= OFFLINE_ALERT_AFTER:
                log("tablet back online after %d failed polls" % offline_polls)
                try:
                    notify("Tablet reconnected", "Back online - catching up now.",
                           priority="low", tags="zzz", dry=args.dry_run)
                except Exception:
                    pass
            offline_polls = 0
            # Settle gate: only act on a page once its mtime has held still for
            # SETTLE_POLLS consecutive polls - i.e. Isaac has stopped writing.
            # Without this, a pause at a line break commits half a question and
            # we would answer it mid-sentence.
            changed = []
            if first_pass:
                settling.clear()               # baseline, never fire on old ink
            else:
                for r, mt in now.items():
                    if state["mtimes"].get(r) == mt:
                        settling.pop(r, None)
                        continue
                    s = settling.get(r)
                    if s and s[0] == mt:
                        s[1] += 1
                        if s[1] >= SETTLE_POLLS:
                            changed.append(r)
                            del settling[r]
                    else:
                        settling[r] = [mt, 0]  # fresh ink - start the clock
            # Oldest write first, so circling page 11 then page 12 answers in
            # that order. Filesystem glob order is by UUID, which would deliver
            # them in an arbitrary sequence.
            changed.sort(key=lambda r: now[r])
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
            # pages still settling must also stay "unseen", or this assignment
            # would mark them handled and their ink would never fire
            for rel in settling:
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
            _channel.close()      # force a clean reopen (wifi first, then USB)
            offline_polls += 1
            # tell the PHONE, once - the console is not where he is looking
            if offline_polls == OFFLINE_ALERT_AFTER:
                try:
                    notify("Tablet unreachable - asleep?",
                           "Lost the connection to the reMarkable (usually sleep). "
                           "Tap it awake and I'll catch up - circles drawn in the "
                           "meantime are not lost.",
                           priority="high", tags="zzz", dry=args.dry_run)
                except Exception:
                    pass
        if args.once:
            return
        try:
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("stopped")
            return


if __name__ == "__main__":
    main()

