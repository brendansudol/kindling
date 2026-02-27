"""
Kindle hands-free page turner.

Opens a Kindle book in the browser and auto-advances pages
at a configurable interval so you can read hands-free.

Usage:
    python kindle-reader.py [--seconds 60] [--asin B00FO74WXA] [--pages 0]
                            [--no-restart] [--no-metadata]

On first run, you'll need to log into Amazon manually.
Your session is saved so subsequent runs won't require login.
"""

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

CONTENT_CAPTURE_SELECTORS = (
    "#kr-renderer .kg-full-page-img img",
    "#kr-renderer .kg-full-page-img",
    "#kr-renderer",
)
ALERT_ROOT_SELECTORS = ("ion-alert", '[role="alertdialog"]')
READER_HEADER_SELECTOR = "#reader-header"
READER_SETTINGS_TEST_ID = "top_menu_reader_settings"
FOOTER_TEXT_SELECTORS = (
    'ion-title[item-i-d="reader-footer-title"] .text-div',
    "ion-footer ion-title",
    ".footer-label-color-default",
)
ROMAN_NUMERALS = {
    "I": 1,
    "V": 5,
    "X": 10,
    "L": 50,
    "C": 100,
    "D": 500,
    "M": 1000,
}


def deromanize(roman):
    """Convert a Roman numeral string to an integer."""
    value = 0
    previous = 0
    for char in roman.upper()[::-1]:
        numeral = ROMAN_NUMERALS.get(char)
        if numeral is None:
            return None
        if numeral < previous:
            value -= numeral
        else:
            value += numeral
            previous = numeral
    return value


def parse_footer_nav_text(text):
    """Parse footer text into page/location navigation values."""
    if not text:
        return None, None, None, None

    normalized = " ".join(text.split())
    page_match = re.search(r"page\s+(\d+)\s+of\s+(\d+)", normalized, re.IGNORECASE)
    if page_match:
        return int(page_match.group(1)), int(page_match.group(2)), None, None

    location_match = re.search(
        r"location\s+(\d+)\s+of\s+(\d+)", normalized, re.IGNORECASE
    )
    if location_match:
        return None, None, int(location_match.group(1)), int(location_match.group(2))

    roman_match = re.search(
        r"page\s+([ivxlcdm]+)\s+of\s+(\d+)", normalized, re.IGNORECASE
    )
    if roman_match:
        location = deromanize(roman_match.group(1))
        if location is not None:
            return None, None, location, int(roman_match.group(2))

    return None, None, None, None


def get_footer_text(page, timeout_ms=800):
    """Return footer text from the first visible selector match."""
    for selector in FOOTER_TEXT_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0 or not locator.is_visible():
                continue
            text = locator.text_content()
            if text and text.strip():
                return text.strip()
        except Exception:
            continue

    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        for selector in FOOTER_TEXT_SELECTORS:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                if not locator.is_visible():
                    continue
                text = locator.text_content()
                if text and text.strip():
                    return text.strip()
            except Exception:
                continue
        page.wait_for_timeout(100)
    return None


def get_page_info(page):
    """Parse page/location navigation values from the Kindle footer."""
    return parse_footer_nav_text(get_footer_text(page))


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


def on_page_turn(page, current, total, current_location=None, total_location=None):
    """Called each time a new page is displayed."""
    if current is not None and total is not None:
        print(f"Page {current} of {total}")
    elif current_location is not None and total_location is not None:
        print(f"Location {current_location} of {total_location}")
    else:
        print("Page turned")


def dismiss_possible_alert(page):
    """Dismiss a blocking reader alert if present."""
    roots = []
    for selector in ALERT_ROOT_SELECTORS:
        root = page.locator(selector).first
        try:
            if root.count() > 0 and root.is_visible():
                roots.append(root)
        except Exception:
            continue

    if not roots:
        return False

    for root in roots:
        try:
            no_btn = root.locator("button", has_text="No").first
            if no_btn.count() > 0 and no_btn.is_visible():
                no_btn.click()
                page.wait_for_timeout(200)
                return True
        except Exception:
            continue

    for root in roots:
        for selector in (
            "button[aria-label='Close']",
            "button[title='Close']",
            ".alert-button-role-cancel",
        ):
            try:
                close_btn = root.locator(selector).first
                if close_btn.count() > 0 and close_btn.is_visible():
                    close_btn.click()
                    page.wait_for_timeout(200)
                    return True
            except Exception:
                continue

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        return True
    except Exception:
        return False


