"""Transcribe Kindle page screenshots using Gemini vision OCR.

This script reads images from books/<asin>/pages, performs one high-fidelity OCR pass,
and writes auditable structured outputs plus a compiled Markdown transcript.

Usage:
    python scripts/transcribe.py --asin B00FO74WXA
    python scripts/transcribe.py --asin B00FO74WXA --start-at 10 --max-pages 25
    python scripts/transcribe.py --asin B00FO74WXA --concurrency 2
    python scripts/transcribe.py --asin B00FO74WXA --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_IMAGE_DETAIL = "high"
DEFAULT_THINKING_LEVEL = "minimal"
DEFAULT_FALLBACK_MODEL = "gpt-5.6-luna"
DEFAULT_FALLBACK_IMAGE_DETAIL = "high"
DEFAULT_FALLBACK_REASONING_EFFORT = "none"
PROMPT_VERSION = "literal-v2"
OUTPUT_NORMALIZATION_VERSION = "ascii-ordinals-v1"
QUALITY_CHECK_VERSION = "v1"
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_CONCURRENCY = 2

MODEL_PRICING_USD_PER_MILLION = {
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00, "batch_multiplier": 0.5},
    "gemini-3.1-flash-lite": {
        "input": 0.25,
        "output": 1.50,
        "batch_multiplier": 0.5,
    },
}

FALLBACK_MODEL_PRICING_USD_PER_MILLION = {
    "gpt-5.6-luna": {"input": 1.00, "output": 6.00, "batch_multiplier": 0.5},
}

_THREAD_LOCAL = threading.local()

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

OCR_INSTRUCTIONS = """You are a high-precision OCR system for ebook screenshots.
Return only JSON matching the supplied schema.

Transcribe every visible content character in natural reading order.
- Preserve punctuation, capitalization, paragraph boundaries, headings, list numbers,
  dialogue, and meaningful line or section breaks.
- Preserve visible italics using Markdown *italics* and bold using **bold** when the
  styling conveys structure or emphasis.
- Represent diagrams in concise Markdown reading order, transcribing every visible
  label or passage exactly once. Never duplicate text while translating the layout.
- Represent blank workbook answer lines concisely; do not invent text for them.
- Remove only obvious display line-wrap artifacts and soft-hyphen word splits.
- Do not paraphrase, summarize, complete clipped sentences, or add commentary.
- Do not include the Kindle application chrome when it is present.
- Put a best-effort reading in text and record genuinely uncertain segments in the
  uncertainties array. Confidence is an overall 0-to-1 transcription confidence.

Literal-copy requirements:
- Reproduce what is visibly printed even when a word, spelling, acronym, grammar choice,
  or punctuation mark seems mistaken or semantically unlikely. Never silently correct it.
- Do not add punctuation, words, or quote marks that are not visible at a cropped page edge.
- When two readings seem possible, choose the visible letter shapes over the more likely phrase
  and record the ambiguity in uncertainties.
