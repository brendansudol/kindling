"""Export allow-listed book analyses into title-named shareable directories.

The canonical analysis remains under ``books/<asin>/analysis``. This script copies
the approved Markdown and JSON artifacts byte-for-byte into ``shared-book-analyses``
and writes a deterministic index and manifest.

Usage:
    python scripts/export_book_analyses.py
    python scripts/export_book_analyses.py --check
    python scripts/export_book_analyses.py --prune
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_ALLOWLIST = Path("config/book-analysis-allowlist.txt")
DEFAULT_OUTPUT_DIR = Path("shared-book-analyses")
REQUIRED_ANALYSIS_FILES = (
    Path("chapter-map.md"),
    Path("book-synthesis.md"),
    Path("summary-audit.md"),
)
ACCEPTED_AUDIT_VERDICTS = ("Ready", "Ready after listed corrections")
AUDIT_VERDICTS = (
    "Ready after listed corrections",
    "Requires substantial revision",
    "Cannot be confidently assessed from the supplied source material",
    "Ready",
)


@dataclass(frozen=True)
class ExportBook:
    asin: str
    title: str
    slug: str
    audit_verdict: str
    source_dir: Path
    files: tuple[Path, ...]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parse_allowlist(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Allowlist not found: {path}")

    asins: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.split("#", 1)[0].strip()
        if not value:
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError(f"Invalid ASIN on {path}:{line_number}: {value!r}")
        if value in seen:
            raise ValueError(f"Duplicate ASIN in allowlist: {value}")
        seen.add(value)
        asins.append(value)

    if not asins:
        raise ValueError(f"Allowlist contains no ASINs: {path}")
    return asins


def clean_markdown_title(value: str) -> str:
    value = value.strip().strip("#").strip()
    value = re.sub(r"[*_`]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def title_from_chapter_map(path: Path) -> str | None:
    if not path.is_file():
        return None
    first_heading = next(
        (
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("# ")
        ),
        None,
    )
    if not first_heading:
        return None
    heading = first_heading[2:].strip()
    match = re.fullmatch(r"Chapter Map\s*(?:—|–|:)\s*(.+)", heading, re.IGNORECASE)
    if not match:
        return None
    title = clean_markdown_title(match.group(1))
    return title or None


def title_from_synthesis(path: Path) -> str | None:
    if not path.is_file():
        return None
    first_heading = next(
        (
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("# ")
        ),
        None,
    )
    if not first_heading:
        return None
    heading = first_heading[2:].strip()
    patterns = (
        r"(?:Whole-Book Synthesis|Book Synthesis)\s*(?:—|–|:)\s*(.+)",
        r"(.+?)\s*(?:—|–)\s*Whole-Book Synthesis",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, heading, re.IGNORECASE)
        if match:
            title = clean_markdown_title(match.group(1))
            if title:
                return title
    return None


def resolve_title(book_dir: Path) -> str:
    metadata_path = book_dir / "metadata.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid metadata JSON: {metadata_path}: {exc}") from exc
        title = metadata.get("title") if isinstance(metadata, dict) else None
        if isinstance(title, str) and title.strip():
            return re.sub(r"\s+", " ", title).strip()

    analysis_dir = book_dir / "analysis"
    title = title_from_chapter_map(analysis_dir / "chapter-map.md")
    if title:
        return title
    title = title_from_synthesis(analysis_dir / "book-synthesis.md")
    if title:
        return title
    raise ValueError(
        f"No trustworthy title found for {book_dir.name}; add metadata or a standard analysis heading"
    )


def slugify_title(title: str) -> str:
    value = title.casefold().replace("&", " and ")
    value = re.sub(r"['’]", "", value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value:
        raise ValueError(f"Title cannot produce a directory slug: {title!r}")
    return value


def read_final_audit_verdict(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(value) for value in AUDIT_VERDICTS) + r")(?![\w-])",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError(f"No recognized final audit verdict found: {path}")
    raw_verdict = matches[-1].group(1)
    verdict = next(value for value in AUDIT_VERDICTS if value.casefold() == raw_verdict.casefold())
    if verdict not in ACCEPTED_AUDIT_VERDICTS:
        raise ValueError(f"Analysis is not approved for export ({verdict}): {path}")
    return verdict


def collect_analysis_files(analysis_dir: Path) -> tuple[Path, ...]:
    if not analysis_dir.is_dir():
        raise FileNotFoundError(f"Analysis directory not found: {analysis_dir}")
    for required in REQUIRED_ANALYSIS_FILES:
        if not (analysis_dir / required).is_file():
            raise FileNotFoundError(f"Required analysis file not found: {analysis_dir / required}")

    chapters = sorted((analysis_dir / "chapters").glob("*.md"))
    if not chapters:
        raise FileNotFoundError(f"No chapter summaries found: {analysis_dir / 'chapters'}")
    shareable = [Path("chapter-map.md"), Path("book-synthesis.md")]
    shareable.extend(path.relative_to(analysis_dir) for path in chapters)
    for relative in shareable:
        if (analysis_dir / relative).is_symlink():
            raise ValueError(f"Analysis export refuses symlinks: {analysis_dir / relative}")
    return tuple(shareable)


def load_export_books(repo_root: Path, asins: list[str]) -> list[ExportBook]:
    books: list[ExportBook] = []
    slugs: dict[str, str] = {}
    for asin in asins:
        book_dir = repo_root / "books" / asin
        if not book_dir.is_dir():
            raise FileNotFoundError(f"Allow-listed book directory not found: {book_dir}")
        title = resolve_title(book_dir)
        slug = slugify_title(title)
        if slug in slugs:
            raise ValueError(
                f"Title slug collision: {asin} and {slugs[slug]} both resolve to {slug!r}"
            )
        slugs[slug] = asin
        source_dir = book_dir / "analysis"
        books.append(
            ExportBook(
                asin=asin,
                title=title,
                slug=slug,
                audit_verdict=read_final_audit_verdict(source_dir / "summary-audit.md"),
                source_dir=source_dir,
                files=collect_analysis_files(source_dir),
            )
        )
    return books


def build_index(books: list[ExportBook], allowlist_label: str) -> bytes:
    lines = [
        "# Shared Book Analyses",
        "",
        "Generated from the canonical analyses under `books/<asin>/analysis/` by",
        "`scripts/export_book_analyses.py`. These files are shareable copies; edit the",
        "canonical analysis and regenerate rather than editing this directory directly.",
        "Each report was independently checked against its source transcript, with",
        "substantive corrections applied before publication.",
        "",
        f"Allowlist: `{allowlist_label}`",
        "",
        "| Book | ASIN | Synthesis | Chapters | Chapter Map | Audit Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for book in books:
        lines.append(
            f"| {book.title} | `{book.asin}` | "
            f"[Synthesis]({book.slug}/book-synthesis.md) | "
            f"[Chapters]({book.slug}/chapters/) | "
            f"[Map]({book.slug}/chapter-map.md) | "
            f"{book.audit_verdict} |"
        )
    return ("\n".join(lines) + "\n").encode()


def build_export(
    repo_root: Path, allowlist_path: Path
) -> tuple[dict[Path, bytes], list[ExportBook]]:
    asins = parse_allowlist(allowlist_path)
    books = load_export_books(repo_root, asins)
    expected: dict[Path, bytes] = {}
    manifest_books = []

    for book in books:
        manifest_files = []
        for relative in book.files:
            content = (book.source_dir / relative).read_bytes()
            destination = Path(book.slug) / relative
            expected[destination] = content
            manifest_files.append(
                {
                    "path": relative.as_posix(),
                    "sha256": sha256_bytes(content),
                    "size_bytes": len(content),
                }
            )
        manifest_books.append(
            {
                "asin": book.asin,
                "title": book.title,
                "directory": book.slug,
                "audit_verdict": book.audit_verdict,
                "chapter_summary_count": sum(
                    relative.parent == Path("chapters") and relative.suffix == ".md"
                    for relative in book.files
                ),
                "files": manifest_files,
            }
        )

    try:
        allowlist_label = allowlist_path.relative_to(repo_root).as_posix()
    except ValueError:
        allowlist_label = str(allowlist_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "allowlist": allowlist_label,
        "book_count": len(books),
        "books": manifest_books,
    }
    expected[Path("manifest.json")] = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode()
    expected[Path("README.md")] = build_index(books, allowlist_label)
    return expected, books


def inventory_output(output_dir: Path) -> tuple[set[Path], set[Path]]:
    files: set[Path] = set()
    symlinks: set[Path] = set()
    if not output_dir.exists():
        return files, symlinks
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError(f"Output path must be a real directory: {output_dir}")
    for path in output_dir.rglob("*"):
        relative = path.relative_to(output_dir)
        if path.is_symlink():
            symlinks.add(relative)
        elif path.is_file():
            files.add(relative)
    return files, symlinks


def check_export(output_dir: Path, expected: dict[Path, bytes]) -> list[str]:
    if not output_dir.is_dir() or output_dir.is_symlink():
        return [f"Export directory is missing or invalid: {output_dir}"]
    actual_files, symlinks = inventory_output(output_dir)
    expected_files = set(expected)
    errors = [f"Symlink is not allowed in export: {path}" for path in sorted(symlinks)]
    errors.extend(f"Missing export file: {path}" for path in sorted(expected_files - actual_files))
    errors.extend(
        f"Unexpected export file: {path}" for path in sorted(actual_files - expected_files)
    )
    for relative in sorted(expected_files & actual_files):
        if (output_dir / relative).read_bytes() != expected[relative]:
            errors.append(f"Stale or modified export file: {relative}")
    return errors


def prune_paths(output_dir: Path, relative_paths: set[Path]) -> None:
    for relative in sorted(relative_paths, key=lambda path: len(path.parts), reverse=True):
        target = output_dir / relative
        if target.is_symlink() or target.is_file():
            target.unlink()
    directories = sorted(
        (path for path in output_dir.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def write_export(output_dir: Path, expected: dict[Path, bytes], *, prune: bool) -> None:
    actual_files, symlinks = inventory_output(output_dir)
    unexpected = actual_files - set(expected)
    unsafe_paths = unexpected | symlinks
    if unsafe_paths and not prune:
        details = ", ".join(str(path) for path in sorted(unsafe_paths))
        raise ValueError(f"Unexpected export paths found ({details}); rerun with --prune")
    if prune and output_dir.exists():
        prune_paths(output_dir, unsafe_paths)

    output_dir.mkdir(parents=True, exist_ok=True)
    for relative, content in sorted(expected.items()):
        destination = output_dir / relative
        if destination.is_symlink():
            raise ValueError(f"Refusing to overwrite export symlink: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def resolve_repo_path(repo_root: Path, value: Path, *, label: str) -> Path:
    resolved = value.resolve() if value.is_absolute() else (repo_root / value).resolve()
    if resolved == repo_root or repo_root not in resolved.parents:
        raise ValueError(
            f"{label} must be inside the repository and cannot be its root: {resolved}"
        )
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help=f"ASIN allowlist (default: {DEFAULT_ALLOWLIST})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Shareable export directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--check", action="store_true", help="Validate without writing")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove unexpected files from the generated export before writing",
    )
    args = parser.parse_args()
    if args.check and args.prune:
        parser.error("--check and --prune cannot be combined")

    repo_root = Path.cwd().resolve()
    try:
        allowlist_path = resolve_repo_path(repo_root, args.allowlist, label="Allowlist")
        output_dir = resolve_repo_path(repo_root, args.output_dir, label="Output directory")
        books_dir = (repo_root / "books").resolve()
        if output_dir == books_dir or books_dir in output_dir.parents:
            raise ValueError("Output directory must be outside the gitignored books/ tree")
        expected, books = build_export(repo_root, allowlist_path)
        if args.check:
            errors = check_export(output_dir, expected)
            if errors:
                for error in errors:
                    print(f"ERROR: {error}")
                return 1
            print(f"Export valid: {len(books)} books, {len(expected)} files in {output_dir}")
            return 0

        write_export(output_dir, expected, prune=args.prune)
        errors = check_export(output_dir, expected)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(f"Exported {len(books)} books and {len(expected)} files to {output_dir}")
        for book in books:
            print(f"- {book.asin}: {book.slug}/")
        return 0
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
