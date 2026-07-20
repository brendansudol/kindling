"""Build deterministic, capture-aware transcript sections for one book.

The whole-book transcript remains the canonical compiled view. This script uses the
verified JSON boundary block in ``analysis/chapter-map.md`` plus ``captures.jsonl``
and canonical OCR records to create reproducible section-sized views.

Usage:
    python scripts/build_sections.py --asin B00FO74WXA
    python scripts/build_sections.py --asin B00FO74WXA --dry-run
    python scripts/build_sections.py --asin B00FO74WXA --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GENERATOR_VERSION = "capture-sections-v2"


@dataclass(frozen=True)
class Capture:
    index: int
    capture_id: str
    label: str
    marker_kind: str | None
    marker_value: int | None
    transcript_ref: str
    text: str
    status: str


@dataclass(frozen=True, order=True)
class Anchor:
    capture_index: int
    text_offset: int


@dataclass(frozen=True)
class Resolution:
    anchor: Anchor | None
    method: str
    matched_text: str | None
    warnings: tuple[str, ...]
    review_note: str | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def manifest_path(path: Path, book_dir: Path) -> str:
    try:
        return str(path.relative_to(book_dir))
    except ValueError:
        return str(path)


def parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return slug or "section"


def format_capture_label(record: dict[str, Any]) -> tuple[str, str | None, int | None]:
    page = parse_int(record.get("page"))
    total = parse_int(record.get("total"))
    location = parse_int(record.get("location"))
    total_location = parse_int(record.get("total_location"))
    if page is not None and total is not None:
        return f"Page {page} of {total}", "Page", page
    if location is not None and total_location is not None:
        return f"Location {location} of {total_location}", "Location", location
    if page is not None:
        return f"Page {page}", "Page", page
    if location is not None:
        return f"Location {location}", "Location", location
    return "Capture", None, None


def parse_chapter_map(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if len(blocks) != 1:
        raise ValueError(f"Expected exactly one fenced JSON block in {path}; found {len(blocks)}")
    payload = json.loads(blocks[0])
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Chapter-map JSON must be a nonempty list: {path}")

    required = {"seq", "title", "type", "start_marker", "end_marker", "completeness"}
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Chapter-map entry {index} is not an object")
        missing = required - item.keys()
        if missing:
            raise ValueError(f"Chapter-map entry {index} is missing: {', '.join(sorted(missing))}")
        seq = parse_int(item.get("seq"))
        if seq is None:
            raise ValueError(f"Chapter-map entry {index} has an invalid seq")
        entries.append({**item, "seq": seq})

    sequences = [entry["seq"] for entry in entries]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ValueError("Chapter-map seq values must be unique and increasing")
    return entries


def load_boundary_overrides(
    path: Path | None, entries: list[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Boundary overrides must use schema_version {SCHEMA_VERSION}: {path}")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, dict):
        raise ValueError(f"Boundary overrides must contain a sections object: {path}")
    valid_sequences = {entry["seq"] for entry in entries}
    overrides: dict[int, dict[str, Any]] = {}
    for raw_seq, override in raw_sections.items():
        seq = parse_int(raw_seq)
        if seq is None or seq not in valid_sequences:
            raise ValueError(f"Boundary override references an unknown section: {raw_seq}")
        if not isinstance(override, dict):
            raise ValueError(f"Boundary override for section {seq} is not an object")
        capture_id = override.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id:
            raise ValueError(f"Boundary override for section {seq} needs a capture_id")
        overrides[seq] = override
    return overrides


def load_capture_records(transcripts_dir: Path) -> list[Capture]:
    records_path = transcripts_dir / "captures.jsonl"
    if not records_path.exists():
        raise FileNotFoundError(f"Missing capture manifest: {records_path}")

    captures: list[Capture] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"Capture record {line_number} is not an object")
        capture_id = record.get("capture_id")
        transcript_ref = record.get("transcript_ref")
        if not isinstance(capture_id, str) or not capture_id:
            raise ValueError(f"Capture record {line_number} has no capture_id")
        if not isinstance(transcript_ref, str) or not transcript_ref:
            raise ValueError(f"Capture record {line_number} has no transcript_ref")

        canonical_path = transcripts_dir / transcript_ref
        canonical = read_json(canonical_path)
        final = canonical.get("final") if isinstance(canonical, dict) else None
        text = final.get("text") if isinstance(final, dict) else ""
        if not isinstance(text, str):
            text = ""
        status = canonical.get("status") if isinstance(canonical, dict) else None
        label, marker_kind, marker_value = format_capture_label(record)
        captures.append(
            Capture(
                index=len(captures),
                capture_id=capture_id,
                label=label,
                marker_kind=marker_kind,
                marker_value=marker_value,
                transcript_ref=transcript_ref,
                text=text.strip(),
                status=str(status or "unknown"),
            )
        )
    if not captures:
        raise ValueError(f"No capture records found in {records_path}")
    return captures


def parse_marker(value: str) -> tuple[str, int] | None:
    match = re.search(r"\b(Page|Location)\s+(\d+)\b", value, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).title(), int(match.group(2))


def normalize_title(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.translate(str.maketrans({"'": "", "‘": "", "’": "", "‚": ""}))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def title_variants(title: str) -> list[str]:
    candidates = [title]
    candidates.extend(part.strip() for part in re.split(r"\s*[:—–-]\s*", title) if part.strip())
    normalized = normalize_title(title)
    stripped = re.sub(r"^(?:chapter|section)\s+\d+\s+", "", normalized)
    stripped = re.sub(r"^\d+\s+", "", stripped)
    candidates.append(stripped)
    variants: list[str] = []
    for candidate in candidates:
        value = normalize_title(candidate)
        if value and value not in variants:
            variants.append(value)
    return variants


def title_match_key(value: str) -> str:
    tokens = normalize_title(value).split()
    if tokens and tokens[0] == "chapter":
        tokens = tokens[1:]
    return " ".join(token for token in tokens if token not in {"a", "an", "the"})


def significant_lines(text: str) -> list[tuple[int, str, bool]]:
    lines: list[tuple[int, str, bool]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        normalized = normalize_title(line)
        if normalized:
            lines.append((offset, normalized, bool(re.match(r"^\s*#", line))))
        offset += len(raw)
    return lines


def best_title_match(text: str, title: str) -> tuple[int, str, int] | None:
    lines = significant_lines(text)
    variants = title_variants(title)
    full_title = variants[0]
    best: tuple[int, int, int, str] | None = None

    for line_index, (offset, _, _) in enumerate(lines):
        for width in range(1, min(3, len(lines) - line_index) + 1):
            group = lines[line_index : line_index + width]
            combined = " ".join(item[1] for item in group)
            heading_count = sum(item[2] for item in group)
            score = 0
            if combined == full_title:
                score = 1000
            elif len(full_title.split()) >= 2 and full_title in combined:
                score = 900
            elif len(combined.split()) >= 2 and combined in full_title:
                score = 850
            else:
                for variant in variants[1:]:
                    token_count = len(variant.split())
                    if combined == variant:
                        score = max(score, 700 + min(token_count, 20))
                    elif token_count >= 2 and variant in combined:
                        score = max(score, 600 + min(token_count, 20))
            combined_key = title_match_key(combined)
            for variant in variants:
                variant_key = title_match_key(variant)
                if variant_key and combined_key == variant_key:
                    score = max(score, 675 + min(len(variant_key.split()), 20))
            if not score:
                continue
            score += heading_count * 25
            candidate = (score, -width, -offset, combined)
            if best is None or candidate > best:
                best = candidate

    if best is None:
        return None
    score, neg_width, neg_offset, matched = best
    return -neg_offset, matched, score


def resolve_override(
    entry: dict[str, Any],
    captures: list[Capture],
    previous: Anchor | None,
    override: dict[str, Any],
) -> Resolution:
    capture_id = str(override["capture_id"])
    capture = next((item for item in captures if item.capture_id == capture_id), None)
    if capture is None:
        raise ValueError(f"Section {entry['seq']} override references unknown capture {capture_id}")
    marker = parse_marker(str(entry["start_marker"]))
    if marker is not None and (capture.marker_kind, capture.marker_value) != marker:
        review_note = override.get("reason")
        if not override.get("allow_marker_mismatch") or not isinstance(review_note, str):
            raise ValueError(
                f"Section {entry['seq']} override capture {capture_id} does not match "
                f"{entry['start_marker']}; set allow_marker_mismatch with a reason after review"
            )
        review_note = review_note.strip()
        if not review_note:
            raise ValueError(f"Section {entry['seq']} marker-mismatch override needs a reason")
    else:
        raw_note = override.get("reason")
        review_note = raw_note.strip() if isinstance(raw_note, str) and raw_note.strip() else None

    match_text = override.get("match_text")
    matched_text: str | None = None
    if isinstance(match_text, str) and match_text:
        occurrence = parse_int(override.get("match_occurrence")) or 1
        if occurrence < 1:
            raise ValueError(f"Section {entry['seq']} override occurrence must be positive")
        offset = -1
        search_from = 0
        for _ in range(occurrence):
            offset = capture.text.find(match_text, search_from)
            if offset < 0:
                raise ValueError(
                    f"Section {entry['seq']} override text was not found in {capture_id}: "
                    f"{match_text!r}"
                )
            search_from = offset + len(match_text)
        matched_text = match_text
        method = "override_text"
    else:
        offset = parse_int(override.get("text_offset")) or 0
        method = "override_capture"
    if offset < 0 or offset > len(capture.text):
        raise ValueError(f"Section {entry['seq']} override offset is out of range in {capture_id}")
    anchor = Anchor(capture.index, offset)
    if previous is not None and anchor <= previous:
        raise ValueError(f"Section {entry['seq']} override is not after the prior section boundary")
    return Resolution(anchor, method, matched_text, (), review_note)


def resolve_start(
    entry: dict[str, Any],
    captures: list[Capture],
    previous: Anchor | None,
    override: dict[str, Any] | None = None,
) -> Resolution:
    if override is not None:
        return resolve_override(entry, captures, previous, override)
    marker_text = str(entry["start_marker"])
    marker = parse_marker(marker_text)
    if marker is None:
        if re.search(r"not present|unavailable|toc only", marker_text, flags=re.IGNORECASE):
            return Resolution(None, "unavailable", None, ())
        raise ValueError(
            f"Section {entry['seq']} ({entry['title']}) has an unparseable start marker: "
            f"{marker_text}"
        )

    kind, value = marker
    candidates = [
        capture
        for capture in captures
        if capture.marker_kind == kind and capture.marker_value == value
    ]
    if not candidates:
        if re.search(r"toc only|not present", marker_text, flags=re.IGNORECASE):
            return Resolution(None, "unavailable", None, ())
        raise ValueError(
            f"Section {entry['seq']} ({entry['title']}) start marker does not exist: {marker_text}"
        )

    matches: list[tuple[int, int, int, str]] = []
    for capture in candidates:
        match = best_title_match(capture.text, str(entry["title"]))
        if match is None:
            continue
        offset, matched_text, score = match
        anchor = Anchor(capture.index, offset)
        if previous is None or anchor > previous:
            matches.append((score, -capture.index, -offset, matched_text))

    if matches:
        score, neg_index, neg_offset, matched_text = max(matches)
        del score
        return Resolution(
            Anchor(-neg_index, -neg_offset),
            "title_match",
            matched_text,
            (),
        )

    eligible = [
        capture for capture in candidates if previous is None or Anchor(capture.index, 0) > previous
    ]
    if not eligible:
        raise ValueError(
            f"Section {entry['seq']} ({entry['title']}) cannot be placed monotonically at "
            f"{marker_text}; add a more precise mapped boundary"
        )
    capture = eligible[0]
    warnings = (
        f"Title was not found at {marker_text}; used the first eligible capture boundary "
        f"({capture.capture_id}).",
    )
    return Resolution(Anchor(capture.index, 0), "marker_fallback", None, warnings)


def resolve_final_end(entry: dict[str, Any], captures: list[Capture], start: Anchor) -> Anchor:
    marker_text = str(entry["end_marker"])
    marker = parse_marker(marker_text)
    if marker is None:
        return Anchor(len(captures), 0)
    kind, value = marker
    candidates = [
        capture
        for capture in captures
        if capture.marker_kind == kind
        and capture.marker_value == value
        and capture.index >= start.capture_index
    ]
    if not candidates:
        raise ValueError(
            f"Final section {entry['seq']} ({entry['title']}) end marker does not exist: "
            f"{marker_text}"
        )
    return Anchor(candidates[-1].index + 1, 0)


def segment_captures(captures: list[Capture], start: Anchor, end: Anchor) -> list[dict[str, Any]]:
    if end <= start:
        raise ValueError(f"Invalid section boundary: {start} to {end}")
    segments: list[dict[str, Any]] = []
    final_capture_index = end.capture_index if end.text_offset else end.capture_index - 1
    for index in range(start.capture_index, final_capture_index + 1):
        if index < 0 or index >= len(captures):
            continue
        capture = captures[index]
        text_start = start.text_offset if index == start.capture_index else 0
        text_end = end.text_offset if index == end.capture_index else len(capture.text)
        if text_start > text_end or text_end > len(capture.text):
            raise ValueError(f"Invalid offsets for capture {capture.capture_id}")
        excerpt = capture.text[text_start:text_end].strip()
        segments.append(
            {
                "capture_id": capture.capture_id,
                "transcript_ref": capture.transcript_ref,
                "label": capture.label,
                "text_start_char": text_start,
                "text_end_char": text_end,
                "source_text_sha256": sha256_text(excerpt),
                "text": excerpt,
                "capture_status": capture.status,
            }
        )
    return segments


def nest_markdown(text: str, *, parent_level: int = 3) -> str:
    def replace(match: re.Match[str]) -> str:
        return f"{'#' * min(6, parent_level + len(match.group(1)))} "

    return re.sub(r"(?m)^(#{1,6})\s+", replace, text)


def anchor_payload(anchor: Anchor | None, captures: list[Capture]) -> dict[str, Any] | None:
    if anchor is None:
        return None
    if anchor.capture_index == len(captures):
        return {"capture_id": None, "capture_index": anchor.capture_index, "text_offset": 0}
    capture = captures[anchor.capture_index]
    return {
        "capture_id": capture.capture_id,
        "capture_index": anchor.capture_index,
        "marker": capture.label,
        "text_offset": anchor.text_offset,
    }


def render_section(
    *,
    asin: str,
    entry: dict[str, Any],
    resolution: Resolution,
    start: Anchor | None,
    end: Anchor | None,
    segments: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Section {entry['seq']} — {entry['title']}",
        "",
        f"- **ASIN:** `{asin}`",
        f"- **Section type:** {entry['type']}",
        f"- **Mapped source range:** {entry['start_marker']}–{entry['end_marker']}",
        f"- **Mapped completeness:** {entry['completeness']}",
        f"- **Boundary resolution:** {resolution.method}",
        "",
        "> Derived transcript view. Canonical source remains `../book.md` and the referenced "
        "capture records.",
        "",
    ]
    if start is None or end is None:
        lines.extend(
            [
                "## Transcript Slice",
                "",
                "[No transcript text is available for this mapped section.]",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(["## Transcript Slice", ""])
    for segment in segments:
        lines.append(f"### {segment['label']}")
        lines.append(
            f"<!-- capture_id: {segment['capture_id']}; "
            f"text_chars: {segment['text_start_char']}-{segment['text_end_char']} -->"
        )
        lines.append("")
        text = segment["text"]
        lines.append(nest_markdown(text) if text else "[no text returned]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_section_outputs(
    *,
    asin: str,
    book_dir: Path,
    chapter_map_path: Path,
    overrides_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    transcripts_dir = book_dir / "transcripts"
    book_path = transcripts_dir / "book.md"
    captures_path = transcripts_dir / "captures.jsonl"
    if not book_path.exists():
        raise FileNotFoundError(f"Missing compiled transcript: {book_path}")
    if not chapter_map_path.exists():
        raise FileNotFoundError(f"Missing verified chapter map: {chapter_map_path}")

    entries = parse_chapter_map(chapter_map_path)
    overrides = load_boundary_overrides(overrides_path, entries)
    captures = load_capture_records(transcripts_dir)
    resolutions: list[Resolution] = []
    previous: Anchor | None = None
    for entry in entries:
        resolution = resolve_start(entry, captures, previous, overrides.get(entry["seq"]))
        resolutions.append(resolution)
        if resolution.anchor is not None:
            previous = resolution.anchor

    generated_at = utc_now_iso()
    width = max(2, len(str(max(entry["seq"] for entry in entries))))
    files: dict[str, str] = {}
    section_records: list[dict[str, Any]] = []
    warning_count = 0

    for index, (entry, resolution) in enumerate(zip(entries, resolutions, strict=True)):
        start = resolution.anchor
        end: Anchor | None = None
        segments: list[dict[str, Any]] = []
        if start is not None:
            next_start = next(
                (
                    candidate.anchor
                    for candidate in resolutions[index + 1 :]
                    if candidate.anchor is not None
                ),
                None,
            )
            end = next_start or resolve_final_end(entry, captures, start)
            segments = segment_captures(captures, start, end)

        file_name = f"{entry['seq']:0{width}d}-{slugify(str(entry['title']))}.md"
        relative_file = f"sections/{file_name}"
        content = render_section(
            asin=asin,
            entry=entry,
            resolution=resolution,
            start=start,
            end=end,
            segments=segments,
        )
        files[relative_file] = content
        warning_count += len(resolution.warnings)
        source_text = "\n".join(segment["text"] for segment in segments)
        section_records.append(
            {
                "seq": entry["seq"],
                "title": entry["title"],
                "type": entry["type"],
                "file": relative_file,
                "status": "available" if start is not None else "unavailable",
                "completeness": entry["completeness"],
                "mapped_start_marker": entry["start_marker"],
                "mapped_end_marker": entry["end_marker"],
                "resolution": {
                    "method": resolution.method,
                    "matched_text": resolution.matched_text,
                    "review_note": resolution.review_note,
                    "warnings": list(resolution.warnings),
                },
                "start": anchor_payload(start, captures),
                "end_exclusive": anchor_payload(end, captures),
                "capture_count": len(segments),
                "word_count": len(source_text.split()),
                "source_text_sha256": sha256_text(source_text),
                "file_sha256": sha256_text(content),
                "segments": [
                    {key: value for key, value in segment.items() if key != "text"}
                    for segment in segments
                ],
            }
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "asin": asin,
        "generated_at": generated_at,
        "source": {
            "offset_basis": {
                "content": "canonical final.text with outer whitespace stripped",
                "unit": "Unicode code points",
                "rendering": "OCR Markdown headings are nested below each capture heading",
            },
            "book_markdown": {
                "path": "book.md",
                "sha256": sha256_file(book_path),
                "size_bytes": book_path.stat().st_size,
            },
            "captures": {
                "path": "captures.jsonl",
                "sha256": sha256_file(captures_path),
                "count": len(captures),
            },
            "chapter_map": {
                "path": manifest_path(chapter_map_path, book_dir),
                "sha256": sha256_file(chapter_map_path),
            },
            "boundary_overrides": (
                {
                    "path": manifest_path(overrides_path, book_dir),
                    "sha256": sha256_file(overrides_path),
                }
                if overrides_path is not None and overrides_path.exists()
                else None
            ),
        },
        "counts": {
            "mapped_sections": len(entries),
            "available_sections": sum(item["status"] == "available" for item in section_records),
            "unavailable_sections": sum(
                item["status"] == "unavailable" for item in section_records
            ),
            "title_matched_sections": sum(
                item["resolution"]["method"] == "title_match" for item in section_records
            ),
            "marker_fallback_sections": sum(
                item["resolution"]["method"] == "marker_fallback" for item in section_records
            ),
            "overridden_sections": sum(
                item["resolution"]["method"].startswith("override_") for item in section_records
            ),
            "warnings": warning_count,
        },
        "sections": section_records,
    }
    return manifest, files


def validate_manifest(book_dir: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    transcripts_dir = book_dir / "transcripts"
    if not manifest_path.exists():
        return [f"Missing section manifest: {manifest_path}"]
    try:
        manifest = read_json(manifest_path)
    except Exception as exc:
        return [f"Invalid section manifest: {exc}"]
    if not isinstance(manifest, dict):
        return ["Section manifest is not an object"]
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported schema_version: {manifest.get('schema_version')}")
    if manifest.get("generator_version") != GENERATOR_VERSION:
        errors.append(f"Stale generator_version: {manifest.get('generator_version')}")

    source = manifest.get("source")
    if not isinstance(source, dict):
        return errors + ["Manifest source block is missing"]
    for key, base in (("book_markdown", transcripts_dir), ("captures", transcripts_dir)):
        record = source.get(key)
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            errors.append(f"Manifest source.{key} is invalid")
            continue
        path = base / record["path"]
        if not path.exists():
            errors.append(f"Missing source file: {path}")
        elif sha256_file(path) != record.get("sha256"):
            errors.append(f"Stale source hash: {path}")
    chapter_map = source.get("chapter_map")
    if not isinstance(chapter_map, dict) or not isinstance(chapter_map.get("path"), str):
        errors.append("Manifest source.chapter_map is invalid")
    else:
        path = book_dir / chapter_map["path"]
        if not path.exists():
            errors.append(f"Missing chapter map: {path}")
        elif sha256_file(path) != chapter_map.get("sha256"):
            errors.append(f"Stale chapter-map hash: {path}")
    boundary_overrides = source.get("boundary_overrides")
    if boundary_overrides is not None:
        if not isinstance(boundary_overrides, dict) or not isinstance(
            boundary_overrides.get("path"), str
        ):
            errors.append("Manifest source.boundary_overrides is invalid")
        else:
            path = book_dir / boundary_overrides["path"]
            if not path.exists():
                errors.append(f"Missing boundary overrides: {path}")
            elif sha256_file(path) != boundary_overrides.get("sha256"):
                errors.append(f"Stale boundary-overrides hash: {path}")

    sections = manifest.get("sections")
    if not isinstance(sections, list):
        return errors + ["Manifest sections must be a list"]
    sequences: list[int] = []
    for section in sections:
        if not isinstance(section, dict):
            errors.append("Manifest contains a non-object section")
            continue
        seq = parse_int(section.get("seq"))
        if seq is not None:
            sequences.append(seq)
        relative = section.get("file")
        if not isinstance(relative, str):
            errors.append(f"Section {seq} has no file")
            continue
        path = transcripts_dir / relative
        if not path.exists():
            errors.append(f"Missing section file: {path}")
        elif sha256_file(path) != section.get("file_sha256"):
            errors.append(f"Modified or stale section file: {path}")
        segments = section.get("segments")
        if not isinstance(segments, list):
            errors.append(f"Section {seq} has invalid segments")
            continue
        source_parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                errors.append(f"Section {seq} contains a non-object segment")
                continue
            transcript_ref = segment.get("transcript_ref")
            text_start = parse_int(segment.get("text_start_char"))
            text_end = parse_int(segment.get("text_end_char"))
            if not isinstance(transcript_ref, str) or text_start is None or text_end is None:
                errors.append(f"Section {seq} contains an invalid segment boundary")
                continue
            canonical_path = transcripts_dir / transcript_ref
            if not canonical_path.exists():
                errors.append(f"Section {seq} references a missing capture: {canonical_path}")
                continue
            canonical = read_json(canonical_path)
            final = canonical.get("final") if isinstance(canonical, dict) else None
            canonical_text = final.get("text") if isinstance(final, dict) else ""
            if not isinstance(canonical_text, str):
                canonical_text = ""
            canonical_text = canonical_text.strip()
            if text_start < 0 or text_end < text_start or text_end > len(canonical_text):
                errors.append(
                    f"Section {seq} has out-of-range offsets for {segment.get('capture_id')}"
                )
                continue
            excerpt = canonical_text[text_start:text_end].strip()
            source_parts.append(excerpt)
            if sha256_text(excerpt) != segment.get("source_text_sha256"):
                errors.append(
                    f"Section {seq} has stale source text for {segment.get('capture_id')}"
                )
        if sha256_text("\n".join(source_parts)) != section.get("source_text_sha256"):
            errors.append(f"Section {seq} has a stale aggregate source hash")
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        errors.append("Section sequences are not unique and increasing")
    return errors


def clean_stale_files(
    transcripts_dir: Path, previous_manifest: dict[str, Any] | None, desired: set[str]
) -> None:
    if not isinstance(previous_manifest, dict):
        return
    sections = previous_manifest.get("sections")
    if not isinstance(sections, list):
        return
    output_dir = (transcripts_dir / "sections").resolve()
    for section in sections:
        relative = section.get("file") if isinstance(section, dict) else None
        if not isinstance(relative, str) or relative in desired:
            continue
        candidate = (transcripts_dir / relative).resolve()
        if candidate.parent == output_dir and candidate.suffix == ".md" and candidate.exists():
            candidate.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asin", required=True, help="Book ASIN under books/<asin>")
    parser.add_argument(
        "--map",
        dest="chapter_map",
        type=Path,
        help="Verified chapter-map Markdown path (default: books/<asin>/analysis/chapter-map.md)",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        help=(
            "Reviewed boundary overrides (default: "
            "books/<asin>/analysis/section-boundaries.json when present)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve boundaries without writing")
    parser.add_argument("--check", action="store_true", help="Validate existing derived sections")
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Exit nonzero when any boundary uses a marker fallback",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.asin):
        print(f"ERROR: invalid ASIN/path component: {args.asin!r}")
        return 1
    book_dir = Path.cwd() / "books" / args.asin
    transcripts_dir = book_dir / "transcripts"
    chapter_map_path = args.chapter_map or (book_dir / "analysis" / "chapter-map.md")
    if not chapter_map_path.is_absolute():
        chapter_map_path = (Path.cwd() / chapter_map_path).resolve()
    overrides_path = args.overrides or (book_dir / "analysis" / "section-boundaries.json")
    if not overrides_path.is_absolute():
        overrides_path = (Path.cwd() / overrides_path).resolve()
    if not overrides_path.exists():
        overrides_path = None
    manifest_path = transcripts_dir / "sections.json"

    if args.check:
        errors = validate_manifest(book_dir, manifest_path)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        manifest = read_json(manifest_path)
        counts = manifest.get("counts", {})
        print(
            f"Sections valid for {args.asin}: {counts.get('mapped_sections')} mapped, "
            f"{counts.get('available_sections')} available, {counts.get('warnings')} warnings"
        )
        return 0

    try:
        manifest, files = build_section_outputs(
            asin=args.asin,
            book_dir=book_dir,
            chapter_map_path=chapter_map_path,
            overrides_path=overrides_path,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1

    counts = manifest["counts"]
    print(
        f"Resolved {counts['mapped_sections']} sections for {args.asin}: "
        f"{counts['title_matched_sections']} title matches, "
        f"{counts['overridden_sections']} reviewed overrides, "
        f"{counts['marker_fallback_sections']} marker fallbacks, "
        f"{counts['unavailable_sections']} unavailable"
    )
    for section in manifest["sections"]:
        for warning in section["resolution"]["warnings"]:
            print(f"WARNING: section {section['seq']} ({section['title']}): {warning}")
    if args.fail_on_warnings and counts["warnings"]:
        print("ERROR: boundary warnings found and --fail-on-warnings was requested")
        return 1
    if args.dry_run:
        return 0

    previous_manifest = read_json(manifest_path) if manifest_path.exists() else None
    for relative, content in files.items():
        write_text(transcripts_dir / relative, content)
    clean_stale_files(transcripts_dir, previous_manifest, set(files))
    write_json(manifest_path, manifest)

    errors = validate_manifest(book_dir, manifest_path)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Wrote {len(files)} section files and {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