- Render superscript ordinal suffixes with ordinary ASCII letters, such as 1st and 2nd.
"""

OCR_PROMPT = "Transcribe this Kindle page screenshot exactly according to the OCR rules."


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "book"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")
    temporary.replace(path)


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


def normalize_ordinal_suffixes(text: str) -> str:
    """Render superscript and known malformed ordinal glyphs as portable ASCII."""

    replacements = {
        "1ၲဵ": "1st",
        "2ၲ၁": "2nd",
        "1ၑ႓": "1st",
        "2ⁿ၁": "2nd",
        "ˢᵗ": "st",
        "ⁿᵈ": "nd",
        "ʳᵈ": "rd",
        "ᵗʰ": "th",
    }
    normalized = text
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def build_quality_checks(result: dict[str, Any]) -> dict[str, Any]:
    flags: list[dict[str, Any]] = []
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    confidence = clamp_confidence(result.get("confidence"))
    uncertainties = (
        result.get("uncertainties") if isinstance(result.get("uncertainties"), list) else []
    )

    if not text.strip():
        flags.append({"type": "empty_text", "severity": "error"})
    if confidence < 0.99:
        flags.append(
            {
                "type": "low_confidence",
                "severity": "review",
                "confidence": confidence,
            }
        )
    if uncertainties:
        flags.append(
            {
                "type": "model_uncertainties",
                "severity": "review",
                "count": len(uncertainties),
            }
        )

    normalized_lines: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        normalized = re.sub(r"[\s*_#>`|]+", " ", raw_line).strip().lower()
        if len(normalized) >= 20:
            normalized_lines.append((normalized, raw_line.strip()))
    counts = Counter(normalized for normalized, _ in normalized_lines)
    reported_duplicates: set[str] = set()
    for normalized, original in normalized_lines:
        if counts[normalized] > 1 and normalized not in reported_duplicates:
            flags.append(
                {
                    "type": "duplicate_line",
                    "severity": "review",
                    "count": counts[normalized],
                    "snippet": original[:200],
                }
            )
            reported_duplicates.add(normalized)

    suspicious_characters = []
    for character in text:
        name = unicodedata.name(character, "")
        unexpected_control = (
            unicodedata.category(character).startswith("C") and character not in "\n\t"
        )
        if "MYANMAR" in name or character == "\ufffd" or unexpected_control:
            suspicious_characters.append(
                {
                    "character": character,
                    "codepoint": f"U+{ord(character):04X}",
                    "name": name,
                }
            )
    if suspicious_characters:
        flags.append(
            {
                "type": "suspicious_unicode",
                "severity": "error",
                "characters": suspicious_characters[:10],
            }
        )

    return {"version": QUALITY_CHECK_VERSION, "flags": flags}


def validate_ocr_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("OCR result is not an object")

    raw_text = payload.get("text")
    if not isinstance(raw_text, str):
        raise ValueError("OCR result missing string field: text")
    text = normalize_ordinal_suffixes(raw_text)

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
                uncertainties.append(
                    {"snippet": normalize_ordinal_suffixes(snippet), "reason": reason}
                )

    notes_raw = payload.get("normalization_notes")
    notes: list[str] = []
    if isinstance(notes_raw, list):
        for item in notes_raw:
            if isinstance(item, str):
                notes.append(item)
    if text != raw_text:
        note = "Normalized superscript ordinal suffixes to portable ASCII."
        if note not in notes:
            notes.append(note)

    return {
        "text": text,
        "confidence": confidence,
        "uncertainties": uncertainties,
        "normalization_notes": notes,
    }


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
        "variant_index": 0,
    }

    page_match = re.search(r"^page-(\d+)-of-(\d+)(?:[.-]v(\d+))?\.png$", name)
    if page_match:
        metadata["page"] = parse_int(page_match.group(1))
        metadata["total"] = parse_int(page_match.group(2))
        metadata["variant_index"] = parse_int(page_match.group(3)) or 0
        return metadata

    location_match = re.search(r"^loc-(\d+)-of-(\d+)(?:[.-]v(\d+))?\.png$", name)
    if location_match:
        metadata["location"] = parse_int(location_match.group(1))
        metadata["total_location"] = parse_int(location_match.group(2))
        metadata["variant_index"] = parse_int(location_match.group(3)) or 0

    return metadata


def build_capture_id(capture: dict[str, Any], *, fallback_index: int) -> str:
    raw_path = capture.get("path")
    raw_file = capture.get("file")

    if isinstance(raw_path, str) and raw_path.strip():
        base = Path(raw_path).stem
    elif isinstance(raw_file, str) and raw_file.strip():
        base = Path(raw_file).stem
    else:
        base = f"capture-{fallback_index:05d}"

    return sanitize_slug(base)


def build_source_image_fingerprint(image_path: Path, image_rel: str) -> dict[str, Any]:
    stat = image_path.stat()
    return {
        "path": image_rel,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def source_image_fingerprint_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    source_image = payload.get("source_image")
    if not isinstance(source_image, dict):
        return False

    path = source_image.get("path")
    size_bytes = parse_int(source_image.get("size_bytes"))
    mtime_ns = parse_int(source_image.get("mtime_ns"))

    return (
        isinstance(path, str)
        and path == expected["path"]
        and isinstance(size_bytes, int)
        and size_bytes == expected["size_bytes"]
        and isinstance(mtime_ns, int)
        and mtime_ns == expected["mtime_ns"]
    )


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


def nest_ocr_markdown(text: str, *, parent_level: int = 3) -> str:
    """Keep OCR headings below the per-capture heading in the compiled book."""

    def replace_heading(match: re.Match[str]) -> str:
        source_level = len(match.group(1))
        nested_level = min(6, parent_level + source_level)
        return f"{'#' * nested_level} "

    return re.sub(r"(?m)^(#{1,6})\s+", replace_heading, text)


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
            return to_plain_object(model_dump(mode="json"))
        except TypeError:
            return to_plain_object(model_dump())

    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        return dict_method()

    return str(value)


def parse_json_payload(text: str) -> Any:
    normalized = text.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s*```$", "", normalized)
    return json.loads(normalized)


def build_ocr_config(
    *,
    model: str,
    image_detail: str,
    thinking_level: str,
    fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "provider": "gemini",
        "model": model,
        "image_detail": image_detail,
        "thinking_level": thinking_level,
        "prompt_version": PROMPT_VERSION,
        "output_normalization_version": OUTPUT_NORMALIZATION_VERSION,
        "passes": 1,
    }
    if fallback_model:
        config["fallback"] = {
            "provider": "openai",
            "model": fallback_model,
            "image_detail": DEFAULT_FALLBACK_IMAGE_DETAIL,
            "reasoning_effort": DEFAULT_FALLBACK_REASONING_EFFORT,
            "trigger": "gemini_recitation",
            "content_filter_fallback": {
                "provider": "macos-vision",
                "model": "VNRecognizeTextRequest",
                "package": "ocrmac-1.0.1",
            },
        }
    return config


def ocr_config_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    return payload.get("ocr_config") == expected


def ocr_generation_config_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Compare API-generation settings while ignoring post-processing version."""

    actual = payload.get("ocr_config")
    if not isinstance(actual, dict):
        return False
    actual = dict(actual)
    # Configs written before the refusal fallback was added are equivalent for pages
    # that Gemini completed. Upgrade them in place without making another API call.
    if "fallback" not in actual and "fallback" in expected:
        ocr = payload.get("ocr")
        if not isinstance(ocr, dict) or ocr.get("provider") != "gemini":
            return False
        actual["fallback"] = expected["fallback"]
    elif isinstance(actual.get("fallback"), dict) and isinstance(expected.get("fallback"), dict):
        actual_fallback = dict(actual["fallback"])
        expected_fallback = expected["fallback"]
        if (
            "content_filter_fallback" not in actual_fallback
            and "content_filter_fallback" in expected_fallback
        ):
            actual_fallback["content_filter_fallback"] = expected_fallback[
                "content_filter_fallback"
            ]
        actual["fallback"] = actual_fallback
    actual_generation = {
        key: value for key, value in actual.items() if key != "output_normalization_version"
    }
    expected_generation = {
        key: value for key, value in expected.items() if key != "output_normalization_version"
    }
    return actual_generation == expected_generation


def upgrade_cached_normalization(payload: dict[str, Any], expected_config: dict[str, Any]) -> bool:
    """Apply deterministic output upgrades without making another model call."""

    if payload.get("ocr_config") == expected_config:
        return False
    if not ocr_generation_config_matches(payload, expected_config):
        return False
    final = payload.get("final")
    if not isinstance(final, dict):
        return False

    normalized = validate_ocr_result(final)
    payload["final"] = normalized
    ocr = payload.get("ocr")
    if isinstance(ocr, dict):
        ocr["result"] = normalized
    payload["ocr_config"] = expected_config
    payload["updated_at"] = utc_now_iso()
    return True