def click_next_button(page):
    """Click the next-page button if available."""
    next_btn = page.query_selector("#kr-chevron-right")
    if not next_btn or not next_btn.is_visible():
        return False
    next_btn.click()
    return True


def apply_reader_settings(page):
    """Best-effort: apply stable reader settings for capture consistency."""
    try:
        header = page.locator(READER_HEADER_SELECTOR).first
        if header.count() > 0:
            header.hover(force=True)
            page.wait_for_timeout(150)

        settings_button = page.get_by_test_id(READER_SETTINGS_TEST_ID).first
        if settings_button.count() == 0:
            return False
        settings_button.wait_for(state="visible", timeout=5_000)
        settings_button.click()
        page.wait_for_timeout(700)
    except Exception:
        return False

    applied_any = False

    try:
        font_option = page.locator("#AmazonEmber").first
        if font_option.count() > 0 and font_option.is_visible():
            font_option.click()
            applied_any = True
            page.wait_for_timeout(200)
    except Exception:
        pass

    try:
        single_column = page.locator(
            '[role="radiogroup"][aria-label$=" columns"]',
            has_text="Single Column",
        ).first
        if single_column.count() > 0 and single_column.is_visible():
            single_column.click()
            applied_any = True
            page.wait_for_timeout(200)
    except Exception:
        pass

    # Best-effort close of the settings panel.
    try:
        if header.count() > 0:
            header.hover(force=True)
            page.wait_for_timeout(100)
        if settings_button.count() > 0 and settings_button.is_visible():
            settings_button.click()
            page.wait_for_timeout(200)
    except Exception:
        pass

    return applied_any


def wait_for_turn_content_change(
    page,
    previous_signature,
    timeout_seconds=8,
    poll_interval_ms=100,
    max_retries=2,
    retry_interval_ms=1000,
):
    """Wait for content identity change and retry clicking next if needed."""
    deadline = time.time() + timeout_seconds
    retries_used = 0
    next_retry_at = time.time() + (retry_interval_ms / 1000)
    while time.time() < deadline:
        current_signature = get_content_signature(page)
        if (
            previous_signature
            and current_signature
            and current_signature != previous_signature
        ):
            return True, retries_used

        now = time.time()
        if retries_used < max_retries and now >= next_retry_at:
            if click_next_button(page):
                retries_used += 1
                print(f"Info: retrying next click ({retries_used}/{max_retries})...")
            next_retry_at = now + (retry_interval_ms / 1000)
        page.wait_for_timeout(poll_interval_ms)
    return False, retries_used


def sanitize_slug(value):
    """Convert a string into a filesystem-safe slug."""
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "book"


def parse_jsonp_response(body):
    """Extract a JSON object from a JSONP response body."""
    start = body.find("(")
    end = body.rfind(")")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Invalid JSONP response")
    return json.loads(body[start + 1 : end])


def normalize_authors(raw):
    """Normalize Kindle author payloads into a simple list of names."""
    if not isinstance(raw, list):
        return []

    authors = []
    for item in raw:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("authorName")
        else:
            name = None

        if isinstance(name, str):
            trimmed = name.strip()
            if trimmed:
                authors.append(trimmed)
    return authors


def build_flattened_metadata(target_asin, info_payload=None, yj_payload=None):
    """Build normalized metadata from intercepted network payloads."""
    meta_asin = None
    title = None
    authors = []

    if isinstance(yj_payload, dict):
        raw_asin = yj_payload.get("asin")
        if raw_asin is not None:
            meta_asin = str(raw_asin)

        raw_title = yj_payload.get("title")
        if raw_title is not None:
            title = str(raw_title)

        authors = normalize_authors(
            yj_payload.get("authorsList") or yj_payload.get("authorList")
        )

    return {
        "asin": meta_asin or target_asin,
        "title": title,
        "authors": authors,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "start_reading": info_payload is not None,
            "yj_metadata": yj_payload is not None,
        },
        "info": info_payload,
        "meta": yj_payload,
    }


