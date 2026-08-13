---
name: module-ingest
description: Convert a university module's raw PDFs (lecture decks, past papers, quizzes, feedback) into a clean, verified markdown study corpus with extracted figures. Use when the user drops a folder of course PDFs and wants it turned into a study-ready corpus, or asks to "ingest", "convert" or "organise" module materials.
---

# Module ingest — raw PDFs to a verified study corpus

You are converting a module's PDFs into markdown + figures so that every later stage (planning, workbook-building, live tutoring) reads cheap, greppable text instead of re-parsing PDFs.

## Step 1 — Audit before touching anything

List every file. Classify: lecture / lab / lab-solution / practice-questions / practice-answers / reference. **Hash suspected duplicates (MD5) rather than assuming** — a "duplicate" is sometimes a mislabelled answer sheet. Never delete: move byte-identical copies to `_duplicates/`. Rename so week + type + topic are recoverable from the filename alone — recover topics from slide 1 when filenames are opaque.

Report the classification table before reorganising if anything looks ambiguous; otherwise proceed but keep everything recoverable.

## Step 2 — Extract with the four traps handled

Use `scripts/extract_lectures.py` as the base (PyMuPDF). It already handles:

1. **Symbol-font mojibake** — legacy SymbolMT maths extracts as Latin-1 (`á ñ` → `⟨ ⟩`, `¹` → `≠`, S/P/t → Σ/Π/τ). Mapping MUST be per-span by font, never global (`³` is a real superscript in body text but `≥` in Symbol). Verify every mapping against its surrounding line before applying; perfect pair-counts (e.g. equal `á` and `ñ`) are strong evidence.
2. **Glyph-split monospace** — `l e t t i n g` → `letting`: rejoin runs of ≥3 single alphabetic tokens, breaking at lower→UPPER transitions.
3. **Beamer sidebar boilerplate** — strip lines on ≥40% of pages; group words by (block, line), never by y-coordinate.
4. **Vector-only diagram slides** — zero embedded images; render the page (`get_drawings()` count ≥ 14 as the trigger) and caption it as a *render* so provenance is honest.

Also handle raised-baseline exponents (smaller span, raised origin → `^`), and flag unrecoverable glyphs as `^?` rather than printing something wrong — **a wrong symbol is worse than a flagged one**.

## Step 3 — Verify, and write STATUS lines

- Spot-read the highest-stakes decks page-by-page against renders (equations, code listings, tables).
- Every output .md gets a `> Source:` line (exact source path) and a `> STATUS:` line stating HOW it was produced and what was verified. Downstream consumers rely on these.
- Transcribe **lecturer errors as-shown with a QA note** — never silently correct source material; and never attribute your own extraction artefacts to the lecturer (verify against the rendered page before flagging anything as a source error).

## Step 4 — Write the module map

Produce `INFO.md`: directory tree, file inventory, the exam's mark split if papers are available, conversion notes (what is/isn't guaranteed), and known issues (missing lectures, answerless quizzes, damaged exports). Produce/extend `CLAUDE.md` with reading rules ("md first, PDF fallback") and the known-issues list.

## Non-negotiables

- Read-only toward sources; never delete.
- Answer keys are sacred: keep questions and answers in separately named files so mock practice can use questions-only copies.
- If a source has no official answers, say so everywhere its content is reused — tutor-computed answers must never masquerade as official.
