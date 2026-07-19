# Kindling — Task Breakdown

Execution companion to [AUDIT.md](AUDIT.md) and [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md).

**Instructions for the executing agent (read first):**

- Line numbers below were pinned on 2026-07-03 against commit `4f21821` plus one
  uncommitted formatting change. **Locate code by the quoted symbol/function names, not
  by raw line number** — lines will drift.
- Work on one task per branch/commit. After every task run:
  `make check` (must exit 0). After T3 exists, also run `make test` (must exit 0).
- Do not change behavior anywhere a task doesn't explicitly call for it. If a change
  you believe is needed isn't specified here, stop and ask instead of improvising.
- Never commit anything under `books/`, `.env`, or `.venv/`.
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
  Playwright browser install is NOT needed for any task except T5's manual
  verification step.

Quick wins (small, low-risk, do anytime): **T2, T8a, T8b, T8d, T8e**.

---

## T1 — Make transcript outputs merge-complete across partial runs

**Severity**: P1-1. **Goal**: `book.md`, `captures.jsonl`, and `manifest.json` always
reflect the union of all transcription work for the book, regardless of
`--start-at`/`--max-pages` selection.

**Files**: `scripts/transcribe.py`; new test file `tests/test_partial_run_merge.py`
(depends on T3's `tests/conftest.py`; if doing T1 before T3, create that conftest as
specified in T3 first).

**Current defect**: in `main()`, `capture_id` is assigned only to `selected_captures`
(the `for idx, capture in enumerate(selected_captures)` loop after
`selected_captures = captures[start_index:end_index]`), and `capture_records` /
`canonical_results` — the inputs to `build_markdown_transcript`, `write_jsonl`, and the
manifest counts — are built only from the selection. The writes near the end of
`main()` (`book_markdown_path.write_text(...)`, `write_jsonl(captures_jsonl_path, ...)`)
then overwrite the full-book files with the subset.

**Required changes**:

1. Move capture-id assignment so it runs over the **full** `captures` list (all
   captures returned by `load_captures`), *before* slicing the selection. Keep the
   exact same dedup logic (`capture_id_counts`, `-2` suffix style) — just applied to
   the full list so ids are stable regardless of selection.
2. Process (submit to the thread pool) **only** `selected_captures` — unchanged.
3. After processing, build `capture_records` and the `canonical_results` map over the
   **full** `captures` list:
   - For captures processed this run: use the in-memory result (current behavior).
   - For all other captures: attempt to read
     `transcripts/canonical/<capture_id>.json`; if it exists and parses to a dict, use
     it as that capture's canonical result; otherwise record `status: None` (renders as
     the existing `[transcription error]` / missing-text path in `book.md`).
4. Manifest: keep the existing per-run counts but nest them under a `last_run` key, and
   add top-level `totals`: `{"captures": <full count>, "completed": <count of full list
   with status completed>}`. Update the README "Transcript outputs" section (3-line
   diff) to mention that `book.md`/`captures.jsonl` always cover the whole book.

**Acceptance criteria** (this exact scenario is the regression test; it needs no API
key because completed canonical results with matching fingerprints are reused without
API calls — set `OPENAI_API_KEY=dummy`):

1. In a temp cwd, create `books/TESTBOOK/pages/page-000{1,2,3}-of-0003-v0001.png` with
   arbitrary bytes, and for each a matching
   `books/TESTBOOK/transcripts/canonical/<stem>.json` with
   `status: "completed"`, `final: {"text": "PAGE <n> TEXT", "confidence": 0.99,
   "uncertainties": [], "normalization_notes": []}`, and a `source_image` object whose
   `path` is `pages/<name>`, and whose `size_bytes`/`mtime_ns` are taken from
   `Path.stat()` of the real file.
2. Run `python scripts/transcribe.py --asin TESTBOOK` → `book.md` contains 3
   `### Page` sections.
