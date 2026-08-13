# Building a module: from a folder of PDFs to a tutored study sprint

The ink loop (README) is the last mile. What makes it *worth* wiring up is the corpus behind it: clean markdown of your course, a mark-weighted plan, write-on workbooks, and marking notes for the tutor to mark against. This document is the pipeline that produced those, distilled from a real 100+ file, two-module build. The `skills/` folder packages each stage so a Claude Code agent can run it for you.

## Stage 0 — What to collect (an hour on your VLE, the highest-value hour)

In descending order of value:
1. **Past papers with answers.** Even one. The *format* teaches more than any lecture.
2. **Examiner / cohort feedback.** Where last year's students lost marks = a literal list of what to drill. This is the single most valuable document a module publishes.
3. **Your own previous sitting** if resitting — per-question marks tell you exactly where your recoverable marks are.
4. Weekly quizzes/formatives **with answer keys**.
5. The lecture decks, named so week and topic are recoverable.

## Stage 1 — Convert to markdown (`scripts/extract_lectures.py`)

Why not just read PDFs? Because everything downstream (plan-building, workbook-building, live tutoring) re-reads the material dozens of times, and markdown is ~50× cheaper per read — and greppable. The extractor already handles the four traps that silently corrupt naive extraction:

- **Symbol-font mojibake**: PowerPoint maths in legacy SymbolMT extracts as Latin-1 (`á ñ` are really `⟨ ⟩`, `¹` is `≠`, and letters S/P/t are Σ/Π/τ). The fix must be **font-aware, per-span** — global replacement corrupts real text. Verify each mapping in context first.
- **Glyph-split code**: LaTeX `\texttt` listings extract one letter per token (`l e t t i n g`). Rejoin runs of ≥3 single letters.
- **Beamer boilerplate**: LaTeX decks repeat a sidebar on every slide; strip lines appearing on ≥40% of pages, and group words by block (never by y-coordinate — the sidebar shares rows with body text).
- **Vector-only slides**: diagram slides may contain *zero* embedded images. Detect (`len(page.get_drawings()) >= 14`) and render the page to PNG instead, *labelled as a render* so provenance stays honest.

Also: raised-baseline exponents flatten (`O(ed³)` → `O(ed3)`) — detect geometrically by span size and baseline offset. And note some sources lose maths from the text layer entirely while still *rendering* it — **verify against page renders, never against extracted text alone.**

## Stage 2 — Mine the exam (this is strategy, not summarisation)

From the past papers and feedback, extract and write into a `STUDY-PLAN.md`:
- **The mark split by topic.** Weight study hours by marks and by your own weakness, not by teaching weeks. (In the source build: one topic was 50% of the paper and got 75% of the hours.)
- **Question archetypes.** Papers repeat structures year to year. Name them.
- **The named traps** from cohort feedback, verbatim. Each is a drill, not a reading item.
- **Format rules**: open vs closed book changes everything. Open book makes memorisation near-worthless and hand-execution speed + knowing-where-to-look decisive; closed book inverts that.

## Stage 3 — Build write-on workbooks (see `skills/exam-workbook-builder`)

One PDF per study block, sized to a real session (2 h), built from **real past questions** with model answers at the *back*. reportlab + matplotlib-mathtext. Non-negotiable quality gates, learned the hard way:

1. **Every numeric answer independently recomputed** — write throwaway solver code; never copy a number from any source, official ones included (the source build found errors in lecturer slides *and* official answer keys).
2. **Glyph audit**: Helvetica renders ∀∃∈⟨⟩ᵢⱼ as black boxes. Route all text through a sanitizer to markup; an automated audit must print clean before the build counts.
3. **Render every page to PNG and look at it.** Layout bugs (clipped figures, orphaned headings, literal `&gt;` in code blocks) survive successful builds.
4. **Independent review pass**: a second agent that did NOT build the workbook re-derives the load-bearing answers and checks page-by-page. Then a **cross-workbook audit** for contradictions — independently-built units WILL disagree somewhere (the source build found ten contradictions, including a mark scheme that penalised a correct answer).

## Stage 4 — Tutor briefs (what red-circle marking marks against)

One markdown per workbook: every drill's answer **with marking logic** (what earns each mark, where partial credit lives), a fresh alternative explanation per topic (different from the workbook's wording — re-reading the same explanation is worthless), common misconceptions, and the exam mapping. This file is what `rm_feedback.py` greps when you circle in red — its quality *is* the marking quality.

Protocol worth encoding at the top of each brief: **attempt-first** (never explain before an attempt), explain *differently* than the workbook, one check question per explanation, strict marking (a generous mark the week before an exam is worse than useless).

## Stage 5 — Wire the loop

Add each workbook to `rm_feedback.py`'s `WORKBOOKS` map, put the PDFs on the tablet, start the watcher. From then on the cycle is: work in black → circle in red → turn page → keep working → read feedback at the block's end → erase circles.

## What this costs

Stages 1–2 are mostly deterministic Python plus a few agent-hours. Stage 3–4 with the QA gates is the expensive part — the source build spent several million tokens across build + review agents per module. Skipping the gates halves the cost and produces workbooks you cannot trust; if you must economise, cut workbook *count*, not verification.
