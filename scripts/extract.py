"""
Opens a book in Kindle Cloud Reader via Playwright, screenshots each page,
and captures metadata and TOC. Auto-stops at end-matter boundaries and
restores your reading position when done.

Usage:
    python scripts/extract.py [--seconds 1] [--asin B00FO74WXA] [--pages 0]
                              [--start-page 1] [--start-location 1]
                              [--capture-pages 50-55,114,140]
                              [--no-restart] [--no-metadata]
                              [--include-end-matter] [--refresh-toc]
                              [--no-restore-position] [--overwrite-existing]
"""

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

CONTENT_CAPTURE_SELECTORS = (
    "#kr-renderer .kg-full-page-img img",
    "#kr-renderer .kg-full-page-img",
    "#kr-renderer",
)
ALERT_ROOT_SELECTORS = ("ion-alert", '[role="alertdialog"]')
READER_HEADER_SELECTOR = "#reader-header"
TOP_CHROME_SELECTOR = ".top-chrome"
READER_SETTINGS_TEST_ID = "top_menu_reader_settings"
TOC_BUTTON_TEST_ID = "top_menu_table_of_contents"
NAVIGATION_MENU_TEST_ID = "top_menu_navigation_menu"
READER_MENU_LABEL = "Reader menu"
TOC_ITEM_SELECTOR = "ion-list ion-item"
TOC_BUTTON_SELECTOR = "button.toc-item-button"
TOC_CHAPTER_TITLE_SELECTOR = ".chapter-title"
TOC_SCROLLABLE_SELECTOR = ".side-menu-content .scrollable-content"
TOC_BOTTOM_SELECTOR = ".toc-bottom"
SIDE_MENU_CLOSE_SELECTOR = ".side-menu-close-button"
GO_TO_PAGE_MENU_ITEM_SELECTOR = 'ion-item[role="listitem"]'
GO_TO_PAGE_INPUT_SELECTOR = 'ion-modal input[placeholder="page number"]'
GO_TO_PAGE_BUTTON_SELECTOR = 'ion-modal ion-button[item-i-d="go-to-modal-go-button"]'
NEXT_PAGE_BUTTON_SELECTOR = "#kr-chevron-right"
NEXT_PAGE_CONTAINER_SELECTOR = ".kr-chevron-container-right"
PREVIOUS_PAGE_BUTTON_SELECTOR = "#kr-chevron-left"
PREVIOUS_PAGE_CONTAINER_SELECTOR = ".kr-chevron-container-left"
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
END_MATTER_PATTERNS = (
    re.compile(r"acknowledgements", re.IGNORECASE),
    re.compile(r"^discover more$", re.IGNORECASE),
    re.compile(r"^extras$", re.IGNORECASE),
    re.compile(r"about the author", re.IGNORECASE),
    re.compile(r"meet the author", re.IGNORECASE),
    re.compile(r"^also by ", re.IGNORECASE),
    re.compile(r"^copyright$", re.IGNORECASE),
    re.compile(r" teaser$", re.IGNORECASE),
    re.compile(r" preview$", re.IGNORECASE),
    re.compile(r"^excerpt from", re.IGNORECASE),
    re.compile(r"^cast of characters$", re.IGNORECASE),
    re.compile(r"^timeline$", re.IGNORECASE),
    re.compile(r"^other titles", re.IGNORECASE),
    re.compile(r" books by ", re.IGNORECASE),
)


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

    location_match = re.search(r"location\s+(\d+)\s+of\s+(\d+)", normalized, re.IGNORECASE)
    if location_match:
        return None, None, int(location_match.group(1)), int(location_match.group(2))

    roman_match = re.search(r"page\s+([ivxlcdm]+)\s+of\s+(\d+)", normalized, re.IGNORECASE)
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


def ensure_fixed_header_ui(page):
    """Best-effort: disable top chrome motion to reduce flaky UI interactions."""
    try:
        top_chrome = page.locator(TOP_CHROME_SELECTOR).first
        if top_chrome.count() == 0:
            return False
        top_chrome.evaluate("""
            (el) => {
                el.style.transition = "none";
                el.style.transform = "none";
            }
        """)
        return True
    except Exception:
        return False


def reveal_top_chrome(page, button_test_id=None, timeout_ms=5000):
    """Hover reader header so top controls become visible."""
    try:
        header = page.locator(READER_HEADER_SELECTOR).first
        if header.count() > 0:
            header.hover(force=True)
            page.wait_for_timeout(150)
    except Exception:
        pass

    if not button_test_id:
        return True

    try:
        button = page.get_by_test_id(button_test_id).first
        if button.count() == 0:
            return False
        button.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def open_toc_menu(page):
    """Open the TOC sidebar if possible."""
    if is_toc_panel_open(page):
        return True

    if not reveal_top_chrome(page, TOC_BUTTON_TEST_ID):
        return False

    try:
        toc_btn = page.get_by_test_id(TOC_BUTTON_TEST_ID).first
        if toc_btn.count() == 0 or not toc_btn.is_visible():
            return False
        toc_btn.click()
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def is_toc_panel_open(page):
    """Return True if TOC panel appears open using visible panel markers."""
    selectors = (
        SIDE_MENU_CLOSE_SELECTOR,
        TOC_ITEM_SELECTOR,
        TOC_BOTTOM_SELECTOR,
    )
    for selector in selectors:
        try:
            node = page.locator(selector).first
            if node.count() > 0 and node.is_visible():
                return True
        except Exception:
            continue
    return False


def is_toc_panel_closed(page):
    """Return True when TOC panel does not appear to be open."""
    return not is_toc_panel_open(page)


def close_toc_menu(page):
    """Close the TOC sidebar if open and verify closure."""
    if is_toc_panel_closed(page):
        return True

    for _ in range(4):
        try:
            close_btn = page.locator(SIDE_MENU_CLOSE_SELECTOR).first
            if close_btn.count() > 0 and close_btn.is_visible():
                close_btn.click()
                page.wait_for_timeout(250)
                if is_toc_panel_closed(page):
                    return True
        except Exception:
            pass

        try:
            if is_toc_panel_open(page) and reveal_top_chrome(page, TOC_BUTTON_TEST_ID):
                toc_btn = page.get_by_test_id(TOC_BUTTON_TEST_ID).first
                if toc_btn.count() > 0 and toc_btn.is_visible():
                    toc_btn.click()
                    page.wait_for_timeout(250)
                    if is_toc_panel_closed(page):
                        return True
        except Exception:
            pass

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            if is_toc_panel_closed(page):
                return True
        except Exception:
            pass

        # Last-resort: click reader content area to dismiss side panels.
        for selector in ("#kr-renderer", "#reader", "body"):
            try:
                surface = page.locator(selector).first
                if surface.count() > 0 and surface.is_visible():
                    surface.click(force=True, position={"x": 24, "y": 24})
                    page.wait_for_timeout(200)
                    if is_toc_panel_closed(page):
                        return True
            except Exception:
                continue

    return is_toc_panel_closed(page)


def go_to_cover(page):
    """Navigate to the cover/first page via the table of contents."""
    if not open_toc_menu(page):
        return False

    try:
        cover_btn = page.locator('button.toc-item-button[aria-label="Cover"]').first
        if cover_btn.count() == 0:
            if not close_toc_menu(page):
                print("Warning: TOC did not fully close after missing Cover entry.")
            return False
        cover_btn.click()
        page.wait_for_timeout(1000)
        if not close_toc_menu(page):
            print("Warning: TOC did not fully close after navigating to Cover.")
        return True
    except Exception:
        if not close_toc_menu(page):
            print("Warning: TOC did not fully close after Cover navigation failure.")
        return False


