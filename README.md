# Kindling

Tools to get more out of your digital book collection.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
playwright install chromium
```

## Usage

```bash
python scripts/extract.py [--seconds 1] [--asin B00FO74WXA] [--pages 0] [--start-page 1] [--no-restart] [--no-metadata] [--include-end-matter] [--refresh-toc] [--no-restore-position]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--seconds` | 1 | Seconds to wait per page |
| `--asin` | B00FO74WXA | Book ASIN (from the Amazon book URL) |
| `--pages` | 0 | Number of pages to advance (0 = unlimited) |
| `--start-page` | off | Jump to a specific page before capture starts |
| `--no-restart` | off | Resume from current page instead of starting from the cover |
| `--no-metadata` | off | Disable network metadata capture and `metadata.json` output |
| `--include-end-matter` | off | Disable TOC-based trimming and include end matter |
| `--refresh-toc` | off | Ignore existing `toc.json` and rebuild TOC from browser |
| `--no-restore-position` | off | Keep current reader position at exit instead of restoring start position |

### Examples

```bash
# Quick test — flip 5 pages every 3 seconds
python scripts/extract.py --asin B00FO74WXA --seconds 3 --pages 5

# Resume from current page instead of restarting from the cover
python scripts/extract.py --asin B00FO74WXA --no-restart

# Jump directly to a specific page before capture starts
python scripts/extract.py --asin B00FO74WXA --start-page 238 --pages 5
```

## How it works

- Opens your book in Kindle Cloud Reader via Playwright
- Screenshots each page to `./books/<asin>/pages/`
- Captures metadata and TOC to `metadata.json`, `toc.json`, and `pages.json`
- Auto-stops at end-matter boundaries (acknowledgements, about the author, etc.)
- Restores your reading position when done

## Notes

- First run requires Amazon login; session is persisted to `~/.kindle-reader-profile`
- Applies Single Column + Amazon Ember font for consistent captures
- Falls back to location-based filenames when page numbers aren't available
- Press `Ctrl+C` to stop at any time
