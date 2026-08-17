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
* The 2-second poll NEVER invokes a model. It is one `stat` over a reused SSH
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
SSH_KEY = os.path.expanduser("~/.ssh/remarkable")
# On-the-fly fallback: when neither ssh-config alias answers (out and about,
# tablet + laptop on a phone hotspot), scan the tiny subnets phones hand out
# and key-auth anything with port 22 open that proves it is the tablet.
# iPhone Personal Hotspot leases 172.20.10.2-14; 10.11.99.1 is the USB cable.
# RM_HOTSPOT_NET="192.168.43." (env var) adds an Android-style /24 if needed.
HOTSPOT_NETS = [("172.20.10.", 2, 15), ("10.11.99.", 1, 2)]
if os.environ.get("RM_HOTSPOT_NET"):
    HOTSPOT_NETS.append((os.environ["RM_HOTSPOT_NET"], 2, 255))
XOCHITL = "/home/root/.local/share/remarkable/xochitl"

NTFY_TOPIC = os.environ.get("RM_NTFY_TOPIC", "CHANGE-ME-long-random-string")
NTFY_URL = "https://ntfy.sh/" + NTFY_TOPIC
# Phone push is OFF by default - the dashboard already shows every reply, so
# the ntfy round-trip is just a second copy. RM_NTFY=1 turns it back on.
NTFY_ENABLED = os.environ.get("RM_NTFY", "0").lower() in ("1", "true", "yes", "on")

