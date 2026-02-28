"""
Transcribe Kindle page screenshots using OpenAI vision models.

This script reads images from books/<asin>/pages, performs OCR with a quality-first
2-pass pipeline (OCR + QA), deduplicates repeated images by SHA256, and writes
structured outputs plus a compiled markdown transcript.

Usage:
    python scripts/transcribe.py --asin B00FO74WXA
    python scripts/transcribe.py --asin B00FO74WXA --start-at 10 --max-pages 25
    python scripts/transcribe.py --asin B00FO74WXA --dry-run
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_MODEL = "gpt-5"
DEFAULT_QA_MODEL = "gpt-5"
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 4000

OCR_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "confidence", "uncertainties", "normalization_notes"],
    "properties": {
        "text": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "uncertainties": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["snippet", "reason"],
                "properties": {
                    "snippet": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "normalization_notes": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

PASS1_INSTRUCTIONS = """You are a high-precision OCR system for ebook page images.
Return ONLY JSON that matches the provided schema.

Requirements:
- Extract visible text in natural reading order.
- Preserve punctuation, capitalization, and paragraph boundaries.
- Balanced normalization mode:
  - Fix obvious line-wrap artifacts and soft-hyphen word splits.
  - Keep heading and paragraph structure intact.
  - Do not paraphrase or summarize.
- If text is uncertain, keep your best guess in text and mark as [unclear: ...] where needed.
- Add uncertain segments to uncertainties with short reasons.
- Confidence must be between 0 and 1.
"""

PASS2_INSTRUCTIONS = """You are an OCR quality-assurance reviewer.
Return ONLY JSON that matches the provided schema.

You will receive:
1) The page image.
2) A draft OCR text.

