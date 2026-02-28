"""
Migrate captured page assets to idempotent nav-keyed filenames.

Rules:
- Canonical names: page-####-of-####.png / loc-####-of-####.png
- Collisions: keep newest sequence (highest legacy sequence number)
- Legacy unknown files (*-page-unknown.png): delete
- pages.json becomes canonical snapshot (no sequence field)
- transcript capture/canonical path references are rewritten to new filenames
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from transcribe import build_markdown_transcript  # noqa: E402

CANONICAL_PAGE_RE = re.compile(r"^page-(\d+)-of-(\d+)\.png$")
CANONICAL_LOCATION_RE = re.compile(r"^loc-(\d+)-of-(\d+)\.png$")
LEGACY_PAGE_RE = re.compile(r"^(?:capture-)?(\d+)-page-(\d+)-of-(\d+)\.png$")
LEGACY_LOCATION_RE = re.compile(r"^(?:capture-)?(\d+)-loc-(\d+)-of-(\d+)\.png$")
LEGACY_UNKNOWN_RE = re.compile(r"^(?:capture-)?(\d+)-page-unknown\.png$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")


def parse_canonical_filename(name: str) -> dict[str, int] | None:
    page_match = CANONICAL_PAGE_RE.match(name)
    if page_match:
        return {
            "page": int(page_match.group(1)),
            "total": int(page_match.group(2)),
            "location": None,
            "total_location": None,
        }

    location_match = CANONICAL_LOCATION_RE.match(name)
    if location_match:
        return {
            "page": None,
            "total": None,
            "location": int(location_match.group(1)),
            "total_location": int(location_match.group(2)),
        }

    return None


def parse_legacy_filename(name: str) -> dict[str, int] | None:
    page_match = LEGACY_PAGE_RE.match(name)
    if page_match:
        return {
            "sequence": int(page_match.group(1)),
            "page": int(page_match.group(2)),
            "total": int(page_match.group(3)),
            "location": None,
            "total_location": None,
        }

    location_match = LEGACY_LOCATION_RE.match(name)
    if location_match:
        return {
            "sequence": int(location_match.group(1)),
            "page": None,
            "total": None,
            "location": int(location_match.group(2)),
            "total_location": int(location_match.group(3)),
        }

    return None


def build_canonical_filename(metadata: dict[str, int]) -> str:
    if metadata.get("page") is not None and metadata.get("total") is not None:
        return f"page-{metadata['page']:04d}-of-{metadata['total']:04d}.png"

    location = metadata.get("location")
    total_location = metadata.get("total_location")
    if location is not None and total_location is not None:
        width = max(4, len(str(total_location)))
        return f"loc-{location:0{width}d}-of-{total_location:0{width}d}.png"

    raise ValueError(f"Unsupported metadata for canonical naming: {metadata}")


def canonical_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    if entry.get("location") is not None:
        return (
            0,
            entry.get("location") or 0,
            entry.get("total_location") or 0,
            entry.get("file") or "",
        )
    if entry.get("page") is not None:
        return (
            1,
            entry.get("page") or 0,
            entry.get("total") or 0,
            entry.get("file") or "",
        )
    return (2, 0, 0, entry.get("file") or "")


def build_pages_snapshot_payload(asin: str, pages_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    ignored_noncanonical_count = 0

    for screenshot_path in pages_dir.glob("*.png"):
        parsed = parse_canonical_filename(screenshot_path.name)
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
            }
        )

    entries.sort(key=canonical_sort_key)
    for idx, entry in enumerate(entries):
        entry["index"] = idx

    page_nav_count = sum(
        1 for item in entries if item.get("page") is not None and item.get("total") is not None
    )
    location_nav_count = sum(
        1
        for item in entries
        if item.get("location") is not None and item.get("total_location") is not None
    )

    return {
        "asin": asin,
        "captured_at": utc_now_iso(),
        "pages": entries,
        "summary": {
            "capture_count": len(entries),
            "page_nav_count": page_nav_count,
            "location_nav_count": location_nav_count,
            "unknown_nav_count": 0,
            "ignored_noncanonical_count": ignored_noncanonical_count,
        },
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def choose_winner(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    with_sequence = [item for item in candidates if isinstance(item.get("sequence"), int)]
    if with_sequence:
        with_sequence.sort(
            key=lambda item: (item["sequence"], item["path"].name),
            reverse=True,
        )
        return with_sequence[0]

    candidates.sort(key=lambda item: item["path"].name)
    return candidates[0]


def update_transcript_references(book_dir: Path, rel_path_map: dict[str, str | None]) -> None:
    transcripts_dir = book_dir / "transcripts"
    if not transcripts_dir.exists():
        return

    captures_path = transcripts_dir / "captures.jsonl"
    canonical_dir = transcripts_dir / "canonical"
    book_markdown_path = transcripts_dir / "book.md"

    capture_rows: list[dict[str, Any]] = []
    if captures_path.exists():
        capture_rows = load_jsonl(captures_path)
        updated_rows: list[dict[str, Any]] = []
        for row in capture_rows:
            old_rel = row.get("path") if isinstance(row.get("path"), str) else None
            mapped_rel = rel_path_map.get(old_rel, old_rel)
            if mapped_rel is None or not isinstance(mapped_rel, str):
                continue
            target_path = book_dir / mapped_rel
            if not target_path.exists():
                continue

            row["path"] = mapped_rel
            row["file"] = target_path.name
            row.pop("sequence", None)
            updated_rows.append(row)

        # If multiple legacy captures map to one canonical file, keep the latest row.
        deduped_by_path: dict[str, dict[str, Any]] = {}
        path_order: list[str] = []
        for row in updated_rows:
            rel_path = row.get("path")
            if not isinstance(rel_path, str):
                continue
            if rel_path not in deduped_by_path:
                path_order.append(rel_path)
            deduped_by_path[rel_path] = row

        updated_rows = [deduped_by_path[rel_path] for rel_path in path_order]
        for idx, row in enumerate(updated_rows):
            row["index"] = idx

        write_jsonl(captures_path, updated_rows)
        capture_rows = updated_rows

    canonical_results: dict[str, dict[str, Any]] = {}
    if canonical_dir.exists():
        for canonical_path in sorted(canonical_dir.glob("*.json")):
            try:
                payload = read_json(canonical_path)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            representative = payload.get("representative_image")
            if isinstance(representative, str):
                mapped = rel_path_map.get(representative, representative)
                if isinstance(mapped, str) and (book_dir / mapped).exists():
                    payload["representative_image"] = mapped
                else:
                    payload["representative_image"] = None

            captures = payload.get("captures")
            if isinstance(captures, list):
                updated_captures: list[dict[str, Any]] = []
                for capture in captures:
                    if not isinstance(capture, dict):
                        continue
                    old_rel = capture.get("path") if isinstance(capture.get("path"), str) else None
                    mapped_rel = rel_path_map.get(old_rel, old_rel)
                    if mapped_rel is None or not isinstance(mapped_rel, str):
                        continue
                    if not (book_dir / mapped_rel).exists():
                        continue
                    capture["path"] = mapped_rel
                    capture.pop("sequence", None)
                    updated_captures.append(capture)

                payload["captures"] = updated_captures
                if payload.get("representative_image") is None and updated_captures:
                    payload["representative_image"] = updated_captures[0].get("path")

            write_json(canonical_path, payload)
            canonical_id = payload.get("canonical_id")
            if not isinstance(canonical_id, str) or not canonical_id:
                canonical_id = canonical_path.stem
            canonical_results[canonical_id] = payload

    if capture_rows and canonical_results:
        markdown = build_markdown_transcript(
            asin=book_dir.name,
            book_dir=book_dir,
            capture_records=capture_rows,
            canonical_results=canonical_results,
        )
        book_markdown_path.write_text(markdown, encoding="utf-8")


def migrate_book(book_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    pages_dir = book_dir / "pages"
    if not pages_dir.exists():
        return {
            "asin": book_dir.name,
            "renamed": 0,
            "deleted_duplicates": 0,
            "deleted_unknown": 0,
            "untouched_noncanonical": 0,
            "skipped": True,
        }

    groups: dict[str, list[dict[str, Any]]] = {}
    unknown_paths: list[Path] = []
    untouched_noncanonical: list[Path] = []
    rel_path_map: dict[str, str | None] = {}

    for image_path in pages_dir.glob("*.png"):
        canonical_meta = parse_canonical_filename(image_path.name)
        if canonical_meta is not None:
            target_name = image_path.name
            groups.setdefault(target_name, []).append(
                {
                    "path": image_path,
                    "sequence": None,
                }
            )
            continue

        legacy_meta = parse_legacy_filename(image_path.name)
        if legacy_meta is not None:
            target_name = build_canonical_filename(legacy_meta)
            groups.setdefault(target_name, []).append(
                {
                    "path": image_path,
                    "sequence": legacy_meta["sequence"],
                }
            )
            continue

        if LEGACY_UNKNOWN_RE.match(image_path.name):
            unknown_paths.append(image_path)
            rel_path_map[f"pages/{image_path.name}"] = None
            continue

        untouched_noncanonical.append(image_path)

    renamed = 0
    deleted_duplicates = 0
    deleted_unknown = 0

    for target_name in sorted(groups.keys()):
        candidates = groups[target_name]
        winner = choose_winner(candidates)
        winner_path = winner["path"]
        target_path = pages_dir / target_name
        original_paths = [item["path"] for item in candidates]

        for original_path in original_paths:
            rel_path_map[f"pages/{original_path.name}"] = f"pages/{target_name}"

        if winner_path != target_path:
            if target_path.exists() and target_path != winner_path:
                if not dry_run:
                    target_path.unlink()
                deleted_duplicates += 1

            if not dry_run:
                winner_path.rename(target_path)
            renamed += 1

        for original_path in original_paths:
            if original_path == winner_path:
                continue
            if original_path.exists():
                if not dry_run:
                    original_path.unlink()
                deleted_duplicates += 1

    for unknown_path in unknown_paths:
        if unknown_path.exists():
            if not dry_run:
                unknown_path.unlink()
            deleted_unknown += 1

    if not dry_run:
        pages_payload = build_pages_snapshot_payload(book_dir.name, pages_dir)
        write_json(book_dir / "pages.json", pages_payload)
        update_transcript_references(book_dir, rel_path_map)

    return {
        "asin": book_dir.name,
        "renamed": renamed,
        "deleted_duplicates": deleted_duplicates,
        "deleted_unknown": deleted_unknown,
        "untouched_noncanonical": len(untouched_noncanonical),
        "skipped": False,
    }


def find_book_dirs(root: Path, asin: str | None) -> list[Path]:
    if asin:
        target = root / asin
        return [target] if target.exists() else []

    return sorted(path for path in root.iterdir() if path.is_dir())


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate page captures to nav-keyed filenames")
    parser.add_argument("--asin", default=None, help="Specific ASIN directory under books/")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    books_root = Path.cwd() / "books"
    if not books_root.exists():
        print(f"Error: books directory not found: {books_root}")
        return 1

    book_dirs = find_book_dirs(books_root, args.asin)
    if not book_dirs:
        if args.asin:
            print(f"Error: ASIN directory not found under books/: {args.asin}")
            return 1
        print("No book directories found.")
        return 0

    for book_dir in book_dirs:
        result = migrate_book(book_dir, dry_run=args.dry_run)
        if result["skipped"]:
            print(f"[{result['asin']}] skipped (no pages directory)")
            continue
        print(
            f"[{result['asin']}] renamed={result['renamed']} "
            f"deleted_duplicates={result['deleted_duplicates']} "
            f"deleted_unknown={result['deleted_unknown']} "
            f"untouched_noncanonical={result['untouched_noncanonical']}"
        )

    if args.dry_run:
        print("Dry run complete. No files were modified.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
