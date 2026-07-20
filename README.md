# Kindling

Tools to get more out of your digital book collection.

For the complete one-book workflow—from Kindle extraction through independently
audited analysis—follow the [end-to-end book playbook](docs/BOOK_PLAYBOOK.md).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Optional: lint/format tooling
pip install -r requirements-dev.txt
playwright install chromium
cp .env.example .env
```

## Code quality

```bash
# Lint
make lint

# Auto-fix lint issues (including import sorting)
make lint-fix

# Check formatting
make format-check

# Apply formatting
make format

# Lint + format check
make check
```

## Extract pages

```bash
python scripts/extract.py [--seconds 1] [--asin B00FO74WXA] [--pages 0] [--start-page 1|--start-location 1] [--capture-pages 50-55,114,140] [--no-restart] [--no-metadata] [--include-end-matter] [--refresh-toc] [--no-restore-position] [--overwrite-existing]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--seconds` | 1 | Seconds to wait per page |
| `--asin` | B00FO74WXA | Book ASIN (from the Amazon book URL) |
| `--pages` | 0 | Number of pages to advance (0 = unlimited) |
| `--start-page` | off | Jump to a specific page before capture starts (mutually exclusive with `--start-location`) |
| `--start-location` | off | Jump to a specific location before capture starts (mutually exclusive with `--start-page`) |
| `--capture-pages` | off | Capture an explicit page list/ranges using Go to Page (e.g. `50-55,114,140`) |
| `--no-restart` | off | Resume from current page instead of starting from the cover |
| `--no-metadata` | off | Disable network metadata capture and `metadata.json` output |
| `--include-end-matter` | off | Disable TOC-based trimming and include end matter |
| `--refresh-toc` | off | Ignore existing `toc.json` and rebuild TOC from browser |
| `--no-restore-position` | off | Keep current reader position at exit instead of restoring start position |
| `--overwrite-existing` | off | Replace existing nav-keyed screenshot files (default is skip existing files) |

### Examples

```bash
# Quick test — flip 5 pages every 3 seconds
python scripts/extract.py --asin B00FO74WXA --seconds 3 --pages 5

# Resume from current page instead of restarting from the cover
python scripts/extract.py --asin B00FO74WXA --no-restart

# Jump directly to a specific page before capture starts
python scripts/extract.py --asin B00FO74WXA --start-page 238 --pages 5

# Jump directly to a specific location before capture starts
python scripts/extract.py --asin B00FO74WXA --start-location 250 --pages 5

# Re-capture and overwrite already saved pages
python scripts/extract.py --asin B00FO74WXA --overwrite-existing

# Capture an exact list/ranges of pages (no long auto-turn run)
python scripts/extract.py --asin B00FO74WXA --capture-pages 50-55,114,140 --no-metadata
```

## Extract library

```bash
python scripts/extract_library.py [--headless] [--output books/library.json] [--max-scroll-steps 800] [--scroll-pause-ms 900] [--stagnant-rounds 6]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | Kindle library URL | Source library URL (resourceType=EBOOK + acquisition desc) |
| `--output` | `books/library.json` | Output JSON path for extracted library books |
| `--max-scroll-steps` | 800 | Maximum number of scroll actions while loading lazy content |
| `--scroll-pause-ms` | 900 | Wait time after each scroll action |
| `--stagnant-rounds` | 6 | Stop after this many no-growth rounds while near bottom |
| `--headless` | off | Run Chromium in headless mode |

### Example

```bash
# Load all books in Kindle library and save asin/title/author/cover URL
python scripts/extract_library.py --headless --output books/library.json
```

## Transcribe pages

Set your API key (the scripts auto-load `.env` via `python-dotenv`):

```bash
# Recommended: put key in .env
GEMINI_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here  # used only if Gemini declines a page as RECITATION

# Optional: export directly in shell instead
# export GEMINI_API_KEY=your_key_here
```

Run transcription:

```bash
python scripts/transcribe.py --asin B00FO74WXA [--model gemini-3.5-flash] [--image-detail high] [--thinking-level minimal] [--no-fallback] [--start-at 0] [--max-pages 0] [--concurrency 2] [--force] [--dry-run] [--max-retries 3] [--max-output-tokens 4096]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--asin` | required | Book ASIN (maps to `books/<asin>/`) |
| `--model` | gemini-3.5-flash | Gemini vision OCR model |
| `--image-detail` | high | Image media resolution (`low`, `medium`, or `high`) |
| `--thinking-level` | minimal | Gemini thinking level (`minimal`, `low`, `medium`, or `high`) |
| `--no-fallback` | off | Disable the GPT-5.6 Luna fallback for Gemini `RECITATION` responses |
| `--start-at` | 0 | Start index in ordered capture list |
| `--max-pages` | 0 | Max captures after start index (0 = all) |
| `--concurrency` | 2 | Number of captures to transcribe concurrently |
| `--force` | off | Re-run OCR even when canonical output exists |
| `--dry-run` | off | Show planned workload without API calls |
| `--max-retries` | 3 | Retry attempts per OCR request on transient failures |
| `--max-output-tokens` | 4096 | Maximum output tokens for each model response |

### Examples

```bash
# Transcribe everything for a book
python scripts/transcribe.py --asin B00FO74WXA

# Transcribe 25 captures starting from index 100
python scripts/transcribe.py --asin B00FO74WXA --start-at 100 --max-pages 25

# Transcribe with 2 workers in parallel
python scripts/transcribe.py --asin B00FO74WXA --concurrency 2

# Preview workload only
python scripts/transcribe.py --asin B00FO74WXA --dry-run
```

## How it works

