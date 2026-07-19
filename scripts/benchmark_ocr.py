"""Benchmark multiple vision OCR configurations against representative Kindle pages.

Outputs are isolated from production transcripts under books/ocr-benchmarks by default.
Each provider response, normalized OCR result, usage record, latency, and estimated cost
is persisted independently so interrupted runs can resume safely.

Examples:
    python scripts/benchmark_ocr.py --list-configs
    python scripts/benchmark_ocr.py --dry-run
    python scripts/benchmark_ocr.py --output-dir books/ocr-benchmarks/experiment-1
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_SAMPLES_PATH = Path("benchmarks/ocr_samples.json")
DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_FULL_CORPUS_CAPTURES = 2367

_THREAD_LOCAL = threading.local()

OCR_SCHEMA = {
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
- Represent blank workbook answer lines concisely; do not invent text for them.
- Remove only obvious display line-wrap artifacts and soft-hyphen word splits.
- Do not paraphrase, summarize, complete clipped sentences, or add commentary.
- Do not include the Kindle application chrome when it is present.
- Put a best-effort reading in text and record genuinely uncertain segments in the
  uncertainties array. Confidence is an overall 0-to-1 transcription confidence.
"""

OCR_PROMPT = "Transcribe this Kindle page screenshot exactly according to the OCR rules."

LITERAL_OCR_ADDENDUM = """
Literal-copy requirements:
- Reproduce what is visibly printed even when a word, spelling, acronym, grammar choice,
  or punctuation mark seems mistaken or semantically unlikely. Never silently correct it.
- Do not add punctuation, words, or quote marks that are not visible at a cropped page edge.
- When two readings seem possible, choose the visible letter shapes over the more likely phrase
  and record the ambiguity in uncertainties.
"""


@dataclass(frozen=True)
class BenchmarkConfig:
    id: str
    provider: str
    model: str
    image_detail: str
    reasoning: str
    input_usd_per_million: float
    output_usd_per_million: float
    batch_multiplier: float = 0.5
    instruction_variant: str = "baseline"


CONFIGS = {
    config.id: config
    for config in [
        BenchmarkConfig(
            id="openai-nano-high-none",
            provider="openai",
            model="gpt-5.4-nano",
            image_detail="high",
            reasoning="none",
            input_usd_per_million=0.20,
            output_usd_per_million=1.25,
        ),
        BenchmarkConfig(
            id="openai-luna-high-none",
            provider="openai",
            model="gpt-5.6-luna",
            image_detail="high",
            reasoning="none",
            input_usd_per_million=1.00,
            output_usd_per_million=6.00,
        ),
        BenchmarkConfig(
            id="openai-terra-original-low",
            provider="openai",
            model="gpt-5.6-terra",
            image_detail="original",
            reasoning="low",
            input_usd_per_million=2.50,
            output_usd_per_million=15.00,
        ),
        BenchmarkConfig(
            id="gemini-flash-lite-high-minimal",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            image_detail="high",
            reasoning="minimal",
            input_usd_per_million=0.25,
            output_usd_per_million=1.50,
        ),
        BenchmarkConfig(
            id="gemini-flash-high-minimal",
            provider="gemini",
            model="gemini-3.5-flash",
            image_detail="high",
            reasoning="minimal",
            input_usd_per_million=1.50,
            output_usd_per_million=9.00,
        ),
        BenchmarkConfig(
            id="gemini-flash-lite-high-minimal-literal",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            image_detail="high",
            reasoning="minimal",
            input_usd_per_million=0.25,
            output_usd_per_million=1.50,
            instruction_variant="literal",
        ),
        BenchmarkConfig(
            id="gemini-flash-high-minimal-literal",
            provider="gemini",
            model="gemini-3.5-flash",
            image_detail="high",
            reasoning="minimal",
            input_usd_per_million=1.50,
            output_usd_per_million=9.00,
            instruction_variant="literal",
        ),
    ]
}


def instructions_for(config: BenchmarkConfig) -> str:
    if config.instruction_variant == "literal":
        return OCR_INSTRUCTIONS + LITERAL_OCR_ADDENDUM
    return OCR_INSTRUCTIONS


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def utc_run_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def sanitize_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "item"