def prime_location_mode_via_toc_first_entry(page):
    """Prime reader context for Go to Location via the first TOC entry."""
    dismiss_modal_if_open(page)
    page.wait_for_timeout(200)

    toc_opened = False
    for _ in range(3):
        if open_toc_menu(page):
            toc_opened = True
            break
        dismiss_modal_if_open(page)
        page.wait_for_timeout(250)

    if not toc_opened:
        return False

    try:
        toc_items = page.locator(TOC_ITEM_SELECTOR)
        try:
            toc_items.first.wait_for(state="visible", timeout=5000)
        except Exception:
            pass

        scrollable = page.locator(TOC_SCROLLABLE_SELECTOR).first
        if scrollable.count() > 0:
            try:
                scrollable.evaluate("(el) => { el.scrollTop = 0; }")
                page.wait_for_timeout(200)
            except Exception:
                pass

        # Let the TOC fully settle at top before selecting the first entry.
        page.wait_for_timeout(3000)

        first_toc_button = toc_items.first.locator(TOC_BUTTON_SELECTOR).first
        if first_toc_button.count() == 0:
            first_toc_button = page.locator(TOC_BUTTON_SELECTOR).first
        if first_toc_button.count() == 0:
            if not close_toc_menu(page):
                print("Warning: TOC did not fully close after missing first TOC entry.")
            return False

        clicked_first = False
        try:
            first_toc_button.scroll_into_view_if_needed()
            if first_toc_button.is_visible():
                first_toc_button.click()
                clicked_first = True
        except Exception:
            clicked_first = False

        if not clicked_first:
            try:
                first_toc_item = toc_items.first
                if first_toc_item.count() > 0 and first_toc_item.is_visible():
                    first_toc_item.click()
                    clicked_first = True
            except Exception:
                clicked_first = False

        if not clicked_first:
            if not close_toc_menu(page):
                print("Warning: TOC did not fully close after first-entry click failure.")
            return False

        page.wait_for_timeout(900)
        if not close_toc_menu(page):
            print("Warning: TOC did not fully close after first-entry TOC navigation.")
            dismiss_modal_if_open(page)
            page.wait_for_timeout(150)
            if not close_toc_menu(page):
                print("Warning: aborting location-mode priming because TOC is still open.")
                return False
        return True
    except Exception:
        if not close_toc_menu(page):
            print("Warning: TOC did not fully close after location-mode TOC priming failure.")
        return False


def open_navigation_menu(page):
    """Open reader navigation menu and wait for go-to items."""
    try:
        reveal_top_chrome(page, NAVIGATION_MENU_TEST_ID)
        menu_btn = page.get_by_test_id(NAVIGATION_MENU_TEST_ID).first
        if menu_btn.count() > 0 and menu_btn.is_visible():
            menu_btn.click()
        else:
            fallback_menu = page.get_by_label(READER_MENU_LABEL).first
            if fallback_menu.count() == 0:
                return False
            fallback_menu.click()
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def click_go_to_menu_item(page, preferred_labels):
    """Click the first visible Go To menu item label."""
    targets = [str(label).strip().lower() for label in preferred_labels if str(label).strip()]
    if not targets:
        return False

    try:
        menu_items = page.locator(GO_TO_PAGE_MENU_ITEM_SELECTOR)
        item_count = menu_items.count()
        for idx in range(item_count):
            menu_item = menu_items.nth(idx)
            raw_text = menu_item.text_content()
            normalized = " ".join((raw_text or "").split()).lower()
            if not normalized:
                continue
            if not any(target in normalized for target in targets):
                continue
            if not menu_item.is_visible():
                continue
            menu_item.click()
            page.wait_for_timeout(250)
            return True
    except Exception:
        return False
    return False


