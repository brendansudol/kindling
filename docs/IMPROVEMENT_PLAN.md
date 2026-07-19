# Kindling — Improvement Plan

Strategy companion to [AUDIT.md](AUDIT.md). Five themes, sequenced. Each theme lists
what it unlocks and rough effort. Discrete, executable tasks live in
[TASKS.md](TASKS.md); this document explains *why this order*.

Guiding principle: this project's value is a **trustworthy archive** — every page
captured, every label true, every dollar of OCR spend preserved. Prioritize anything
that protects data already produced, then anything that lets you change code without
fear, then feature/cost polish.

---

## Theme 1 — Stop destroying outputs (trust the transcripts)

**What**: Fix the partial-run clobber of `book.md`/`captures.jsonl` (AUDIT P1-1) and the
small metadata correctness nits in the same area (P3-2 `created_at: null`).

**Why first**: It's the only active data-destruction path, it's triggered by a
documented workflow, and the fix is small and self-contained. Nothing else in this plan
should be attempted while the tool can eat its own compiled output — refactors multiply
the chance of hitting it.

**Unlocks**: Safe incremental transcription (the whole point of `--start-at`/
`--max-pages`); confidence that a `transcripts/` directory is always the union of all
work ever done, not the residue of the last command.

**Effort**: Small — half a day including the regression test. Tasks: T1, part of T8.

## Theme 2 — Build the safety net (tests, CI, version floor)

**What**: Declare and enforce the real Python floor (AUDIT P1-2: today the repo is
silently 3.14-only and the formatter enforces it); add a pytest suite over the pure
logic core (P2-1); add CI running `make check` + tests on a version matrix (P2-2).

**Why second**: Every later theme rewrites code. The pure functions (filename
naming/parsing, coverage math, footer parsing, fingerprints) define the meaning of the
archive; they must be pinned by tests before anyone — especially a cheaper agent —
touches them. CI makes the gate real; the version matrix makes P1-2 structurally
impossible to regress.

**Unlocks**: Every subsequent theme. Also makes the repo portable to your next machine.

**Effort**: Medium — 1–2 days. Tasks: T2, T3, T4.

## Theme 3 — Make capture stop skipping pages (reliability of the core loop)

**What**: Fix the self-inflicted double-click risk in `wait_for_turn_content_change`
(AUDIT P1-3): no blind retries when there's no content signature, footer-aware early
exit, longer retry interval. Refactor the wait loop to take injected probes so the
retry policy is unit-testable. Small CLI hardening rides along (`--seconds` validation,
P3-3).

**Why third**: It needs Theme 2's tests to refactor safely, and it needs live
verification against a real book (the only theme that does). Expected payoff: fewer
`jump_forward` anomalies, fewer manual `--capture-pages` backfill runs, faster runs
(no 8-second stalls when signatures are unavailable).

**Unlocks**: Higher first-pass coverage per capture run; shrinks the main manual
babysitting cost of the tool.

**Effort**: Medium — a day of code plus a live capture run to compare anomaly counts
before/after. Tasks: T5, part of T8.

## Theme 4 — One source of truth for the archive format (shared core, thin CLIs)

**What**: Extract the duplicated logic (slug, canonical filename build/parse, sort key,
JSON IO, int coercion) into one shared module imported by all three scripts (AUDIT
P2-3); then split extract.py's ~730-line `main()` into per-mode functions and dedupe
the two `save_pages_manifest_best_effort` closures.

**Why fourth**: The filename grammar currently exists in two hand-synced copies — the
most likely way a future change silently corrupts the pipeline is these drifting apart.
But consolidation is a pure refactor: do it only once tests (Theme 2) can prove
behavior parity, and after the loop changes (Theme 3) so you're not refactoring code
you're about to rewrite.

**Unlocks**: The test suite reaches deeper with less setup; adding a fourth script
(e.g., batch-driving extraction from `library.json` — a natural next feature given
`extract_library.py` exists) becomes cheap and safe.

**Effort**: Medium–large — 1–2 days across two PR-sized steps (shared module first,
`main()` split second). Tasks: T6, T7.

## Theme 5 — Cost and ergonomics polish

**What**: Fail fast on non-retryable API errors (AUDIT P3-6); optional/cheaper QA pass
and a cost estimate in `--dry-run` (P3-7); make `--asin` required in extract.py (P3-4);
decide what to do about roman-numeral front matter stored as pseudo-locations (P3-1 —
this one is a *decision*, not just code, because it may change the on-disk naming
scheme).

**Why last**: All real money/ergonomics wins, none of them protect existing data or
enable other work. The QA-pass economics alone are meaningful — pass 2 roughly doubles
per-book OCR spend at `detail: high` — but it's an optimization, not a defect.

**Unlocks**: Cheaper full-book runs; a repo a stranger (or future you) can run without
tribal knowledge.

**Effort**: Small–medium — half a day to a day, excluding the naming-scheme decision.
Tasks: T8, T9, T10.

---

## Sequencing summary

| Order | Theme | Tasks | Effort | Depends on |
|-------|-------|-------|--------|------------|
| 1 | Stop destroying outputs | T1 | S | — |
| 2 | Safety net: floor + tests + CI | T2, T3, T4 | M | — |
| 3 | Capture-loop reliability | T5, T8(part) | M (+ live run) | T3 |
| 4 | Shared core, thin CLIs | T6, T7 | M–L | T3, T5 |
| 5 | Cost & ergonomics | T8, T9, T10 | S–M | T3 |

T1 and T2 are independent and can be done in either order or in parallel; everything
else should wait for T3 (tests) to exist.