# Local dashboard: a read-only mirror of the loop served on this machine.
# RM_WEB_HOST=0.0.0.0 opens it to the LAN (phone browser); default is local.
WEB_HOST = os.environ.get("RM_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("RM_WEB_PORT", "8477"))

HERE = os.path.dirname(os.path.abspath(__file__))
STUDY = os.environ.get("RM_STUDY_ROOT") or os.path.dirname(os.path.dirname(HERE))
STATE_PATH = os.path.join(HERE, ".rm_feedback_state.json")
WORK_DIR = os.path.join(HERE, ".rm_feedback_tmp")
LOG_PATH = os.path.join(HERE, "rm_feedback.log")

POLL_SECONDS = 2                 # one SSH stat per poll - cheap enough to run
                                 # this often, and the poll wait dominated the
                                 # ink-to-response latency at 5s
HEARTBEAT_SECONDS = 300          # proof-of-life, instead of a line per stroke
OFFLINE_ALERT_AFTER = 8          # consecutive failed polls before phoning it
                                 # in - scaled with the faster poll so the
                                 # alert still waits ~25s of wall-clock
SETTLE_POLLS = 0                 # fire the moment a page's mtime changes -
                                 # xochitl only commits ink after pen-up, so a
                                 # write is already a complete thought. For long
                                 # prompts built over several commits, grey
                                 # 'wait' ... 'begin' holds fire explicitly
                                 # (patience mode) instead of a blanket debounce
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

# EXAMPLE MAP - REPLACE WITH YOUR OWN MODULES. Keys are document names as they
# appear on the tablet; values are (source PDF, marking-notes markdown), both
# relative to RM_STUDY_ROOT. The FIRST PATH SEGMENT of the source PDF is taken
# as the module name - it groups conversations, tutor memory, exam dates and
# question lookups, so keep one folder per module.
WORKBOOKS = {
    "MODULE-A-workbook-1": ("MODULE-A/workbooks/workbook-1.pdf",
                            "MODULE-A/workbooks/marking-notes-1.md"),
    "MODULE-B-workbook-1": ("MODULE-B/workbooks/workbook-1.pdf",
                            "MODULE-B/workbooks/marking-notes-1.md"),
}

# Exam date (and any format notes) per module, quoted to the tutor for context.
# Optional - modules missing from here just get no exam line.
EXAM_DATES = {
    "MODULE-A": "Mon 1 Jun 2026, closed book",
    "MODULE-B": "Tue 2 Jun 2026, open book",
}


# Documents are named inconsistently on the tablet - some carry the ".pdf"
# extension, some don't, and renamed copies keep their old names. Map any
# alternate tablet names to their WORKBOOKS key here.
ALIASES = {}


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
        self.host_args = []          # extra ssh/scp args for discovered hosts

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

    def _try_open(self, h, extra):
        """Open one candidate; a discovered host must also prove it IS the
        tablet (has the xochitl store) before we accept it - a hotspot scan
        can hit any device that happens to run sshd."""
        try:
            p = subprocess.Popen(
                ["ssh", "-T", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes"]
                + extra + [h, "sh"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)     # binary pipes, deliberately
        except Exception:
            return False
        q = queue.Queue()
        threading.Thread(target=self._reader, args=(p, q), daemon=True).start()
        try:
            probe = b"echo __READY__\n"
            if extra:
                probe = ("test -d '%s' && echo __READY__ || echo __NOTRM__\n"
                         % XOCHITL).encode()
            p.stdin.write(probe)
            p.stdin.flush()
            deadline = time.time() + 8
            while True:
                line = q.get(timeout=max(0.1, deadline - time.time()))
                if line is None or "__NOTRM__" in line:
                    raise RuntimeError("not the tablet")
                if "__READY__" in line:
                    break
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            return False
        self.p, self.q, self.host, self.host_args = p, q, h, extra
        log("ssh channel open via %s" % h)
        return True

    def ensure(self):
        if self.p and self.p.poll() is None:
            return True
        self.close()
        for h in (SSH_HOST, SSH_FALLBACK):
            if self._try_open(h, []):
                return True
        # both aliases dead: maybe we're on a phone hotspot where the tablet
        # has an IP the ssh config has never heard of - go and find it
        extra = _key_args()
        for h in discover_tablet():
            if self._try_open(h, extra):
                return True
        return False

    def run(self, cmd, timeout=30):
        if not self.ensure():
            raise RuntimeError("tablet unreachable on %s, %s or hotspot scan"
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


def _key_args():
    """ssh/scp args for hosts not in ~/.ssh/config (hotspot-discovered IPs)."""
    args = ["-o", "StrictHostKeyChecking=accept-new"]
    if os.path.exists(SSH_KEY):
        args = ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes"] + args
    return args


def discover_tablet():
    """Scan the hotspot-sized subnets in HOTSPOT_NETS for anything with port
    22 open. Cheap (a handful of parallel half-second probes) and only runs
    once both ssh-config aliases have already failed."""
    import socket
    open_ips, lock, threads = [], threading.Lock(), []

    def probe(ip):
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect((ip, 22))
            with lock:
                open_ips.append(ip)
        except Exception:
            pass
        finally:
            s.close()

    for base, lo, hi in HOTSPOT_NETS:
        for n in range(lo, hi):
            t = threading.Thread(target=probe, args=(base + str(n),), daemon=True)
            t.start()
            threads.append(t)
    for t in threads:
        t.join()
    if open_ips:
        log("hotspot scan: ssh open on %s" % ", ".join(sorted(open_ips)))
    return ["root@" + ip for ip in sorted(open_ips)]


_channel = SshChannel()


def ssh_host():
    """The interface the live channel uses (needed for scp fallback)."""
    if not _channel.ensure():
        raise RuntimeError("tablet unreachable on %s, %s or hotspot scan"
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
    r = subprocess.run(["scp", "-q"] + _channel.host_args
                       + ["%s:%s/%s" % (ssh_host(), XOCHITL, rel), dest],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("scp failed: %s" % r.stderr.strip()[:200])
    return dest


PROGRESS_MIN_BYTES = 6000    # a .rm smaller than this is a stray mark, not work
EXAM_SECTION_RE = re.compile(
    r"\b(mock|exam paper|past paper|timed (mock|exam)|exam section"
    r"|exam questions)\b", re.I)


# What counts toward progress: drills D<n> AND mid-book exercises E<n>.
# What never counts: the mock/exam section at the back, and items the workbook
# itself flags as outside the timed session (e.g. "optional overspill" pages
# and explicit "optional drills D11-D13" style notes).
OPTIONAL_PAGE_RE = re.compile(
    r"(?i)optional overspill|not part of the .{0,30}(?:budget|timer)"
    r"|only if you finish(?:ed)? early"
    r"|not (?:included|counted) in the .{0,30}(?:time|timer|min)")
OPTIONAL_RANGE_RE = re.compile(
    r"(?i)optional drills?\s+D(\d{1,2})\s*[–—-]\s*D?(\d{1,2})")


def drill_list(pdf_path):
    """Countable item labels (D3, E1, ...) in the workbook. Deterministic and
    cheap - this is the progress bar's denominator. An item is dropped only if
    it is named in an explicit "optional drills D11-D13" note, or if EVERY page
    it appears on is optional-flagged - contents pages that both list all
    drills and mention the optional section must not poison regular drills."""
    pages_of, excluded = {}, set()
    try:
        doc = pymupdf.open(pdf_path)
        n = doc.page_count
        boundary = n
        for i in range(n // 2, n):
            t = doc[i].get_text()
            # a day-agenda page may MENTION the mock in its budget line
            # ("§12 mock 40 min") - the real exam section never carries
            # LEARN/DRILL headers, so require their absence too
            if (EXAM_SECTION_RE.search(t[:400])
                    and not re.search(r"\b(LEARN|DRILL)\b", t)):
                boundary = i
                break
        opt_pages = set()
        caps_opt = re.compile(r"\bOPTIONAL\b")     # section headers, not prose
        for i in range(boundary):
            t = doc[i].get_text()
            # [1-9]\d? and the <=30 cap keep scientific-notation debris like
            # "E76" or "E00" in body text out of the census
            for m in re.finditer(r"\b([DE][1-9]\d?)\b", t):
                if int(m.group(1)[1:]) <= 30:
                    pages_of.setdefault(m.group(1), set()).add(i)
            if OPTIONAL_PAGE_RE.search(t) or caps_opt.search(t):
                opt_pages.add(i)
            for m in OPTIONAL_RANGE_RE.finditer(t):
                for k in range(int(m.group(1)), int(m.group(2)) + 1):
                    excluded.add("D%d" % k)
        doc.close()
        for lab, pgs in pages_of.items():
            if pgs and pgs <= opt_pages:
                excluded.add(lab)
    except Exception:
        pass
    keep = [l for l in pages_of if l not in excluded]
    return sorted(keep, key=lambda l: (l[0], int(l[1:])))


# Progress is DRILL-based and lives in a human-readable checklist per workbook:
# .rm_progress/<wb>.md. The first grey "update progress" builds it with an
# agent audit of the inked pages; from then on the marker ticks drills live
# (a drill counts only at FULL marks) and rescans just re-audit.
PROG_DIR = os.path.join(HERE, ".rm_progress")


def prog_path(wb):
    return os.path.join(PROG_DIR, wb + ".md")


def prog_load(wb):
    """-> (drill list or None if no file yet, set of completed drills)."""
    try:
        txt = io.open(prog_path(wb), encoding="utf-8").read()
    except Exception:
        return None, set()
    drills, done = [], set()
    for m in re.finditer(r"^- \[(x| )\] ([DE]\d+)", txt, re.M):
        lab = m.group(2)
        drills.append(lab)
        if m.group(1) == "x":
            done.add(lab)
    return (drills or None), done


def prog_save(wb, drills, done):
    os.makedirs(PROG_DIR, exist_ok=True)
    lines = ["# Drill progress - %s" % wb, "",
             "Updated %s. A drill is ticked only when it has achieved FULL "
             "marks. Hand-editable; grey 'update progress' re-audits."
             % time.strftime("%d %b %H:%M"), ""]
    lines += ["- [%s] %s" % ("x" if lab in done else " ", lab) for lab in drills]
    with io.open(prog_path(wb), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def prog_info(drills, done):
    return {"done": len(done), "total": len(drills), "unit": "drills",
            "pct": int(round(100.0 * len(done) / max(1, len(drills)))),
            "ts": time.strftime("%d %b %H:%M")}


def prog_tick(state, wb, labels, pdf_path=None):
    """Mark items complete (idempotent) and refresh every progress surface."""
    drills, done = prog_load(wb)
    if drills is None:
        drills = drill_list(pdf_path) if pdf_path else []
    fresh = [l for l in labels if l not in done]
    if fresh:
        WEB.add_drills(len(fresh))     # session counter: first-time ticks only
    for lab in labels:
        if lab not in drills:
            drills = sorted(set(drills) | {lab},
                            key=lambda l: (l[0], int(l[1:])))
        done.add(lab)
    prog_save(wb, drills, done)
    info = prog_info(drills, done)
    state.setdefault("progress", {})[wb] = info
    WEB.set_progress(wb, info)
    return info


_drill_cache = {}


def refresh_modules():
    """Whole-module totals across every workbook in the map - drills only,
    exam questions never counted. Unscanned workbooks contribute their full
    drill count as not-done (PDF text-scanned once, then cached), so the
    module bar is honest about the road ahead."""
    try:
        agg = {}
        for wb, (pdf_rel, _brief) in WORKBOOKS.items():
            module = pdf_rel.split("/")[0]
            drills, done = prog_load(wb)
            if drills is None:
                if wb not in _drill_cache:
                    _drill_cache[wb] = drill_list(
                        os.path.join(STUDY, pdf_rel.replace("/", os.sep)))
                drills, done = _drill_cache[wb], set()
            if not drills:
                # an empty census means the PDF was unreadable - counting it
                # as zero total would silently inflate the module bar
                log("  !! %s: census EMPTY (pdf unreadable?) - module bar "
                    "will overstate progress until fixed" % wb)
            WEB.set_progress(wb, prog_info(drills, done))
            a = agg.setdefault(module, {"done": 0, "total": 0})
            a["done"] += len(done)
            a["total"] += len(drills)
        for a in agg.values():
            a["pct"] = int(round(100.0 * a["done"] / max(1, a["total"])))
        WEB.set_modules(agg)
    except Exception as e:
        log("     module progress refresh failed: %s" % str(e)[:80])


PROGRESS_SCAN_PROMPT = u"""You are auditing a student's workbook for completion.
Workbook: {wb}. Countable items (drills D<n> and exercises E<n>): {drills}.
The marking notes (every drill's answer and marking logic) are here - Read this file first:
{brief}

Then Read each page render below. They show the printed workbook page with the
student's handwriting overlaid:
{pages}

For each item, decide whether the visible written answer is COMPLETE and would
earn FULL marks against the marking notes. Be strict: partially answered,
partially correct, or unattempted all count as NOT complete.
Output ONLY lines of the form "D3: complete" or "E2: complete" for items at
full marks - no commentary, nothing else. If none qualify, output NONE."""


def progress_scan(doc_uuid, order, pdf_map, wb_key, pdf_path, brief_path,
                  state, dry=False):
    """Grey 'update progress'. First run is the expensive one: renders every
    inked content page and has the marking model judge which drills are at
    full marks. Later runs re-audit the same way but keep already-ticked
    drills (the checklist only moves forward unless hand-edited)."""
    drills, done = prog_load(wb_key)
    if drills is not None:
        # Checklist already exists: the expensive audit only ever runs ONCE.
        # From here the marker ticks it live; re-running 'update progress'
        # re-reports - but always re-syncs the census first, so a checklist
        # written by an older census (or with a broken path) heals itself.
        census = drill_list(pdf_path)
        if census:
            done = done & set(census)
            drills = census
        prog_save(wb_key, drills, done)
        info = prog_info(drills, done)
        state.setdefault("progress", {})[wb_key] = info
        WEB.set_progress(wb_key, info)
        return info, 0
    drills = drill_list(pdf_path)
    sizes = {}
    # "|| true": a workbook with no ink has no .rm files, the glob matches
    # nothing and stat exits 1 - that is an empty result, not an error
    out = ssh("stat -c '%%s %%n' %s/%s/*.rm 2>/dev/null || true"
              % (XOCHITL, doc_uuid))
    for line in out.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            sizes[os.path.splitext(os.path.basename(parts[1]))[0]] = int(parts[0])
    inked = [i for i, pu in enumerate(order)
             if sizes.get(pu, 0) >= PROGRESS_MIN_BYTES]
    shots = []
    for i in inked[:30]:
        try:
            local = os.path.join(WORK_DIR, order[i] + ".rm")
            pull("%s/%s.rm" % (doc_uuid, order[i]), local)
            # tablet position -> PDF page: an inserted notes page would
            # otherwise render the wrong page for every page after it
            pidx, _ins = pdf_page_for(pdf_map, i)
            full, _ = render(pdf_path, pidx, parse_strokes(local), crop=None)
            p = os.path.join(WORK_DIR, "prog_%s_p%d.png" % (wb_key, pidx + 1))
            full.save(p)
            shots.append(p)
        except Exception as e:
            log("     progress render p%d failed: %s" % (i + 1, str(e)[:60]))
    if shots and not dry:
        mdl, eff = profile(state, "mark")
        log("     progress audit: %d inked pages -> %s/%s" % (len(shots), mdl, eff))
        try:
            outp = run_claude(PROGRESS_SCAN_PROMPT.format(
                wb=wb_key, drills=", ".join(drills) or "unknown",
                brief=brief_path, pages="\n".join(shots)),
                mdl, eff, tools="Read", timeout=600)
            for m in re.finditer(r"\b([DE]\d{1,2})\b\s*:?\s*complete", outp, re.I):
                done.add(m.group(1).upper())
        except Stopped:
            raise
        except Exception as e:
            log("     progress audit failed: %s" % str(e)[:100])
    prog_save(wb_key, drills, done)
    info = prog_info(drills, done)
    state.setdefault("progress", {})[wb_key] = info
    WEB.set_progress(wb_key, info)
    return info, len(inked)


def progress_scan_all(state, dry=False):
    """Grey 'update progress': every mapped workbook, both modules. Workbooks
    found on the tablet get the full ink audit; the rest get their checklist
    (created empty if new). One shared progress file per workbook is the
    ledger every marking agent ticks from then on."""
    on_tablet = {}
    try:
        listing = ssh("ls %s" % XOCHITL, timeout=60)
        uuids = sorted({x[:-9] for x in listing.split() if x.endswith(".metadata")})
        for u in uuids:
            try:
                name, order, pmap = doc_info(u)
            except Exception:
                continue
            k = lookup_key(name)
            if k and k not in on_tablet:
                on_tablet[k] = (u, order, pmap)
    except Exception as e:
        log("     tablet doc listing failed: %s" % str(e)[:80])
    lines = []
    for wb in sorted(WORKBOOKS):
        pdf_rel, brief_rel = WORKBOOKS[wb]
        pdf_path = os.path.join(STUDY, pdf_rel.replace("/", os.sep))
        brief_path = os.path.join(STUDY, brief_rel.replace("/", os.sep))
        if not os.path.exists(pdf_path):
            continue
        try:
            if wb in on_tablet:
                u, order, pmap = on_tablet[wb]
                save_pagemap(wb, order, pmap)
                log("     progress: auditing %s" % wb)
                info, _ = progress_scan(u, order, pmap, wb, pdf_path,
                                        brief_path, state, dry=dry)
            else:
                drills, done = prog_load(wb)
                if drills is None:
                    drills, done = drill_list(pdf_path), set()
                prog_save(wb, drills, done)
                info = prog_info(drills, done)
                state.setdefault("progress", {})[wb] = info
                WEB.set_progress(wb, info)
        except Stopped:
            raise
        except Exception as e:
            # one broken workbook must never sink the other nine
            log("     progress %s failed: %s" % (wb, str(e)[:90]))
            lines.append("%s: scan failed - %s" % (wb, str(e)[:60]))
            continue
        lines.append("%s: %d/%d (%d%%)"
                     % (wb, info["done"], info["total"], info["pct"]))
    refresh_modules()
    mods = WEB.snapshot().get("modules", {})
    mline = "  ·  ".join("%s %d%% (%d/%d)"
                         % (k, v["pct"], v["done"], v["total"])
                         for k, v in sorted(mods.items()))
    return lines, mline


def doc_info(doc_uuid):
    """-> (visibleName, page order, pdf_map).

    pdf_map runs parallel to `order`: the index of the SOURCE PDF page each
    tablet page shows, or None for a page inserted on the tablet (a notes page,
    which has no PDF behind it).

    This matters because a tablet page's position is NOT its PDF page number.
    Insert one notes page and every page after it shifts by one; insert two and
    it shifts by two. Marking then reads the wrong page and the tutor answers a
    question that isn't the one circled. xochitl already records the true
    mapping per page as `redir`, so nothing has to be inferred - a page with no
    `redir` is an inserted one."""
    meta = json.loads(ssh("cat %s/%s.metadata" % (XOCHITL, doc_uuid)))
    content = json.loads(ssh("cat %s/%s.content" % (XOCHITL, doc_uuid)))
    order, pdf_map = [], []
    pages = content.get("cPages", {}).get("pages")
    if pages:
        for p in pages:
            if p.get("deleted", {}).get("value"):
                continue
            order.append(p.get("id"))
            redir = p.get("redir")
            pdf_map.append(redir.get("value") if isinstance(redir, dict) else None)
    elif isinstance(content.get("pages"), list):
        # legacy format: no redirection data, so positions are the best we have
        order = content["pages"]
        pdf_map = list(range(len(order)))
    return meta.get("visibleName", "?"), order, pdf_map


# --------------------------------------------------------------------------- #
# tablet page -> PDF page
# --------------------------------------------------------------------------- #
PAGEMAP_DIR = os.path.join(HERE, ".rm_pagemap")


def save_pagemap(wb_key, order, pdf_map):
    """Record the mapping so it is inspectable, and so a stale copy survives the
    tablet going offline. Written on every read; cheap and always current."""
    try:
        os.makedirs(PAGEMAP_DIR, exist_ok=True)
        inserted = [i for i, v in enumerate(pdf_map) if v is None]
        data = {"updated": time.strftime("%Y-%m-%d %H:%M"),
                "tablet_pages": len(order),
                "pdf_pages": len([v for v in pdf_map if v is not None]),
                "inserted_at_tablet_page": [i + 1 for i in inserted],
                # 1-based on both sides, which is what the log and cards show
                "tablet_to_pdf": {str(i + 1): (None if v is None else v + 1)
                                  for i, v in enumerate(pdf_map)}}
        with io.open(os.path.join(PAGEMAP_DIR, "%s.json" % wb_key),
                     "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1))
    except Exception as e:
        log("  !! could not save page map: %s" % e)


def pdf_page_for(pdf_map, nb_idx):
    """-> (pdf_idx, inserted). An inserted notes page has no PDF page of its
    own, so it borrows the last real page before it - work continued onto a
    notes page still belongs to the question it follows. Falls back to the
    tablet position only when there is no mapping at all."""
    if nb_idx >= len(pdf_map):
        return nb_idx, False
    v = pdf_map[nb_idx]
    if v is not None:
        return v, False
    for j in range(nb_idx - 1, -1, -1):
        if pdf_map[j] is not None:
            return pdf_map[j], True
    return 0, True


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


# --------------------------------------------------------------------------- #
# mark -> explain handoff
# --------------------------------------------------------------------------- #
# The marker writes its latest verdict per question here and the explainer reads
# it back. Today both channels share one resumed conversation, so this is
# belt-and-braces - but it survives what the session does not: a workbook switch
# wipes the session, and so does a restart. It is also the prerequisite for
# running the two as genuinely separate threads, since after this the explainer
# no longer needs the marker's transcript to know what was just marked.
MARK_DIR = os.path.join(HERE, ".rm_marks")
MARK_KEEP = 40                   # per workbook; oldest questions drop off


def mark_path(wb):
    return os.path.join(MARK_DIR, "%s.json" % wb)


def mark_key(pageno, verdict):
    """One slot per question: a drill/exercise label when the verdict names one
    (they open "D4(a) 3/5 - ..."), otherwise the page. Keying on the label means
    re-marking the same drill REPLACES the entry rather than piling up."""
    m = re.match(r"\s*([DE]\d+)", verdict or "")
    return "%s p%d" % (m.group(1), pageno) if m else "p%d" % pageno


def save_mark(wb, pageno, verdict, body):
    """Record the most recent mark for one question. Never raises - a failure
    here must not cost them the feedback itself."""
    try:
        os.makedirs(MARK_DIR, exist_ok=True)
        try:
            data = json.load(io.open(mark_path(wb), encoding="utf-8"))
        except Exception:
            data = {}
        data[mark_key(pageno, verdict)] = {
            "ts": time.strftime("%Y-%m-%d %H:%M"), "page": pageno,
            "verdict": (verdict or "").strip(),
            "body": (body or "").strip()[:1200],
        }
        if len(data) > MARK_KEEP:                  # drop the oldest by timestamp
            for k in sorted(data, key=lambda k: data[k]["ts"])[:len(data) - MARK_KEEP]:
                data.pop(k, None)
        with io.open(mark_path(wb), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1, ensure_ascii=False))
    except Exception as e:
        log("  !! could not save mark: %s" % e)


def load_marks(wb, pageno, limit=3):
    """What the marker last said about this page, newest first, for the
    explainer's prompt. Returns "" when there is nothing worth sending."""
    try:
        data = json.load(io.open(mark_path(wb), encoding="utf-8"))
    except Exception:
        return ""
    here = [(k, v) for k, v in data.items() if v.get("page") == pageno]
    if not here:
        return ""
    here.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    out = []
    for k, v in here[:limit]:
        out.append("[%s, %s] %s\n%s" % (k, v.get("ts", "?"),
                                        v.get("verdict", ""), v.get("body", "")))
    return ("\n--- HOW HIS WORK ON THIS PAGE WAS MARKED (most recent first) ---\n"
            + "\n\n".join(out)
            + "\n--- END ---\nThis is what the marker already told them. Build on "
              "it rather than repeating it, and do not contradict a mark without "
              "saying plainly that you are doing so.\n")


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


# Blue-channel agents can attach source images: "ATTACH: <path> p<N>" lines are
# stripped from the reply, the named PDF page (or image file) is rendered, and
# it rides to the phone as an extra picture - so "the workbook mentions an
# example it doesn't show" can be answered with the actual slide.
ATTACH_RE = re.compile(r"^\s*ATTACH:\s*(.+?)(?:\s+p(?:age)?\.?\s*(\d+))?\s*$", re.M)
ATTACH_MAX = 2


# The marker ticks the drill checklist with a hidden line, full marks only.
PROGRESS_RE = re.compile(r"^\s*PROGRESS:\s*([DE]\d{1,2})\s*complete\s*\.?\s*$",
                         re.I | re.M)


def split_progress(reply):
    """-> (reply without PROGRESS lines, [item labels now complete])"""
    labs = [m.group(1).upper() for m in PROGRESS_RE.finditer(reply)]
    return PROGRESS_RE.sub("", reply).strip(), labs


def split_attach(reply):
    """-> (reply without ATTACH lines, [(rel_path, page_or_None), ...])"""
    reqs = [(m.group(1).strip().strip('"'),
             int(m.group(2)) if m.group(2) else None)
            for m in ATTACH_RE.finditer(reply)]
    return ATTACH_RE.sub("", reply).strip(), reqs[:ATTACH_MAX]


def render_attachment(rel, pageno):
    """One PNG for an ATTACH request. Path must stay inside STUDY; PDFs are
    rendered at 2.5x, existing images pass through. Returns None on refusal."""
    p = os.path.normpath(os.path.join(STUDY, rel.replace("/", os.sep)))
    if not p.startswith(os.path.normpath(STUDY) + os.sep) or not os.path.exists(p):
        log("     ATTACH refused or missing: %s" % rel)
        return None
    ext = os.path.splitext(p)[1].lower()
    if ext in (".png", ".jpg", ".jpeg"):
        return p
    if ext == ".pdf":
        try:
            doc = pymupdf.open(p)
            i = min(max(1, pageno or 1), doc.page_count) - 1
            out = os.path.join(WORK_DIR, "attach_%s_p%d.png"
                               % (re.sub(r"\W+", "_", os.path.basename(p))[:40], i + 1))
            doc[i].get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5)).save(out)
            doc.close()
            return out
        except Exception as e:
            log("     ATTACH render failed (%s): %s" % (rel, str(e)[:80]))
    return None


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
QUESTION_DIRS = {"MODULE-A": ["MODULE-A/past-papers"],
                 "MODULE-B": ["MODULE-B/past-papers"]}
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
        # "3 (25 marks)" - some exam rebuilds head their QUESTIONS section
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
    # with-answers rebuilds, and any real-sitting export (the student's own wrong answers
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


def page_shot(pdf_path, page_idx, bbox=None, crop_only=False):
    """Clean high-res renders of the source page - the PDF's own pixels, no ink.
    For when the tablet mangles a question's display. Returns [region?, full],
    region first so the thing they asked about is the primary attachment.

    crop_only: the box IS the request (a drawn grey box), so return just the
    crop and skip the whole-page render. It also bypasses the 180px height
    test, because an explicit box around one line is still an explicit box."""
    doc = pymupdf.open(pdf_path)
    page = doc[page_idx]
    pw, ph = page.rect.width, page.rect.height
    s = ph / RM_DOC_HEIGHT
    shots = []
    if bbox:
        x0, y0, x1, y1 = bbox
        # taller than a written word = they circled a region, so crop to it
        if (y1 - y0) > 180 or crop_only:
            r = pymupdf.Rect(max(0, s * x0 + pw / 2 - 15), max(0, s * y0 - 15),
                             min(pw, s * x1 + pw / 2 + 15), min(ph, s * y1 + 15))
            p = os.path.join(WORK_DIR, "shot_p%d_region.png" % (page_idx + 1))
            page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=r).save(p)
            shots.append(p)
    if crop_only and shots:
        doc.close()
        return shots
    p = os.path.join(WORK_DIR, "shot_p%d_page.png" % (page_idx + 1))
    page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).save(p)
    shots.append(p)
    doc.close()
    return shots


# --------------------------------------------------------------------------- #
# dispatch + notify
# --------------------------------------------------------------------------- #
HEADER = u"[{workbook} | page {pageno} | {tag}] {colour} ink -> {action}"

PROMPT = u"""You are tutoring a student for the {module} module through a workbook.
Exam: {exam}. It is OPEN BOOK with internet access, no generative AI.
You will get several requests from this workbook in a row; keep the context.

--- WHAT YOU ALREADY KNOW ABOUT THE STUDENT (earlier sessions) ---
{memory}
--- END ---
Use this: chase patterns you have seen before, and say so when they repeat a
mistake you have already flagged. Do not re-teach things they have demonstrably got.
{marks}
{header}

Read this image - it shows what they circled, with their handwriting:
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
  scheme - the workbook's answer section must agree with what you tell them.
- If marking, give the mark as "n/m" and say precisely where marks were lost or
  would be lost in the exam. Be strict; a generous mark is worthless to them.
- Hold them to exam discipline: justify claims, show working, answer the question
  actually asked.
- Reply with the feedback ONLY - no preamble, no "here is my feedback". Under
  200 words. Plain text, no markdown headers. Write ALL mathematics as LaTeX
  between $...$ (inline) or $$...$$ (display) - their dashboard renders it
  properly; never use unicode maths symbols or ascii-art fractions.
- Start with a single line of the form: VERDICT: <short summary, max 8 words>
  (for marking include the mark, e.g. "VERDICT: D4(a) 3/5 - method right, arithmetic slip")
- After marking: if the drill or exercise you marked has NOW earned full marks
  on every part it contains, add one final line exactly: PROGRESS: <id> complete
  where <id> is its label, e.g. D4 or E2 (nothing else on the line). Partial
  marks or unmarked parts -> no line. It is stripped before they see the reply;
  it ticks their progress bar.
{memrule}"""

MEMRULE = u"""- Finally, if and only if you learned something durable about how the student works,
  add ONE last line: MEMORY: <observation>. Record PATTERNS, not events -
  "drops nodes from the frontier when tracing by hand" or "solid on admissibility
  arguments", never "scored 2/5 on D3". A pattern is still true next week; a score
  is not. Skip the line entirely if nothing durable came up - most requests should
  not produce one. This line is stripped before they see the reply.
"""

# Only the blue channel gets this (appended to {memrule} for explain/tutor/
# deep): it may deliberately override the "no other tool call" default when a
# real slide is worth showing.
ATTACH_RULE = u"""- If SEEING the actual source would genuinely help - the workbook names an
  example or figure it does not show, or a diagram carries the idea - you MAY
  use your file tools to locate the source PDF, then add one line per image at
  the VERY END of your reply:  ATTACH: <path relative to the study root> p<N>
  e.g. "ATTACH: MODULE-A/pdf/lectures/week-06-lighting.pdf p14".
  Lecture markdown files carry their exact source PDF path in a "> Source:"
  line near the top, and note slide numbers as they go. At most 2. The page is
  rendered and sent to the student's phone with your reply; mention in the text
  what each attached image shows. Never guess a path - only name a file you
  have confirmed exists. These lines are stripped before the student sees the reply.
"""

TASKS = {
    "mark": "Mark what they have written inside or beside the red circle.",
    "explain": ("Explain what is circled in blue. If there is blue handwriting, "
                "treat it as their question and answer that specifically. Explain it "
                "DIFFERENTLY from how the workbook words it, and end with one short "
                "check question."),
}

# Grey "tutor" swaps the explain task for this. The standard rules still arrive
# with every prompt, so the override has to name which of them stop applying -
# otherwise the 200-word cap and the circled-work-only rule quietly win.
TUTOR_TASK = u"""FULL-TUTOR MODE. This is a question to the student's personal tutor, and the following
overrides the standard rules where they conflict:
- The blue handwriting is the question; the circle only anchors where they are.
  The question may be about ANYTHING - this module, exam strategy, their past
  performance - so answer the question actually asked, not the circled drill.
- You have Read, Grep and Glob over the whole study tree. Start from:
    {module_root}/md/lectures/week-*/   the lectures as markdown
    {module_root}/md/practice/          every practice paper and answer set
    {module_root}/workbooks/tutor-*.md  drill answers + marking logic
  Go and read what the question needs; never answer a question about their own
  past work without opening the file that holds it.
- CAUTION: if the corpus holds a real first-sitting export, its recorded
  answers are preserved MISTAKES by design. Judge them against the
  tutor-derived blocks only; never quote one as a model answer.
- The 200-word cap is lifted: up to 600 words when the question deserves it.
- Everything else stands: strict standards, exam discipline, say it differently
  from the workbook, end with one check question, and the first line is still
  "VERDICT: <max 8 words>"."""

DEEP_PROMPT = u"""You are a student's deep-dive tutor for the {module} module (exam {exam}).
They have enabled DEEP explain because they are
STRUGGLING with something complex and wants as much perspective as possible. Take your
time - depth is the point.

{header}

The image shows what they circled in blue (blue handwriting = their own question): {image}

--- PRINTED TEXT OF THE PAGE ---
{ptext}

--- TUTOR-BRIEF SECTIONS FOR THIS PAGE ---
{excerpt}
--- END ---

RESEARCH FIRST - you have Read, Grep and Glob. The converted corpus:
  {module_root}/md/lectures/week-*/        the lecturer's own slides, one .md per lecture
  {module_root}/md/practice/               every practice paper and answer set
  {module_root}/workbooks/tutor-*.md       drill answers and marking logic
Chase the circled concept through it: the lecture that introduces it, every worked
example of it anywhere, every past-paper appearance, and what the marking logic
actually rewards. Read what you find, not just the excerpt above.

Then write the explanation, structured as:
1. The core idea in one short paragraph, in DIFFERENT words from the workbook.
2. Two or three genuinely different angles - an analogy, a worked micro-example with
   fresh small numbers, or the design problem this idea was invented to solve.
3. Where this shows up in HIS exam and what the marks are actually awarded for.
4. The misconception most likely causing their confusion, named plainly.
5. One check question.

Under 600 words, plain text, no markdown headers. Write all mathematics as
LaTeX between $...$ or $$...$$ - their dashboard renders it.
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


# Dashboard STOP button. Jobs run one at a time on the main loop, so a single
# event + a single tracked Popen is all the machinery needed: the web thread
# sets the flag and kills the process; the main loop sees Stopped, marks the
# trigger as answered (i.e. the prompt is IGNORED, not retried) and moves on.
class Stopped(Exception):
    """The dashboard STOP button aborted this job - not an error."""


STOP_EVT = threading.Event()
_LIVE_PROC = {"p": None}
_LIVE_LOCK = threading.Lock()


def request_stop():
    """-> True if a job was running (flag set + agent killed), else False."""
    with WEB.lock:
        busy = bool(WEB.jobs)
    if not busy:
        return False
    STOP_EVT.set()
    with _LIVE_LOCK:
        p = _LIVE_PROC["p"]
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    # claude is a .cmd shim whose node child survives a bare
                    # kill() and keeps the stdout pipe open - kill the tree
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    p.kill()
            except Exception:
                pass
    return True


def lane_key(wb_key, kind):
    """One conversation per workbook PER CHANNEL. Mark and explain used to share
    a session, which is why they could never run at the same time: two concurrent
    `claude --resume <same id>` would race the transcript. Splitting them is what
    lets the lanes run concurrently - the explainer no longer needs the marker's
    transcript, because it reads the marks back from .rm_marks (see save_mark)."""
    return "%s|%s" % (wb_key, kind)


def session_args(state, wb_key, skey):
    """One live conversation per (workbook, channel) - `skey` from lane_key().

    Switching workbook ends the previous conversation (the user's stated preference),
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
        state["sess_n"] = {}
        state["last_page"] = {}
    sid = state.setdefault("sessions", {}).get(skey)
    if sid:
        return ["--resume", sid], False
    sid = str(_uuid.uuid4())
    state["sessions"][skey] = sid
    return ["--session-id", sid], True


def cmd_session_args(state):
    """Persistent conversation for the grey channel, mirroring the workbook
    sessions: resuming keeps the CLI's prompt cache warm so transcriptions
    stop paying the cold-start price on every command. Rotated every 12
    commands so the accumulated page images never bloat the context.
    Returns (cli_args, is_new_session)."""
    import uuid as _uuid
    sid = state.get("cmd_session")
    if not sid or state.get("cmd_n", 0) >= 12:
        sid = str(_uuid.uuid4())
        state["cmd_session"] = sid
        state["cmd_n"] = 0
        return ["--session-id", sid], True
    return ["--resume", sid], False


def run_claude(prompt, model, effort=EFFORT_DEFAULT, tools="Read Grep",
               timeout=CLAUDE_TIMEOUT, extra=None):
    # The prompt goes in on STDIN, not as an argument. claude is a .cmd shim, so
    # argv passes through cmd.exe and hits the 8191-char Windows command-line
    # limit - which anything with an inlined brief excerpt comfortably exceeds.
    # json output wraps the reply with exact token usage - the dashboard's
    # session panel is fed from it; the text itself is in the "result" field
    cmd = [claude_exe(), "-p", "--output-format", "json",
           "--model", model, "--effort", effort]
    # Headless agents may only read below their cwd (here: workbooks\). Grant
    # the whole Study tree, or "feedback on my exam work" gets a file-access
    # denial - the tools are read-only, so this is safe.
    cmd += ["--add-dir", STUDY]
    if tools:
        cmd += ["--allowedTools", tools]
    if extra:
        cmd += list(extra)
    if STOP_EVT.is_set():        # stop pressed while the page was still rendering
        raise Stopped()
    t0 = time.time()
    # Popen rather than run() so the dashboard STOP button can kill the
    # in-flight agent from the web thread.
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace")
    with _LIVE_LOCK:
        _LIVE_PROC["p"] = p
    try:
        out_s, err_s = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=10)
        except Exception:
            pass
        # the default message dumps the entire command line, which is useless
        # in a phone notification
        raise RuntimeError("timed out after %ds (%s/%s)" % (timeout, model, effort))
    finally:
        with _LIVE_LOCK:
            _LIVE_PROC["p"] = None
    if STOP_EVT.is_set():
        raise Stopped()
    out = (out_s or "").strip()
    log("     (%s/%s, %.1fs)" % (model, effort, time.time() - t0))
    if p.returncode != 0 or not out:
        msg = (err_s or out or "no output").strip()[:300]
        if "authenticate" in msg.lower() or "oauth" in msg.lower():
            msg += "  ->  run `claude` in a terminal and sign in, then retry"
        raise RuntimeError(msg)
    try:
        j = json.loads(out)
    except ValueError:
        j = None
    if isinstance(j, dict) and "result" in j:
        WEB.add_tokens(j.get("usage") or {}, j.get("total_cost_usd"))
        out = (j["result"] or "").strip()
        if j.get("is_error") or not out:
            raise RuntimeError((out or "agent returned an error")[:300])
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

They have moved to a new page in the same workbook. Image: {image}

--- PRINTED TEXT OF PAGE {pageno} ---
{ptext}

--- TUTOR-BRIEF SECTIONS FOR THIS PAGE ---
{excerpt}
--- END ---

Task: {task}
Same rules as before. Under 200 words, first line "VERDICT: ...".
{memrule}"""


# A workbook conversation is resumed request after request, and every request
# adds a page image - left alone it grows for the whole sitting. After this
# many requests it is compacted: the old conversation writes a handover
# (final outcomes only, but the student's errors along the way are KEPT), then a fresh
# session starts with that handover in its opening prompt.
COMPACT_AFTER = 10

COMPACT_PROMPT = u"""This tutoring conversation is being compacted; a fresh one
takes over from your handover. Write it now, 150 words maximum, plain text, no
preamble:
1. Each drill/exercise touched, with its FINAL state only (latest mark or
   explanation outcome). Where something was marked or re-explained several
   times, keep only the last result - drop the superseded attempts.
2. The specific errors the student made along the way, kept even where they later
   fixed them - these are the most valuable part of the handover.
3. Any standing instructions or preferences stated during the conversation."""


def compact_session(state, skey):
    """-> handover text (or None). Ends the workbook's current conversation
    either way; the caller's next session_args() starts a fresh one."""
    sid = state.get("sessions", {}).get(skey)
    summary = None
    if sid:
        try:
            summary = run_claude(COMPACT_PROMPT, "sonnet", "low", tools="Read",
                                 timeout=120, extra=["--resume", sid])
        except Exception as e:
            log("     compact failed (%s) - rotating without handover"
                % str(e)[:60])
    state.get("sessions", {}).pop(skey, None)
    state.get("sess_n", {}).pop(skey, None)
    return summary


def ask_claude(ctx, model, effort, state, wb_key, dry=False):
    # session bookkeeping is per (workbook, channel) so mark and explain can run
    # side by side without sharing - and corrupting - one transcript
    skey = lane_key(wb_key, ctx["kind"])
    n = state.setdefault("sess_n", {}).get(skey, 0)
    if (not dry and n >= COMPACT_AFTER
            and state.get("sessions", {}).get(skey)):
        log("     compacting %s conversation after %d requests" % (wb_key, n))
        handover = compact_session(state, skey)
        if handover:
            ctx["memory"] = (ctx["memory"] +
                "\n\n--- HANDOVER from your previous conversation on this "
                "workbook (compacted: final outcomes; their errors along the "
                "way are kept deliberately) ---\n" + handover)
    args, is_new = session_args(state, wb_key, skey)
    same_page = (not is_new) and \
        state.get("last_page", {}).get(skey) == ctx["pageno"]

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
            state["sessions"].pop(skey, None)
            state.get("sess_n", {}).pop(skey, None)
            args, _ = session_args(state, wb_key, skey)
            prompt = PROMPT.format(task=task, **ctx)
            out = run_claude(prompt, model, effort, tools="Read Grep Glob", extra=args)
        else:
            raise
    state.setdefault("last_page", {})[skey] = ctx["pageno"]
    state.setdefault("sess_n", {})[skey] = \
        state.get("sess_n", {}).get(skey, 0) + 1
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
    if re.search(r"\bguide\b|\bhow (?:do|to) i\b|\bgetting started\b", t):
        return "guide", None
    if re.search(r"\bhelp\b|\bcommands?\b|\bwhat can you do\b", t):
        return "help", None
    if re.search(r"\brestart\b|\breboot\b|\breload\b|\breset\b", t):
        return "restart", None
    if re.search(r"\bprogress\b", t):
        return "progress", None
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
    # patience mode: 'wait' holds coloured ink, 'begin' fires everything held
    if re.search(r"\bwait\b|\bhold\b|\bpatience\b", t):
        return "wait", None
    if re.search(r"\bbegin\b|\bexecute\b|\bfire\b", t):
        return "begin", None
    # "start timer 25" / "timer 90" - any number of minutes
    m = re.search(r"\btimer\b\D{0,12}(\d{1,3})", t)
    if m:
        return "timer", int(m.group(1))
    if re.search(r"\bwhite\s*rabbit\b", t):
        return "rabbit", None
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
'screenshot q<N>' - Question N (any number) from the default paper
'screenshot q1 real' / 'q3 game theory' / 'q1 formative' - name the paper
'screenshot from the exam paper' - infers the question from the page you're on
'screenshot' alone + a grey circle - renders the circled region of THIS page
a grey BOX around anything, no words needed - sends just that crop
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
anything in the study tree (lectures, past papers, your real exam) and
answers at length - not limited to the circled drill. Stays in the same
conversation, so follow-ups build on it. 'tutor off' - quick explains.
Blue answers can also ATTACH images - ask e.g. 'show me the slide with
the worked example' in blue and the lecture page arrives as a PNG.

LONG PROMPTS (patience mode)
'wait' - hold fire: coloured ink is queued, not answered, while you keep
writing - across pages if you like. Grey commands still work.
'begin' - execute everything added since 'wait'.

PROGRESS + DASHBOARD
'update progress' - audits this workbook's ink against the marking notes
and rebuilds its DRILL checklist (.rm_progress/<wb>.md). A drill counts
only at FULL marks; exam/mock questions never count. After the first
audit the marker ticks drills automatically as they reach full marks.
Dashboard: http://localhost:8477 on the machine running the watcher -
live feed, working lights, workbook bar (hover it for module totals).

SYSTEM
'start timer 25' - countdown (any minutes) at the top of the dashboard
'guide' - the 60-second how-to
'status' - models, deep on/off, requests answered, active workbook
'restart' - relaunch the watcher with the latest code
'help' - this list"""

GUIDE_TEXT = u"""HOW TO USE THIS - the 60-second guide

1. Work in BLACK in the workbook, exactly as on paper.
2. Finished a drill? Circle your answer in RED and turn the page (the
   page-turn makes the tablet save the ink). ~30s later your phone gets
   the mark and exactly where marks were lost. Erase the circle once read.
3. Stuck? Circle the drill in BLUE for a fresh explanation - or WRITE a
   question in blue and it gets answered directly. Write 'tutor' in grey
   first and blue questions can be about anything, at length.
4. GREY steers the machine - 'help' lists every command.
5. Full-mark drills tick the progress bars by themselves. The dashboard
   (http://localhost:8477 on the watcher machine) shows replies with
   proper maths, live statuses, and progress.

Black to work - red to be marked - blue to ask - grey to steer."""


def handle_command(crop_png, state, dry=False, page=None, doc=None):
    """Grey ink is a control channel, not a question. Always read with the
    fastest model so switching away from a slow one is itself quick.
    `page` = (pdf_path, page_idx, grey_bbox) so screenshot requests can render
    from the source PDF. Screenshots are handed back via state["_shots"].
    `doc` = (doc_uuid, page_order, wb_key, pdf_path, brief_path) for the
    progress scan."""
    if dry:
        log("DRY-RUN command read of %s" % crop_png)
        return "(dry run)", "cmd"
    args, fresh = cmd_session_args(state)
    try:
        raw = run_claude(CMD_PROMPT.format(png=crop_png), COMMAND_MODEL,
                         COMMAND_EFFORT, tools="Read", timeout=120, extra=args)
    except RuntimeError as e:
        # a resume can fail if the transcript was cleaned up - start over once
        if not fresh and "resume" in str(e).lower():
            state.pop("cmd_session", None)
            args, _ = cmd_session_args(state)
            raw = run_claude(CMD_PROMPT.format(png=crop_png), COMMAND_MODEL,
                             COMMAND_EFFORT, tools="Read", timeout=120,
                             extra=args)
        else:
            raise
    state["cmd_n"] = state.get("cmd_n", 0) + 1
    # the transcription is normally the whole reply; if the model added chatter,
    # the shortest non-empty line is almost always the transcription itself
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    said = min(lines, key=len) if lines else ""
    kind, arg = classify(said)
    # A drawn BOX with nothing legible in it is grey used as a framing gesture,
    # not as writing: crop what was boxed and send that. Gated on "unknown" so
    # every real command still wins, and on a size a scrawled word cannot reach.
    if kind == "unknown" and page and page[2]:
        bw, bh = page[2][2] - page[2][0], page[2][3] - page[2][1]
        if bw > 160 and bh > 80:
            kind, arg = "crop", None
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
        web_profiles(state)
        return ("%s %s -> %s\n\nNow: %s\n\nScope it by saying 'explain opus' or "
                "'mark haiku'; unscoped changes both."
                % (where, field, value, describe())), "%s %s=%s" % (where, field, value)
    if kind == "get-model":
        return (describe() + "\n\nIn grey: 'model opus', 'explain opus', "
                "'mark sonnet', 'effort high', 'think harder', 'faster'."), describe()
    if kind == "status":
        return ("%s\nDEEP explain: %s. Full tutor: %s. Patience (wait): %s. "
                "%d requests answered. Watching %d pages. Active workbook: %s."
                % (describe(), "ON" if state.get("deep") else "off",
                   "ON" if state.get("tutor") else "off",
                   "HOLDING" if state.get("waiting") else "off",
                   len(state.get("answered", [])),
                   len(state.get("mtimes", {})),
                   state.get("active_wb") or "none")), "status"
    if kind == "timer":
        mins = max(1, min(600, arg))
        WEB.set_timer(mins)
        return ("Timer started: %d minute%s. Counting down at the top of the "
                "dashboard - when it hits zero you choose continue or break."
                % (mins, "" if mins == 1 else "s")), "timer %dm" % mins
    if kind == "rabbit":
        WEB.set_rabbit()
        return "Knock, knock, Neo.", "..."
    if kind == "wait":
        state["waiting"] = True
        state["wait_seen"] = []
        return ("PATIENCE ON. Coloured ink is held from here - write and edit "
                "across as many pages as you like. Nothing fires until you "
                "write 'begin' in grey; then everything added since this "
                "'wait' is executed."), "waiting for 'begin'"
    if kind == "begin":
        was = state.pop("waiting", False)
        held = len(state.pop("wait_seen", []) or [])
        if not was:
            return ("'wait' was not active - nothing was being held. Coloured "
                    "ink fires as normal."), "not waiting"
        return ("GO. Patience off - %s."
                % (("firing the %d held trigger%s now"
                    % (held, "" if held == 1 else "s")) if held
                   else "no held ink found; new circles fire as normal")), \
               "begin - %d held" % held
    if kind == "guide":
        return GUIDE_TEXT, "the 60-second guide"
    if kind == "help":
        return HELP_TEXT, "command list"
    if kind == "crop":
        if not page:
            return "No page context - draw the box on a workbook page.", "crop failed"
        state["_shots"] = page_shot(page[0], page[1], page[2], crop_only=True)
        # no prose - the crop IS the answer, and the dashboard opens it from the
        # card itself rather than expanding one, so the title is the instruction
        return "", "Click to open screenshot"
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
            module = (page or (None, None, None, next(iter(QUESTION_DIRS), "")))[3]
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
    if kind == "progress":
        lines, mline = progress_scan_all(state, dry=dry)
        return ("MODULES:  %s\n\n%s\n\nDrills and mid-book exercises count; "
                "untimed extras and exam/mock questions never do. A drill "
                "ticks only at FULL marks. Checklists: .rm_progress/<wb>.md - "
                "the marker updates them automatically from here on."
                % (mline, "\n".join(lines))), \
               ("progress: %s" % mline)[:120]
    if kind == "restart":
        # actual respawn happens in the main loop AFTER this trigger is recorded
        # as answered and state is saved - otherwise the new process re-fires on
        # the same grey ink and restarts itself forever
        state["_restart"] = True
        return ("Restarting the watcher now. Back in ~15 s with the current code. "
                "Conversation and model defaults reset; memory file kept."), "restarting"
    if kind == "deep":
        state["deep"] = (arg == "on")
        web_profiles(state)
        if arg == "on":
            return ("DEEP explain enabled. Blue circles now go to a standalone %s "
                    "agent at %s effort that reads the whole converted corpus - "
                    "expect several minutes per answer. Red marking is unaffected. "
                    "Write 'deep off' in grey to return to quick explains."
                    % (DEEP_MODEL, DEEP_EFFORT)), "DEEP explain enabled"
        state["deep"] = False
        return ("DEEP explain off - blue circles back to %s/%s quick explains."
                % profile(state, "explain")), "DEEP explain off"
    if kind == "tutor":
        state["tutor"] = (arg == "on")
        web_profiles(state)
        if arg == "on":
            return ("FULL TUTOR enabled (%s/%s). Blue handwriting is now a question "
                    "to your tutor: it reads whatever the question needs from the "
                    "study tree - lectures, practice papers, your real first "
                    "sitting - and answers at length instead of only explaining "
                    "the circled drill. Red marking is unchanged. 'tutor off' "
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
    if not NTFY_ENABLED:
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
# local dashboard (read-only mirror; the ink stays the only control surface)
# --------------------------------------------------------------------------- #
DASH_PATH = os.path.join(HERE, "rm_dashboard.html")

# --- home-screen app assets ------------------------------------------------
# The dashboard is plain HTTP on the LAN, so there is no service worker and no
# web push (iOS requires HTTPS for both). What these DO buy is "Add to Home
# Screen": an icon, a name, and a standalone chrome-less window. Icons are
# drawn once at import and held in memory - no files on disk, so nothing here
# for OneDrive to sync or conflict over.
APP_NAME = "Study Watcher"


def _app_icon(size):
    """The red marking circle on near-black: the tool's one gesture, and still
    legible at 60px on a home screen. Drawn at 4x and downsampled because PIL
    has no antialiased ellipse stroke."""
    s = size * 4
    img = Image.new("RGB", (s, s), (28, 28, 26))
    d = ImageDraw.Draw(img)
    pad, w = int(s * 0.20), max(1, int(s * 0.075))
    d.ellipse([pad, pad, s - pad, s - pad], outline=(255, 30, 70), width=w)
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


APP_ICONS = {"/icon-%d.png" % n: _app_icon(n) for n in (180, 192, 512)}

WEB_MANIFEST = {
    "name": APP_NAME, "short_name": "Study",
    "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#1c1c1a", "theme_color": "#1c1c1a",
    "icons": [{"src": "/icon-%d.png" % n, "sizes": "%dx%d" % (n, n),
               "type": "image/png", "purpose": "any maskable"}
              for n in (192, 512)],
}


class WebState:
    """Everything the dashboard shows. Content of grey commands is never
    stored here - jobs carry only their kind, so the page can show a
    '(working)' tag without seeing what was written."""

    def __init__(self):
        self.lock = threading.Lock()
        self.online, self.host = False, None
        self.active_wb = None
        self.jobs = {}
        self.responses = []
        self.progress = {}
        self.modules = {}
        self.profiles = {}
        self.times = {}          # kind -> [secs...]; first = cache warm-up
        self.session = {"started": None, "drills": 0, "tok_in": 0,
                        "tok_out": 0, "tok_cache": 0, "cost": 0.0}
        self.timer = None        # {"mins", "ts", "n"} - grey "start timer N"
        self.rabbit = None       # {"n"} - they know what they wrote
        self._next = 1

    def set_timer(self, mins):
        with self.lock:
            self.timer = {"mins": mins, "ts": time.time(), "n": self._next}
            self._next += 1

    def set_rabbit(self):
        with self.lock:
            self.rabbit = {"n": self._next, "ts": time.time()}
            self._next += 1

    def set_session_start(self):
        with self.lock:
            self.session["started"] = time.strftime("%a %H:%M")

    def add_drills(self, n):
        with self.lock:
            self.session["drills"] += n

    def add_tokens(self, usage, cost=None):
        with self.lock:
            s = self.session
            s["tok_in"] += (int(usage.get("input_tokens") or 0)
                            + int(usage.get("cache_creation_input_tokens") or 0))
            s["tok_cache"] += int(usage.get("cache_read_input_tokens") or 0)
            s["tok_out"] += int(usage.get("output_tokens") or 0)
            if cost:
                s["cost"] = round(s["cost"] + float(cost), 4)

    def set_online(self, ok, host=None):
        with self.lock:
            self.online, self.host = ok, host

    def set_wb(self, wb):
        with self.lock:
            self.active_wb = wb

    def job_start(self, kind, wb=None, page=None):
        with self.lock:
            jid = self._next
            self._next += 1
            self.jobs[jid] = {"kind": kind, "wb": wb, "page": page}
            return jid

    def job_end(self, jid):
        with self.lock:
            self.jobs.pop(jid, None)

    def remove_response(self, rid):
        with self.lock:
            self.responses = [r for r in self.responses if r["id"] != rid]

    def add_response(self, kind, wb, page, title, body, images=()):
        import shutil
        with self.lock:
            rid = self._next
            self._next += 1
        webimgs = []
        for i, img in enumerate([p for p in images if p and os.path.exists(p)]):
            name = "web_%d_%d.png" % (rid, i)
            try:
                shutil.copyfile(img, os.path.join(WORK_DIR, name))
                webimgs.append(name)
            except Exception:
                pass
        with self.lock:
            self.responses.append({"id": rid, "ts": time.strftime("%H:%M"),
                                   "kind": kind, "wb": wb, "page": page,
                                   "title": title, "body": body,
                                   "images": webimgs})
            del self.responses[:-80]

    def set_progress(self, wb, info):
        with self.lock:
            self.progress[wb] = info

    def set_modules(self, agg):
        with self.lock:
            self.modules = agg

    def set_profiles(self, prof):
        with self.lock:
            self.profiles = prof

    def add_time(self, kind, secs):
        with self.lock:
            self.times.setdefault(kind, []).append(round(secs, 1))

    def snapshot(self):
        with self.lock:
            times = {k: {"n": len(v) - 1,
                         "avg": (round(sum(v[1:]) / (len(v) - 1), 1)
                                 if len(v) > 1 else None)}
                     for k, v in self.times.items()}
            return {"online": self.online, "host": self.host,
                    "active_wb": self.active_wb,
                    "jobs": [dict(v) for v in self.jobs.values()],
                    "progress": dict(self.progress),
                    "modules": dict(self.modules),
                    "profiles": dict(self.profiles),
                    "times": times,
                    "session": dict(self.session),
                    "timer": dict(self.timer) if self.timer else None,
                    "rabbit": dict(self.rabbit) if self.rabbit else None,
                    "responses": list(self.responses)}


WEB = WebState()


def web_profiles(state):
    """Push the current per-channel model setup (and modes) to the dashboard."""
    mm, me = profile(state, "mark")
    em, ee = profile(state, "explain")
    WEB.set_profiles({"mark": "%s/%s" % (mm, me),
                      "explain": "%s/%s" % (em, ee),
                      "command": "%s/%s" % (COMMAND_MODEL, COMMAND_EFFORT),
                      "deep": bool(state.get("deep")),
                      "tutor": bool(state.get("tutor"))})


def start_web():
    import http.server
    import urllib.parse

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # cache: /state must never be cached, but the PNGs are written once
        # under a unique name (web_<rid>_<n>.png) and never rewritten, so they
        # can be cached hard. Without this every feed rebuild re-downloaded
        # every image, which is why they visibly reloaded on unrelated clicks.
        IMMUTABLE = "public, max-age=31536000, immutable"

        def _send(self, code, ctype, data, cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                path = urllib.parse.urlparse(self.path).path
                if path in ("/", "/index.html"):
                    with open(DASH_PATH, "rb") as fh:
                        self._send(200, "text/html; charset=utf-8", fh.read())
                elif path == "/state":
                    self._send(200, "application/json",
                               json.dumps(WEB.snapshot()).encode("utf-8"))
                elif path == "/del":
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlparse(self.path).query)
                    try:
                        WEB.remove_response(int(q.get("id", ["0"])[0]))
                    except Exception:
                        pass
                    self._send(200, "application/json", b'{"ok": true}')
                elif path == "/stop":
                    self._send(200, "application/json",
                               json.dumps({"stopped": request_stop()})
                               .encode("utf-8"))
                elif path == "/manifest.webmanifest":
                    self._send(200, "application/manifest+json",
                               json.dumps(WEB_MANIFEST).encode("utf-8"))
                elif path in APP_ICONS:
                    self._send(200, "image/png", APP_ICONS[path],
                               cache=self.IMMUTABLE)
                elif path.startswith("/img/"):
                    name = os.path.basename(path[5:])
                    p = os.path.join(WORK_DIR, name)
                    if name.endswith(".png") and os.path.exists(p):
                        with open(p, "rb") as fh:
                            self._send(200, "image/png", fh.read(),
                                       cache=self.IMMUTABLE)
                    else:
                        self._send(404, "text/plain", b"not found")
                else:
                    self._send(404, "text/plain", b"not found")
            except Exception:
                pass

    try:
        srv = http.server.ThreadingHTTPServer((WEB_HOST, WEB_PORT), H)
    except OSError as e:
        log("dashboard NOT started (%s) - is port %d in use?" % (e, WEB_PORT))
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("dashboard at http://%s:%d"
        % ("localhost" if WEB_HOST == "127.0.0.1" else WEB_HOST, WEB_PORT))


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

    name, order, pdf_map = doc_info(doc_uuid)
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
    # nb_idx is where the page sits ON THE TABLET; idx is the page it shows in
    # the SOURCE PDF. They diverge as soon as a notes page is inserted, and
    # everything downstream - render, page text, brief excerpt, crops, question
    # lookup - must use the PDF page or it answers about the wrong question.
    save_pagemap(key, order, pdf_map)
    nb_idx = order.index(page_uuid) if page_uuid in order else 0
    idx, on_notes = pdf_page_for(pdf_map, nb_idx)
    if idx != nb_idx or on_notes:
        log("  page map: tablet p.%d -> pdf p.%d%s (%d notes page(s) inserted)"
            % (nb_idx + 1, idx + 1, " [notes page]" if on_notes else "",
               len([v for v in pdf_map if v is None])))
    module = pdf_rel.split("/")[0]
    exam = EXAM_DATES.get(module, "not configured")

    done = failed = deferred = 0
    # greys first: a 'begin' on this page must release the page's own held ink
    # in the same pass, not one poll later
    trg.sort(key=lambda g: g["kind"] != "command")

    # Patience mode is settled BEFORE anything starts, so held ink never lights
    # a chip. Held ink counts as "failed" below so its page stays un-seen and
    # refires after 'begin'.
    runnable = []
    for g in trg:
        if state.get("waiting") and g["kind"] != "command":
            if g["hash"] not in state.setdefault("wait_seen", []):
                state["wait_seen"].append(g["hash"])
                log("  %s trigger on %s p.%d HELD (grey 'wait' active)"
                    % (g["colour"], name, idx + 1))
            deferred += 1
            continue
        runnable.append(g)
    if not runnable:
        return 0, deferred

    # Every chip lights on DETECTION, not when its agent gets a turn: all the
    # jobs are registered here, before any of them runs.
    WEB.set_wb(key)
    STOP_EVT.clear()              # one clean stop flag for this whole batch
    for g in runnable:
        log("  %s trigger on %s p.%d (hash %s)"
            % (g["colour"], name, idx + 1, g["hash"]))
        g["_jid"] = WEB.job_start(g["kind"], key, idx + 1)
        notify("%s - %s p.%d - working..." % (g["kind"].upper(), name, idx + 1),
               "Seen your %s circle. Working on it." % g["colour"].lower(),
               priority="low", tags="eyes", dry=dry)

    def run_one(g):
        """One trigger, start to finish. -> "done" | "failed". Runs on its
        lane's thread, so everything it touches in `state` must belong to that
        lane (sessions and sess_n are per-lane; `answered` is a list append,
        which is atomic)."""
        jid = g["_jid"]
        full, crop = render(pdf_path, idx, strokes, crop=g["bbox"])
        # the trigger hash goes into the filename: resumed conversations have
        # earlier captures in context, and a REUSED path would let the model
        # "transcribe" the old image from memory instead of re-reading it
        full_png = os.path.join(WORK_DIR, "%s_p%d_%s_full.png"
                                % (name, idx + 1, g["hash"][:6]))
        full.save(full_png)
        crop_png = None
        if crop is not None:
            crop_png = os.path.join(WORK_DIR, "%s_p%d_%s_crop.png"
                                    % (name, idx + 1, g["hash"][:6]))
            crop.save(crop_png)

        # grey = control channel; no marking, no brief, always the fast model
        if g["kind"] == "command":
            t0 = time.time()
            try:
                body, short = handle_command(crop_png or full_png, state, dry=dry,
                                             page=(pdf_path, idx, g["bbox"], module),
                                             doc=(doc_uuid, order, key, pdf_path,
                                                  brief_path))
            except Stopped:
                log("  .. STOPPED from the dashboard - trigger ignored")
                WEB.job_end(jid)
                STOP_EVT.clear()
                state["answered"].append(g["hash"])
                return "done"
            except Exception as e:
                log("  !! command failed: %s" % e)
                notify("CMD ERROR", str(e)[:400], priority="high",
                       tags="warning", dry=dry)
                WEB.job_end(jid)
                return "failed"
            shots = state.pop("_shots", [])       # transient - never persisted
            notify("CMD - %s" % short, body, tags="gear", dry=dry,
                   image=(shots[0] if shots else None))
            for extra in shots[1:]:
                notify("CMD - %s" % short, "full page for context",
                       image=extra, dry=dry)
            WEB.job_end(jid)
            WEB.add_time("command", time.time() - t0)
            WEB.add_response("command", key, idx + 1, short, body, shots)
            state["answered"].append(g["hash"])
            return "done"

        ptext = page_text(pdf_path, idx)
        ctx = {"module": module, "exam": exam, "workbook": key, "pageno": idx + 1,
               "colour": g["colour"].lower(), "action": g["kind"],
               "kind": g["kind"], "image": (crop_png or full_png),
               "ptext": ptext, "excerpt": brief_excerpt(brief_path, ptext),
               "tag": location_tag(ptext),
               "memrule": MEMRULE + (ATTACH_RULE if g["kind"] == "explain" else ""),
               "memory": load_memory(module) or "(nothing recorded yet)",
               # only the explainer needs it; the marker is the one writing it
               "marks": (load_marks(key, idx + 1)
                         if g["kind"] == "explain" else "")}
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
        t0 = time.time()
        try:
            if deep:
                dctx = dict(ctx, module_root=os.path.join(STUDY, module))
                prompt = DEEP_PROMPT.format(**dctx)
                log("     DEEP dispatch (%s/%s, timeout %ds)"
                    % (DEEP_MODEL, DEEP_EFFORT, DEEP_TIMEOUT))
                reply = ("VERDICT: dry run\n(no model called)" if dry else
                         run_claude(prompt, DEEP_MODEL, DEEP_EFFORT,
                                    tools=DEEP_TOOLS, timeout=DEEP_TIMEOUT))
            else:
                reply = ask_claude(ctx, mdl, eff, state, key, dry=dry)
        except Stopped:
            # deliberate abort: the circle is marked answered so the prompt is
            # ignored for good, not retried on the next poll
            log("  .. STOPPED from the dashboard - trigger ignored")
            WEB.job_end(jid)
            STOP_EVT.clear()
            state["answered"].append(g["hash"])
            return "done"
        except Exception as e:
            log("  !! claude failed: %s" % e)
            log("     -> leaving this page pending; it will retry each poll")
            notify("ERROR - %s p.%d" % (name, idx + 1), str(e)[:400],
                   priority="high", tags="warning", dry=dry)
            WEB.job_end(jid)
            return "failed"

        if not deep:                      # deep dives would skew the average
            WEB.add_time(g["kind"], time.time() - t0)
        reply, note = split_memory(reply)
        if note:
            append_memory(module, note, key, idx + 1)
        attach_reqs = []
        if g["kind"] == "explain":
            reply, attach_reqs = split_attach(reply)
        if g["kind"] == "mark":
            reply, done_drills = split_progress(reply)
            if not done_drills:
                # safety net: models occasionally forget the hidden PROGRESS
                # line. A verdict of the form "D1 7/7" - whole drill, no part
                # suffix, full marks - is just as explicit, so tick from it.
                mv0 = re.search(r"^\s*VERDICT\s*:\s*([DE]\d{1,2})\s+"
                                r"(\d+)\s*/\s*(\d+)", reply, re.M)
                if (mv0 and mv0.group(2) == mv0.group(3)
                        and int(mv0.group(2)) > 0):
                    done_drills = [mv0.group(1).upper()]
                    log("     progress: ticking %s from full-mark verdict"
                        % done_drills[0])
            if done_drills:
                info = prog_tick(state, key, done_drills, pdf_path)
                refresh_modules()
                log("     progress: +%s -> %d%%"
                    % (",".join(done_drills), info["pct"]))
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
        # hand this verdict to the explain channel (see save_mark)
        if g["kind"] == "mark":
            save_mark(key, idx + 1, verdict, rest.strip() or reply)
        title = "%s - %s p.%d - %s" % (kind_label, name, idx + 1, verdict)
        notify(title[:200], (rest.strip() or reply),
               image=(crop_png if g["kind"] == "mark" else None),
               tags=("white_check_mark" if g["kind"] == "mark" else "bulb"), dry=dry)
        extra_imgs = []
        for rel_a, pg_a in attach_reqs:
            shot = render_attachment(rel_a, pg_a)
            if shot:
                extra_imgs.append(shot)
                notify(("img - %s%s" % (os.path.basename(rel_a),
                        " p.%d" % pg_a if pg_a else ""))[:180],
                       "attached by the tutor", image=shot, dry=dry)
        WEB.job_end(jid)
        WEB.add_response(kind_label.lower(), key, idx + 1, verdict,
                         (rest.strip() or reply),
                         [crop_png or full_png] + extra_imgs)
        state["answered"].append(g["hash"])
        return "done"

    # One lane per channel. Inside a lane the work is sequential, because a lane
    # owns a Claude session and two concurrent --resume on one id would race the
    # transcript. Across lanes it is genuinely parallel, so mark, explain and
    # command overlap and all three chips blink at once.
    lanes = {}
    for g in runnable:
        lanes.setdefault(g["kind"], []).append(g)

    results, rlock = [], threading.Lock()

    def run_lane(items):
        for g in items:
            try:
                r = run_one(g)
            except Exception as e:                  # never strand a lit chip
                log("  !! %s lane crashed: %s" % (g["kind"], e))
                log(traceback.format_exc())
                WEB.job_end(g["_jid"])
                r = "failed"
            with rlock:
                results.append(r)

    threads = [threading.Thread(target=run_lane, args=(v,), daemon=True)
               for v in lanes.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()          # the caller saves state, so nothing may still be running

    return results.count("done"), results.count("failed") + deferred


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
    state.pop("sess_n", None)
    state["active_wb"] = None
    state["last_page"] = {}          # per (workbook, channel); see lane_key()

    if args.test_page:
        state["answered"] = []          # so an already-answered circle still fires
        done, failed = handle(args.test_page, state, dry=args.dry_run)
        log("test-page: %d answered, %d failed" % (done, failed))
        return

    log("watcher starting (poll %ds, topic %s)" % (POLL_SECONDS, NTFY_TOPIC))
    log("  mark %s/%s | explain %s/%s | commands %s/%s"
        % (profile(state, "mark") + profile(state, "explain")
           + (COMMAND_MODEL, COMMAND_EFFORT)))
    WEB.progress.update(state.get("progress", {}))
    web_profiles(state)
    WEB.set_session_start()
    start_web()
    # module totals need a one-off text scan of every workbook PDF - do it off
    # the main loop so startup stays instant
    threading.Thread(target=refresh_modules, daemon=True).start()
    first_pass = not state["mtimes"]
    if first_pass:
        log("no previous state - baselining, will not fire on existing ink")
    last_beat, seen_changes = time.time(), 0
    offline_polls = 0
    settling = {}                 # rel -> [mtime, stable_poll_count]

    while True:
        try:
            now = poll_mtimes()
            WEB.set_online(True, _channel.host)
            if offline_polls >= OFFLINE_ALERT_AFTER:
                log("tablet back online after %d failed polls" % offline_polls)
                try:
                    notify("Tablet reconnected", "Back online - catching up now.",
                           priority="low", tags="zzz", dry=args.dry_run)
                except Exception:
                    pass
            offline_polls = 0
            # Settle gate: only act on a page once its mtime has held still for
            # SETTLE_POLLS consecutive polls - i.e. the pen has stopped.
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
                    elif SETTLE_POLLS <= 0:
                        changed.append(r)      # no debounce - act immediately
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
            WEB.set_online(False)
            offline_polls += 1
            # tell the PHONE, once - the console is not where they are looking
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