def to_plain_object(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [to_plain_object(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain_object(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_plain_object(model_dump(mode="json"))
        except TypeError:
            return to_plain_object(model_dump())
    return str(value)


def clamp_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return 0.0
    return max(0.0, min(1.0, parsed))


def normalize_ocr_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("OCR result is missing a text string")

    uncertainties: list[dict[str, str]] = []
    raw_uncertainties = payload.get("uncertainties")
    if isinstance(raw_uncertainties, list):
        for item in raw_uncertainties:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet")
            reason = item.get("reason")
            if isinstance(snippet, str) and isinstance(reason, str):
                uncertainties.append({"snippet": snippet, "reason": reason})

    notes = [item for item in payload.get("normalization_notes", []) if isinstance(item, str)]

    return {
        "text": payload["text"].strip(),
        "confidence": clamp_confidence(payload.get("confidence")),
        "uncertainties": uncertainties,
        "normalization_notes": notes,
    }


def get_openai_client():
    client = getattr(_THREAD_LOCAL, "openai_client", None)
    if client is None:
        from openai import OpenAI

        client = OpenAI(timeout=180)
        _THREAD_LOCAL.openai_client = client
    return client


def get_gemini_client():
    client = getattr(_THREAD_LOCAL, "gemini_client", None)
    if client is None:
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        _THREAD_LOCAL.gemini_client = client
    return client


def run_openai(
    config: BenchmarkConfig, image_path: Path, max_output_tokens: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = get_openai_client().responses.create(
        model=config.model,
        instructions=instructions_for(config),
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": OCR_PROMPT},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                        "detail": config.image_detail,
                    },
                ],
            }
        ],
        reasoning={"effort": config.reasoning},
        text={
            "format": {
                "type": "json_schema",
                "name": "ocr_page",
                "strict": True,
                "schema": OCR_SCHEMA,
            }
        },
        max_output_tokens=max_output_tokens,
        store=False,
    )
    payload = json.loads(response.output_text)
    raw = to_plain_object(response)
    raw_usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
    usage = {
        "input_tokens": int(raw_usage.get("input_tokens") or 0),
        "output_tokens": int(raw_usage.get("output_tokens") or 0),
        "reasoning_tokens": int(
            (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        ),
        "cached_input_tokens": int(
            (raw_usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
        ),
    }
    return normalize_ocr_payload(payload), raw, usage


def run_gemini(
    config: BenchmarkConfig, image_path: Path, max_output_tokens: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    from google.genai import types

    resolution = {
        "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
        "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
        "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
    }[config.image_detail]
    thinking_level = {
        "minimal": types.ThinkingLevel.MINIMAL,
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
    }[config.reasoning]
    image_part = types.Part.from_bytes(
        data=image_path.read_bytes(),
        mime_type="image/png",
        media_resolution=resolution.value,
    )
    response = get_gemini_client().models.generate_content(
        model=config.model,
        contents=[OCR_PROMPT, image_part],
        config=types.GenerateContentConfig(
            system_instruction=instructions_for(config),
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=OCR_SCHEMA,
            media_resolution=resolution,
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        ),
    )
    payload = json.loads(response.text)
    raw = to_plain_object(response)
    raw_usage = raw.get("usage_metadata", {}) if isinstance(raw, dict) else {}
    candidate_tokens = int(raw_usage.get("candidates_token_count") or 0)
    thought_tokens = int(raw_usage.get("thoughts_token_count") or 0)
    usage = {
        "input_tokens": int(raw_usage.get("prompt_token_count") or 0),
        "output_tokens": candidate_tokens + thought_tokens,
        "reasoning_tokens": thought_tokens,
        "cached_input_tokens": int(raw_usage.get("cached_content_token_count") or 0),
    }
    return normalize_ocr_payload(payload), raw, usage


def estimate_cost(config: BenchmarkConfig, usage: dict[str, int]) -> dict[str, float]:
    standard = (
        usage["input_tokens"] * config.input_usd_per_million
        + usage["output_tokens"] * config.output_usd_per_million
    ) / 1_000_000
    return {
        "standard_usd": round(standard, 8),
        "batch_usd": round(standard * config.batch_multiplier, 8),
    }


def load_samples(samples_path: Path, root: Path) -> list[dict[str, Any]]:
    payload = read_json(samples_path)
    raw_samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(raw_samples, list):
        raise ValueError(f"Sample manifest must contain a samples list: {samples_path}")

    samples: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_samples:
        if not isinstance(raw, dict):
            continue
        sample_id = raw.get("id")
        raw_path = raw.get("path")
        if not isinstance(sample_id, str) or not isinstance(raw_path, str):
            raise ValueError("Every sample needs string id and path fields")
        if sample_id in seen_ids:
            raise ValueError(f"Duplicate sample id: {sample_id}")
        image_path = (root / raw_path).resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"Sample image not found: {image_path}")
        seen_ids.add(sample_id)
        samples.append({**raw, "source_path": image_path})
    return samples


def retry_call(label: str, max_retries: int, fn):
    for attempt in range(1, max_retries + 1):
        try:
            return fn(), attempt
        except Exception as exc:
            if attempt == max_retries:
                raise RuntimeError(f"{label} failed after {attempt} attempts: {exc}") from exc
            delay = 2 ** (attempt - 1) + random.random() * 0.5
            print(f"Warning: {label} attempt {attempt} failed: {exc}; retrying in {delay:.1f}s")
            time.sleep(delay)
    raise AssertionError("unreachable")


def run_one(
    *,
    root: Path,
    output_dir: Path,
    config: BenchmarkConfig,
    sample: dict[str, Any],
    max_output_tokens: int,
    max_retries: int,
    force: bool,
) -> dict[str, Any]:
    sample_id = sanitize_slug(str(sample["id"]))
    result_path = output_dir / "results" / config.id / f"{sample_id}.json"
    if result_path.exists() and not force:
        existing = read_json(result_path)
        if isinstance(existing, dict) and existing.get("status") == "completed":
            return existing

    image_path = sample["source_path"]
    started = time.perf_counter()
    result: dict[str, Any] = {
        "config": asdict(config),
        "sample": {key: value for key, value in sample.items() if key != "source_path"},
        "status": "error",
        "created_at": utc_now_iso(),
    }
    try:
        runner = run_openai if config.provider == "openai" else run_gemini
        (call_result, attempts) = retry_call(
            f"{config.id}/{sample_id}",
            max_retries,
            lambda: runner(config, image_path, max_output_tokens),
        )
        ocr, raw_response, usage = call_result
        result.update(
            {
                "status": "completed",
                "attempts": attempts,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "ocr": ocr,
                "usage": usage,
                "estimated_cost": estimate_cost(config, usage),
                "raw_response": raw_response,
            }
        )
    except Exception as exc:
        result.update(
            {
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
    write_json(result_path, result)
    print(f"[{config.id}/{sample_id}] {result['status']} ({result['duration_ms']} ms)")
    return result


def build_summary(
    results: list[dict[str, Any]], configs: list[BenchmarkConfig], full_corpus_captures: int
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for config in configs:
        matching = [item for item in results if item.get("config", {}).get("id") == config.id]
        completed = [item for item in matching if item.get("status") == "completed"]
        total_cost = sum(
            float(item.get("estimated_cost", {}).get("standard_usd") or 0) for item in completed
        )
        total_batch_cost = sum(
            float(item.get("estimated_cost", {}).get("batch_usd") or 0) for item in completed
        )
        divisor = len(completed) or 1
        average_cost = total_cost / divisor
        average_batch_cost = total_batch_cost / divisor
        rows.append(
            {
                "config": asdict(config),
                "attempted": len(matching),
                "completed": len(completed),
                "failed": len(matching) - len(completed),
                "average_duration_ms": round(
                    sum(int(item.get("duration_ms") or 0) for item in completed) / divisor
                ),
                "average_input_tokens": round(
                    sum(int(item.get("usage", {}).get("input_tokens") or 0) for item in completed)
                    / divisor
                ),
                "average_output_tokens": round(
                    sum(int(item.get("usage", {}).get("output_tokens") or 0) for item in completed)
                    / divisor
                ),
                "average_reasoning_tokens": round(
                    sum(
                        int(item.get("usage", {}).get("reasoning_tokens") or 0)
                        for item in completed
                    )
                    / divisor
                ),
                "average_confidence": round(
                    sum(float(item.get("ocr", {}).get("confidence") or 0) for item in completed)
                    / divisor,
                    4,
                ),
                "average_standard_cost_usd": round(average_cost, 8),
                "average_batch_cost_usd": round(average_batch_cost, 8),
                "projected_full_corpus_standard_usd": round(average_cost * full_corpus_captures, 2),
                "projected_full_corpus_batch_usd": round(
                    average_batch_cost * full_corpus_captures, 2
                ),
            }
        )
    return {
        "generated_at": utc_now_iso(),
        "full_corpus_captures": full_corpus_captures,
        "result_count": len(results),
        "configs": rows,
    }


def parse_config_ids(raw: str) -> list[str]:
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [item for item in ids if item not in CONFIGS]
    if unknown:
        raise ValueError(f"Unknown config(s): {', '.join(unknown)}")
    return ids


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Benchmark OCR models on Kindle screenshots")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_PATH)
    parser.add_argument(
        "--configs",
        default=",".join(CONFIGS),
        help="Comma-separated benchmark config IDs (default: all)",
    )
    parser.add_argument(
        "--sample-ids",
        help="Optional comma-separated sample IDs (default: all samples)",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--full-corpus-captures", type=int, default=DEFAULT_FULL_CORPUS_CAPTURES)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-configs", action="store_true")
    args = parser.parse_args()

    if args.list_configs:
        for config in CONFIGS.values():
            print(
                f"{config.id}: {config.provider} {config.model}, "
                f"detail={config.image_detail}, reasoning={config.reasoning}, "
                f"instructions={config.instruction_variant}"
            )
        return 0
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.max_retries < 1:
        parser.error("--max-retries must be >= 1")
    if args.max_output_tokens < 256:
        parser.error("--max-output-tokens must be >= 256")
    if args.full_corpus_captures < 1:
        parser.error("--full-corpus-captures must be >= 1")

    root = Path.cwd().resolve()
    samples_path = (root / args.samples).resolve()
    try:
        config_ids = parse_config_ids(args.configs)
        samples = load_samples(samples_path, root)
    except Exception as exc:
        parser.error(str(exc))
    if args.sample_ids:
        requested_sample_ids = {item.strip() for item in args.sample_ids.split(",") if item.strip()}
        available_sample_ids = {str(sample["id"]) for sample in samples}
        unknown_sample_ids = sorted(requested_sample_ids - available_sample_ids)
        if unknown_sample_ids:
            parser.error(f"Unknown sample ID(s): {', '.join(unknown_sample_ids)}")
        samples = [sample for sample in samples if sample["id"] in requested_sample_ids]
    configs = [CONFIGS[config_id] for config_id in config_ids]
    output_dir = (
        (root / args.output_dir).resolve()
        if args.output_dir
        else root / "books" / "ocr-benchmarks" / utc_run_stamp()
    )

    providers = {config.provider for config in configs}
    missing_keys = []
    if "openai" in providers and not os.environ.get("OPENAI_API_KEY"):
        missing_keys.append("OPENAI_API_KEY")
    if "gemini" in providers and not os.environ.get("GEMINI_API_KEY"):
        missing_keys.append("GEMINI_API_KEY")
    if missing_keys and not args.dry_run:
        parser.error(f"Missing environment variables: {', '.join(missing_keys)}")

    print(
        f"Benchmark matrix: {len(samples)} samples x {len(configs)} configs = "
        f"{len(samples) * len(configs)} calls"
    )
    print(f"Output directory: {output_dir}")
    if args.dry_run:
        for config in configs:
            print(
                f"- {config.id}: {config.model}, detail={config.image_detail}, "
                f"reasoning={config.reasoning}, instructions={config.instruction_variant}"
            )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        output_dir / "run.json",
        {
            "created_at": utc_now_iso(),
            "samples_manifest": str(samples_path.relative_to(root)),
            "samples": [
                {key: value for key, value in sample.items() if key != "source_path"}
                for sample in samples
            ],
            "configs": [asdict(config) for config in configs],
            "options": {
                "concurrency": args.concurrency,
                "max_retries": args.max_retries,
                "max_output_tokens": args.max_output_tokens,
                "full_corpus_captures": args.full_corpus_captures,
            },
        },
    )

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                run_one,
                root=root,
                output_dir=output_dir,
                config=config,
                sample=sample,
                max_output_tokens=args.max_output_tokens,
                max_retries=args.max_retries,
                force=args.force,
            )
            for config in configs
            for sample in samples
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            print(f"Progress: {index}/{len(futures)}")

    results.sort(
        key=lambda item: (
            str(item.get("config", {}).get("id")),
            str(item.get("sample", {}).get("id")),
        )
    )
    summary = build_summary(results, configs, args.full_corpus_captures)
    write_json(output_dir / "results.json", results)
    write_json(output_dir / "summary.json", summary)
    print(f"Wrote benchmark summary: {output_dir / 'summary.json'}")

    failures = sum(1 for result in results if result.get("status") != "completed")
    if failures:
        print(f"Warning: {failures} benchmark calls failed; rerun the same command to resume.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
