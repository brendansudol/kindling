"""
Kindle hands-free page turner.

Opens a Kindle book in the browser and auto-advances pages
at a configurable interval so you can read hands-free.

Usage:
    python kindle-reader.py [--seconds 60] [--asin B00FO74WXA]

On first run, you'll need to log into Amazon manually.
Your session is saved so subsequent runs won't require login.
"""

import argparse
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def get_page_info(page):
    """Parse 'Page X of Y' from the Kindle footer."""
    text = page.evaluate("""
        const el = document.querySelector('.footer-label-color-default');
        el ? el.textContent : '';
    """)
    match = re.search(r"Page (\d+) of (\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def go_to_cover(page):
    """Navigate to the cover/first page via the table of contents."""
    # Open the TOC sidebar
    toc_btn = page.query_selector('[data-testid="top_menu_table_of_contents"]')
    if toc_btn:
        toc_btn.click()
        page.wait_for_timeout(1000)

    # Click the Cover entry
    cover_btn = page.query_selector('button.toc-item-button[aria-label="Cover"]')
    if cover_btn:
        cover_btn.click()
        page.wait_for_timeout(1000)
        # Close the TOC sidebar
        close_btn = page.query_selector('button.side-menu-close-button')
        if close_btn:
            close_btn.click()
            page.wait_for_timeout(500)
        return True

    return False


def on_page_turn(page, current, total):
    """Called each time a new page is displayed."""
    print(f"Page {current} of {total}" if current and total else "Page turned")


def main():
    parser = argparse.ArgumentParser(description="Kindle hands-free page turner")
    parser.add_argument(
        "--seconds", type=int, default=60, help="Seconds to wait per page (default: 60)"
    )
    parser.add_argument(
        "--asin", type=str, default="B00FO74WXA", help="Book ASIN to open"
    )
    parser.add_argument(
        "--pages", type=int, default=0, help="Number of pages to advance (0 = unlimited)"
    )
    parser.add_argument(
        "--no-restart", action="store_true", help="Resume from current page instead of starting from the beginning"
    )
    args = parser.parse_args()

    user_data_dir = Path.home() / ".kindle-reader-profile"

    url = f"https://read.amazon.com/?asin={args.asin}"

    with sync_playwright() as p:
        # Use persistent context to preserve Amazon login across runs
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        # Check if we need to log in
        if "signin" in page.url or "ap/signin" in page.url:
            print("Please log into Amazon in the browser window.")
            print("Waiting for you to complete login...")
            page.wait_for_url("**/read.amazon.com/**", timeout=300_000)  # 5 min
            print("Login detected! Waiting for book to load...")

        # Wait for the reader to be ready
        page.wait_for_selector("#kr-chevron-right", timeout=30_000)

        # Navigate to the beginning unless --no-restart is set
        if not args.no_restart:
            print("Navigating to the beginning...")
            if go_to_cover(page):
                print("Starting from the cover.")
            else:
                print("Could not find cover button — starting from current page.")

        current, total = get_page_info(page)
        if current and total:
            print(f"On page {current} of {total}.")
        print(f"Auto-advancing every {args.seconds}s. Press Ctrl+C to stop.\n")

        pages_turned = 0

        try:
            while True:
                if args.pages and pages_turned >= args.pages:
                    print(f"Done — advanced {pages_turned} pages.")
                    break

                # Check if we've reached the last page
                current, total = get_page_info(page)
                if current and total and current >= total:
                    print(f"Reached the last page ({current} of {total}). Done!")
                    break

                time.sleep(args.seconds)

                next_btn = page.query_selector("#kr-chevron-right")
                if not next_btn or not next_btn.is_visible():
                    print("Next button not found — likely at the end of the book.")
                    break

                next_btn.click()
                pages_turned += 1

                current, total = get_page_info(page)
                on_page_turn(page, current, total)

        except KeyboardInterrupt:
            print(f"\nStopped after {pages_turned} pages.")

        print("Closing in 5 seconds (press Ctrl+C again to close immediately)...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            pass

        context.close()


if __name__ == "__main__":
    main()
