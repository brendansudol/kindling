# Kindling

Hands-free Kindle page turner. Opens a book in the Kindle web reader and auto-advances pages at a configurable interval.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
playwright install chromium
```

## Usage

```bash
python kindle-reader.py [--seconds 1] [--asin B00FO74WXA] [--pages 0] [--no-restart] [--no-metadata] [--include-end-matter] [--refresh-toc]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--seconds` | 1 | Seconds to wait per page |
| `--asin` | B00FO74WXA | Book ASIN (from the Amazon book URL) |
| `--pages` | 0 | Number of pages to advance (0 = unlimited) |
| `--no-restart` | off | Resume from current page instead of starting from the cover |
| `--no-metadata` | off | Disable network metadata capture and `metadata.json` output |
| `--include-end-matter` | off | Disable TOC-based trimming and include end matter |
| `--refresh-toc` | off | Ignore existing `toc.json` and rebuild TOC from browser |

### Examples

```bash
# Quick test — flip 5 pages every 3 seconds (Shadow of the Hegemon)
python kindle-reader.py --asin B00FO74WXA --seconds 3 --pages 5

# Read at a relaxed pace, unlimited pages
python kindle-reader.py --asin B00FO74WXA --seconds 90

# Resume from current page instead of restarting from the cover
python kindle-reader.py --asin B00FO74WXA --no-restart

```

On first run, you'll need to log into Amazon in the browser window. Your session is saved to `~/.kindle-reader-profile` so subsequent runs won't require login.

By default, screenshots are saved to `./books/<asin>/pages` (for example `./books/B00FO74WXA/pages`) and created automatically if needed.
Screenshots target the main Kindle content element for cleaner captures, with an automatic viewport fallback if that element is unavailable.
The script also applies reader settings (Single Column + Amazon Ember) for more consistent captures when possible.
The script also applies a best-effort top-header motion fix to reduce flaky TOC/settings interactions.
If Kindle exposes location instead of page numbers, screenshot filenames use `loc-<current>-of-<total>`.
The script also saves intercepted Kindle metadata to `./books/<asin>/metadata.json`.
The script saves parsed table-of-contents data to `./books/<asin>/toc.json`.
If `toc.json` already exists, the script reuses it and skips TOC browser traversal by default.
By default, if a likely end-matter boundary is detected from TOC entries, capture stops at the last content page/location before that boundary.
Use `--include-end-matter` to capture through the full book.
Use `--refresh-toc` to force rebuilding TOC from the browser.

Press `Ctrl+C` to stop at any time.