def dismiss_modal_if_open(page):
    """Best-effort close for open modals/menus after navigation failures."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception:
        pass


def go_to_page(page, page_number):
    """Best-effort navigation to a specific page using the reader menu."""
    if page_number is None or page_number < 1:
        return False

    try:
        if not open_navigation_menu(page):
            return False
        if not click_go_to_menu_item(page, ("Go to Page",)):
            dismiss_modal_if_open(page)
            return False

        go_to_page_input = None
        for selector in (
            GO_TO_PAGE_INPUT_SELECTOR,
            "ion-modal input",
        ):
            candidate = page.locator(selector).first
            if candidate.count() > 0:
                go_to_page_input = candidate
                break
        go_to_page_button = page.locator(GO_TO_PAGE_BUTTON_SELECTOR).first
        if go_to_page_button.count() == 0:
            go_to_page_button = page.locator("ion-modal ion-button", has_text="Go").first

        if go_to_page_input is None or go_to_page_button.count() == 0:
            dismiss_modal_if_open(page)
            return False

        go_to_page_input.fill(str(page_number))
        go_to_page_button.click()
        page.wait_for_timeout(900)
        return True
    except Exception:
        dismiss_modal_if_open(page)
        return False


def go_to_location(page, location_number):
    """Best-effort navigation to a specific location using the reader menu."""
    if location_number is None or location_number < 1:
        return False

    try:
        if not open_navigation_menu(page):
            return False
        if not click_go_to_menu_item(page, ("Go to Location",)):
            dismiss_modal_if_open(page)
            return False

        go_to_location_input = None
        for selector in (
            'ion-modal input[placeholder*="location" i]',
            "ion-modal input",
        ):
            candidate = page.locator(selector).first
            if candidate.count() > 0:
                go_to_location_input = candidate
                break

        go_to_location_button = page.locator(GO_TO_PAGE_BUTTON_SELECTOR).first
        if go_to_location_button.count() == 0:
            go_to_location_button = page.locator("ion-modal ion-button", has_text="Go").first

        if go_to_location_input is None or go_to_location_button.count() == 0:
            dismiss_modal_if_open(page)
            return False

        go_to_location_input.fill(str(location_number))
        go_to_location_button.click()
        page.wait_for_timeout(900)
        return True
    except Exception:
        dismiss_modal_if_open(page)
        return False


def restore_start_position(page, start_page=None, start_location=None):
    """Best-effort: restore the reader to the position captured at startup."""
    try:
        dismiss_possible_alert(page)
    except Exception:
        pass

    if start_page is not None and start_page > 0:
        try:
            if go_to_page(page, start_page):
                print(f"Info: restored start position to page {start_page}.")
                return True
            print(f"Warning: could not restore start position to page {start_page}.")
            return False
        except Exception:
            print(f"Warning: restore to start page {start_page} failed.")
            return False

    if start_location is not None and start_location > 0:
        try:
            if go_to_location(page, start_location):
                print(f"Info: restored start position to location {start_location}.")
                return True
            print(f"Warning: could not restore start position to location {start_location}.")
            return False
        except Exception:
            print(f"Warning: restore to start location {start_location} failed.")
            return False

    print("Warning: no start position was captured; skipping restore.")
    return False


def is_end_matter_title(title):
    """Return True if a TOC title looks like end matter."""
    if not title:
        return False
    return any(pattern.search(title) for pattern in END_MATTER_PATTERNS)


def get_page_info_with_retry(page, attempts=8, wait_ms=120):
    """Retry footer parsing to account for slow nav updates after TOC clicks."""
    for _ in range(attempts):
        current, total, current_location, total_location = get_page_info(page)
        if current is not None and total is not None:
            return current, total, current_location, total_location
        if current_location is not None and total_location is not None:
            return current, total, current_location, total_location
        page.wait_for_timeout(wait_ms)
    return None, None, None, None


def extract_toc_entries(page, max_scroll_passes=160):
    """Read TOC entries from a potentially virtualized TOC list."""
    if not open_toc_menu(page):
        return []

    entries = []
    seen_entries = set()
    toc_items = page.locator(TOC_ITEM_SELECTOR)
    scrollable = page.locator(TOC_SCROLLABLE_SELECTOR).first
    toc_bottom = page.locator(TOC_BOTTOM_SELECTOR).first
    stagnant_scroll_rounds = 0

    try:
        toc_items.first.wait_for(state="visible", timeout=5000)
    except Exception:
        if not close_toc_menu(page):
            print("Warning: TOC did not fully close after TOC open timeout.")
        return []

    try:
        for _ in range(max_scroll_passes):
            added_this_round = 0
            current_count = toc_items.count()
            for index in range(current_count):
                toc_item = toc_items.nth(index)

                try:
                    toc_button = toc_item.locator(TOC_BUTTON_SELECTOR).first
                    if toc_button.count() == 0:
                        continue
                    raw_key = toc_button.get_attribute("aria-label")
                except Exception:
                    continue

                try:
                    title_node = toc_item.locator(TOC_CHAPTER_TITLE_SELECTOR).first
                    raw_title = (
                        title_node.text_content()
                        if title_node.count() > 0
                        else toc_item.text_content()
                    )
                except Exception:
                    raw_title = None

                title = " ".join((raw_title or "").split())
                if not title:
                    continue

                entry_key = ((raw_key or title).strip()).lower()
                if entry_key in seen_entries:
                    continue

                try:
                    toc_button.scroll_into_view_if_needed()
                    if not toc_button.is_visible():
                        continue
                    toc_button.click()
                    page.wait_for_timeout(180)
                except Exception:
                    continue

                current, total, current_location, total_location = get_page_info_with_retry(page)

                seen_entries.add(entry_key)
                added_this_round += 1

                entries.append(
                    {
                        "title": title,
                        "page": current,
                        "location": current_location,
                        "total": (
                            total if current is not None and total is not None else total_location
                        ),
                    }
                )

            reached_bottom = False
            try:
                reached_bottom = toc_bottom.count() > 0 and toc_bottom.is_visible()
            except Exception:
                reached_bottom = False

            moved = False
            try:
                if scrollable.count() > 0:
                    result = scrollable.evaluate("""
                        (el) => {
                            const previous = el.scrollTop;
                            const delta = Math.max(240, Math.floor(el.clientHeight * 0.8));
                            el.scrollBy(0, delta);
                            return { previous, next: el.scrollTop };
                        }
                    """)
                    moved = result.get("next") != result.get("previous")
                elif current_count > 0:
                    toc_items.nth(current_count - 1).scroll_into_view_if_needed()
                    moved = True
            except Exception:
                moved = False

            if reached_bottom and added_this_round == 0:
                break
            if not moved:
                stagnant_scroll_rounds += 1
            else:
                stagnant_scroll_rounds = 0
            if stagnant_scroll_rounds >= 3:
                break

            page.wait_for_timeout(180)
    finally:
        if not close_toc_menu(page):
            print("Warning: TOC did not fully close after TOC scan.")

    return entries


def classify_toc_entries(entries):
    """Classify entries as content/end matter based on title + position."""
    first_end_matter_index = None
    for idx, entry in enumerate(entries):
        title = entry.get("title")
        total = entry.get("total")
        marker = entry.get("page")
        if marker is None:
            marker = entry.get("location")

        if not title or not isinstance(total, int) or total <= 0:
            continue
        if marker is None:
            continue
        if not is_end_matter_title(title):
            continue
        if marker / total < 0.9:
            continue

        first_end_matter_index = idx
        break

    classified = []
    for idx, entry in enumerate(entries):
        kind = "content"
        if first_end_matter_index is not None and idx >= first_end_matter_index:
            kind = "end_matter"
        classified.append({**entry, "kind": kind})

    return classified


def build_toc_payload(target_asin, entries, include_end_matter):
    """Create a normalized TOC payload and optional content boundary."""
    classified = classify_toc_entries(entries)
    first_end_matter = next(
        (item for item in classified if item.get("kind") == "end_matter"),
        None,
    )

    content_max_page = None
    content_max_location = None
    if not include_end_matter and first_end_matter:
        if first_end_matter.get("page") is not None:
            tentative = first_end_matter["page"] - 1
            if tentative >= 1:
                content_max_page = tentative
        elif first_end_matter.get("location") is not None:
            tentative = first_end_matter["location"] - 1
            if tentative >= 1:
                content_max_location = tentative

    output_entries = []
    for idx, entry in enumerate(classified):
        output_entries.append(
            {
                "index": idx,
                "title": entry.get("title"),
                "page": entry.get("page"),
                "location": entry.get("location"),
                "total": entry.get("total"),
                "kind": entry.get("kind"),
            }
        )

    summary = {
        "entry_count": len(output_entries),
        "content_count": sum(1 for item in output_entries if item["kind"] == "content"),
        "end_matter_count": sum(1 for item in output_entries if item["kind"] == "end_matter"),
        "first_end_matter_title": (first_end_matter.get("title") if first_end_matter else None),
        "first_end_matter_page": (first_end_matter.get("page") if first_end_matter else None),
        "first_end_matter_location": (
            first_end_matter.get("location") if first_end_matter else None
        ),
        "content_max_page": content_max_page,
        "content_max_location": content_max_location,
        "include_end_matter": include_end_matter,
    }

    return {
        "asin": target_asin,
        "captured_at": datetime.now(UTC).isoformat(),
        "entries": output_entries,
        "summary": summary,
    }


def save_toc(toc_path, toc_payload):
    """Write TOC JSON to disk."""
    toc_path.write_text(
        json.dumps(toc_payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"Saved TOC: {toc_path}")


def _coerce_positive_int(value):
    """Parse an integer-like value and require positive integer output."""
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return None
    return parsed if parsed > 0 else None


def parse_capture_pages_spec(spec):
    """Parse comma-separated page tokens (single values and inclusive ranges)."""
    if spec is None:
        return []

    tokens = [token.strip() for token in str(spec).split(",")]
    if not tokens or any(not token for token in tokens):
        raise ValueError("empty token in --capture-pages specification")

    pages = []
    seen = set()

    for token in tokens:
        range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start < 1 or end < 1:
                raise ValueError(f"range values must be >= 1: {token}")
            if end < start:
                raise ValueError(f"range end must be >= start: {token}")
            for value in range(start, end + 1):
                if value not in seen:
                    seen.add(value)
                    pages.append(value)
            continue

        single_match = re.fullmatch(r"\d+", token)
        if single_match:
            value = int(token)
            if value < 1:
                raise ValueError(f"page values must be >= 1: {token}")
            if value not in seen:
                seen.add(value)
                pages.append(value)
            continue

        raise ValueError(f"invalid token in --capture-pages: {token}")

    return pages


def load_toc_entries_from_file(toc_path):
    """Load normalized TOC entries from a previous toc.json file."""
    try:
        payload = json.loads(toc_path.read_text(encoding="utf-8"))
    except Exception:
        print("Warning: existing toc.json could not be parsed; rebuilding TOC.")
        return []

    if not isinstance(payload, dict):
        print("Warning: existing toc.json has unexpected shape; rebuilding TOC.")
        return []

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        print("Warning: existing toc.json has no entries list; rebuilding TOC.")
        return []

    normalized_entries = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue

        raw_title = item.get("title")
        if not isinstance(raw_title, str):
            continue
        title = " ".join(raw_title.split())
        if not title:
            continue

        page = _coerce_positive_int(item.get("page"))
        location = _coerce_positive_int(item.get("location"))
        total = _coerce_positive_int(item.get("total"))
        if page is None and location is None:
            continue

        normalized_entries.append(
            {
                "title": title,
                "page": page,
                "location": location,
                "total": total if total is not None else 0,
            }
        )

    if not normalized_entries:
        print("Warning: existing toc.json had no usable entries; rebuilding TOC.")
    return normalized_entries


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


def is_selector_visible(page, selector):
    """Return True when a selector exists and is visible."""
    try:
        node = page.locator(selector).first
        return node.count() > 0 and node.is_visible()
    except Exception:
        return False


def wait_for_next_control(page, timeout_ms=30_000, poll_ms=100):
    """Wait until a known next-page control is visible."""
    deadline = time.time() + (timeout_ms / 1000)
    next_recovery_at = time.time()
    while time.time() < deadline:
        if is_selector_visible(page, NEXT_PAGE_BUTTON_SELECTOR) or is_selector_visible(
            page, NEXT_PAGE_CONTAINER_SELECTOR
        ):
            return True

        now = time.time()
        if now >= next_recovery_at:
            if is_toc_panel_open(page):
                close_toc_menu(page)
            else:
                dismiss_modal_if_open(page)
            next_recovery_at = now + 0.8
        page.wait_for_timeout(poll_ms)
    return False


def click_next_button(page):
    """Click the next-page button or fallback container if available."""
    for _attempt in range(2):
        for selector in (NEXT_PAGE_BUTTON_SELECTOR, NEXT_PAGE_CONTAINER_SELECTOR):
            try:
                next_control = page.query_selector(selector)
                if not next_control or not next_control.is_visible():
                    continue
                next_control.click()
                return True
            except Exception:
                continue

        # Recover from TOC/modal overlap before the next click attempt.
        if is_toc_panel_open(page):
            close_toc_menu(page)
        else:
            dismiss_modal_if_open(page)
        wait_for_next_control(page, timeout_ms=1_000, poll_ms=120)
    return False


def click_previous_button(page):
    """Click the previous-page button or fallback container if available."""
    for selector in (PREVIOUS_PAGE_BUTTON_SELECTOR, PREVIOUS_PAGE_CONTAINER_SELECTOR):
        try:
            previous_control = page.query_selector(selector)
            if not previous_control or not previous_control.is_visible():
                continue
            previous_control.click()
            return True
        except Exception:
            continue
    return False


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
        if previous_signature and current_signature and current_signature != previous_signature:
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
    """Build normalized metadata from intercepted network payloads.

    Raw intercepted payloads are intentionally not persisted.
    """
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

        authors = normalize_authors(yj_payload.get("authorsList") or yj_payload.get("authorList"))

    return {
        "asin": meta_asin or target_asin,
        "title": title,
        "authors": authors,
        "captured_at": datetime.now(UTC).isoformat(),
        "sources": {
            "start_reading": info_payload is not None,
            "yj_metadata": yj_payload is not None,
        },
    }


def save_metadata(metadata_path, metadata):
    """Write metadata JSON to disk."""
    metadata_path.write_text(
        json.dumps(metadata, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"Saved metadata: {metadata_path}")


def parse_canonical_capture_filename(file_name):
    """Parse canonical nav-keyed filename into page/location metadata."""
    page_match = re.match(r"^page-(\d+)-of-(\d+)(?:[.-]v(\d+))?\.png$", file_name)
    if page_match:
        return {
            "page": int(page_match.group(1)),
            "total": int(page_match.group(2)),
            "location": None,
            "total_location": None,
            "variant_index": int(page_match.group(3) or 0),
        }

    location_match = re.match(r"^loc-(\d+)-of-(\d+)(?:[.-]v(\d+))?\.png$", file_name)
    if location_match:
        return {
            "page": None,
            "total": None,
            "location": int(location_match.group(1)),
            "total_location": int(location_match.group(2)),
            "variant_index": int(location_match.group(3) or 0),
        }

    return None


def canonical_capture_sort_key(entry):
    """Sort canonical entries deterministically (location first, then page)."""
    if entry.get("location") is not None:
        return (
            0,
            entry.get("location") or 0,
            entry.get("total_location") or 0,
            entry.get("variant_index") or 0,
            entry.get("file") or "",
        )
    if entry.get("page") is not None:
        return (
            1,
            entry.get("page") or 0,
            entry.get("total") or 0,
            entry.get("variant_index") or 0,
            entry.get("file") or "",
        )
    return (2, 0, 0, 0, entry.get("file") or "")


def scan_canonical_pages_manifest_entries(screenshots_dir):
    """Scan pages dir and return canonical nav-key entries for pages.json."""
    entries = []
    ignored_noncanonical_count = 0

    for screenshot_path in screenshots_dir.glob("*.png"):
        parsed = parse_canonical_capture_filename(screenshot_path.name)
        if parsed is None:
            ignored_noncanonical_count += 1
            continue

        entries.append(
            {
                "file": screenshot_path.name,
                "path": f"pages/{screenshot_path.name}",
                "page": parsed["page"],
                "total": parsed["total"],
                "location": parsed["location"],
                "total_location": parsed["total_location"],
                "variant_index": parsed.get("variant_index", 0),
            }
        )

    entries.sort(key=canonical_capture_sort_key)
    for idx, item in enumerate(entries):
        item["index"] = idx

    return entries, ignored_noncanonical_count


def infer_total_pages_from_existing_captures(screenshots_dir):
    """Infer total page count from existing canonical page-* filenames."""
    totals = []
    for screenshot_path in screenshots_dir.glob("page-*-of-*.png"):
        parsed = parse_canonical_capture_filename(screenshot_path.name)
        if parsed and isinstance(parsed.get("total"), int):
            totals.append(parsed["total"])
    if not totals:
        return None
    return max(totals)


def normalize_positive_int_list(values):
    """Normalize a list-like input into sorted unique positive integers."""
    if not isinstance(values, list):
        return []

    normalized = set()
    for value in values:
        parsed = _coerce_positive_int(value)
        if parsed is not None:
            normalized.add(parsed)
    return sorted(normalized)


def load_existing_anomaly_events(pages_manifest_path):
    """Load existing anomaly events from pages.json (best-effort)."""
    if not pages_manifest_path.exists():
        return []

    try:
        payload = json.loads(pages_manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(payload, dict):
        return []
    anomalies = payload.get("anomalies")
    if not isinstance(anomalies, dict):
        return []
    raw_events = anomalies.get("events")
    if not isinstance(raw_events, list):
        return []

    events = []
    for event in raw_events:
        if isinstance(event, dict):
            events.append(event)
    return events


def collect_unresolved_page_candidates(anomaly_events):
    """Collect unresolved page candidate numbers from anomaly events."""
    unresolved = set()
    if not isinstance(anomaly_events, list):
        return []
    for event in anomaly_events:
        if not isinstance(event, dict):
            continue
        for page_number in normalize_positive_int_list(event.get("unresolved_pages")):
            unresolved.add(page_number)
    return sorted(unresolved)


def build_pages_coverage_payload(entries, anomaly_events, last_run_mode):
    """Build page-coverage view from captures on disk + anomaly hints."""
    captured_pages = set()
    page_totals = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        page_number = _coerce_positive_int(item.get("page"))
        if page_number is not None:
            captured_pages.add(page_number)
        page_total = _coerce_positive_int(item.get("total"))
        if page_total is not None:
            page_totals.add(page_total)

    expected_total_pages = max(page_totals) if page_totals else None
    raw_missing_pages = []
    if expected_total_pages is not None:
        raw_missing_pages = sorted(
            page_number
            for page_number in range(1, expected_total_pages + 1)
            if page_number not in captured_pages
        )

    unresolved_page_candidates = collect_unresolved_page_candidates(anomaly_events)
    unresolved_missing_pages = []
    if raw_missing_pages:
        missing_set = set(raw_missing_pages)
        unresolved_missing_pages = [
            page_number for page_number in unresolved_page_candidates if page_number in missing_set
        ]

    unresolved_missing_set = set(unresolved_missing_pages)
    missing_pages = [
        page_number for page_number in raw_missing_pages if page_number not in unresolved_missing_set
    ]

    if expected_total_pages is None:
        status = "unknown_total"
    elif missing_pages:
        status = "incomplete"
    elif unresolved_missing_pages:
        status = "uncertain_gaps"
    else:
        status = "complete"

    return {
        "status": status,
        "last_run_mode": last_run_mode,
        "expected_total_pages": expected_total_pages,
        "captured_unique_page_count": len(captured_pages),
        "raw_missing_pages": raw_missing_pages,
        "raw_missing_count": len(raw_missing_pages),
        "missing_pages": missing_pages,
        "missing_count": len(missing_pages),
        "unresolved_page_candidates": unresolved_page_candidates,
        "unresolved_page_candidate_count": len(unresolved_page_candidates),
        "unresolved_missing_pages": unresolved_missing_pages,
        "unresolved_missing_count": len(unresolved_missing_pages),
    }


def build_pages_manifest_payload(
    target_asin,
    captured_at,
    entries,
    capture_stats=None,
    ignored_noncanonical_count=0,
    anomaly_events=None,
    last_run_mode=None,
):
    """Build a canonical pages manifest snapshot."""
    page_nav_count = 0
    location_nav_count = 0
    unknown_nav_count = 0

    for item in entries:
        has_page_nav = item.get("page") is not None and item.get("total") is not None
        has_location_nav = (
            item.get("location") is not None and item.get("total_location") is not None
        )
        if has_page_nav:
            page_nav_count += 1
        elif has_location_nav:
            location_nav_count += 1
        else:
            unknown_nav_count += 1

    summary = {
        "capture_count": len(entries),
        "page_nav_count": page_nav_count,
        "location_nav_count": location_nav_count,
        "unknown_nav_count": unknown_nav_count,
        "ignored_noncanonical_count": ignored_noncanonical_count,
    }
    if isinstance(capture_stats, dict):
        summary.update(capture_stats)

    normalized_events = []
    if isinstance(anomaly_events, list):
        for event in anomaly_events:
            if isinstance(event, dict):
                normalized_events.append(event)

    return {
        "asin": target_asin,
        "captured_at": captured_at,
        "pages": entries,
        "summary": summary,
        "coverage": build_pages_coverage_payload(entries, normalized_events, last_run_mode),
        "anomalies": {
            "schema_version": 1,
            "count": len(normalized_events),
            "events": normalized_events,
        },
    }


def save_pages_manifest(manifest_path, payload):
    """Write pages manifest JSON to disk using an atomic replace."""
    temp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(manifest_path)


def build_canonical_capture_filename(
    current=None,
    total=None,
    current_location=None,
    total_location=None,
    prefer_location=False,
    variant_index=1,
):
    """Return canonical nav-key filename or None when nav values are unknown."""
    normalized_variant_index = variant_index if isinstance(variant_index, int) else 1
    if normalized_variant_index < 1:
        normalized_variant_index = 1
    variant_suffix = f"-v{normalized_variant_index:04d}"

    if prefer_location and current_location is not None and total_location is not None:
        width = max(4, len(str(total_location)))
        return f"loc-{current_location:0{width}d}-of-{total_location:0{width}d}{variant_suffix}.png"

    if current is not None and total is not None:
        return f"page-{current:04d}-of-{total:04d}{variant_suffix}.png"

    if current_location is not None and total_location is not None:
        width = max(4, len(str(total_location)))
        return f"loc-{current_location:0{width}d}-of-{total_location:0{width}d}{variant_suffix}.png"

    return None


def resolve_variant_screenshot_path(
    screenshots_dir,
    *,
    current,
    total,
    current_location,
    total_location,
    prefer_location,
):
    """Return a free dashed-variant path for a nav marker."""
    variant_index = 1
    while True:
        variant_filename = build_canonical_capture_filename(
            current=current,
            total=total,
            current_location=current_location,
            total_location=total_location,
            prefer_location=prefer_location,
            variant_index=variant_index,
        )
        if not variant_filename:
            return None
        variant_path = screenshots_dir / variant_filename
        if not variant_path.exists():
            return variant_path
        variant_index += 1


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


def capture_current_view_to_path(page, screenshot_path, overwrite_existing=False):
    """Capture current reader content into a specific path with overwrite controls."""
    already_exists = screenshot_path.exists()
    if already_exists and not overwrite_existing:
        print(f"Info: skipping existing screenshot: {screenshot_path}")
        return "skipped_existing", screenshot_path

    capture_path = screenshot_path
    should_overwrite = already_exists and overwrite_existing
    if should_overwrite:
        capture_path = screenshot_path.with_name(
            f"{screenshot_path.stem}.tmp{screenshot_path.suffix}"
        )

    had_content_candidate = False
    for _selector, locator in iter_capture_locators(page):
        had_content_candidate = True
        try:
            locator.screenshot(path=str(capture_path))
            if should_overwrite:
                capture_path.replace(screenshot_path)
                print(f"Overwrote screenshot: {screenshot_path}")
                return "overwritten", screenshot_path
            print(f"Saved screenshot: {screenshot_path}")
            return "new", screenshot_path
        except Exception:
            continue

    if had_content_candidate:
        print("Warning: content element capture failed; using viewport screenshot.")
    else:
        print("Warning: content element not found; using viewport screenshot.")
    page.screenshot(path=str(capture_path))

    if should_overwrite:
        capture_path.replace(screenshot_path)
        print(f"Overwrote screenshot: {screenshot_path}")
        return "overwritten", screenshot_path
    print(f"Saved screenshot: {screenshot_path}")
    return "new", screenshot_path


def save_page_screenshot(
    page,
    screenshots_dir,
    overwrite_existing=False,
    prefer_location=False,
    allow_existing_variant=False,
    current=None,
    total=None,
    current_location=None,
    total_location=None,
):
    """Save current content to canonical filename; skip/overwrite existing as configured."""
    filename = build_canonical_capture_filename(
        current=current,
        total=total,
        current_location=current_location,
        total_location=total_location,
        prefer_location=prefer_location,
    )
    if not filename:
        print("Info: skipping capture because page/location is unknown.")
        return "skipped_unknown", None

    screenshot_path = screenshots_dir / filename
    if allow_existing_variant and not overwrite_existing and screenshot_path.exists():
        variant_path = resolve_variant_screenshot_path(
            screenshots_dir,
            current=current,
            total=total,
            current_location=current_location,
            total_location=total_location,
            prefer_location=prefer_location,
        )
        if variant_path is not None:
            screenshot_path = variant_path

    return capture_current_view_to_path(
        page,
        screenshot_path,
        overwrite_existing=overwrite_existing,
    )


def save_explicit_page_capture(
    page,
    screenshots_dir,
    target_page,
    total_pages,
    overwrite_existing=False,
):
    """Save current content using an explicit page-* filename target."""
    if not isinstance(target_page, int) or target_page < 1:
        print(f"Warning: invalid requested page number: {target_page}.")
        return "skipped_unknown", None
    if not isinstance(total_pages, int) or total_pages < 1:
        print(
            "Warning: could not determine total page count for explicit page capture; "
            f"skipping page {target_page}."
        )
        return "skipped_unknown", None

    filename = f"page-{target_page:04d}-of-{total_pages:04d}-v0001.png"
    screenshot_path = screenshots_dir / filename
    return capture_current_view_to_path(
        page,
        screenshot_path,
        overwrite_existing=overwrite_existing,
    )


def update_capture_stats(capture_stats, capture_status):
    """Increment capture counters for manifest summary/debug output."""
    status_key = f"{capture_status}_count"
    if status_key in capture_stats:
        capture_stats[status_key] += 1


def append_capture_pages_anomaly_event(
    anomaly_events,
    run_id,
    request_index,
    requested_page,
    classification,
    resolved_page=None,
    resolved_location=None,
    unresolved_pages=None,
):
    """Record requested-page anomaly details for manifest diagnostics."""
    anomaly_events.append(
        {
            "kind": "capture_pages_resolution",
            "run_id": run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "request_index": request_index,
            "requested_page": requested_page,
            "resolved_page": resolved_page,
            "resolved_location": resolved_location,
            "classification": classification,
            "unresolved_pages": normalize_positive_int_list(unresolved_pages),
        }
    )


def append_auto_turn_anomaly_event(
    anomaly_events,
    run_id,
    turn_index,
    from_page,
    to_page,
    delta,
    classification,
    unresolved_pages,
    changed_by_signature,
    changed_by_footer_value,
):
    """Record auto-turn page delta anomaly details for manifest diagnostics."""
    anomaly_events.append(
        {
            "kind": "auto_turn_delta",
            "run_id": run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "turn_index": turn_index,
            "from_page": from_page,
            "to_page": to_page,
            "delta": delta,
            "classification": classification,
            "unresolved_pages": normalize_positive_int_list(unresolved_pages),
            "changed_by_signature": changed_by_signature,
            "changed_by_footer_value": changed_by_footer_value,
        }
    )


def capture_requested_pages(
    page,
    target_pages,
    screenshots_dir,
    overwrite_existing,
    capture_stats,
    save_pages_manifest_callback,
    anomaly_events,
    run_id,
):
    """Capture an explicit list of pages via Go to Page navigation."""
    failed_pages = []
    known_total = infer_total_pages_from_existing_captures(screenshots_dir)

    for idx, target_page in enumerate(target_pages, start=1):
        print(f"Requested page capture {idx}/{len(target_pages)}: page {target_page}")

        if dismiss_possible_alert(page):
            print("Info: dismissed blocking alert.")

        if not go_to_page(page, target_page):
            print(f"Warning: could not navigate to requested page {target_page}.")
            append_capture_pages_anomaly_event(
                anomaly_events,
                run_id,
                request_index=idx,
                requested_page=target_page,
                classification="navigation_failed",
                unresolved_pages=[target_page],
            )
            failed_pages.append(target_page)
            save_pages_manifest_callback()
            continue

        current, total, current_location, total_location = get_page_info_with_retry(
            page, attempts=10, wait_ms=150
        )
        if isinstance(total, int) and total > 0:
            known_total = total

        # Kindle can land one page off after Go to Page; nudge to requested page.
        adjustment_attempts = 0
        while adjustment_attempts < 3 and isinstance(current, int) and current != target_page:
            adjustment_attempts += 1
            moved = False
            if current < target_page:
                moved = click_next_button(page)
                if not moved:
                    try:
                        page.keyboard.press("ArrowRight")
                        moved = True
                    except Exception:
                        moved = False
            else:
                moved = click_previous_button(page)
                if not moved:
                    try:
                        page.keyboard.press("ArrowLeft")
                        moved = True
                    except Exception:
                        moved = False

            if not moved:
                break

            page.wait_for_timeout(700)
            current, total, current_location, total_location = get_page_info_with_retry(
                page, attempts=6, wait_ms=120
            )
            if isinstance(total, int) and total > 0:
                known_total = total

        if isinstance(current, int) and current != target_page and abs(current - target_page) <= 2:
            if go_to_page(page, target_page):
                current, total, current_location, total_location = get_page_info_with_retry(
                    page, attempts=8, wait_ms=150
                )
                if isinstance(total, int) and total > 0:
                    known_total = total

        if current_location is not None and current is None:
            print(
                "Warning: footer is location-only for this page; skipping requested page "
                f"{target_page}."
            )
            append_capture_pages_anomaly_event(
                anomaly_events,
                run_id,
                request_index=idx,
                requested_page=target_page,
                classification="location_only",
                resolved_location=current_location,
                unresolved_pages=[target_page],
            )
            failed_pages.append(target_page)
            save_pages_manifest_callback()
            continue

        if current is None:
            print(
                "Warning: could not confirm resolved page number for requested page "
                f"{target_page}; skipping capture to avoid mislabeled output."
            )
            append_capture_pages_anomaly_event(
                anomaly_events,
                run_id,
                request_index=idx,
                requested_page=target_page,
                classification="capture_skipped_unknown",
                resolved_location=current_location,
                unresolved_pages=[target_page],
            )
            failed_pages.append(target_page)
            save_pages_manifest_callback()
            continue

        if current != target_page:
            print(
                f"Warning: requested page {target_page} resolved to page {current}; "
                "skipping capture to avoid mislabeled output."
            )
            append_capture_pages_anomaly_event(
                anomaly_events,
                run_id,
                request_index=idx,
                requested_page=target_page,
                classification="resolved_mismatch",
                resolved_page=current,
                resolved_location=current_location,
                unresolved_pages=[target_page],
            )
            failed_pages.append(target_page)
            save_pages_manifest_callback()
            continue

        effective_total = total if isinstance(total, int) and total > 0 else known_total
        capture_status, _screenshot_path = save_explicit_page_capture(
            page,
            screenshots_dir,
            target_page=target_page,
            total_pages=effective_total,
            overwrite_existing=overwrite_existing,
        )
        update_capture_stats(capture_stats, capture_status)
        save_pages_manifest_callback()

        if capture_status == "skipped_unknown":
            append_capture_pages_anomaly_event(
                anomaly_events,
                run_id,
                request_index=idx,
                requested_page=target_page,
                classification="capture_skipped_unknown",
                resolved_page=current,
                resolved_location=current_location,
                unresolved_pages=[target_page],
            )
            failed_pages.append(target_page)
            save_pages_manifest_callback()

    return failed_pages


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Kindle hands-free page turner")
    parser.add_argument(
        "--seconds", type=int, default=1, help="Seconds to wait per page (default: 1)"
    )
    parser.add_argument("--asin", type=str, default="B00FO74WXA", help="Book ASIN to open")
    parser.add_argument(
        "--pages", type=int, default=0, help="Number of pages to advance (0 = unlimited)"
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=None,
        help="Jump to a specific page before capture starts",
    )
    parser.add_argument(
        "--start-location",
        type=int,
        default=None,
        help="Jump to a specific location before capture starts",
    )
    parser.add_argument(
        "--capture-pages",
        type=str,
        default=None,
        help="Capture explicit pages via Go to Page (e.g. 50-55,114,140)",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Resume from current page instead of starting from the beginning",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Disable network metadata capture and metadata.json output",
    )
    parser.add_argument(
        "--include-end-matter",
        action="store_true",
        help="Capture end matter pages/locations instead of trimming by TOC boundary",
    )
    parser.add_argument(
        "--refresh-toc",
        action="store_true",
        help="Ignore existing toc.json and rebuild TOC from browser navigation",
    )
    parser.add_argument(
        "--no-restore-position",
        action="store_true",
        help="Do not return to the starting page when run finishes",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Overwrite existing canonical screenshots instead of skipping them",
    )
    args = parser.parse_args()
    try:
        requested_capture_pages = parse_capture_pages_spec(args.capture_pages)
    except ValueError as exc:
        parser.error(f"--capture-pages {exc}")
    if args.start_page is not None and args.start_page < 1:
        parser.error("--start-page must be >= 1")
    if args.start_location is not None and args.start_location < 1:
        parser.error("--start-location must be >= 1")
    if args.start_page is not None and args.start_location is not None:
        parser.error("--start-page and --start-location are mutually exclusive")
    if requested_capture_pages and args.start_page is not None:
        parser.error("--capture-pages and --start-page are mutually exclusive")
    if requested_capture_pages and args.start_location is not None:
        parser.error("--capture-pages and --start-location are mutually exclusive")
    if requested_capture_pages and args.pages:
        parser.error("--capture-pages cannot be combined with --pages")

    user_data_dir = Path.home() / ".kindle-reader-profile"
    book_dir = Path.cwd() / "books" / sanitize_slug(args.asin)
    screenshots_dir = book_dir / "pages"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = book_dir / "metadata.json"
    toc_path = book_dir / "toc.json"
    pages_manifest_path = book_dir / "pages.json"

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

        # Wait for the reader to be ready.
        has_next_control = wait_for_next_control(page, timeout_ms=30_000)
        if not has_next_control:
            if requested_capture_pages:
                print(
                    "Info: next-page controls not visible within 30s; continuing because "
                    "--capture-pages uses Go to Page navigation."
                )
            else:
                raise TimeoutError("Next-page controls not visible within 30s.")
        if dismiss_possible_alert(page):
            print("Info: dismissed blocking alert.")
        if ensure_fixed_header_ui(page):
            print("Info: stabilized top chrome UI motion.")
        if apply_reader_settings(page):
            print("Info: applied reader settings (Single Column + Amazon Ember).")
        else:
            print("Warning: could not fully apply reader settings; continuing.")

        initial_page, _initial_total, initial_location, _initial_total_location = get_page_info(
            page
        )
        if initial_page is not None:
            print(f"Info: saved start position page {initial_page}.")
        elif initial_location is not None:
            print(f"Info: saved start position location {initial_location}.")
        else:
            print("Warning: could not determine starting page/location.")

        toc_entries = []
        toc_source = "browser"
        if toc_path.exists() and not args.refresh_toc:
            toc_entries = load_toc_entries_from_file(toc_path)
            if toc_entries:
                toc_source = "cache"
                print(f"Loaded TOC from cache: {toc_path} ({len(toc_entries)} entries).")

        if not toc_entries:
            should_skip_live_toc = (
                args.no_restart
                and args.start_page is None
                and args.start_location is None
                and not args.refresh_toc
                and initial_page is None
                and initial_location is not None
            )
            if should_skip_live_toc:
                toc_source = "skipped_no_restart_location"
                print(
                    "Info: skipping live TOC extraction for location-based --no-restart run "
                    "to avoid position drift."
                )
            else:
                try:
                    toc_entries = extract_toc_entries(page)
                except Exception:
                    print("Warning: TOC extraction failed; continuing without TOC boundaries.")
                    toc_entries = []
                toc_source = "browser"

        toc_payload = build_toc_payload(args.asin, toc_entries, args.include_end_matter)
        if toc_source == "browser":
            if toc_entries:
                save_toc(toc_path, toc_payload)
            elif toc_path.exists():
                print(
                    "Warning: browser TOC extraction returned no entries; "
                    "keeping existing toc.json."
                )
            else:
                print("Warning: browser TOC extraction returned no entries.")

        toc_summary = toc_payload["summary"]
        content_max_page = toc_summary["content_max_page"]
        content_max_location = toc_summary["content_max_location"]

        if toc_source == "cache":
            print(f"TOC entries loaded: {toc_summary['entry_count']}")
        elif toc_source == "skipped_no_restart_location":
            print("TOC entries skipped: preserving current location for --no-restart run.")
        else:
            print(f"TOC entries captured: {toc_summary['entry_count']}")
        if toc_summary["first_end_matter_title"]:
            marker = toc_summary["first_end_matter_title"]
            if toc_summary["first_end_matter_page"] is not None:
                print(
                    f"TOC end-matter marker: {marker} (page {toc_summary['first_end_matter_page']})"
                )
            elif toc_summary["first_end_matter_location"] is not None:
                print(
                    "TOC end-matter marker: "
                    f"{marker} (location {toc_summary['first_end_matter_location']})"
                )
        else:
            print("TOC end-matter marker: none detected")

        if args.include_end_matter:
            print("TOC boundary trimming disabled (--include-end-matter).")
        elif content_max_page is not None:
            print(f"TOC content boundary: page <= {content_max_page}")
        elif content_max_location is not None:
            print(f"TOC content boundary: location <= {content_max_location}")
        else:
            print("TOC content boundary: unavailable (using reader end only).")

        if args.start_page is None and args.no_restart and toc_source == "browser":
            if initial_page is not None:
                if go_to_page(page, initial_page):
                    print(f"Info: restored position to page {initial_page} after TOC scan.")
                else:
                    print(
                        "Warning: could not restore initial page after TOC scan; "
                        "continuing from current position."
                    )
            elif initial_location is not None:
                print(
                    "Warning: current position uses location values; "
                    "page-based restore after TOC scan is unavailable."
                )

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

        existing_anomaly_events = load_existing_anomaly_events(pages_manifest_path)

        if requested_capture_pages:
            print(
                "Requested page capture mode enabled: "
                f"{','.join(str(page_number) for page_number in requested_capture_pages)}"
            )
            print(f"Saving page screenshots to {screenshots_dir}")
            if args.overwrite_existing:
                print("Overwrite mode enabled: existing screenshots will be replaced.")
            else:
                print(
                    "Idempotent mode enabled: existing screenshots are skipped "
                    "(use --overwrite-existing to replace)."
                )

            pages_manifest_captured_at = datetime.now(UTC).isoformat()
            run_id = pages_manifest_captured_at
            pages_manifest_warning_printed = False
            new_anomaly_events = []
            capture_stats = {
                "new_count": 0,
                "overwritten_count": 0,
                "skipped_existing_count": 0,
                "skipped_unknown_count": 0,
            }

            def save_pages_manifest_best_effort():
                nonlocal pages_manifest_warning_printed
                entries, ignored_noncanonical_count = scan_canonical_pages_manifest_entries(
                    screenshots_dir
                )
                payload = build_pages_manifest_payload(
                    args.asin,
                    pages_manifest_captured_at,
                    entries,
                    capture_stats=capture_stats,
                    ignored_noncanonical_count=ignored_noncanonical_count,
                    anomaly_events=existing_anomaly_events + new_anomaly_events,
                    last_run_mode="capture_pages",
                )
                try:
                    save_pages_manifest(pages_manifest_path, payload)
                except Exception:
                    if not pages_manifest_warning_printed:
                        print("Warning: failed to write pages.json manifest.")
                        pages_manifest_warning_printed = True

            failed_pages = []
            try:
                failed_pages = capture_requested_pages(
                    page,
                    requested_capture_pages,
                    screenshots_dir,
                    args.overwrite_existing,
                    capture_stats,
                    save_pages_manifest_best_effort,
                    new_anomaly_events,
                    run_id,
                )
            except KeyboardInterrupt:
                print("\nStopped during requested-page capture.")
            finally:
                save_pages_manifest_best_effort()
                print(
                    "Capture summary: "
                    f"new={capture_stats['new_count']} "
                    f"overwritten={capture_stats['overwritten_count']} "
                    f"skipped_existing={capture_stats['skipped_existing_count']} "
                    f"skipped_unknown={capture_stats['skipped_unknown_count']}"
                )
                if failed_pages:
                    print(
                        "Requested pages not captured: "
                        + ", ".join(str(page_number) for page_number in failed_pages)
                    )

                if args.no_restore_position:
                    print("Info: start position restore disabled (--no-restore-position).")
                else:
                    print("Info: restoring start position...")
                    try:
                        restore_start_position(page, initial_page, initial_location)
                    except KeyboardInterrupt:
                        print(
                            "Info: interrupted while restoring start position; continuing shutdown."
                        )
                    except Exception:
                        print("Warning: unexpected error while restoring start position.")

                print("Closing in 5 seconds (press Ctrl+C again to close immediately)...")
                try:
                    time.sleep(5)
                except KeyboardInterrupt:
                    pass
                try:
                    context.close()
                except KeyboardInterrupt:
                    print("Info: interrupted while closing browser context.")
                except Exception:
                    print("Warning: browser context close raised an unexpected error.")
            return

        # Apply requested startup navigation.
        if args.start_page is not None:
            print(f"Navigating to start page {args.start_page}...")
            if go_to_page(page, args.start_page):
                print(f"Info: jumped to start page {args.start_page}.")
            else:
                raise SystemExit(
                    f"Error: could not navigate to start page {args.start_page}; aborting."
                )
        elif args.start_location is not None:
            print(f"Navigating to start location {args.start_location}...")
            location_jump_succeeded = go_to_location(page, args.start_location)
            if not location_jump_succeeded:
                print(
                    "Info: Go to Location is unavailable in the current context; "
                    "trying TOC top-first-entry fallback."
                )
                if prime_location_mode_via_toc_first_entry(page):
                    location_jump_succeeded = go_to_location(page, args.start_location)
                else:
                    print("Warning: could not prime location mode via TOC.")

            if location_jump_succeeded:
                (
                    resolved_page,
                    resolved_total,
                    resolved_location,
                    resolved_total_location,
                ) = get_page_info_with_retry(page, attempts=8, wait_ms=120)
                if resolved_location is not None and resolved_total_location is not None:
                    print(
                        "Info: requested start location "
                        f"{args.start_location} resolved to location "
                        f"{resolved_location} of {resolved_total_location}."
                    )
                elif resolved_page is not None and resolved_total is not None:
                    print(
                        "Info: requested start location "
                        f"{args.start_location} landed on page "
                        f"{resolved_page} of {resolved_total}."
                    )
                else:
                    print(f"Info: jumped to start location {args.start_location}.")
            else:
                raise SystemExit(
                    f"Error: could not navigate to start location {args.start_location}; aborting."
                )
        elif not args.no_restart:
            print("Navigating to the beginning...")
            if go_to_cover(page):
                print("Starting from the cover.")
            else:
                print("Could not find cover button — starting from current page.")

        current, total, current_location, total_location = get_page_info(page)
        nav_mode = "unknown"
        if current is not None and total is not None:
            nav_mode = "page"
        elif current_location is not None and total_location is not None:
            nav_mode = "location"

        if nav_mode == "location":
            print("Info: navigation mode locked to location.")
        elif nav_mode == "page":
            print("Info: navigation mode locked to page.")
        else:
            print("Warning: navigation mode is unknown; using mixed page/location checks.")

        if current is not None and total is not None:
            print(f"On page {current} of {total}.")
        elif current_location is not None and total_location is not None:
            print(f"On location {current_location} of {total_location}.")
        print(f"Saving page screenshots to {screenshots_dir}")
        if args.overwrite_existing:
            print("Overwrite mode enabled: existing screenshots will be replaced.")
        else:
            print(
                "Idempotent mode enabled: existing screenshots are skipped "
                "(use --overwrite-existing to replace)."
            )
        print(f"Auto-advancing every {args.seconds}s. Press Ctrl+C to stop.\n")

        pages_turned = 0
        turn_attempts = 0
        pages_manifest_captured_at = datetime.now(UTC).isoformat()
        run_id = pages_manifest_captured_at
        pages_manifest_warning_printed = False
        new_anomaly_events = []
        capture_stats = {
            "new_count": 0,
            "overwritten_count": 0,
            "skipped_existing_count": 0,
            "skipped_unknown_count": 0,
        }

        def save_pages_manifest_best_effort():
            nonlocal pages_manifest_warning_printed
            entries, ignored_noncanonical_count = scan_canonical_pages_manifest_entries(
                screenshots_dir
            )
            payload = build_pages_manifest_payload(
                args.asin,
                pages_manifest_captured_at,
                entries,
                capture_stats=capture_stats,
                ignored_noncanonical_count=ignored_noncanonical_count,
                anomaly_events=existing_anomaly_events + new_anomaly_events,
                last_run_mode="auto_turn",
            )
            try:
                save_pages_manifest(pages_manifest_path, payload)
            except Exception:
                if not pages_manifest_warning_printed:
                    print("Warning: failed to write pages.json manifest.")
                    pages_manifest_warning_printed = True

        # Capture the page currently on screen before any turns.
        capture_status, _screenshot_path = save_page_screenshot(
            page,
            screenshots_dir,
            overwrite_existing=args.overwrite_existing,
            prefer_location=(nav_mode == "location"),
            current=current,
            total=total,
            current_location=current_location,
            total_location=total_location,
        )
        update_capture_stats(capture_stats, capture_status)
        save_pages_manifest_best_effort()

        try:
            while True:
                if args.pages and pages_turned >= args.pages:
                    print(f"Done — advanced {pages_turned} pages.")
                    break

                # Check if we've reached the last page
                current, total, current_location, total_location = get_page_info(page)
                has_page_bounds = current is not None and total is not None
                has_location_bounds = current_location is not None and total_location is not None
                if nav_mode == "page":
                    if (
                        has_page_bounds
                        and content_max_page is not None
                        and current >= content_max_page
                    ):
                        print(f"Reached TOC content boundary (page {content_max_page}). Done!")
                        break
                    if has_page_bounds and current >= total:
                        print(f"Reached the last page ({current} of {total}). Done!")
                        break
                elif nav_mode == "location":
                    if (
                        has_location_bounds
                        and content_max_location is not None
                        and current_location >= content_max_location
                    ):
                        print(
                            f"Reached TOC content boundary (location {content_max_location}). Done!"
                        )
                        break
                    if has_location_bounds and current_location >= total_location:
                        print(
                            f"Reached the last location ({current_location} of {total_location}). Done!"
                        )
                        break
                else:
                    if has_page_bounds and content_max_page is not None:
                        if current >= content_max_page:
                            print(f"Reached TOC content boundary (page {content_max_page}). Done!")
                            break
                    if (
                        not has_page_bounds
                        and has_location_bounds
                        and content_max_location is not None
                    ):
                        if current_location >= content_max_location:
                            print(
                                f"Reached TOC content boundary (location {content_max_location}). Done!"
                            )
                            break
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

                turn_attempts += 1
                if not click_next_button(page):
                    print("Warning: next-page click failed; attempting TOC/modal recovery.")
                    if wait_for_next_control(
                        page, timeout_ms=2_500, poll_ms=120
                    ) and click_next_button(page):
                        print("Info: next-page click succeeded after recovery.")
                    else:
                        print("Next-page controls not found — likely at the end of the book.")
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
                    previous_page is not None and current is not None and current != previous_page
                )
                changed_by_location = (
                    previous_location is not None
                    and current_location is not None
                    and current_location != previous_location
                )
                if nav_mode == "page":
                    changed_by_footer_value = changed_by_page_number
                elif nav_mode == "location":
                    changed_by_footer_value = changed_by_location
                else:
                    changed_by_footer_value = changed_by_page_number or changed_by_location

                if isinstance(previous_page, int) and isinstance(current, int):
                    page_delta = current - previous_page
                    if page_delta != 1:
                        if page_delta == 0:
                            classification = "repeat"
                            unresolved_pages = []
                        elif page_delta > 1:
                            classification = "jump_forward"
                            unresolved_pages = list(range(previous_page + 1, current))
                        else:
                            classification = "jump_backward"
                            unresolved_pages = []

                        append_auto_turn_anomaly_event(
                            new_anomaly_events,
                            run_id=run_id,
                            turn_index=turn_attempts,
                            from_page=previous_page,
                            to_page=current,
                            delta=page_delta,
                            classification=classification,
                            unresolved_pages=unresolved_pages,
                            changed_by_signature=changed_by_signature,
                            changed_by_footer_value=changed_by_footer_value,
                        )

                        if unresolved_pages:
                            print(
                                "Warning: non-sequential page jump "
                                f"{previous_page} -> {current}; unresolved page candidates: "
                                + ", ".join(str(page_number) for page_number in unresolved_pages)
                            )
                        else:
                            print(
                                f"Warning: non-sequential page turn {previous_page} -> {current} "
                                f"({classification})."
                            )

                if changed_by_signature or changed_by_footer_value:
                    pages_turned += 1
                if not changed_by_signature:
                    if changed_by_footer_value:
                        print("Info: footer fallback confirmed page turn.")
                    else:
                        print("Warning: page content did not confirm change within 8s; continuing.")
                on_page_turn(page, current, total, current_location, total_location)

                same_page_marker = (
                    isinstance(previous_page, int)
                    and isinstance(current, int)
                    and current == previous_page
                )
                same_location_marker = (
                    isinstance(previous_location, int)
                    and isinstance(current_location, int)
                    and current_location == previous_location
                )
                allow_existing_variant = changed_by_signature and (
                    same_page_marker or same_location_marker
                )
                capture_status, _screenshot_path = save_page_screenshot(
                    page,
                    screenshots_dir,
                    overwrite_existing=args.overwrite_existing,
                    prefer_location=(nav_mode == "location"),
                    allow_existing_variant=allow_existing_variant,
                    current=current,
                    total=total,
                    current_location=current_location,
                    total_location=total_location,
                )
                update_capture_stats(capture_stats, capture_status)
                save_pages_manifest_best_effort()

        except KeyboardInterrupt:
            print(f"\nStopped after {pages_turned} pages.")
        finally:
            save_pages_manifest_best_effort()
            print(
                "Capture summary: "
                f"new={capture_stats['new_count']} "
                f"overwritten={capture_stats['overwritten_count']} "
                f"skipped_existing={capture_stats['skipped_existing_count']} "
                f"skipped_unknown={capture_stats['skipped_unknown_count']}"
            )
            if args.no_restore_position:
                print("Info: start position restore disabled (--no-restore-position).")
            else:
                print("Info: restoring start position...")
                try:
                    restore_start_position(page, initial_page, initial_location)
                except KeyboardInterrupt:
                    print("Info: interrupted while restoring start position; continuing shutdown.")
                except Exception:
                    print("Warning: unexpected error while restoring start position.")

            print("Closing in 5 seconds (press Ctrl+C again to close immediately)...")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                pass
            try:
                context.close()
            except KeyboardInterrupt:
                print("Info: interrupted while closing browser context.")
            except Exception:
                print("Warning: browser context close raised an unexpected error.")


if __name__ == "__main__":
    main()
