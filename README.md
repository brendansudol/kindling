# Kindling

Tools to get more out of your digital book collection.

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
python scripts/extract.py [--seconds 1] [--asin B00FO74WXA] [--pages 0] [--start-page 1|--start-location 1] [--no-restart] [--no-metadata] [--include-end-matter] [--refresh-toc] [--no-restore-position] [--overwrite-existing]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--seconds` | 1 | Seconds to wait per page |
| `--asin` | B00FO74WXA | Book ASIN (from the Amazon book URL) |
| `--pages` | 0 | Number of pages to advance (0 = unlimited) |
| `--start-page` | off | Jump to a specific page before capture starts (mutually exclusive with `--start-location`) |
| `--start-location` | off | Jump to a specific location before capture starts (mutually exclusive with `--start-page`) |
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
```

## Transcribe pages

Set your API key (the scripts auto-load `.env` via `python-dotenv`):

```bash
# Recommended: put key in .env
OPENAI_API_KEY=your_key_here

# Optional: export directly in shell instead
# export OPENAI_API_KEY=your_key_here
```

Run transcription:

```bash
python scripts/transcribe.py --asin B00FO74WXA [--model gpt-5] [--qa-model gpt-5] [--start-at 0] [--max-pages 0] [--force] [--dry-run] [--max-retries 3] [--max-output-tokens 4000]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--asin` | required | Book ASIN (maps to `books/<asin>/`) |
| `--model` | gpt-5 | Vision OCR model for pass 1 |
| `--qa-model` | gpt-5 | QA correction model for pass 2 |
| `--start-at` | 0 | Start index in ordered capture list |
| `--max-pages` | 0 | Max captures after start index (0 = all) |
| `--force` | off | Re-run OCR even when canonical output exists |
| `--dry-run` | off | Show planned workload without API calls |
| `--max-retries` | 3 | Retry attempts per OCR pass on transient failures |
| `--max-output-tokens` | 4000 | Maximum output tokens for each model response |

### Examples

```bash
# Transcribe everything for a book
python scripts/transcribe.py --asin B00FO74WXA

# Transcribe 25 captures starting from index 100
python scripts/transcribe.py --asin B00FO74WXA --start-at 100 --max-pages 25

# Preview workload only
python scripts/transcribe.py --asin B00FO74WXA --dry-run
```

## How it works

- Opens your book in Kindle Cloud Reader via Playwright
- Saves screenshots to `./books/<asin>/pages/` with canonical nav-keyed names (`page-0238-of-0452.png`, `loc-0002-of-6446.png`)
- Captures normalized metadata and TOC to `metadata.json`, `toc.json`, and `pages.json`
- Transcribes screenshots with OpenAI (2-pass OCR + QA) into `./books/<asin>/transcripts/`
- Auto-stops at end-matter boundaries (acknowledgements, about the author, etc.)
- Restores your reading position when done

## Transcript outputs

The transcription script writes:

- `books/<asin>/transcripts/manifest.json` (run metadata and counts)
- `books/<asin>/transcripts/captures.jsonl` (one record per capture)
- `books/<asin>/transcripts/canonical/*.json` (one result per captured page image)
- `books/<asin>/transcripts/book.md` (compiled transcript in reading order)

## Notes

- First run requires Amazon login; session is persisted to `~/.kindle-reader-profile`
- Applies Single Column + Amazon Ember font for consistent captures
- `metadata.json` stores normalized fields only (`asin`, `title`, `authors`, `captured_at`, `sources`)
- Capture is idempotent by default: existing nav-keyed files are skipped unless `--overwrite-existing` is set
- Pages with unknown footer navigation are skipped (no unstable `unknown` files are written)
- Transcription resumes from saved per-capture canonical results and auto re-runs when source image path/mtime/size changes (or use `--force`)
- Press `Ctrl+C` to stop at any time
