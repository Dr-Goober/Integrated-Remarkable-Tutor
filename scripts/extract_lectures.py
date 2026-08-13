# -*- coding: utf-8 -*-
"""Phase 3 - convert AIPS lecture PDFs to markdown with figures pulled out as PNGs.

Extraction notes (why this is not just get_text()):
  * Beamer decks repeat a navigation sidebar on EVERY slide. Lines appearing on
    >=40% of pages are stripped as boilerplate.
  * Grouping words by (block, line) keeps that sidebar from interleaving with body text.
  * LaTeX \texttt listings come out one glyph per token ("l e t t i n g"), which would
    destroy every Essence Prime code sample. Runs of >=3 single-char tokens are collapsed.
  * NFKC repairs ligature damage (di<ff>erent -> different) and folds Mathematical
    Alphanumeric Symbols back to ASCII.
  * Tables are detected and emitted as markdown; their words are removed from the
    flowed text so content is not duplicated.
  * Embedded rasters are extracted verbatim, never re-rendered, deduped by xref.
    Vector-only diagram slides have nothing to extract, so those pages are rendered
    and labelled as renders to keep provenance honest.
"""
import io, os, re, sys, glob, collections, unicodedata
import fitz

SRC = sys.argv[1] if len(sys.argv) > 1 else "lectures"
OUT = sys.argv[2] if len(sys.argv) > 2 else "md/lectures"

MIN_W = MIN_H = 90
MIN_AREA = 22000
BOILER_FRAC = 0.40
DRAW_THRESHOLD = 14
RENDER_ZOOM = 2.0


# These decks set maths in the legacy "SymbolMT" font, which extracts as Latin-1.
# The mapping is applied ONLY to spans whose font is Symbol - that is what makes it
# safe to map plain letters (S/P/t are Sigma/Pi/tau in Symbol but ordinary letters
# in body text), and what stops it corrupting genuine superscripts like O(e*d^3).
# Every non-obvious entry was confirmed against its surrounding line:
#   "a 9 [x] 9 grid"                        -> multiplication
#   "[A](block in blocks) (Sum ...) = k"    -> for-all
#   "Minimise [Sum]xf LOST(...)"            -> Promise/Cruciality heuristics
#   "[tau] = Last(<xi,d>, c)"               -> AC2001 last-support pointer
#   "ABCDEF [<=] ACBDFE"                    -> lex-leader symmetry constraint
#   "Q=Q[union] {[<]xk,c'[>]}"              -> GAC queue update
SYMBOL_FONT = {
    u"\u00e1": u"\u27e8", u"\u00f1": u"\u27e9", u"\u00a3": u"\u2264", u"\u00b3": u"\u2265", u"\u00b9": u"\u2260",
    u"\u00ae": u"\u2192", u"\u00ac": u"\u2190", u"\u00b4": u"\u00d7", u"-": u"\u2212", u'"': u"\u2200", u"$": u"\u2203",
    u"\u00d9": u"\u2227", u"\u00da": u"\u2228", u"\u00cd": u"\u2286", u"\u00c8": u"\u222a", u"\u00c7": u"\u2229", u"\u00ce": u"\u2208",
    u"@": u"\u2245", u"\u00bb": u"\u2248", u"\u00ba": u"\u00b0", u"\u00b1": u"\u00b1", u"\u00a5": u"\u221e", u"\u00d8": u"\u00ac",
    # Symbol has no Latin alphabet at all - any Latin letter in a Symbol span is Greek
    u"a": u"\u03b1", u"b": u"\u03b2", u"c": u"\u03c7", u"d": u"\u03b4", u"e": u"\u03b5", u"f": u"\u03c6",
    u"g": u"\u03b3", u"h": u"\u03b7", u"i": u"\u03b9", u"k": u"\u03ba", u"l": u"\u03bb", u"m": u"\u03bc",
    u"n": u"\u03bd", u"o": u"\u03bf", u"p": u"\u03c0", u"q": u"\u03b8", u"r": u"\u03c1", u"s": u"\u03c3",
    u"t": u"\u03c4", u"u": u"\u03c5", u"w": u"\u03c9", u"x": u"\u03be", u"y": u"\u03c8", u"z": u"\u03b6",
    u"A": u"\u0391", u"B": u"\u0392", u"D": u"\u0394", u"F": u"\u03a6", u"G": u"\u0393", u"L": u"\u039b",
    u"P": u"\u03a0", u"Q": u"\u0398", u"S": u"\u03a3", u"W": u"\u03a9", u"X": u"\u039e", u"Y": u"\u03a8",
}
# NFKC would flatten these to bare digits, turning O(e*d^3) into a misleading O(ed3)
SUPERS = {u"\u2070": u"^0", u"\u00b9": u"^1", u"\u00b2": u"^2", u"\u00b3": u"^3", u"\u2074": u"^4",
          u"\u2075": u"^5", u"\u2076": u"^6", u"\u2077": u"^7", u"\u2078": u"^8", u"\u2079": u"^9"}