- Opens your book in Kindle Cloud Reader via Playwright
- Saves screenshots to `./books/<asin>/pages/` with canonical nav-keyed names (`page-0238-of-0452-v0001.png`, `loc-0002-of-6446-v0001.png`); when content changes but footer nav repeats, variant captures increment (`-v0002`, `-v0003`, ...)
- Captures normalized metadata and TOC to `metadata.json`, `toc.json`, and `pages.json`
- Transcribes screenshots with one Gemini 3.5 Flash OCR pass into `./books/<asin>/transcripts/`
- Uses GPT-5.6 Luna only when Gemini returns `RECITATION` without OCR text; the canonical record retains both providers' usage, cost, and response metadata
- On macOS, uses local Vision OCR as a final no-cost fallback if Luna also returns `content_filter`; local results are deliberately low-confidence review items because typography cannot be recovered reliably
- Auto-stops at end-matter boundaries (acknowledgements, about the author, etc.)
- Best-effort: restores your reading position when done

## `pages.json` diagnostics

`pages.json` is written incrementally during extraction and now includes:

- `coverage`: capture completeness diagnostics
  - `raw_missing_pages`: all uncaptured page numbers in `1..expected_total_pages`
  - `missing_pages`: confirmed missing pages (excludes unresolved jump candidates)
  - `unresolved_missing_pages`: uncaptured pages that may be composite/implicit due jump behavior
  - `status`: one of `complete`, `uncertain_gaps`, `incomplete`, `unknown_total`
- `anomalies`: cumulative event log for navigation irregularities across runs
  - `auto_turn_delta`: emitted when next-page iteration observes page deltas other than `+1`
  - `capture_pages_resolution`: emitted when `--capture-pages` cannot resolve exactly (mismatch, location-only, navigation failure, skipped unknown)

`coverage.unresolved_page_candidates` indicates pages affected by observed navigation anomalies. Treat these as unresolved candidates, not guaranteed hard gaps.

## Transcript outputs

The transcription script writes:

- `books/<asin>/transcripts/manifest.json` (run metadata and counts)
- `books/<asin>/transcripts/captures.jsonl` (one record per capture)
- `books/<asin>/transcripts/canonical/*.json` (one result per captured page image)
- `books/<asin>/transcripts/book.md` (compiled transcript in reading order)
- `books/<asin>/transcripts/review.json` (captures flagged by deterministic quality checks)

## Build verified transcript sections

After `analysis/chapter-map.md` has been verified, build reproducible section-sized
transcript views:

```bash
python scripts/build_sections.py --asin B00FO74WXA
```

This writes:

- `books/<asin>/transcripts/sections.json` (source hashes, resolved boundaries, capture
  IDs, character offsets, completeness, warnings, and output hashes)
- `books/<asin>/transcripts/sections/*.md` (one derived view for every mapped unit,
  including front and back matter)

`book.md` and the canonical capture records remain the sources of truth. Section files
must not be edited manually; regenerate them after changing the transcript or chapter
map. Boundary resolution prefers section-title text within the mapped capture and falls
back to the first eligible capture boundary when no title is visible. Marker fallbacks
are recorded in `sections.json` for review.

When repeated captures share a marker and the next section has no printed title, record
the reviewed start in `analysis/section-boundaries.json`:

```json
{
  "schema_version": 1,
  "sections": {
    "5": {
      "capture_id": "page-0004-of-0157-v0002"
    },
    "12": {
      "capture_id": "loc-1742-of-5212-v0002",
      "match_text": "Chapter 4"
    },
    "27": {
      "capture_id": "loc-3403-of-4548-v0001",
      "allow_marker_mismatch": true,
      "reason": "The mapped anchor contains only the preceding chapter's conclusion."
    }
  }
}
```

An optional `match_text` (and one-based `match_occurrence`) resolves a boundary inside
the chosen capture. Reviewed overrides are hashed into `sections.json`, so later edits
make `--check` report the derived files as stale. If the first visible section text is
at a different marker from the mapped structural anchor, `allow_marker_mismatch` also
requires a human-readable `reason`; the reason is copied into the section manifest.

Useful checks:

```bash
# Resolve and report boundaries without writing files
python scripts/build_sections.py --asin B00FO74WXA --dry-run

# Reject unresolved marker fallbacks; title matches and reviewed overrides are accepted
python scripts/build_sections.py --asin B00FO74WXA --dry-run --fail-on-warnings

# Detect changed sources, maps, capture text, or derived files
python scripts/build_sections.py --asin B00FO74WXA --check
```

## Notes

- First run requires Amazon login; session is persisted to `~/.kindle-reader-profile`
- Applies Single Column + Amazon Ember font for consistent captures
- `metadata.json` stores normalized fields only (`asin`, `title`, `authors`, `captured_at`, `sources`)
- Capture is idempotent by default: existing nav-keyed files are skipped unless `--overwrite-existing` is set
- Kindle `Go to Page` vs `Go to Location` is context-dependent; for `--start-location`, the script may prime via TOC-first-entry fallback before retrying location navigation, and the resolved visible location may differ from the requested value
- In `--capture-pages` mode, screenshots are only saved when Kindle resolves exactly to the requested page; mismatches are logged as anomalies and skipped
- Pages with unknown footer navigation are skipped (no unstable `unknown` files are written)
- Transcription resumes from saved per-capture canonical results and auto re-runs when source image path/mtime/size changes (or use `--force`)
- Transcription also invalidates cached results when the model, image detail, thinking level, or prompt version changes
- See [`docs/OCR_MODEL_BENCHMARK.md`](docs/OCR_MODEL_BENCHMARK.md) for the model experiment and production rationale
- Press `Ctrl+C` to stop at any time
