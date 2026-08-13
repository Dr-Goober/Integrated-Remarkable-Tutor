---
name: exam-workbook-builder
description: Build verified, write-on PDF workbooks and tutor briefs from an ingested module corpus — exam-strategic drills from real past questions, sized to study blocks, with strict QA gates. Use when the user asks for study workbooks, drill PDFs, a study plan from past papers, or tutor briefs for a module that already has a markdown corpus.
---

# Exam workbook builder

You are building the artefacts a student writes on and is marked against. Errors here teach wrong methods at the worst possible time, so verification outranks speed.

## Stage A — Mine the exam before writing anything

From past papers + examiner/cohort feedback, establish and record in `STUDY-PLAN.md`:
- **Mark split by topic** → weight study hours by marks × the student's weakness, never by teaching order.
- **Question archetypes** (papers repeat structures) and **named traps** from the feedback — each trap becomes a drill.
- **Format**: open-book makes hand-execution speed and looking-things-up-fast the scarce skills (memorisation ≈ worthless); closed-book inverts this. Say which regime applies at the top of every workbook.
- If the student has a previous sitting, its per-question marks override all guessing about where to spend time. Preserve their attempt verbatim (labelled as wrong-by-design) and derive their failure *patterns*.

## Stage B — Build (one workbook per study block)

- Size each workbook to a real session (2 h), with explicit day dividers and per-section minute budgets that **actually sum to the block length** — audit the arithmetic.
- Drills come from **real past questions** wherever possible; invented ones are clearly labelled.
- Model answers with per-mark marking logic go at the **back**, never beside the question.
- End every workbook with a one-page crib sheet + a lookup index (topic → exact file + slide) for open-book exams.
- If the student claims confidence in a topic, build a **cold diagnostic first** (scored, with a binding skip/redo threshold) — placed BEFORE the teaching, so the result arrives while the plan can still react.
- reportlab plumbing: write-on answer boxes sized for handwriting, monospace bordered blocks for code, `KeepTogether` so a prompt never strands from its answer box.

## Stage C — QA gates (all four; skipping any produces untrustworthy output)

1. **Independent recomputation** of every numeric answer via throwaway solver code. Trust nothing — the source build found errors in lecturer slides AND official answer keys. Where sources have no key, mark answers as tutor-derived everywhere they appear.
2. **Glyph audit**: sanitize all text (Unicode subscripts/arrows/logic symbols → markup); an automated audit must print clean. Test which glyphs actually render in the PDF font — don't assume.
3. **Render every page to PNG and look at it**: clipped figures, overlapping text, orphaned headings, literal XML entities in code blocks (`Preformatted` does not parse entities), tables split mid-row.
4. **Independent review** by an agent that didn't build it (re-derive the load-bearing answers blind — reading the answer first means confirming it), then a **cross-workbook audit**: coverage matrix vs the real paper, contradiction hunt between workbooks (mark schemes, notation, crib sheets), spot-check every lookup-index entry, verify cross-referenced page numbers against the built PDFs.

## Stage D — Tutor briefs (one per workbook)

Each brief carries: the tutoring protocol (attempt-first; explain *differently* than the workbook; one check question per explanation; strict marking with partial-credit locations), every drill's answer with per-mark logic, alternative explanations + likely misconceptions per topic, and the exam mapping. For open-ended questions (modelling, "justify"), the brief must say how to mark a **correct answer that differs from the model answer** — that judgement is the hard part of marking.

## Standing rules

- Never rebuild a shipped workbook casually: schedules and cross-references pin its page numbers.
- Encode the discipline "never leave a trace/procedural question blank — write the first step and its consequences" wherever partial credit exists.
- Drill IDs restart per workbook; always cite drills as workbook + ID.