def norm(s):
    return unicodedata.normalize("NFKC", s).replace(u"\u00ad", "")


def collapse_spaced_word(line):
    """'l e t t i n g N be domain' -> 'letting N be domain'

    The LaTeX listings in these decks emit a real space glyph between every
    character, so Essence Prime samples arrive letter-spaced. Only runs of >=3
    single LETTERS are joined - restricting to letters keeps digit rows
    ('1 2 3 4') and operator runs ('a and b') intact.
    """
    toks = line.split(u" ")
    out, i, n = [], 0, len(toks)
    while i < n:
        j = i
        while j < n and len(toks[j]) == 1 and toks[j].isalpha():
            # a lone capital after lower-case is a separate identifier in these
            # listings ("letting N be domain"), not another letter of the word
            if j > i and toks[j].isupper() and toks[j - 1].islower():
                break
            j += 1
        if j - i >= 3:
            out.append(u"".join(toks[i:j]))
            i = j
        else:
            out.append(toks[i])
            i += 1
    return u" ".join(out)


def page_lines(page, skip_rects=()):
    """Rebuild lines from per-character geometry.

    Working at char level (rawdict) rather than word level does three jobs at once:
    it exposes the span font so Symbol maths can be decoded, it reconstructs
    letter-spaced monospace listings from the actual glyph gaps instead of guessing,
    and it lets genuine superscripts survive NFKC.
    """
    out = []
    for blk in page.get_text("rawdict").get("blocks", []):
        if blk.get("type", 0) != 0:          # skip image blocks
            continue
        for line in blk.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            # PowerPoint sets exponents as a smaller span on a raised baseline
            # rather than as superscript codepoints, so "O(e*d^3)" would otherwise
            # flatten to the misleading "O(ed3)". Detect them geometrically.
            sizes = sorted(sp.get("size") or 10.0 for sp in spans)
            body = sizes[len(sizes) // 2] or 10.0
            base_y = max((sp.get("origin", (0, 0))[1] for sp in spans), default=0.0)
            chars = []
            for sp in spans:
                sym = "symbol" in sp.get("font", "").lower()
                size = sp.get("size") or 10.0
                oy = sp.get("origin", (0, base_y))[1]
                raised = (size < 0.85 * body) and (oy < base_y - 0.12 * body)
                for ch in sp.get("chars", []):
                    c = ch["c"]
                    c = SYMBOL_FONT.get(c, c) if sym else SUPERS.get(c, c)
                    if raised and c in u"!\"#$%&'()*+,":
                        # Raised glyph that did not survive the font's ToUnicode
                        # map - subsetted Cambria Math / LaTeX maths fonts renumber
                        # glyphs, so O(b^d) arrives as "O(b!)", which reads as a
                        # factorial. Flag it rather than print a lie.
                        c = u"?"
                    chars.append((ch["bbox"][0], ch["bbox"][2], c, size, raised))
            if not chars:
                continue
            bb = fitz.Rect(line["bbox"])
            if any(bb.intersects(r) for r in skip_rects):
                continue
            # Do NOT sort by x. Ligature glyphs are emitted zero-width (x0 == x1) and
            # the following glyph can start a hair earlier, so sorting silently
            # transposes them ("Satisfiability" -> "Satisfaibility"). Document order
            # within a rawdict line is already reading order.
            # A whole raised RUN is one exponent. Emitting per-character would give
            # "2^n/^2-1", which reads as 2^n divided by 2 rather than 2^(n/2)-1.
            buf, prev_x1, in_sup = [], None, False
            for x0, x1, c, size, raised in chars:
                if raised and not in_sup and c.strip():
                    buf.append(u"^{")
                    in_sup = True
                elif in_sup and not raised:
                    buf.append(u"}")
                    in_sup = False
                if prev_x1 is not None and (x0 - prev_x1) > 0.55 * size:
                    buf.append(u" ")
                buf.append(c)
                prev_x1 = x1
            if in_sup:
                buf.append(u"}")
            txt = norm(u"".join(buf)).strip()
            txt = re.sub(r"\^\{(\w)\}", r"^\1", txt)          # ^{3} -> ^3
            txt = re.sub(r"\^\{\??\}|\^\{\}", "^?", txt)      # unknown exponent
            # Not every raised span is an exponent: primes and stars (v^{'} -> v')
            # are already unambiguous, and a multi-letter run is a small-caps label
            # or an ordinal (^{best_child}, 4^{th}), not a power.
            txt = re.sub(r"\^\{([^0-9A-Za-z]+)\}", r"\1", txt)
            txt = re.sub(r"\^\{([A-Za-z_]{2,})\}", r"\1", txt)
            txt = collapse_spaced_word(txt)
            if txt:
                out.append(txt)
    return out


def md_table(tab):
    try:
        data = tab.extract()
    except Exception:
        return None
    data = [[norm((c or "").replace("\n", " ")).strip() for c in row] for row in data]
    data = [r for r in data if any(r)]
    if len(data) < 2 or len(data[0]) < 2:
        return None
    head = data[0]
    rows = [u"| " + u" | ".join(head) + u" |",
            u"|" + u"---|" * len(head)]
    for r in data[1:]:
        r = (r + [u""] * len(head))[:len(head)]
        rows.append(u"| " + u" | ".join(r) + u" |")
    return u"\n".join(rows)


def find_boilerplate(pages_lines, npages):
    counts = collections.Counter()
    for lines in pages_lines:
        for ln in set(lines):
            if len(ln) < 150:
                counts[ln] += 1
    cut = max(3, int(npages * BOILER_FRAC))
    return set(l for l, c in counts.items() if c >= cut)


def main():
    summary = []
    for pdf in sorted(glob.glob(os.path.join(SRC, "*.pdf"))):
        stem = os.path.splitext(os.path.basename(pdf))[0]
        wk, lec = re.match(r"week(\d\d)-lecture(\d)", stem).groups()
        wdir = os.path.join(OUT, "week-%s" % wk)
        fdir = os.path.join(wdir, "figures")
        for d in (wdir, fdir):
            if not os.path.isdir(d):
                os.makedirs(d)

        doc = fitz.open(pdf)
        n, tag = doc.page_count, "w%sl%s" % (wk, lec)

        # pass 1: tables + text, so boilerplate can be measured on final lines
        per_page = []
        for p in range(n):
            page = doc[p]
            tables, rects = [], []
            try:
                for t in page.find_tables().tables:
                    m = md_table(t)
                    if m:
                        tables.append(m)
                        rects.append(fitz.Rect(t.bbox))
            except Exception:
                pass
            per_page.append((page_lines(page, rects), tables))

        boiler = find_boilerplate([x[0] for x in per_page], n)

        seen, n_ext, n_ren, n_tab = {}, 0, 0, 0
        md = []
        for p in range(n):
            page = doc[p]
            lines = [l for l in per_page[p][0]
                     if l not in boiler and not re.fullmatch(r"\d{1,3}", l)]
            tables = per_page[p][1]
            n_tab += len(tables)
            figs = []

            for idx, info in enumerate(page.get_images(full=True)):
                xref = info[0]
                if xref in seen:
                    figs.append((seen[xref], "repeat"))
                    continue
                try:
                    img = doc.extract_image(xref)
                except Exception:
                    continue
                w, h = img.get("width", 0), img.get("height", 0)
                if w < MIN_W or h < MIN_H or w * h < MIN_AREA:
                    continue
                name = "%s-p%03d-i%d.%s" % (tag, p + 1, idx + 1, img["ext"])
                with open(os.path.join(fdir, name), "wb") as fh:
                    fh.write(img["image"])
                seen[xref] = name
                figs.append((name, "extracted"))
                n_ext += 1

            if not figs and len(page.get_drawings()) >= DRAW_THRESHOLD:
                name = "%s-p%03d-render.png" % (tag, p + 1)
                page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM)).save(
                    os.path.join(fdir, name))
                figs.append((name, "render"))
                n_ren += 1

            md.append(u"## Slide %d" % (p + 1))
            md.append(u"")
            if lines:
                md.append(u"\n".join(lines))
                md.append(u"")
            for t in tables:
                md.append(t)
                md.append(u"")
            for name, kind in figs:
                cap = {"extracted": u"figure",
                       "repeat": u"figure (repeat of an earlier slide)",
                       "render": u"page render — vector diagram, nothing embedded to extract"}[kind]
                md.append(u"![%s](figures/%s)" % (cap, name))
            if figs:
                md.append(u"")

        head = [u"# %s" % stem.replace("-", " "),
                u"",
                u"> Source: `lectures/%s.pdf` (%d slides)" % (stem, n),
                u"> Figures: `figures/` — %d extracted from the PDF, %d page renders "
                u"(vector-only slides with no embedded image)." % (n_ext, n_ren),
                u"> Tables: %d reconstructed as markdown." % n_tab,
                u"> STATUS: auto-extracted — sidebar boilerplate stripped, NFKC-normalised, "
                u"glyph-split code runs rejoined. Not yet visually verified.",
                u""]
        with io.open(os.path.join(wdir, stem + ".md"), "w", encoding="utf-8") as fh:
            fh.write(u"\n".join(head + md))

        summary.append((stem[:58], n, n_ext, n_ren, n_tab))
        doc.close()

    print("%-58s %5s %5s %5s %5s" % ("deck", "pp", "figs", "rend", "tbl"))
    for s in summary:
        print("%-58s %5d %5d %5d %5d" % s)
    print("\ntotals: %d decks, %d figures, %d renders, %d tables"
          % (len(summary), sum(x[2] for x in summary), sum(x[3] for x in summary),
             sum(x[4] for x in summary)))


if __name__ == "__main__":
    main()