3. Run `python scripts/transcribe.py --asin TESTBOOK --start-at 2` → **`book.md` must
   still contain all 3 sections** (currently it contains 1 — that is the bug) and
   `captures.jsonl` must still have 3 lines.
4. `make check` and `make test` pass.

## T2 — Declare the Python floor; drop the 3.14-only syntax (QUICK WIN)

**Severity**: P1-2. **Goal**: repo runs on Python ≥3.12 and says so.

**Files**: `pyproject.toml`, `scripts/extract.py`, `scripts/transcribe.py`,
`README.md`.

**Steps (order matters — the formatter reverts step 2 if you skip step 1)**:

1. In `pyproject.toml`, change `target-version = "py314"` to
   `target-version = "py312"`.
2. Replace all three occurrences of `except TypeError, ValueError:` with
   `except (TypeError, ValueError):` — one in `_coerce_positive_int`
   (scripts/extract.py), two in `clamp_confidence` and `parse_int`
   (scripts/transcribe.py).
3. Run `make format` and confirm it does **not** rewrite the parentheses back
   (`git diff` after formatting must still show the parenthesized form).
4. In README "Setup", add one line: `Requires Python 3.12+`.

**Acceptance criteria**:

- `grep -rn "except TypeError, ValueError" scripts/` → no matches.
- `make check` exits 0.
- If a `python3.12` binary is available:
  `python3.12 -m py_compile scripts/extract.py scripts/transcribe.py
  scripts/extract_library.py` exits 0. If unavailable, state so in the task report.
- `python3 -m py_compile scripts/*.py` (3.14) still exits 0.

## T3 — Pytest suite over the pure logic core

**Severity**: P2-1. **Goal**: pin the archive-defining invariants with fast,
Playwright-free unit tests.

**Files**: new `tests/conftest.py`, `tests/test_extract_pure.py`,
`tests/test_transcribe_pure.py`; `requirements-dev.txt` (add a pinned `pytest`, current
stable); `Makefile` (add `test:` target running `.venv/bin/pytest tests -q`, and add
`test` to `check`'s prerequisites or document `make test` in README's Code quality
section).

**conftest.py** (scripts are not a package; import them by path):

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
```

Then `import extract` / `import transcribe` in test modules.

**Required cases** (expected values below are the *current verified behavior* — tests
must pass against unmodified code):

`test_extract_pure.py`:
- `parse_footer_nav_text("Page 12 of 452")` → `(12, 452, None, None)`;
  `"Location 250 of 6446"` → `(None, None, 250, 6446)`;
  `"Page ix of 452"` → `(None, None, 9, 452)`; `""` and `None` → all-None; garbage
  text → all-None.
- `deromanize`: `"IX"`→9, `"xiv"`→14, `"MCMXCIV"`→1994, `"A"`→None.
- `parse_capture_pages_spec`: `"50-55,114,140"` → `[50,51,52,53,54,55,114,140]`;
  `None` → `[]`; duplicates collapse (`"5,5,5"` → `[5]`); each of `"5-3"`, `"0"`,
  `"a"`, `"1,,2"` raises `ValueError`.
- Round-trip: `build_canonical_capture_filename(current=238, total=452)` →
  `"page-0238-of-0452-v0001.png"` and `parse_canonical_capture_filename` of that
  returns page=238, total=452, variant_index=1; location form
  (`current_location=2, total_location=6446, prefer_location=True`) →
  `"loc-0002-of-6446-v0001.png"`; legacy dot-variant `"page-0001-of-0452.v0002.png"`
  parses with variant_index=2; `"cover.png"` → None.
- `build_pages_coverage_payload`: entries for pages 1,2,3 of total 3 → status
  `"complete"`; pages 1,3 of 3 with no anomalies → `"incomplete"` with
  `missing_pages == [2]`; pages 1,3 of 3 with an anomaly event
  `{"unresolved_pages": [2]}` → `"uncertain_gaps"` with `missing_pages == []` and
  `unresolved_missing_pages == [2]`; empty entries → `"unknown_total"`.
- `classify_toc_entries`: an "Acknowledgements" entry at page 400/total 452 marks it
  and everything after as `end_matter`; the same title at page 10/452 (ratio < 0.9)
  stays `content`.
- `is_end_matter_title`: true for "About the Author", "Excerpt from X"; false for
  "Chapter 1".

`test_transcribe_pure.py`:
- `clamp_confidence`: `0.5`→0.5, `-1`→0.0, `2`→1.0, `"abc"`→0.0, `None`→0.0.
- `parse_capture_metadata_from_filename`: same fixtures as extract's parser — the two
  must agree (write one shared fixture list and assert both parsers give equivalent
  page/total/location/variant values).
- `build_capture_id`: path-based stem wins over file; empty dict →
  `capture-00007`-style fallback with `fallback_index=7`.
- `source_image_fingerprint_matches`: exact match true; each field mismatched → false;
  missing `source_image` → false.
- `infer_toc_title`: page-keyed capture picks nearest preceding page-keyed entry;
  capture before all entries → None; location-keyed capture matches location-keyed
  entries.
- `parse_json_payload`: plain JSON; fenced ```` ```json ```` block; leading/trailing
  whitespace.

