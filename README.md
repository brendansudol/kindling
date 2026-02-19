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
python kindle-reader.py [--seconds 60] [--asin B00FO74WXA] [--pages 0]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--seconds` | 60 | Seconds to wait per page |
| `--asin` | B00FO74WXA | Book ASIN (from the Amazon book URL) |
| `--pages` | 0 | Number of pages to advance (0 = unlimited) |

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

Press `Ctrl+C` to stop at any time.