def refresh_cached_quality_checks(payload: dict[str, Any]) -> bool:
    final = payload.get("final")
    if not isinstance(final, dict):
        return False
    expected = build_quality_checks(final)
    if payload.get("quality_checks") == expected:
        return False
    payload["quality_checks"] = expected
    payload["updated_at"] = utc_now_iso()
    return True


def estimate_cost(model: str, usage: dict[str, int]) -> dict[str, float | None]:
    pricing = MODEL_PRICING_USD_PER_MILLION.get(
        model
    ) or FALLBACK_MODEL_PRICING_USD_PER_MILLION.get(model)
    if not pricing:
        return {"standard_usd": None, "batch_usd": None}
    standard = (
        usage["input_tokens"] * pricing["input"] + usage["output_tokens"] * pricing["output"]
    ) / 1_000_000
    return {
        "standard_usd": round(standard, 8),
        "batch_usd": round(standard * pricing["batch_multiplier"], 8),
    }


def combine_usage(*items: dict[str, int]) -> dict[str, int]:
    keys = {
        "input_tokens",
        "output_tokens",
        "candidate_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
    }
    return {key: sum(int(item.get(key) or 0) for item in items) for key in keys}


def combine_costs(*items: dict[str, float | None]) -> dict[str, float]:
    return {
        key: round(sum(float(item.get(key) or 0) for item in items), 8)
        for key in ("standard_usd", "batch_usd")
    }


class NonRetryableOCRError(RuntimeError):
    """An OCR response that will not improve by repeating the same request."""


class GeminiRecitationError(NonRetryableOCRError):
    def __init__(self, usage: dict[str, int], response_metadata: dict[str, Any]) -> None:
        super().__init__("Gemini declined the OCR response with finish reason RECITATION")
        self.usage = usage
        self.response_metadata = response_metadata


class OpenAIContentFilterError(NonRetryableOCRError):
    def __init__(self, usage: dict[str, int], response_metadata: dict[str, Any]) -> None:
        super().__init__("OpenAI declined the OCR response with reason content_filter")
        self.usage = usage
        self.response_metadata = response_metadata


class GeminiVisionOCR:
    def __init__(self) -> None:
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai package is required. Install with: pip install -r requirements.txt"
            ) from exc
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def transcribe(
        self,
        *,
        model: str,
        image_path: Path,
        image_detail: str,
        thinking_level: str,
        max_output_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        from google.genai import types

        resolution = {
            "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        }[image_detail]
        thinking = {
            "minimal": types.ThinkingLevel.MINIMAL,
            "low": types.ThinkingLevel.LOW,
            "medium": types.ThinkingLevel.MEDIUM,
            "high": types.ThinkingLevel.HIGH,
        }[thinking_level]
        image_part = types.Part.from_bytes(
            data=image_path.read_bytes(),
            mime_type="image/png",
            media_resolution=resolution.value,
        )
        response = self.client.models.generate_content(
            model=model,
            contents=[OCR_PROMPT, image_part],
            config=types.GenerateContentConfig(
                system_instruction=OCR_INSTRUCTIONS,
                max_output_tokens=max_output_tokens,
                response_mime_type="application/json",
                response_json_schema=OCR_OUTPUT_SCHEMA,
                media_resolution=resolution,
                thinking_config=types.ThinkingConfig(thinking_level=thinking),
            ),
        )
        raw = to_plain_object(response)
        raw_usage = raw.get("usage_metadata", {}) if isinstance(raw, dict) else {}
        candidate_tokens = int(raw_usage.get("candidates_token_count") or 0)
        thought_tokens = int(raw_usage.get("thoughts_token_count") or 0)
        usage = {
            "input_tokens": int(raw_usage.get("prompt_token_count") or 0),
            "output_tokens": candidate_tokens + thought_tokens,
            "candidate_tokens": candidate_tokens,
            "reasoning_tokens": thought_tokens,
            "cached_input_tokens": int(raw_usage.get("cached_content_token_count") or 0),
        }
        candidates = raw.get("candidates", []) if isinstance(raw, dict) else []
        first_candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        headers = (
            (raw.get("sdk_http_response") or {}).get("headers") or {}
            if isinstance(raw, dict)
            else {}
        )
        citation_metadata = first_candidate.get("citation_metadata") or {}
        response_metadata = {
            "response_id": raw.get("response_id") if isinstance(raw, dict) else None,
            "model_version": raw.get("model_version") if isinstance(raw, dict) else None,
            "finish_reason": first_candidate.get("finish_reason"),
            "service_tier": headers.get("x-gemini-service-tier"),
            "citation_count": len(citation_metadata.get("citations") or []),
        }
        if not isinstance(response.text, str) or not response.text.strip():
            finish_reason = response_metadata.get("finish_reason")
            if finish_reason == "RECITATION":
                raise GeminiRecitationError(usage, response_metadata)
            raise ValueError(
                "Gemini response did not contain OCR JSON "
                f"(finish_reason={finish_reason or 'unknown'})"
            )
        result = validate_ocr_result(parse_json_payload(response.text))
        return result, usage, response_metadata


class OpenAIVisionOCR:
    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required. Install with: pip install -r requirements.txt"
            ) from exc
        self.client = OpenAI(timeout=180)

    def transcribe(
        self,
        *,
        model: str,
        image_path: Path,
        image_detail: str,
        reasoning_effort: str,
        max_output_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = self.client.responses.create(
            model=model,
            instructions=OCR_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": OCR_PROMPT},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{image_data}",
                            "detail": image_detail,
                        },
                    ],
                }
            ],
            reasoning={"effort": reasoning_effort},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "ocr_page",
                    "strict": True,
                    "schema": OCR_OUTPUT_SCHEMA,
                }
            },
            max_output_tokens=max_output_tokens,
            store=False,
        )
        raw = to_plain_object(response)
        raw_usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        output_details = raw_usage.get("output_tokens_details") or {}
        input_details = raw_usage.get("input_tokens_details") or {}
        usage = {
            "input_tokens": int(raw_usage.get("input_tokens") or 0),
            "output_tokens": int(raw_usage.get("output_tokens") or 0),
            "candidate_tokens": int(raw_usage.get("output_tokens") or 0),
            "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
            "cached_input_tokens": int(input_details.get("cached_tokens") or 0),
        }
        response_metadata = {
            "response_id": raw.get("id") if isinstance(raw, dict) else None,
            "model_version": raw.get("model") if isinstance(raw, dict) else None,
            "finish_reason": (
                str(raw.get("status") or "unknown").upper() if isinstance(raw, dict) else None
            ),
            "service_tier": raw.get("service_tier") if isinstance(raw, dict) else None,
            "citation_count": 0,
        }
        incomplete_details = raw.get("incomplete_details") if isinstance(raw, dict) else None
        if (
            isinstance(incomplete_details, dict)
            and incomplete_details.get("reason") == "content_filter"
        ):
            response_metadata["incomplete_reason"] = "content_filter"
            raise OpenAIContentFilterError(usage, response_metadata)
        if not isinstance(response.output_text, str) or not response.output_text.strip():
            raise ValueError("OpenAI response did not contain OCR JSON")
        result = validate_ocr_result(parse_json_payload(response.output_text))
        return result, usage, response_metadata