def save_metadata(metadata_path, metadata):
    """Write metadata JSON to disk."""
    metadata_path.write_text(
        json.dumps(metadata, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"Saved metadata: {metadata_path}")


def iter_capture_locators(page):
    """Yield visible Kindle content locators in preference order."""
    for selector in CONTENT_CAPTURE_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            if not locator.is_visible():
                continue
            box = locator.bounding_box()
            if not box:
                continue
            if box.get("width", 0) <= 1 or box.get("height", 0) <= 1:
                continue
            yield selector, locator
        except Exception:
            continue


def get_content_signature(page):
    """Return a best-effort content signature for turn detection."""
    for selector, locator in iter_capture_locators(page):
        try:
            src = locator.get_attribute("src")
            if src:
                return f"{selector}|src:{src}"

            nested_src = locator.evaluate("""
                (el) => {
                    const node = el.tagName?.toLowerCase() === "img" ? el : el.querySelector("img");
                    if (!node) return null;
                    return node.getAttribute("src") || node.currentSrc || null;
                }
            """)
            if nested_src:
                return f"{selector}|nested-src:{nested_src}"
        except Exception:
            continue
    return None


def save_page_screenshot(
    page,
    screenshots_dir,
    sequence_number,
    current=None,
    total=None,
    current_location=None,
    total_location=None,
):
    """Save the current content area to disk; fallback to viewport if needed."""
    if current is not None and total is not None:
        filename = f"capture-{sequence_number:04d}-page-{current:04d}-of-{total:04d}.png"
    elif current_location is not None and total_location is not None:
        width = max(4, len(str(total_location)))
        filename = (
            f"capture-{sequence_number:04d}-loc-{current_location:0{width}d}"
            f"-of-{total_location:0{width}d}.png"
        )
    elif current is not None:
        filename = f"capture-{sequence_number:04d}-page-{current:04d}.png"
    elif current_location is not None:
        width = max(4, len(str(current_location)))
        filename = f"capture-{sequence_number:04d}-loc-{current_location:0{width}d}.png"
    else:
        filename = f"capture-{sequence_number:04d}-page-unknown.png"

    screenshot_path = screenshots_dir / filename
    had_content_candidate = False
    for _selector, locator in iter_capture_locators(page):
        had_content_candidate = True
        try:
            locator.screenshot(path=str(screenshot_path))
            print(f"Saved screenshot: {screenshot_path}")
            return screenshot_path
        except Exception:
            continue

    if had_content_candidate:
        print("Warning: content element capture failed; using viewport screenshot.")
    else:
        print("Warning: content element not found; using viewport screenshot.")
    page.screenshot(path=str(screenshot_path))

    print(f"Saved screenshot: {screenshot_path}")
    return screenshot_path


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
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Disable network metadata capture and metadata.json output",
    )
    args = parser.parse_args()

    user_data_dir = Path.home() / ".kindle-reader-profile"
    book_dir = Path.cwd() / "books" / sanitize_slug(args.asin)
    screenshots_dir = book_dir / "pages"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = book_dir / "metadata.json"

    url = f"https://read.amazon.com/?asin={args.asin}"

    with sync_playwright() as p:
        # Use persistent context to preserve Amazon login across runs
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()
        if args.no_metadata:
            print("Metadata capture disabled (--no-metadata).")
        else:
            target_asin = args.asin.lower()
            intercepted = {"info": None, "meta": None}
            parse_warning_printed = False

            def on_response(response):
                nonlocal parse_warning_printed
                try:
                    if response.status != 200:
                        return

                    parsed = urlparse(response.url)
                    path = parsed.path or ""

                    if (
                        parsed.hostname == "read.amazon.com"
                        and path == "/service/mobile/reader/startReading"
                    ):
                        query_asin = parse_qs(parsed.query).get("asin", [None])[0]
                        if query_asin and query_asin.lower() == target_asin:
                            intercepted["info"] = response.json()
                        return

                    if path.endswith("YJmetadata.jsonp"):
                        payload = parse_jsonp_response(response.text())
                        if not isinstance(payload, dict):
                            return

                        payload_asin = payload.get("asin")
                        if payload_asin and str(payload_asin).lower() != target_asin:
                            return

                        intercepted["meta"] = payload
                except Exception:
                    # Network interception is best-effort; continue reading on parse failures.
                    if not parse_warning_printed:
                        print("Warning: metadata response parsing failed for one request.")
                        parse_warning_printed = True

            page.on("response", on_response)
            print("Metadata capture enabled (network intercept).")
        page.goto(url, wait_until="domcontentloaded")

        # Check if we need to log in
        if "signin" in page.url or "ap/signin" in page.url:
            print("Please log into Amazon in the browser window.")
            print("Waiting for you to complete login...")
            page.wait_for_url("**/read.amazon.com/**", timeout=300_000)  # 5 min
            print("Login detected! Waiting for book to load...")

        # Wait for the reader to be ready
        page.wait_for_selector("#kr-chevron-right", timeout=30_000)
        if dismiss_possible_alert(page):
            print("Info: dismissed blocking alert.")
        if apply_reader_settings(page):
            print("Info: applied reader settings (Single Column + Amazon Ember).")
        else:
            print("Warning: could not fully apply reader settings; continuing.")
        if not args.no_metadata:
            page.wait_for_timeout(1000)
            metadata = build_flattened_metadata(
                args.asin,
                intercepted["info"],
                intercepted["meta"],
            )
            if not metadata["sources"]["start_reading"]:
                print("Warning: startReading metadata response was not captured.")
            if not metadata["sources"]["yj_metadata"]:
                print("Warning: YJmetadata.jsonp response was not captured.")
            save_metadata(metadata_path, metadata)

        # Navigate to the beginning unless --no-restart is set
        if not args.no_restart:
            print("Navigating to the beginning...")
            if go_to_cover(page):
                print("Starting from the cover.")
            else:
                print("Could not find cover button — starting from current page.")

        current, total, current_location, total_location = get_page_info(page)
        if current is not None and total is not None:
            print(f"On page {current} of {total}.")
        elif current_location is not None and total_location is not None:
            print(f"On location {current_location} of {total_location}.")
        print(f"Saving page screenshots to {screenshots_dir}")
        print(f"Auto-advancing every {args.seconds}s. Press Ctrl+C to stop.\n")

        pages_turned = 0
        screenshots_taken = 0

        # Capture the page currently on screen before any turns.
        save_page_screenshot(
            page,
            screenshots_dir,
            screenshots_taken + 1,
            current,
            total,
            current_location,
            total_location,
        )
        screenshots_taken += 1

        try:
            while True:
                if args.pages and pages_turned >= args.pages:
                    print(f"Done — advanced {pages_turned} pages.")
                    break

                # Check if we've reached the last page
                current, total, current_location, total_location = get_page_info(page)
                has_page_bounds = current is not None and total is not None
                has_location_bounds = (
                    current_location is not None and total_location is not None
                )
                if has_page_bounds and current >= total:
                    print(f"Reached the last page ({current} of {total}). Done!")
                    break
                if (
                    not has_page_bounds
                    and has_location_bounds
                    and current_location >= total_location
                ):
                    print(
                        f"Reached the last location ({current_location} of {total_location}). Done!"
                    )
                    break

                if dismiss_possible_alert(page):
                    print("Info: dismissed blocking alert.")

                previous_page = current
                previous_location = current_location
                previous_signature = get_content_signature(page)
                time.sleep(args.seconds)

                if not click_next_button(page):
                    print("Next button not found — likely at the end of the book.")
                    break

                changed_by_signature, _retries_used = wait_for_turn_content_change(
                    page,
                    previous_signature,
                    timeout_seconds=8,
                    poll_interval_ms=100,
                    max_retries=2,
                    retry_interval_ms=1000,
                )
                current, total, current_location, total_location = get_page_info(page)
                changed_by_page_number = (
                    previous_page is not None
                    and current is not None
                    and current != previous_page
                )
                changed_by_location = (
                    previous_location is not None
                    and current_location is not None
                    and current_location != previous_location
                )
                changed_by_footer_value = changed_by_page_number or changed_by_location
                if changed_by_signature or changed_by_footer_value:
                    pages_turned += 1
                if not changed_by_signature:
                    if changed_by_footer_value:
                        print("Info: footer fallback confirmed page turn.")
                    else:
                        print(
                            "Warning: page content did not confirm change within 8s; continuing."
                        )
                on_page_turn(page, current, total, current_location, total_location)
                save_page_screenshot(
                    page,
                    screenshots_dir,
                    screenshots_taken + 1,
                    current,
                    total,
                    current_location,
                    total_location,
                )
                screenshots_taken += 1

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
