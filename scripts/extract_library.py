"""
Extract Kindle library metadata by scrolling the Kindle web library until
all books are loaded.

Usage:
    python scripts/extract_library.py
    python scripts/extract_library.py --headless --output books/library.json
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

DEFAULT_LIBRARY_URL = (
    "https://read.amazon.com/kindle-library?resourceType=EBOOK&sortType=acquisition_desc"
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_whitespace(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def get_visible_item_count(page: Page) -> int:
    return page.locator('ul#cover > li[id^="library-item-option-"]').count()


def get_scroll_metrics(page: Page) -> dict[str, int]:
    return page.evaluate("""
        () => {
            const main = document.querySelector("main#library");
            if (!main) {
                return { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
            }
            return {
                scrollTop: Math.floor(main.scrollTop),
                clientHeight: Math.floor(main.clientHeight),
                scrollHeight: Math.floor(main.scrollHeight),
            };
        }
    """)


def scroll_library_until_complete(
    page: Page,
    *,
    max_scroll_steps: int,
    scroll_pause_ms: int,
    stagnant_rounds: int,
) -> int:
    previous_count = -1
    stagnant = 0

    for step in range(1, max_scroll_steps + 1):
        current_count = get_visible_item_count(page)
        metrics = get_scroll_metrics(page)
        near_bottom = (
            metrics["scrollTop"] + metrics["clientHeight"] >= metrics["scrollHeight"] - 5
        )

        if current_count != previous_count:
            print(
                "Library load progress: "
                f"{current_count} visible books "
                f"(scroll {step}/{max_scroll_steps})."
            )
            previous_count = current_count
            stagnant = 0
        else:
            stagnant += 1

        if stagnant >= stagnant_rounds and near_bottom:
            print(
                "Scroll complete: no new books detected while at bottom "
                f"for {stagnant_rounds} rounds."
            )
            break

        page.evaluate("""
            () => {
                const main = document.querySelector("main#library");
                if (!main) {
                    return;
                }
                const delta = Math.max(420, Math.floor(main.clientHeight * 0.85));
                main.scrollBy(0, delta);
            }
        """)
        page.wait_for_timeout(scroll_pause_ms)
    else:
        print(
            "Warning: reached --max-scroll-steps before stability; "
            "library may be partially loaded."
        )

    # Give the page one final chance to finish async loading.
    page.wait_for_timeout(max(1200, scroll_pause_ms))
    return get_visible_item_count(page)


def extract_books_from_dom(page: Page) -> list[dict[str, Any]]:
    books: list[dict[str, Any]] = page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();
            const prefix = "library-item-option-";
            const items = Array.from(document.querySelectorAll('ul#cover > li[id^="library-item-option-"]'));

            for (const item of items) {
                const rawId = item.id || "";
                if (!rawId.startsWith(prefix)) {
                    continue;
                }
                const asin = rawId.slice(prefix.length).trim();
                if (!asin || seen.has(asin)) {
                    continue;
                }
                seen.add(asin);

                const titleNode =
                    document.getElementById(`title-${asin}`) ||
                    item.querySelector('[id^="title-"]');
                const authorNode =
                    document.getElementById(`author-${asin}`) ||
                    item.querySelector('[id^="author-"]');
                const imgNode =
                    document.getElementById(`cover-${asin}`) ||
                    item.querySelector("img");

                const title = (titleNode?.textContent || "").replace(/\\s+/g, " ").trim();
                const author = (authorNode?.textContent || "").replace(/\\s+/g, " ").trim();
                const coverImageUrl =
                    (imgNode && (imgNode.currentSrc || imgNode.src || imgNode.getAttribute("src"))) || "";

                results.push({
                    asin,
                    title,
                    author,
                    cover_image_url: coverImageUrl,
                    reader_url: `https://read.amazon.com/?asin=${asin}`,
                });
            }

            return results;
        }
    """)
    return books


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Extract all books from Kindle library.")
    parser.add_argument("--url", type=str, default=DEFAULT_LIBRARY_URL, help="Kindle library URL")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("books/library.json"),
        help="Output JSON path (default: books/library.json)",
    )
    parser.add_argument(
        "--max-scroll-steps",
        type=int,
        default=800,
        help="Maximum number of scroll actions while loading library",
    )
    parser.add_argument(
        "--scroll-pause-ms",
        type=int,
        default=900,
        help="Wait time after each scroll action in milliseconds",
    )
    parser.add_argument(
        "--stagnant-rounds",
        type=int,
        default=6,
        help="Stop after this many no-growth rounds when near bottom",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium in headless mode",
    )
    args = parser.parse_args()

    if args.max_scroll_steps < 1:
        parser.error("--max-scroll-steps must be >= 1")
    if args.scroll_pause_ms < 100:
        parser.error("--scroll-pause-ms must be >= 100")
    if args.stagnant_rounds < 1:
        parser.error("--stagnant-rounds must be >= 1")

    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    user_data_dir = Path.home() / ".kindle-reader-profile"
    print(f"Using profile: {user_data_dir}")
    print(f"Opening library: {args.url}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=args.headless,
            viewport={"width": 1400, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.url, wait_until="domcontentloaded")

            if "signin" in page.url or "ap/signin" in page.url:
                print("Please log into Amazon in the browser window.")
                print("Waiting for you to complete login...")
                page.wait_for_url("**/read.amazon.com/**", timeout=300_000)
                print("Login detected; continuing...")
                page.goto(args.url, wait_until="domcontentloaded")

            page.locator("main#library").first.wait_for(state="visible", timeout=30_000)
            page.locator("ul#cover").first.wait_for(state="visible", timeout=30_000)

            initial_count = get_visible_item_count(page)
            print(f"Initial visible books: {initial_count}")

            total_visible = scroll_library_until_complete(
                page,
                max_scroll_steps=args.max_scroll_steps,
                scroll_pause_ms=args.scroll_pause_ms,
                stagnant_rounds=args.stagnant_rounds,
            )
            print(f"Final visible books: {total_visible}")

            extracted = extract_books_from_dom(page)
            normalized_books = []
            for index, book in enumerate(extracted):
                asin = normalize_whitespace(str(book.get("asin") or ""))
                if not asin:
                    continue
                normalized_books.append(
                    {
                        "index": index,
                        "asin": asin,
                        "title": normalize_whitespace(str(book.get("title") or "")),
                        "author": normalize_whitespace(str(book.get("author") or "")),
                        "cover_image_url": normalize_whitespace(
                            str(book.get("cover_image_url") or "")
                        ),
                        "reader_url": normalize_whitespace(str(book.get("reader_url") or "")),
                    }
                )

            payload = {
                "captured_at": utc_now_iso(),
                "source_url": args.url,
                "book_count": len(normalized_books),
                "books": normalized_books,
            }
            output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Saved library extract: {output_path}")
            print(f"Books extracted: {len(normalized_books)}")
        finally:
            context.close()


if __name__ == "__main__":
    main()