**Acceptance criteria**: `make test` exits 0; test count ≥ 30; suite runs in under 10
seconds; no network, no Playwright import.

## T4 — CI workflow

**Severity**: P2-2. **Goal**: the quality gate runs on every push.
**Depends on**: T2 (version floor), T3 (tests).

**Files**: new `.github/workflows/ci.yml`.

**Spec**: on `push` and `pull_request`; matrix `python-version: ["3.12", "3.14"]`;
steps: checkout → setup-python → `python -m venv .venv && .venv/bin/pip install -r
requirements-dev.txt` → `make check` → `make test` →
`.venv/bin/python -m py_compile scripts/*.py`. Do **not** run `playwright install`
(tests don't need a browser). Keep it one job, no caching cleverness.

**Acceptance criteria**: workflow file passes `actionlint` if available (otherwise
YAML-parses); a push to GitHub shows both matrix legs green.

## T5 — Guard the auto-turn retry logic against self-inflicted page skips

**Severity**: P1-3. **Depends on**: T3. **Requires a human**: final live verification.

**Files**: `scripts/extract.py` (`wait_for_turn_content_change` and its single call
site in the auto-turn loop), new `tests/test_turn_wait.py`.

**Required changes**:

1. Refactor `wait_for_turn_content_change` to accept injectable probes so it is
   unit-testable without Playwright. New signature (keep defaults so the call site
   stays simple):
   `wait_for_turn_content_change(page, previous_signature, previous_page,
   previous_location, *, timeout_seconds=8, poll_interval_ms=100, max_retries=2,
   retry_interval_ms=2500, get_signature=get_content_signature,
   get_info=get_page_info, click_next=click_next_button)`.
   Internals use `get_signature(page)`, `get_info(page)`, `click_next(page)` instead of
   calling the module functions directly, and use `time.monotonic` + `time.sleep`
   directly (not `page.wait_for_timeout`) so tests can pass a plain `object()` as
   `page`. Return `(changed_by_signature, changed_by_footer, retries_used)`.
2. Policy changes inside the loop:
   - **Footer polling**: each iteration, also read `get_info(page)`; if the page or
     location value differs from `previous_page`/`previous_location` (when the previous
     value is not None), return immediately with `changed_by_footer=True`.
   - **No blind retries**: only attempt a retry click when `previous_signature` is not
     None **and** no footer change has been observed.
   - **Interval**: default `retry_interval_ms` raised 1000 → 2500.
3. Call site (auto-turn loop): pass `previous_page`/`previous_location`, use the
   returned `changed_by_footer` instead of recomputing it *for the early-exit case*,
   but keep the existing post-wait recomputation as a fallback (footer may update after
   signature change). Keep the anomaly logging exactly as is.

**Unit tests** (fake probes; no browser):
- Previous signature None, footer never changes → returns after timeout with zero
  clicks (`retries_used == 0`). Use a tiny `timeout_seconds` like 0.3.
- Previous signature None, footer changes at second poll → returns
  `changed_by_footer=True` before timeout, zero clicks.
- Signature present, changes on third poll → `changed_by_signature=True`, zero clicks.
- Signature present, never changes, footer never changes, `max_retries=2`,
  `retry_interval_ms` small → exactly 2 clicks.

**Acceptance criteria**: new tests pass; `make check` green; **human step**: run a
~20-page live capture (`--pages 20`) on a real book before/after and confirm
`pages.json` anomalies do not increase and per-page wall time does not regress for
normal pages. Report the two anomaly counts in the task summary.

## T6 — Extract the shared core module

**Severity**: P2-3 (part 1). **Depends on**: T3 (tests must exist and pass first).

**Files**: new `scripts/kindling_common.py`; edit all three scripts; tests updated to
import shared symbols from the new module (keep at least one cross-check test asserting
`extract` and `transcribe` use the same parser).

**Move (verbatim, no behavior change)**: `sanitize_slug`;
canonical-filename regexes + `parse_canonical_capture_filename` /
`parse_capture_metadata_from_filename` (unify into one function returning the superset
dict; both scripts adapt); `build_canonical_capture_filename`;
`canonical_capture_sort_key`; `_coerce_positive_int` / `parse_int` (unify: keep both
names as thin wrappers if their semantics differ — note `_coerce_positive_int` rejects
non-positive, `parse_int` doesn't); `read_json` / `write_json` / `write_jsonl`;
`utc_now_iso`.

**Constraints**: scripts stay directly runnable (`python scripts/extract.py`), so use
a same-directory import (`from kindling_common import ...` works because the script's
own directory is on `sys.path` when executed as a file). No `__init__.py`, no packaging
changes.

**Acceptance criteria**: `make check` + `make test` green;
`grep -rn "def sanitize_slug" scripts/` → exactly 1 match;
`grep -rn "page-(\\\\d+)-of-" scripts/` → matches only in `kindling_common.py`;
`python scripts/extract.py --help` and `python scripts/transcribe.py --help` exit 0.

## T7 — Split extract.py `main()` by mode

**Severity**: P2-3 (part 2). **Depends on**: T5, T6. **Judgment required** — follow the
structure below literally and do not "improve" logic while moving it.

**Files**: `scripts/extract.py`.

**Target structure** (pure code motion):
- `parse_args()` — argparse + validation block.
- `setup_reader(context, args)` — everything from page acquisition through reader
  settings/TOC load, returning a small context dict (page, initial position, toc
  summary/boundaries, intercepted metadata).
- `make_manifest_saver(screenshots_dir, manifest_path, asin, captured_at,
  capture_stats, existing_anomaly_events, new_anomaly_events, last_run_mode)` — single
  factory replacing the two duplicated `save_pages_manifest_best_effort` closures
  (their bodies are identical except `last_run_mode`).
- `run_capture_pages_mode(...)` and `run_auto_turn_mode(...)` — the two existing
  mode bodies, including their `finally` shutdown/restore blocks (factor the shared
  shutdown into `shutdown(page, context, args, initial_page, initial_location)`).
- `main()` — ~40 lines of orchestration.

**Acceptance criteria**: `make check` + `make test` green; `--help` output unchanged
(diff against pre-change output); no diff in any print string (extract with
`grep -o '"[^"]*"' | sort` before/after as a crude check, or eyeball `git diff` for
string changes); a human runs one small live capture to confirm parity.

## T8 — Small correctness/ergonomics batch

Independent items; separate commits. **a, b, d are quick wins.**

- **a (P3-2)**: in `scripts/transcribe.py` `process_capture`, the
  `created_at = (existing_payload.get("created_at") if isinstance(existing_payload,
  dict) else utc_now_iso())` expression can yield None. Replace with logic matching the
  manifest handling later in the file: use the existing value only if
  `isinstance(..., str)` and non-empty, else `utc_now_iso()`.
  *Acceptance*: unit test — reusing a payload dict lacking `created_at` produces a
  valid ISO string.
- **b (P3-3)**: in `scripts/extract.py`, `--seconds` becomes `type=float` with
  validation `if args.seconds < 0: parser.error("--seconds must be >= 0")`.
  *Acceptance*: `--seconds -1` exits with argparse error; `--seconds 0.5 --help`-level
  parse works (unit test via `parse_args` if T7 done, else manual `--pages 0 --help`).
- **c (P3-4, BREAKING — get owner sign-off before doing)**: make `--asin` required in
  `scripts/extract.py` (remove the `B00FO74WXA` default), matching transcribe.py.
  Update README default column. *Acceptance*: bare `python scripts/extract.py` exits
  with "required: --asin" error.
- **d (P3-5)**: delete the redundant `or "ap/signin" in page.url` in
  `scripts/extract.py` and `scripts/extract_library.py` (first clause already matches
  the substring). *Acceptance*: `grep -rn 'ap/signin' scripts/` → no matches;
  `make check` green.
- **e (dependency advisory, QUICK WIN)**: in `requirements.txt`, bump
  `python-dotenv==1.2.1` → `python-dotenv==1.2.2` (fixes GHSA-mf9w-mj56-hr94,
  symlink-following in `set_key`; unused by this repo but don't carry it).
  *Acceptance*: `pip install -r requirements.txt` succeeds in a fresh venv;
  `python -c "import dotenv; print(dotenv.__version__ if hasattr(dotenv,'__version__') else 'ok')"`
  imports cleanly; all three scripts' `--help` still exit 0.

## T9 — OCR cost controls and fail-fast retries

**Severity**: P3-6/P3-7. **Depends on**: T3.

**Files**: `scripts/transcribe.py`, `README.md`.

**Changes**:
1. `retry_call`: stop retrying obviously non-transient failures. Implementation:
   catch the exception, inspect `getattr(exc, "status_code", None)`; if it is an int in
   `{400, 401, 403, 404, 422}`, re-raise immediately (wrapped in the same
   `RuntimeError` format). Keep retrying everything else (429, 5xx, timeouts,
   connection errors). *Acceptance*: unit test with a stub `fn` raising an object
   with `status_code=401` → exactly 1 attempt; `status_code=429` → retries.
2. Add `--skip-qa` flag: when set, skip pass 2 entirely and set
   `final = pass1_result`, `pass2 = None`. *Acceptance*: unit-level check of the
   result payload shape, plus README table row.
3. Add a cost preview to `--dry-run`: print image count and note that each capture
   costs 2 model calls (1 with `--skip-qa`). No dollar math (prices drift) — counts
   only. *Acceptance*: dry-run output includes planned API call count.

## T10 — DECISION: roman-numeral front matter naming (do not implement without owner)

**Severity**: P3-1. **Goal**: resolve the semantic mixing where "Page ix of 452"
becomes `loc-0009-of-0452-v0001.png` (`parse_footer_nav_text` roman branch in
`scripts/extract.py`).

**Options to present to the owner** (an agent should prepare this comparison, not pick):
- **A. Status quo + document**: add a README note that `loc-` files whose total equals
  the book's page total are roman front matter. Zero migration.
- **B. New `front-` filename prefix**: cleanest semantics; requires updating both
  parsers (one place after T6), the sort key, coverage logic, and a rename migration
  for existing archives.
- **C. Record roman pages as pages with a `roman` flag in pages.json only**: middle
  ground; filenames unchanged.

**Acceptance**: a short written recommendation with the migration cost of each option;
implementation only after the owner picks.