Your job:
- Verify the draft against the image and correct OCR mistakes.
- Keep balanced normalization (light cleanup, no paraphrase).
- Preserve content fidelity and structure.
- Keep uncertainty markers when needed and provide uncertainties list.
- Confidence must reflect final text reliability for this page.
"""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "book"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")


def clamp_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return 0.0
    if parsed < 0:
        return 0.0
    if parsed > 1:
        return 1.0
    return parsed


def validate_ocr_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("OCR result is not an object")

    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("OCR result missing string field: text")

    confidence = clamp_confidence(payload.get("confidence"))

    uncertainties_raw = payload.get("uncertainties")
    uncertainties: list[dict[str, str]] = []
    if isinstance(uncertainties_raw, list):
        for item in uncertainties_raw:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet")
            reason = item.get("reason")
            if isinstance(snippet, str) and isinstance(reason, str):
                uncertainties.append({"snippet": snippet, "reason": reason})

    notes_raw = payload.get("normalization_notes")
    notes: list[str] = []
    if isinstance(notes_raw, list):
        for item in notes_raw:
            if isinstance(item, str):
                notes.append(item)

    return {
        "text": text,
        "confidence": confidence,
        "uncertainties": uncertainties,
        "normalization_notes": notes,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def encode_image_data_url(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def parse_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return None
    return parsed


def parse_capture_metadata_from_filename(name: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "page": None,
        "total": None,
        "location": None,
        "total_location": None,
    }

    page_match = re.search(r"^page-(\d+)-of-(\d+)\.png$", name)
    if page_match:
        metadata["page"] = parse_int(page_match.group(1))
        metadata["total"] = parse_int(page_match.group(2))
        return metadata

    location_match = re.search(r"^loc-(\d+)-of-(\d+)\.png$", name)
    if location_match:
        metadata["location"] = parse_int(location_match.group(1))
        metadata["total_location"] = parse_int(location_match.group(2))

    return metadata


def format_capture_label(capture: dict[str, Any]) -> str:
    page = capture.get("page")
    total = capture.get("total")
    location = capture.get("location")
    total_location = capture.get("total_location")

    if isinstance(page, int) and isinstance(total, int):
        return f"Page {page} of {total}"
    if isinstance(location, int) and isinstance(total_location, int):
        return f"Location {location} of {total_location}"
    if isinstance(page, int):
        return f"Page {page}"
    if isinstance(location, int):
        return f"Location {location}"
    return "Capture"


def to_plain_object(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, list):
        return [to_plain_object(item) for item in value]

    if isinstance(value, dict):
        return {k: to_plain_object(v) for k, v in value.items()}

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except TypeError:
            return model_dump(mode="json")

    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        return dict_method()

    return str(value)


def extract_response_json_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    payload = to_plain_object(response)

    if isinstance(payload, dict):
        candidate = payload.get("output_text")
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    def visit(node: Any) -> str | None:
        if isinstance(node, dict):
            node_type = node.get("type")
            text = node.get("text")
            if node_type == "output_text" and isinstance(text, str) and text.strip():
                return text
            for value in node.values():
                found = visit(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = visit(item)
                if found:
                    return found
        return None

    found_text = visit(payload)
    if found_text:
        return found_text

    raise ValueError("Could not find text output in model response")


def parse_json_payload(text: str) -> Any:
    normalized = text.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s*```$", "", normalized)
    return json.loads(normalized)


class OpenAIResponsesOCR:
    def __init__(self, timeout_seconds: int = 120) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required. Install with: pip install openai"
            ) from exc

        try:
            self.client = OpenAI(timeout=timeout_seconds)
        except TypeError:
            # Older SDKs may not accept timeout in the constructor.
            self.client = OpenAI()

        if not hasattr(self.client, "responses"):
            raise RuntimeError(
                "Installed openai SDK does not expose Responses API. "
                "Upgrade with: pip install --upgrade openai"
            )

    def _call_json(
        self,
        *,
        model: str,
        instructions: str,
        prompt: str,
        image_data_url: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        response = self.client.responses.create(
            model=model,
            instructions=instructions,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": "high",
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "ocr_page",
                    "strict": True,
                    "schema": OCR_OUTPUT_SCHEMA,
                }
            },
            max_output_tokens=max_output_tokens,
        )

        raw_text = extract_response_json_text(response)
        payload = parse_json_payload(raw_text)
        return validate_ocr_result(payload)

    def pass1(
        self,
        *,
        model: str,
        image_data_url: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        prompt = (
            "Perform OCR on this Kindle page screenshot. "
            "Use balanced normalization and uncertainty markers as instructed."
        )
        return self._call_json(
            model=model,
            instructions=PASS1_INSTRUCTIONS,
            prompt=prompt,
            image_data_url=image_data_url,
            max_output_tokens=max_output_tokens,
        )

    def pass2(
        self,
        *,
        model: str,
        image_data_url: str,
        draft_text: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        prompt = (
            "Correct this draft OCR text against the image and return final OCR JSON.\n\n"
            "Draft OCR:\n"
            f"{draft_text}"
        )
        return self._call_json(
            model=model,
            instructions=PASS2_INSTRUCTIONS,
            prompt=prompt,
            image_data_url=image_data_url,
            max_output_tokens=max_output_tokens,
        )


def retry_call(label: str, max_retries: int, fn):
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn(), attempts
        except Exception as exc:
            if attempts >= max_retries:
                raise RuntimeError(f"{label} failed after {attempts} attempts: {exc}") from exc
            delay = (2 ** (attempts - 1)) + random.random() * 0.5
            print(
                f"Warning: {label} attempt {attempts}/{max_retries} failed: {exc}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)


def load_captures(book_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pages_manifest_path = book_dir / "pages.json"
    pages_dir = book_dir / "pages"

    if pages_manifest_path.exists():
        try:
            payload = read_json(pages_manifest_path)
        except Exception:
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("pages"), list):
            captures: list[dict[str, Any]] = []
            for idx, item in enumerate(payload["pages"]):
                if not isinstance(item, dict):
                    continue
                parsed_index = parse_int(item.get("index"))
                rel_path = item.get("path")
                file_name = item.get("file")

                if isinstance(rel_path, str) and rel_path.strip():
                    image_path = (book_dir / rel_path).resolve()
                    normalized_rel = str(Path(rel_path).as_posix())
                elif isinstance(file_name, str) and file_name.strip():
                    image_path = (pages_dir / file_name).resolve()
                    normalized_rel = f"pages/{file_name}"
                else:
                    continue

                if not image_path.exists() or image_path.suffix.lower() != ".png":
                    continue

                captures.append(
                    {
                        "index": parsed_index if parsed_index is not None else idx,
                        "file": image_path.name,
                        "path": normalized_rel,
                        "source_path": image_path,
                        "page": parse_int(item.get("page")),
                        "total": parse_int(item.get("total")),
                        "location": parse_int(item.get("location")),
                        "total_location": parse_int(item.get("total_location")),
                    }
                )

            if captures:
                return captures, {
                    "kind": "pages_manifest",
                    "path": "pages.json",
                    "capture_count": len(captures),
                }

    captures = []
    for idx, image_path in enumerate(sorted(pages_dir.glob("*.png"))):
        metadata = parse_capture_metadata_from_filename(image_path.name)
        captures.append(
            {
                "index": idx,
                "file": image_path.name,
                "path": f"pages/{image_path.name}",
                "source_path": image_path.resolve(),
                "page": metadata.get("page"),
                "total": metadata.get("total"),
                "location": metadata.get("location"),
                "total_location": metadata.get("total_location"),
            }
        )

    return captures, {
        "kind": "glob",
        "path": "pages/*.png",
        "capture_count": len(captures),
    }


def load_toc_entries(book_dir: Path) -> list[dict[str, Any]]:
    toc_path = book_dir / "toc.json"
    if not toc_path.exists():
        return []
    try:
        payload = read_json(toc_path)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return []

    entries: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        page = parse_int(item.get("page"))
        location = parse_int(item.get("location"))
        entries.append(
            {
                "title": " ".join(title.split()),
                "page": page,
                "location": location,
                "index": parse_int(item.get("index")) or len(entries),
            }
        )

    return entries


def infer_toc_title(capture: dict[str, Any], toc_entries: list[dict[str, Any]]) -> str | None:
    if not toc_entries:
        return None

    page = capture.get("page")
    location = capture.get("location")

    best: tuple[int, int, str] | None = None
    for entry in toc_entries:
        title = entry.get("title")
        if not isinstance(title, str):
            continue
        marker = None
        if isinstance(page, int) and isinstance(entry.get("page"), int):
            marker = entry["page"]
        elif isinstance(location, int) and isinstance(entry.get("location"), int):
            marker = entry["location"]

        if not isinstance(marker, int):
            continue

        target = page if isinstance(page, int) else location
        if not isinstance(target, int):
            continue

        if marker > target:
            continue

        distance = target - marker
        entry_index = entry.get("index") if isinstance(entry.get("index"), int) else 10**9
        candidate = (distance, entry_index, title)
        if best is None or candidate < best:
            best = candidate

    return best[2] if best else None


def build_markdown_transcript(
    *,
    asin: str,
    book_dir: Path,
    capture_records: list[dict[str, Any]],
    canonical_results: dict[str, dict[str, Any]],
) -> str:
    metadata_path = book_dir / "metadata.json"
    title = None
    authors: list[str] = []

    if metadata_path.exists():
        try:
            payload = read_json(metadata_path)
        except Exception:
            payload = None

        if isinstance(payload, dict):
            raw_title = payload.get("title")
            raw_authors = payload.get("authors")
            if isinstance(raw_title, str) and raw_title.strip():
                title = raw_title.strip()
            if isinstance(raw_authors, list):
                authors = [x.strip() for x in raw_authors if isinstance(x, str) and x.strip()]

    toc_entries = load_toc_entries(book_dir)

    heading_title = title or f"Transcript for {asin}"
    lines = [f"# {heading_title}", "", f"- ASIN: `{asin}`"]
    if authors:
        lines.append(f"- Authors: {', '.join(authors)}")
    lines.append(f"- Generated: {utc_now_iso()}")
    lines.append("")

    previous_toc_title: str | None = None

    for capture in capture_records:
        toc_title = infer_toc_title(capture, toc_entries)
        if toc_title and toc_title != previous_toc_title:
            lines.append(f"## {toc_title}")
            lines.append("")
            previous_toc_title = toc_title

        lines.append(f"### {format_capture_label(capture)}")
        lines.append("")

        canonical_id = capture.get("canonical_id")
        canonical = canonical_results.get(str(canonical_id), {})
        status = canonical.get("status")

        if status == "completed":
            final_payload = canonical.get("final")
            text = ""
            if isinstance(final_payload, dict):
                text_candidate = final_payload.get("text")
                if isinstance(text_candidate, str):
                    text = text_candidate.strip()
            lines.append(text if text else "[no text returned]")
        else:
            error = canonical.get("error")
            if isinstance(error, dict):
                message = error.get("message")
            else:
                message = None
            if not isinstance(message, str) or not message.strip():
                message = "OCR failed for this page."
            lines.append(f"[transcription error] {message}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def load_existing_manifest(manifest_path: Path) -> dict[str, Any] | None:
    if not manifest_path.exists():
        return None
    try:
        payload = read_json(manifest_path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Transcribe captured Kindle pages via OpenAI OCR")
    parser.add_argument("--asin", required=True, help="Book ASIN (maps to books/<asin>)")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"OCR model (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--qa-model",
        default=DEFAULT_QA_MODEL,
        help=f"QA model for pass 2 (default: {DEFAULT_QA_MODEL})",
    )
    parser.add_argument(
        "--start-at",
        type=int,
        default=0,
        help="Start capture index in ordered input list (default: 0)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Max captures to process after start index (0 = all)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run OCR even when canonical outputs already exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned workload without API calls or file writes",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Retries per pass on failure (default: {DEFAULT_MAX_RETRIES})",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help=f"Maximum output tokens per API call (default: {DEFAULT_MAX_OUTPUT_TOKENS})",
    )
    args = parser.parse_args()

    if args.start_at < 0:
        parser.error("--start-at must be >= 0")
    if args.max_pages < 0:
        parser.error("--max-pages must be >= 0")
    if args.max_retries < 1:
        parser.error("--max-retries must be >= 1")
    if args.max_output_tokens < 256:
        parser.error("--max-output-tokens must be >= 256")

    asin_slug = sanitize_slug(args.asin)
    book_dir = Path.cwd() / "books" / asin_slug
    pages_dir = book_dir / "pages"
    transcripts_dir = book_dir / "transcripts"
    canonical_dir = transcripts_dir / "canonical"
    manifest_path = transcripts_dir / "manifest.json"
    captures_jsonl_path = transcripts_dir / "captures.jsonl"
    book_markdown_path = transcripts_dir / "book.md"

    if not book_dir.exists():
        print(f"Error: book directory not found: {book_dir}")
        return 1
    if not pages_dir.exists():
        print(f"Error: pages directory not found: {pages_dir}")
        return 1

    captures, source_info = load_captures(book_dir)
    if not captures:
        print("Error: no PNG captures found to transcribe.")
        return 1

    start_index = args.start_at
    end_index = (
        len(captures) if args.max_pages == 0 else min(len(captures), start_index + args.max_pages)
    )

    if start_index >= len(captures):
        print(f"Error: --start-at {start_index} is out of range for {len(captures)} captures.")
        return 1

    selected_captures = captures[start_index:end_index]
    if not selected_captures:
        print("Error: selection produced zero captures.")
        return 1

    for capture in selected_captures:
        capture["sha256"] = file_sha256(capture["source_path"])
        capture["canonical_id"] = f"img-{capture['sha256']}"

    groups: dict[str, dict[str, Any]] = {}
    for capture in selected_captures:
        canonical_id = capture["canonical_id"]
        entry = groups.setdefault(
            canonical_id,
            {
                "canonical_id": canonical_id,
                "sha256": capture["sha256"],
                "representative_path": capture["source_path"],
                "captures": [],
            },
        )
        entry["captures"].append(capture)

    print(
        f"Selected captures: {len(selected_captures)} | "
        f"Unique images: {len(groups)} | Source: {source_info['kind']}"
    )

    if args.dry_run:
        print("Dry run only. No API calls or file writes.")
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY is not set.")
        return 1

    transcripts_dir.mkdir(parents=True, exist_ok=True)
    canonical_dir.mkdir(parents=True, exist_ok=True)

    try:
        ocr_client = OpenAIResponsesOCR()
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    canonical_results: dict[str, dict[str, Any]] = {}
    completed_unique = 0
    failed_unique = 0
    resumed_unique = 0

    for canonical_id, group in groups.items():
        canonical_path = canonical_dir / f"{canonical_id}.json"
        existing_payload: dict[str, Any] | None = None
        if canonical_path.exists():
            try:
                payload = read_json(canonical_path)
                if isinstance(payload, dict):
                    existing_payload = payload
            except Exception:
                existing_payload = None

        if (
            not args.force
            and isinstance(existing_payload, dict)
            and existing_payload.get("status") == "completed"
            and isinstance(existing_payload.get("final"), dict)
            and isinstance(existing_payload["final"].get("text"), str)
        ):
            canonical_results[canonical_id] = existing_payload
            resumed_unique += 1
            completed_unique += 1
            print(f"[{canonical_id}] reused existing completed transcript")
            continue

        image_path = group["representative_path"]
        try:
            image_rel = image_path.relative_to(book_dir).as_posix()
        except Exception:
            image_rel = str(image_path)
        image_data_url = encode_image_data_url(image_path)

        print(f"[{canonical_id}] OCR pass on {image_rel}")

        created_at = (
            existing_payload.get("created_at")
            if isinstance(existing_payload, dict)
            else utc_now_iso()
        )

        result_payload: dict[str, Any] = {
            "canonical_id": canonical_id,
            "image_sha256": group["sha256"],
            "representative_image": image_rel,
            "captures": [
                {
                    "index": c.get("index"),
                    "path": c.get("path"),
                    "page": c.get("page"),
                    "total": c.get("total"),
                    "location": c.get("location"),
                    "total_location": c.get("total_location"),
                }
                for c in group["captures"]
            ],
            "created_at": created_at,
            "updated_at": utc_now_iso(),
            "status": "error",
            "pass1": None,
            "pass2": None,
            "final": None,
            "error": None,
        }

        try:
            start_pass1 = time.time()
            pass1_result, pass1_attempts = retry_call(
                f"{canonical_id} pass1",
                args.max_retries,
                lambda: ocr_client.pass1(
                    model=args.model,
                    image_data_url=image_data_url,
                    max_output_tokens=args.max_output_tokens,
                ),
            )
            pass1_duration_ms = int((time.time() - start_pass1) * 1000)

            start_pass2 = time.time()
            pass2_result, pass2_attempts = retry_call(
                f"{canonical_id} pass2",
                args.max_retries,
                lambda: ocr_client.pass2(
                    model=args.qa_model,
                    image_data_url=image_data_url,
                    draft_text=pass1_result["text"],
                    max_output_tokens=args.max_output_tokens,
                ),
            )
            pass2_duration_ms = int((time.time() - start_pass2) * 1000)

            result_payload["pass1"] = {
                "model": args.model,
                "attempts": pass1_attempts,
                "duration_ms": pass1_duration_ms,
                "result": pass1_result,
            }
            result_payload["pass2"] = {
                "model": args.qa_model,
                "attempts": pass2_attempts,
                "duration_ms": pass2_duration_ms,
                "result": pass2_result,
            }
            result_payload["final"] = pass2_result
            result_payload["status"] = "completed"
            result_payload["updated_at"] = utc_now_iso()

            write_json(canonical_path, result_payload)
            canonical_results[canonical_id] = result_payload
            completed_unique += 1
            print(
                f"[{canonical_id}] completed "
                f"(confidence={pass2_result['confidence']:.2f}, "
                f"uncertainties={len(pass2_result['uncertainties'])})"
            )
        except Exception as exc:
            result_payload["error"] = {
                "message": str(exc),
                "failed_at": utc_now_iso(),
            }
            result_payload["updated_at"] = utc_now_iso()
            write_json(canonical_path, result_payload)
            canonical_results[canonical_id] = result_payload
            failed_unique += 1
            print(f"[{canonical_id}] failed: {exc}")

    capture_records: list[dict[str, Any]] = []
    for capture in selected_captures:
        canonical_id = capture["canonical_id"]
        canonical_result = canonical_results.get(canonical_id, {})

        final = canonical_result.get("final")
        if isinstance(final, dict):
            confidence = clamp_confidence(final.get("confidence"))
            uncertainties = final.get("uncertainties")
            uncertainty_count = len(uncertainties) if isinstance(uncertainties, list) else 0
        else:
            confidence = None
            uncertainty_count = None

        capture_records.append(
            {
                "index": capture.get("index"),
                "file": capture.get("file"),
                "path": capture.get("path"),
                "page": capture.get("page"),
                "total": capture.get("total"),
                "location": capture.get("location"),
                "total_location": capture.get("total_location"),
                "sha256": capture.get("sha256"),
                "canonical_id": canonical_id,
                "transcript_ref": f"canonical/{canonical_id}.json",
                "status": canonical_result.get("status"),
                "confidence": confidence,
                "uncertainty_count": uncertainty_count,
            }
        )

    markdown = build_markdown_transcript(
        asin=args.asin,
        book_dir=book_dir,
        capture_records=capture_records,
        canonical_results=canonical_results,
    )
    book_markdown_path.write_text(markdown, encoding="utf-8")
    write_jsonl(captures_jsonl_path, capture_records)

    existing_manifest = load_existing_manifest(manifest_path)
    created_at = (
        existing_manifest.get("created_at")
        if isinstance(existing_manifest, dict)
        and isinstance(existing_manifest.get("created_at"), str)
        else utc_now_iso()
    )

    successful_unique = completed_unique
    total_unique = len(groups)
    failure_ratio = (failed_unique / total_unique) if total_unique else 0.0

    status = "completed" if failed_unique == 0 else "partial"
    if successful_unique == 0:
        status = "failed"

    manifest_payload = {
        "asin": args.asin,
        "created_at": created_at,
        "updated_at": utc_now_iso(),
        "status": status,
        "source": source_info,
        "models": {
            "ocr": args.model,
            "qa": args.qa_model,
        },
        "options": {
            "start_at": args.start_at,
            "max_pages": args.max_pages,
            "force": args.force,
            "max_retries": args.max_retries,
            "max_output_tokens": args.max_output_tokens,
        },
        "counts": {
            "selected_captures": len(selected_captures),
            "unique_images": total_unique,
            "completed_unique_images": completed_unique,
            "failed_unique_images": failed_unique,
            "resumed_unique_images": resumed_unique,
            "failure_ratio": round(failure_ratio, 6),
        },
        "files": {
            "captures": "captures.jsonl",
            "canonical_dir": "canonical",
            "book_markdown": "book.md",
        },
    }
    write_json(manifest_path, manifest_payload)

    print(f"Wrote transcript outputs: {book_markdown_path}, {captures_jsonl_path}, {manifest_path}")

    if successful_unique == 0:
        print("Error: no pages were transcribed successfully.")
        return 1

    if failure_ratio > 0.10:
        print(
            f"Warning: failure ratio is {failure_ratio:.1%}, which exceeds 10%. Exiting non-zero."
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