class MacOSVisionOCR:
    def transcribe(
        self, *, image_path: Path
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        try:
            from ocrmac import ocrmac
        except ImportError as exc:
            raise RuntimeError(
                "ocrmac package is required for the local content-filter fallback. "
                "Install with: pip install -r requirements.txt"
            ) from exc

        raw_lines = ocrmac.OCR(
            str(image_path),
            recognition_level="accurate",
            language_preference=["en-US"],
            confidence_threshold=0.0,
            detail=True,
        ).recognize()
        lines: list[tuple[str, float, list[float]]] = []
        for item in raw_lines:
            if not isinstance(item, tuple) or len(item) < 3:
                continue
            text, confidence, bounds = item[:3]
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(bounds, list) or len(bounds) != 4:
                continue
            lines.append((text.strip(), clamp_confidence(confidence), bounds))
        if not lines:
            raise ValueError("macOS Vision did not recognize any text")

        paragraphs: list[list[tuple[str, float, list[float]]]] = []
        for line in lines:
            if paragraphs:
                previous = paragraphs[-1][-1]
                previous_y = float(previous[2][1])
                current_y = float(line[2][1])
                current_height = float(line[2][3])
                previous_height = float(previous[2][3])
                vertical_gap = previous_y - (current_y + current_height)
                paragraph_threshold = max(previous_height, current_height) * 0.75
                if vertical_gap > paragraph_threshold:
                    paragraphs.append([])
            if not paragraphs:
                paragraphs.append([])
            paragraphs[-1].append(line)

        rendered_paragraphs = []
        for paragraph in paragraphs:
            text = " ".join(line[0] for line in paragraph)
            if paragraph and min(float(line[2][0]) for line in paragraph) >= 0.044:
                text = f"> {text}"
            rendered_paragraphs.append(text)

        average_confidence = sum(line[1] for line in lines) / len(lines)
        result = validate_ocr_result(
            {
                "text": "\n\n".join(rendered_paragraphs),
                "confidence": min(average_confidence, 0.95),
                "uncertainties": [],
                "normalization_notes": [
                    "Generated by macOS Vision after both cloud providers filtered the page; "
                    "typographic emphasis is not recoverable in this fallback."
                ],
            }
        )
        return (
            result,
            combine_usage(),
            {
                "response_id": None,
                "model_version": "VNRecognizeTextRequest",
                "finish_reason": "COMPLETED",
                "service_tier": "local",
                "citation_count": 0,
                "recognized_line_count": len(lines),
            },
        )


def retry_call(label: str, max_retries: int, fn):
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn(), attempts
        except NonRetryableOCRError:
            raise
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
                        "variant_index": parse_int(item.get("variant_index")) or 0,
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
                "variant_index": metadata.get("variant_index"),
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

        capture_id = capture.get("capture_id")
        canonical = canonical_results.get(str(capture_id), {})
        status = canonical.get("status")

        if status == "completed":
            final_payload = canonical.get("final")
            text = ""
            if isinstance(final_payload, dict):
                text_candidate = final_payload.get("text")
                if isinstance(text_candidate, str):
                    text = text_candidate.strip()
            lines.append(nest_ocr_markdown(text) if text else "[no text returned]")
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


def get_thread_ocr_client() -> GeminiVisionOCR:
    client = getattr(_THREAD_LOCAL, "gemini_ocr_client", None)
    if client is None:
        client = GeminiVisionOCR()
        _THREAD_LOCAL.gemini_ocr_client = client
    return client


def get_thread_fallback_ocr_client() -> OpenAIVisionOCR:
    client = getattr(_THREAD_LOCAL, "openai_ocr_client", None)
    if client is None:
        client = OpenAIVisionOCR()
        _THREAD_LOCAL.openai_ocr_client = client
    return client


def get_thread_local_ocr_client() -> MacOSVisionOCR:
    client = getattr(_THREAD_LOCAL, "macos_vision_ocr_client", None)
    if client is None:
        client = MacOSVisionOCR()
        _THREAD_LOCAL.macos_vision_ocr_client = client
    return client


def process_capture(
    capture: dict[str, Any],
    *,
    book_dir: Path,
    canonical_dir: Path,
    model: str,
    image_detail: str,
    thinking_level: str,
    fallback_model: str | None,
    max_retries: int,
    max_output_tokens: int,
    force: bool,
) -> dict[str, Any]:
    capture_id = str(capture["capture_id"])
    canonical_path = canonical_dir / f"{capture_id}.json"
    image_path = capture["source_path"]
    try:
        image_rel = image_path.relative_to(book_dir).as_posix()
    except Exception:
        image_rel = str(image_path)
    source_image_fingerprint = build_source_image_fingerprint(image_path, image_rel)
    expected_ocr_config = build_ocr_config(
        model=model,
        image_detail=image_detail,
        thinking_level=thinking_level,
        fallback_model=fallback_model,
    )

    existing_payload: dict[str, Any] | None = None
    if canonical_path.exists():
        try:
            payload = read_json(canonical_path)
            if isinstance(payload, dict):
                existing_payload = payload
        except Exception:
            existing_payload = None

    override_path = canonical_dir.parent / "overrides" / f"{capture_id}.json"
    manual_override: dict[str, Any] | None = None
    manual_override_fingerprint: dict[str, Any] | None = None
    manual_override_error: Exception | None = None
    if override_path.is_file():
        try:
            override_rel = override_path.relative_to(book_dir).as_posix()
            manual_override_fingerprint = build_source_image_fingerprint(
                override_path, override_rel
            )
            override_payload = read_json(override_path)
            manual_override = validate_ocr_result(override_payload)
        except Exception as exc:
            manual_override_error = exc

    if (
        manual_override is None
        and manual_override_error is None
        and not force
        and isinstance(existing_payload, dict)
        and existing_payload.get("status") == "completed"
        and ocr_generation_config_matches(existing_payload, expected_ocr_config)
    ):
        normalization_upgraded = upgrade_cached_normalization(existing_payload, expected_ocr_config)
        quality_checks_refreshed = refresh_cached_quality_checks(existing_payload)
        if normalization_upgraded or quality_checks_refreshed:
            write_json(canonical_path, existing_payload)
            updates = []
            if normalization_upgraded:
                updates.append("output normalization")
            if quality_checks_refreshed:
                updates.append("quality checks")
            print(f"[{capture_id}] upgraded cached {' and '.join(updates)}")

    has_reusable_status = (
        manual_override is None
        and manual_override_error is None
        and not force
        and isinstance(existing_payload, dict)
        and existing_payload.get("status") == "completed"
        and isinstance(existing_payload.get("final"), dict)
        and isinstance(existing_payload["final"].get("text"), str)
    )
    if has_reusable_status:
        source_matches = source_image_fingerprint_matches(
            existing_payload, source_image_fingerprint
        )
        config_matches = ocr_config_matches(existing_payload, expected_ocr_config)
        if source_matches and config_matches:
            print(f"[{capture_id}] reused existing completed transcript")
            return {
                "capture_id": capture_id,
                "status": "completed",
                "was_resumed": True,
                "result_payload": existing_payload,
            }
        invalidation_reasons = []
        if not source_matches:
            invalidation_reasons.append("source image changed or fingerprint missing")
        if not config_matches:
            invalidation_reasons.append("OCR configuration changed or missing")
        print(
            f"[{capture_id}] cache invalidated ({'; '.join(invalidation_reasons)}); re-running OCR"
        )

    created_at = (
        existing_payload.get("created_at") if isinstance(existing_payload, dict) else utc_now_iso()
    )

    result_payload: dict[str, Any] = {
        "capture_id": capture_id,
        "image_path": image_rel,
        "source_image": source_image_fingerprint,
        "ocr_config": expected_ocr_config,
        "capture": {
            "index": capture.get("index"),
            "path": capture.get("path"),
            "page": capture.get("page"),
            "total": capture.get("total"),
            "location": capture.get("location"),
            "total_location": capture.get("total_location"),
        },
        "created_at": created_at,
        "updated_at": utc_now_iso(),
        "status": "error",
        "ocr": None,
        "final": None,
        "quality_checks": None,
        "error": None,
    }

    if manual_override_error is not None:
        result_payload["error"] = {
            "message": f"Invalid manual OCR override {override_path}: {manual_override_error}",
            "failed_at": utc_now_iso(),
        }
        write_json(canonical_path, result_payload)
        print(f"[{capture_id}] failed: {result_payload['error']['message']}")
        return {
            "capture_id": capture_id,
            "status": "error",
            "was_resumed": False,
            "result_payload": result_payload,
        }

    if manual_override is not None and manual_override_fingerprint is not None:
        previous_override = (
            (existing_payload.get("ocr") or {}).get("manual_override")
            if isinstance(existing_payload, dict)
            else None
        )
        was_resumed = (
            isinstance(previous_override, dict)
            and previous_override == manual_override_fingerprint
            and source_image_fingerprint_matches(existing_payload, source_image_fingerprint)
            and ocr_config_matches(existing_payload, expected_ocr_config)
        )
        quality_checks = build_quality_checks(manual_override)
        quality_checks["flags"].append(
            {
                "type": "manual_override",
                "severity": "review",
                "path": manual_override_fingerprint["path"],
            }
        )
        result_payload["ocr"] = {
            "provider": "manual",
            "model": "reviewed-override",
            "attempts": 0,
            "duration_ms": 0,
            "usage": combine_usage(),
            "estimated_cost": combine_costs(),
            "response_metadata": None,
            "fallback": None,
            "manual_override": manual_override_fingerprint,
            "result": manual_override,
        }
        result_payload["final"] = manual_override
        result_payload["quality_checks"] = quality_checks
        result_payload["status"] = "completed"
        result_payload["updated_at"] = utc_now_iso()
        write_json(canonical_path, result_payload)
        print(f"[{capture_id}] used reviewed manual override {manual_override_fingerprint['path']}")
        return {
            "capture_id": capture_id,
            "status": "completed",
            "was_resumed": was_resumed,
            "result_payload": result_payload,
        }

    print(f"[{capture_id}] Gemini OCR on {image_rel}")

    try:
        ocr_client = get_thread_ocr_client()

        started = time.perf_counter()
        fallback_metadata: dict[str, Any] | None = None
        try:
            call_result, attempts = retry_call(
                f"{capture_id} Gemini OCR",
                max_retries,
                lambda: ocr_client.transcribe(
                    model=model,
                    image_path=image_path,
                    image_detail=image_detail,
                    thinking_level=thinking_level,
                    max_output_tokens=max_output_tokens,
                ),
            )
            actual_provider = "gemini"
            actual_model = model
        except GeminiRecitationError as primary_error:
            if not fallback_model:
                raise
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError(
                    "Gemini returned RECITATION and OPENAI_API_KEY is unavailable for fallback"
                ) from primary_error

            print(
                f"[{capture_id}] Gemini returned RECITATION; "
                f"falling back to OpenAI {fallback_model}"
            )
            fallback_client = get_thread_fallback_ocr_client()
            primary_cost = estimate_cost(model, primary_error.usage)
            local_stage: dict[str, Any] | None = None
            try:
                fallback_call_result, fallback_attempts = retry_call(
                    f"{capture_id} OpenAI fallback OCR",
                    max_retries,
                    lambda: fallback_client.transcribe(
                        model=fallback_model,
                        image_path=image_path,
                        image_detail=DEFAULT_FALLBACK_IMAGE_DETAIL,
                        reasoning_effort=DEFAULT_FALLBACK_REASONING_EFFORT,
                        max_output_tokens=max_output_tokens,
                    ),
                )
                fallback_result, fallback_usage, fallback_response_metadata = fallback_call_result
                call_result = (
                    fallback_result,
                    combine_usage(primary_error.usage, fallback_usage),
                    fallback_response_metadata,
                )
                attempts = 1 + fallback_attempts
                actual_provider = "openai"
                actual_model = fallback_model
            except OpenAIContentFilterError as secondary_error:
                print(
                    f"[{capture_id}] OpenAI returned content_filter; "
                    "falling back to local macOS Vision OCR"
                )
                fallback_attempts = 1
                fallback_usage = secondary_error.usage
                fallback_response_metadata = secondary_error.response_metadata
                local_client = get_thread_local_ocr_client()
                local_result, local_usage, local_response_metadata = local_client.transcribe(
                    image_path=image_path
                )
                call_result = (
                    local_result,
                    combine_usage(primary_error.usage, fallback_usage, local_usage),
                    local_response_metadata,
                )
                attempts = 2
                actual_provider = "macos-vision"
                actual_model = "VNRecognizeTextRequest"
                local_stage = {
                    "provider": "macos-vision",
                    "model": "VNRecognizeTextRequest",
                    "attempts": 1,
                    "usage": local_usage,
                    "estimated_cost": combine_costs(),
                    "response_metadata": local_response_metadata,
                }
            except Exception as fallback_error:
                raise RuntimeError(
                    f"Gemini returned RECITATION and the OpenAI fallback failed: {fallback_error}"
                ) from fallback_error

            fallback_cost = estimate_cost(fallback_model, fallback_usage)
            fallback_metadata = {
                "trigger": "gemini_recitation",
                "primary": {
                    "provider": "gemini",
                    "model": model,
                    "attempts": 1,
                    "usage": primary_error.usage,
                    "estimated_cost": primary_cost,
                    "response_metadata": primary_error.response_metadata,
                },
                "fallback": {
                    "provider": "openai",
                    "model": fallback_model,
                    "attempts": fallback_attempts,
                    "usage": fallback_usage,
                    "estimated_cost": fallback_cost,
                    "response_metadata": fallback_response_metadata,
                },
            }
            if local_stage:
                fallback_metadata["local_fallback"] = local_stage
        duration_ms = int((time.perf_counter() - started) * 1000)
        ocr_result, usage, response_metadata = call_result
        if fallback_metadata:
            estimated_cost = combine_costs(
                fallback_metadata["primary"]["estimated_cost"],
                fallback_metadata["fallback"]["estimated_cost"],
            )
        else:
            estimated_cost = estimate_cost(actual_model, usage)

        result_payload["ocr"] = {
            "provider": actual_provider,
            "model": actual_model,
            "attempts": attempts,
            "duration_ms": duration_ms,
            "usage": usage,
            "estimated_cost": estimated_cost,
            "response_metadata": response_metadata,
            "fallback": fallback_metadata,
            "result": ocr_result,
        }
        result_payload["final"] = ocr_result
        result_payload["quality_checks"] = build_quality_checks(ocr_result)
        result_payload["status"] = "completed"
        result_payload["updated_at"] = utc_now_iso()

        write_json(canonical_path, result_payload)
        print(
            f"[{capture_id}] completed "
            f"(confidence={ocr_result['confidence']:.2f}, "
            f"uncertainties={len(ocr_result['uncertainties'])}, "
            f"provider={actual_provider}, "
            f"cost=${estimated_cost['standard_usd'] or 0:.6f})"
        )
        return {
            "capture_id": capture_id,
            "status": "completed",
            "was_resumed": False,
            "result_payload": result_payload,
        }
    except Exception as exc:
        result_payload["error"] = {
            "message": str(exc),
            "failed_at": utc_now_iso(),
        }
        result_payload["updated_at"] = utc_now_iso()
        write_json(canonical_path, result_payload)
        print(f"[{capture_id}] failed: {exc}")
        return {
            "capture_id": capture_id,
            "status": "error",
            "was_resumed": False,
            "result_payload": result_payload,
        }


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Transcribe captured Kindle pages via Gemini OCR")
    parser.add_argument("--asin", required=True, help="Book ASIN (maps to books/<asin>)")
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_PRICING_USD_PER_MILLION),
        default=DEFAULT_MODEL,
        help=f"Gemini OCR model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--image-detail",
        choices=["low", "medium", "high"],
        default=DEFAULT_IMAGE_DETAIL,
        help=f"Gemini image media resolution (default: {DEFAULT_IMAGE_DETAIL})",
    )
    parser.add_argument(
        "--thinking-level",
        choices=["minimal", "low", "medium", "high"],
        default=DEFAULT_THINKING_LEVEL,
        help=f"Gemini thinking level (default: {DEFAULT_THINKING_LEVEL})",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=("Disable the OpenAI GPT-5.6 Luna fallback used only when Gemini returns RECITATION"),
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
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of captures to transcribe concurrently (default: {DEFAULT_CONCURRENCY})",
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
        help=f"Retries per request on failure (default: {DEFAULT_MAX_RETRIES})",
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
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.max_retries < 1:
        parser.error("--max-retries must be >= 1")
    if args.max_output_tokens < 256:
        parser.error("--max-output-tokens must be >= 256")

    fallback_model = None if args.no_fallback else DEFAULT_FALLBACK_MODEL

    asin_slug = sanitize_slug(args.asin)
    book_dir = Path.cwd() / "books" / asin_slug
    pages_dir = book_dir / "pages"
    transcripts_dir = book_dir / "transcripts"
    canonical_dir = transcripts_dir / "canonical"
    manifest_path = transcripts_dir / "manifest.json"
    captures_jsonl_path = transcripts_dir / "captures.jsonl"
    book_markdown_path = transcripts_dir / "book.md"
    review_path = transcripts_dir / "review.json"

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

    capture_id_counts: dict[str, int] = {}
    for idx, capture in enumerate(selected_captures):
        base_id = build_capture_id(capture, fallback_index=idx)
        count = capture_id_counts.get(base_id, 0) + 1
        capture_id_counts[base_id] = count
        capture["capture_id"] = base_id if count == 1 else f"{base_id}-{count}"

    print(
        f"Selected captures: {len(selected_captures)} | Source: {source_info['kind']} | "
        f"Concurrency: {args.concurrency} | Model: {args.model} | "
        f"Detail: {args.image_detail} | Thinking: {args.thinking_level} | "
        f"Recitation fallback: {fallback_model or 'disabled'}"
    )

    if args.dry_run:
        print("Dry run only. No API calls or file writes.")
        return 0

    if not os.environ.get("GEMINI_API_KEY"):
        print("Error: GEMINI_API_KEY is not set.")
        return 1

    transcripts_dir.mkdir(parents=True, exist_ok=True)
    canonical_dir.mkdir(parents=True, exist_ok=True)

    try:
        GeminiVisionOCR()
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    canonical_results: dict[str, dict[str, Any]] = {}
    completed_count = 0
    failed_count = 0
    resumed_count = 0
    progress_count = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_capture_id = {
            executor.submit(
                process_capture,
                capture,
                book_dir=book_dir,
                canonical_dir=canonical_dir,
                model=args.model,
                image_detail=args.image_detail,
                thinking_level=args.thinking_level,
                fallback_model=fallback_model,
                max_retries=args.max_retries,
                max_output_tokens=args.max_output_tokens,
                force=args.force,
            ): str(capture["capture_id"])
            for capture in selected_captures
        }

        for future in as_completed(future_to_capture_id):
            capture_id = future_to_capture_id[future]
            try:
                worker_result = future.result()
            except Exception as exc:
                failed_count += 1
                progress_count += 1
                print(f"[{capture_id}] failed: unexpected worker crash: {exc}")
                print(f"Progress: {progress_count}/{len(selected_captures)} captures finished")
                continue

            canonical_result = worker_result.get("result_payload")
            if isinstance(canonical_result, dict):
                canonical_results[capture_id] = canonical_result
            else:
                canonical_results[capture_id] = {}

            if worker_result.get("status") == "completed":
                completed_count += 1
                if worker_result.get("was_resumed"):
                    resumed_count += 1
            else:
                failed_count += 1

            progress_count += 1
            print(f"Progress: {progress_count}/{len(selected_captures)} captures finished")

    capture_records: list[dict[str, Any]] = []
    for capture in selected_captures:
        capture_id = capture["capture_id"]
        canonical_result = canonical_results.get(capture_id, {})

        final = canonical_result.get("final")
        if isinstance(final, dict):
            confidence = clamp_confidence(final.get("confidence"))
            uncertainties = final.get("uncertainties")
            uncertainty_count = len(uncertainties) if isinstance(uncertainties, list) else 0
        else:
            confidence = None
            uncertainty_count = None

        ocr = canonical_result.get("ocr")
        if isinstance(ocr, dict):
            usage = ocr.get("usage") if isinstance(ocr.get("usage"), dict) else {}
            estimated_cost = (
                ocr.get("estimated_cost") if isinstance(ocr.get("estimated_cost"), dict) else {}
            )
            attempts = parse_int(ocr.get("attempts"))
            provider = ocr.get("provider")
            used_model = ocr.get("model")
        else:
            usage = {}
            estimated_cost = {}
            attempts = None
            provider = None
            used_model = None

        quality_checks = canonical_result.get("quality_checks")
        quality_flags = (
            quality_checks.get("flags")
            if isinstance(quality_checks, dict) and isinstance(quality_checks.get("flags"), list)
            else []
        )

        capture_records.append(
            {
                "index": capture.get("index"),
                "file": capture.get("file"),
                "path": capture.get("path"),
                "page": capture.get("page"),
                "total": capture.get("total"),
                "location": capture.get("location"),
                "total_location": capture.get("total_location"),
                "capture_id": capture_id,
                "transcript_ref": f"canonical/{capture_id}.json",
                "status": canonical_result.get("status"),
                "provider": provider,
                "model": used_model,
                "confidence": confidence,
                "uncertainty_count": uncertainty_count,
                "attempts": attempts,
                "input_tokens": parse_int(usage.get("input_tokens")),
                "output_tokens": parse_int(usage.get("output_tokens")),
                "estimated_standard_cost_usd": estimated_cost.get("standard_usd"),
                "quality_flag_count": len(quality_flags),
                "quality_flag_types": [
                    flag.get("type") for flag in quality_flags if isinstance(flag, dict)
                ],
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
    review_records = []
    for capture in selected_captures:
        capture_id = str(capture["capture_id"])
        canonical_result = canonical_results.get(capture_id, {})
        checks = canonical_result.get("quality_checks")
        flags = (
            checks.get("flags")
            if isinstance(checks, dict) and isinstance(checks.get("flags"), list)
            else []
        )
        if not flags:
            continue
        final = canonical_result.get("final")
        review_records.append(
            {
                "capture_id": capture_id,
                "image_path": canonical_result.get("image_path"),
                "transcript_ref": f"canonical/{capture_id}.json",
                "confidence": final.get("confidence") if isinstance(final, dict) else None,
                "uncertainties": (final.get("uncertainties") if isinstance(final, dict) else []),
                "flags": flags,
            }
        )
    write_json(
        review_path,
        {
            "generated_at": utc_now_iso(),
            "quality_check_version": QUALITY_CHECK_VERSION,
            "selected_captures": len(selected_captures),
            "flagged_captures": len(review_records),
            "captures": review_records,
        },
    )

    existing_manifest = load_existing_manifest(manifest_path)
    created_at = (
        existing_manifest.get("created_at")
        if isinstance(existing_manifest, dict)
        and isinstance(existing_manifest.get("created_at"), str)
        else utc_now_iso()
    )

    successful_count = completed_count
    total_count = len(selected_captures)
    failure_ratio = (failed_count / total_count) if total_count else 0.0

    status = "completed" if failed_count == 0 else "partial"
    if successful_count == 0:
        status = "failed"

    completed_ocr = [
        result.get("ocr")
        for result in canonical_results.values()
        if result.get("status") == "completed" and isinstance(result.get("ocr"), dict)
    ]
    total_input_tokens = sum(
        int((ocr.get("usage") or {}).get("input_tokens") or 0) for ocr in completed_ocr
    )
    total_output_tokens = sum(
        int((ocr.get("usage") or {}).get("output_tokens") or 0) for ocr in completed_ocr
    )
    total_standard_cost = sum(
        float((ocr.get("estimated_cost") or {}).get("standard_usd") or 0) for ocr in completed_ocr
    )
    total_batch_cost = sum(
        float((ocr.get("estimated_cost") or {}).get("batch_usd") or 0) for ocr in completed_ocr
    )
    total_attempts = sum(int(ocr.get("attempts") or 0) for ocr in completed_ocr)
    retried_captures = sum(int(ocr.get("attempts") or 0) > 1 for ocr in completed_ocr)
    fallback_captures = sum(ocr.get("provider") == "openai" for ocr in completed_ocr)
    local_fallback_captures = sum(ocr.get("provider") == "macos-vision" for ocr in completed_ocr)
    manual_override_captures = sum(ocr.get("provider") == "manual" for ocr in completed_ocr)

    manifest_payload = {
        "asin": args.asin,
        "created_at": created_at,
        "updated_at": utc_now_iso(),
        "status": status,
        "source": source_info,
        "ocr_config": build_ocr_config(
            model=args.model,
            image_detail=args.image_detail,
            thinking_level=args.thinking_level,
            fallback_model=fallback_model,
        ),
        "options": {
            "start_at": args.start_at,
            "max_pages": args.max_pages,
            "concurrency": args.concurrency,
            "force": args.force,
            "max_retries": args.max_retries,
            "max_output_tokens": args.max_output_tokens,
            "recitation_fallback": fallback_model,
        },
        "counts": {
            "selected_captures": len(selected_captures),
            "processed_captures": total_count,
            "completed_captures": completed_count,
            "failed_captures": failed_count,
            "resumed_captures": resumed_count,
            "retried_captures": retried_captures,
            "fallback_captures": fallback_captures,
            "local_fallback_captures": local_fallback_captures,
            "manual_override_captures": manual_override_captures,
            "flagged_captures": len(review_records),
            "api_attempts": total_attempts,
            "failure_ratio": round(failure_ratio, 6),
        },
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        },
        "estimated_cost": {
            "standard_usd": round(total_standard_cost, 6),
            "batch_usd": round(total_batch_cost, 6),
            "pricing_snapshot_usd_per_million_tokens": {
                "primary": MODEL_PRICING_USD_PER_MILLION[args.model],
                "fallback": (
                    FALLBACK_MODEL_PRICING_USD_PER_MILLION[fallback_model]
                    if fallback_model
                    else None
                ),
            },
        },
        "files": {
            "captures": "captures.jsonl",
            "canonical_dir": "canonical",
            "book_markdown": "book.md",
            "review": "review.json",
        },
    }
    write_json(manifest_path, manifest_payload)

    print(
        f"Wrote transcript outputs: {book_markdown_path}, {captures_jsonl_path}, "
        f"{review_path}, {manifest_path}"
    )
    print(
        f"Estimated standard API cost: ${total_standard_cost:.4f} | "
        f"Input tokens: {total_input_tokens} | Output tokens: {total_output_tokens}"
    )

    if successful_count == 0:
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
