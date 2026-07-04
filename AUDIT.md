# Kindling — Code Audit

Audited 2026-07-03 at commit `4f21821` (plus one uncommitted formatting fix, see Quick Wins).

**Context assumption** (your prompt's context placeholder was left unfilled): this is a
single-user, personal-use tool for archiving and transcribing your own Kindle library —
active development, no other users, no deployment. It is judged against that standard:
no findings about auth, multi-tenancy, or scale. The crown jewel is **archive
correctness** — captured pages must be complete and correctly labeled, and transcripts
must not be silently damaged.

**What was verified by running it**: venv setup per README, `make lint` /
`make format-check`, byte-compilation on Python 3.14.6 and 3.12, CLI `--help` for all
three scripts, a full transcribe run and a partial transcribe run against a seeded fake
book (no API calls; exercised the cache-reuse path), a git-history secret scan, and
an OSV-database vulnerability check on all pinned dependencies. **Not verified live**: anything requiring an Amazon
login (reader navigation, TOC scraping, library scrolling). Findings that depend on live
reader behavior are marked *(mechanism verified in code; not reproduced live)*.

## Executive summary

- The core design is genuinely good: idempotent nav-keyed capture naming, atomic
  manifest writes, honest coverage/anomaly telemetry, and correct OCR resume caching.
- One verified P1 bug: any partial transcribe run (`--start-at`/`--max-pages` — a
  workflow the README recommends) overwrites `book.md` and `captures.jsonl` with only
  the selected subset.
- The codebase compiles **only on Python ≥3.14** (PEP 758 `except A, B:` syntax), the
  requirement is declared nowhere, and the ruff config actively *enforces* the
  3.14-only spelling — `ruff format` rewrites the portable form back.
- The auto-turn retry logic can plausibly cause the very page-skips the anomaly system
  was built to track (self-inflicted double-clicks).
- Zero tests and no CI; `make check` failed on a clean checkout at HEAD (format drift —
  fixed in this audit).
- Security posture is clean for the project's threat model: no secrets in history,
  `.env`/`books/` gitignored, no injection surfaces, no known-vulnerable pins.

**Grade: B** — thoughtful, unusually honest data engineering undermined by having no
automated verification at all and one verified output-destroying bug.

---

## P1 — will bite within months

### P1-1. Partial transcribe runs clobber compiled outputs (verified)

- **Where**: [transcribe.py:940](scripts/transcribe.py:940) (selection slice),
  [transcribe.py:1022-1051](scripts/transcribe.py:1022) (`capture_records` built from
  `selected_captures` only), [transcribe.py:1059-1060](scripts/transcribe.py:1059)
  (unconditional overwrite of `book.md` and `captures.jsonl`),
  [transcribe.py:1096-1103](scripts/transcribe.py:1096) (manifest counts likewise
  selection-scoped).
- **What's wrong**: `book.md`, `captures.jsonl`, and manifest counts are rebuilt from
  only the captures selected this run. Reproduced empirically: seeded a 3-page book,
  ran full (book.md = 3 pages), then ran `--start-at 2` — book.md now contains **1**
  page and captures.jsonl **1** record.
- **Why it matters here**: the README explicitly recommends partial runs
  ("Transcribe 25 captures starting from index 100"). Following your own docs destroys
  the compiled transcript of a book you may have spent hours capturing and real API
  dollars transcribing. Recoverable only by noticing and re-running the full book
  (per-page results in `transcripts/canonical/` survive and are reused).
- **Fix**: assign capture ids over the *full* ordered capture list, then always rebuild
  `book.md`/`captures.jsonl` by merging this run's results with existing
  `canonical/*.json` for unselected captures. See TASKS.md T1 (includes the repro as an
  acceptance test).

### P1-2. Undeclared hard dependency on Python 3.14, enforced by the formatter (verified)

- **Where**: [extract.py:792](scripts/extract.py:792),
  [transcribe.py:122](scripts/transcribe.py:122),
  [transcribe.py:176](scripts/transcribe.py:176) — `except TypeError, ValueError:`
  (PEP 758, valid only on 3.14+); [pyproject.toml:2](pyproject.toml:2)
  (`target-version = "py314"`); no `requires-python` anywhere; README Setup says only
  `python3 -m venv .venv`.
- **What's wrong**: verified `SyntaxError: multiple exception types must be
  parenthesized` on Python 3.12. Worse: I applied the portable spelling
  `except (TypeError, ValueError):` as a quick win and **`make format` reverted it** —
  ruff under `target-version = "py314"` rewrites parenthesized two-type excepts back to
  the unparenthesized form. The toolchain is actively pinning you to a
  3.14-only dialect for zero benefit.
- **Why it matters here**: on any machine where `python3` < 3.14 (a new laptop, CI, a
  friend trying the repo), setup per README produces scripts that die on import with a
  confusing SyntaxError. Nothing else in the codebase needs 3.14; the actual floor
  after fixing is 3.11 (`datetime.UTC`).
- **Fix**: set `target-version = "py312"` (or add a `[project]` table with
  `requires-python = ">=3.12"`), parenthesize the three excepts, state the floor in the
  README. Config change was out of audit scope — see TASKS.md T2.

### P1-3. Auto-turn retry clicks can self-inflict the page skips the tool then reports as anomalies *(mechanism verified in code; not reproduced live)*

- **Where**: [extract.py:1065-1089](scripts/extract.py:1065)
  (`wait_for_turn_content_change`), specifically the guard at
  [extract.py:1079](scripts/extract.py:1079) and blind retry at
  [extract.py:1083-1087](scripts/extract.py:1083); called from the capture loop at
  [extract.py:2469-2476](scripts/extract.py:2469).
- **What's wrong**, two modes:
  1. If `previous_signature` is `None` (no `img`/`src` found on the prior page), line
     1079 can never be true, so the loop *always* runs the full 8 s **and fires up to
     two extra next-page clicks** at ~1 s and ~2 s. Those clicks advance real pages
     that are never screenshotted. The main loop then observes a page delta of +3 and
     logs a `jump_forward` anomaly — blaming Kindle for a skip the tool caused.
  2. Even with a valid signature, `retry_interval_ms=1000` means any page render slower
     than 1 s triggers a second click → double-advance → one page skipped.
  The wait loop also never polls the footer, so `changed_by_footer_value` (computed
  later at [extract.py:2478-2491](scripts/extract.py:2478)) can't stop retries early.
- **Why it matters here**: missing pages are the failure mode this whole project is
  built to avoid; each occurrence costs a manual `--capture-pages` backfill run. The
  anomaly telemetry added in recent commits (`59b6861`, `4f21821`) strongly suggests
  jumps are being observed in practice.
- **Fix**: never retry-click when `previous_signature` is `None`; pass the previous
  footer values into the wait loop and early-exit (and suppress retries) once the
  footer changes; raise the retry interval above worst-case render latency (~2.5 s).
  See TASKS.md T5, including a refactor that makes this unit-testable.

## P2 — real debt, not urgent

### P2-1. Zero automated tests over a large pure-logic core

- **Where**: no `tests/` directory exists. Meanwhile the trickiest invariants are pure
  functions: footer parsing incl. roman numerals
  ([extract.py:99-119](scripts/extract.py:99)), capture-spec parsing
  ([extract.py:797-836](scripts/extract.py:797)), canonical filename build/parse/sort
  ([extract.py:1170-1213](scripts/extract.py:1170),
  [extract.py:1437-1462](scripts/extract.py:1437)), coverage semantics
  ([extract.py:1308-1368](scripts/extract.py:1308)), TOC classification
  ([extract.py:692-721](scripts/extract.py:692)), fingerprint matching
  ([transcribe.py:229-245](scripts/transcribe.py:229)), filename metadata parsing
  ([transcribe.py:181-203](scripts/transcribe.py:181)).
- **Why it matters here**: these functions define what your archive *means*. A
  regression in filename building or coverage math silently mislabels every book
  processed afterward, and there is currently nothing that would catch it. The browser
  glue is legitimately hard to test; this core is trivially testable and carries most
  of the correctness risk.
- **Fix**: small pytest suite (~30 cases), no Playwright required. See TASKS.md T3.

### P2-2. No CI, and `make check` failed on a clean checkout

- **Where**: no `.github/workflows/`; at HEAD `ruff format --check` failed on
  [extract.py](scripts/extract.py) and [extract_library.py](scripts/extract_library.py)
  (verified 2026-07-03; two cosmetic hunks — fixed in this audit, see Quick Wins).
- **Why it matters here**: the repo defines a quality gate (`make check`) that nothing
  runs, so it drifts. CI would also have caught P1-2 the first time it ran on a stock
  runner image.
- **Fix**: one GitHub Actions workflow: `make check` + tests + `py_compile` on a matrix
  of {declared floor, 3.14}. See TASKS.md T4.

### P2-3. Monolithic `main()` and duplicated core logic across scripts

- **Where**: [extract.py:1872-2599](scripts/extract.py:1872) — a ~730-line `main()`
  spanning CLI parsing, login flow, TOC, two capture modes, and shutdown.
  `save_pages_manifest_best_effort` is defined twice
  ([extract.py:2180](scripts/extract.py:2180),
  [extract.py:2351](scripts/extract.py:2351));
  [go_to_page](scripts/extract.py:445) / [go_to_location](scripts/extract.py:483) are
  near-clones. Across scripts: `sanitize_slug` duplicated
  ([extract.py:1094](scripts/extract.py:1094) vs
  [transcribe.py:101](scripts/transcribe.py:101)); the canonical-filename regexes exist
  twice ([extract.py:1172/1182](scripts/extract.py:1172) vs
  [transcribe.py:190/197](scripts/transcribe.py:190)).
- **Why it matters here**: the duplicated filename parsers are a concrete drift hazard —
  change the naming scheme in extract.py and transcribe.py silently stops matching
  files (captures fall back to `unknown` handling rather than erroring). The monolith
  makes the capture loop — where P1-3 lives — hard to reason about or test.
- **Fix**: extract a shared module (naming, parsing, manifest IO), then split `main()`
  by mode. Do it *after* the test suite exists. See TASKS.md T6/T7.

## P3 — polish

- **P3-1. Roman-numeral pages are stored as fake "locations"**:
  [extract.py:113-117](scripts/extract.py:113) turns "Page ix of 452" into
  location 9 with `total_location` 452 (a *page* count) → files like
  `loc-0009-of-0452-v0001.png` mix two semantic spaces in `pages.json` and in
  transcript ordering. Works today by coincidence of sort order; document it or give
  front matter its own prefix (naming migration — needs your decision, TASKS.md T10).
- **P3-2. `created_at` can become `None`**:
  [transcribe.py:747-749](scripts/transcribe.py:747) uses
  `existing_payload.get("created_at")` without the string check its sibling at
  [transcribe.py:1063-1068](scripts/transcribe.py:1063) does; a canonical file missing
  the key yields `"created_at": null` in re-written results.
- **P3-3. `--seconds` unvalidated and int-only**:
  [extract.py:1876-1878](scripts/extract.py:1876); a negative value crashes
  `time.sleep` mid-run ([extract.py:2456](scripts/extract.py:2456)), and sub-second
  waits are impossible. `type=float` + `>= 0` check.
- **P3-4. Personal ASIN hardcoded as default**:
  [extract.py:1879](scripts/extract.py:1879) defaults `--asin` to `B00FO74WXA`, so a
  bare invocation silently operates on one specific book; transcribe.py makes it
  required ([transcribe.py:848](scripts/transcribe.py:848)). Make extract match.
- **P3-5. Dead condition**: `"ap/signin" in page.url` is a substring of the preceding
  `"signin" in page.url` check — [extract.py:2014](scripts/extract.py:2014),
  [extract_library.py:220](scripts/extract_library.py:220).
- **P3-6. Retries on non-transient errors**:
  [transcribe.py:438-452](scripts/transcribe.py:438) retries *any* exception; an
  invalid API key burns 3 attempts × 2 passes × N concurrent pages of backoff noise
  before failing. Fail fast on auth/4xx (keep retrying 429/5xx/timeouts).
- **P3-7. OCR cost hotspot**: both passes send the full image at `detail: "high"` to
  `gpt-5` ([transcribe.py:377](scripts/transcribe.py:377),
  [transcribe.py:416-435](scripts/transcribe.py:416)) — the QA pass roughly doubles
  per-book cost. An opt-out flag or cheaper/lower-detail QA default would materially
  cut spend on 400-page books.

## Security review

No P0/P1 security findings for this threat model (local, single-user, own data):

- **Secrets**: `OPENAI_API_KEY` only via env / gitignored `.env`
  ([.gitignore:6](.gitignore:6)), placeholder-only [.env.example](.env.example); full
  git-history scan for key patterns came back clean; the key is never printed.
- **Data**: `books/` (copyrighted content + reading data) is gitignored.
- **Dependencies**: exact-pinned; checked all four pins against the OSV database
  (2026-07-03). playwright 1.58.0, openai 2.24.0, ruff 0.15.4: clean.
  python-dotenv 1.2.1 has one advisory — GHSA-mf9w-mj56-hr94, symlink-following in
  `set_key()`/`unset_key()` (local attacker, fixed in 1.2.2). **Not exploitable
  here**: the scripts only call `load_dotenv()`. Still, bump the pin to `1.2.2`
  (TASKS.md T8e) so future use of `set_key` doesn't inherit it.
- **Injection surfaces**: none — no subprocess/shell/SQL; ASINs are slug-sanitized
  before path joins ([extract.py:1950](scripts/extract.py:1950),
  [transcribe.py:910](scripts/transcribe.py:910)); `page.evaluate` calls use static JS.
- **Awareness, not a finding**: `~/.kindle-reader-profile` holds a persistent,
  fully-authenticated Amazon browser session outside the repo. That's inherent to the
  design; it just means disk access equals Amazon account access — FileVault covers it.

## Genuinely well done — don't break these

1. **Idempotent, self-describing capture naming** — nav-keyed canonical filenames with
   variant suffixes ([extract.py:1437-1462](scripts/extract.py:1437)) that parse back
   losslessly ([extract.py:1170-1192](scripts/extract.py:1170)). Filesystem-as-database
   done right: re-runs are safe by default.
2. **Atomic manifest writes** — `pages.json` via tmp-file + `replace`
   ([extract.py:1427-1434](scripts/extract.py:1427)); Ctrl+C mid-run can't tear it.
3. **Honest coverage semantics** — distinguishing `raw_missing` / confirmed `missing` /
   `unresolved` candidates with an explicit `uncertain_gaps` status
   ([extract.py:1308-1368](scripts/extract.py:1308)). Refusing to overclaim
   completeness is rare discipline.
4. **Refuses to mislabel** — capture-pages mode skips the save unless the reader
   resolved to exactly the requested page, logging an anomaly instead
   ([extract.py:1825-1843](scripts/extract.py:1825)). Gaps over lies: correct call.
5. **OCR resume done correctly** — source-image fingerprint (path + size + mtime_ns)
   invalidates the cache when a page is re-captured
   ([transcribe.py:220-245](scripts/transcribe.py:220),
   [transcribe.py:723-742](scripts/transcribe.py:723)); verified working (both reuse
   and the dry-run/force paths behave as documented).
6. **Interrupt-safety throughout** — KeyboardInterrupt handled at every long stage with
   best-effort position restore ([extract.py:2567-2599](scripts/extract.py:2567)).
7. **Strict structured outputs** — JSON-schema-constrained responses plus
   defense-in-depth parsing/validation
   ([transcribe.py:40-64](scripts/transcribe.py:40),
   [transcribe.py:292-333](scripts/transcribe.py:292)).
8. **Thorough CLI validation** — mutual exclusions and range checks
   ([extract.py:1932-1947](scripts/extract.py:1932),
   [transcribe.py:899-908](scripts/transcribe.py:899)).
9. **README/flag parity** — every documented flag matches argparse; no docs drift found.

## Quick wins applied during this audit (uncommitted)

1. Ran the repo's own `make format` — two cosmetic hunks in
   [extract.py:1340](scripts/extract.py:1340) and
   [extract_library.py:70,102](scripts/extract_library.py:70); `make check` now passes
   on a clean checkout. No behavioral change.
2. **Attempted but rolled back by your own tooling**: parenthesizing the three
   `except A, B:` clauses — `make format` under `target-version = "py314"` rewrites
   them back (see P1-2). Left as TASKS.md T2 since the durable fix requires a
   pyproject.toml change, which was out of audit scope.

## Highest-conviction recommendation

Fix P1-1 first (TASKS.md T1). It is the only place the system destroys data it already
paid to produce, it's triggered by a workflow your own README recommends, the fix is
~20 lines, and the acceptance test is already written. Everything else in this audit
makes the tool better; this one stops it from eating its own output.
