"""Blindly grade OCR benchmark candidates with independent vision models.

Candidate model names are replaced with deterministic letter labels before each
judge sees the page. Results and usage are stored under RUN_DIR/judging.

Example:
    python scripts/judge_ocr_benchmark.py \
      books/ocr-benchmarks/2026-07-18-model-comparison
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 8192

_THREAD_LOCAL = threading.local()

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["evaluations", "best_candidate_id", "rationale"],
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "candidate_id",
                    "text_accuracy",
                    "structure_fidelity",
                    "errors",
                    "notes",
                ],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "text_accuracy": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "structure_fidelity": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "errors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "type",
                                "severity",
                                "source_text",
                                "candidate_text",
                            ],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "omission",
                                        "addition",
                                        "substitution",
                                        "punctuation",
                                        "capitalization",
                                        "ordering",
                                        "formatting",
                                    ],
                                },
                                "severity": {
                                    "type": "string",
                                    "enum": ["minor", "meaningful", "major"],
                                },
                                "source_text": {"type": "string"},
                                "candidate_text": {"type": "string"},
                            },
                        },
                    },
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "best_candidate_id": {"type": "string"},
        "rationale": {"type": "string"},
    },
}

JUDGE_INSTRUCTIONS = """You are an exacting, provider-neutral OCR evaluator.
The supplied image is the sole ground truth. Do not use outside knowledge and do not
reward a candidate for silently correcting or completing what is visibly printed.

Evaluate every anonymized candidate on two separate 0-100 scales:
- text_accuracy: visible words, spelling, capitalization, punctuation, reading order,
  and complete page-boundary handling. 100 means no detected content error; 99 means
  only a trivial typographic normalization; 97-98 means one minor error; 90-96 means
  several minor errors or one meaningful error. Penalize invented or omitted text.
- structure_fidelity: paragraph boundaries, headings, lists, speaker changes, bold,
  italics, quotations, and blank answer lines. Do not penalize display line-wrap
  removal. Markdown is the intended representation.

List concrete differences in errors, using short snippets only. A formatting error
belongs on the structure scale unless it changes textual meaning. Select the best
overall candidate, prioritizing text accuracy over structure. Ties are allowed only
in scores, not in best_candidate_id; break a tie with structure fidelity.
"""


@dataclass(frozen=True)
class JudgeConfig:
    id: str
    provider: str
    model: str
    image_detail: str
    reasoning: str
    input_usd_per_million: float
    output_usd_per_million: float


JUDGES = {
    judge.id: judge
    for judge in [
        JudgeConfig(
            id="openai-sol-original-low",
            provider="openai",
            model="gpt-5.6-sol",
            image_detail="original",
            reasoning="low",
            input_usd_per_million=5.0,
            output_usd_per_million=30.0,
        ),
        JudgeConfig(
            id="gemini-pro-high-low",
            provider="gemini",
            model="gemini-3.1-pro-preview",
            image_detail="high",
            reasoning="low",
            input_usd_per_million=2.0,
            output_usd_per_million=12.0,
        ),
    ]
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def sanitize_slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in ".-_" else "-" for character in value
    )


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


def get_openai_client():
    client = getattr(_THREAD_LOCAL, "openai_client", None)
    if client is None:
        from openai import OpenAI

        client = OpenAI(timeout=240)
        _THREAD_LOCAL.openai_client = client
    return client


def get_gemini_client():
    client = getattr(_THREAD_LOCAL, "gemini_client", None)
    if client is None:
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        _THREAD_LOCAL.gemini_client = client
    return client


def make_blind_candidates(
    sample_id: str, candidates: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    config_ids = sorted(candidates)
    seed = int.from_bytes(hashlib.sha256(sample_id.encode()).digest()[:8], "big")
    random.Random(seed).shuffle(config_ids)
    labels = [chr(ord("A") + index) for index in range(len(config_ids))]
    label_to_config = dict(zip(labels, config_ids, strict=True))
    blinded = {label: candidates[config_id] for label, config_id in label_to_config.items()}
    return blinded, label_to_config


def build_prompt(blinded: dict[str, str]) -> str:
    sections = [
        "Compare each OCR candidate below with the page image. The candidates are anonymized."
    ]
    for label, candidate_text in blinded.items():
        sections.append(f"\n<CANDIDATE_{label}>\n{candidate_text}\n</CANDIDATE_{label}>")
    return "\n".join(sections)


def normalize_judgment(payload: Any, labels: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("evaluations"), list):
        raise ValueError("Judge response is missing evaluations")
    evaluations = payload["evaluations"]

    def normalize_candidate_id(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.upper()
        if normalized.startswith("CANDIDATE_"):
            normalized = normalized.removeprefix("CANDIDATE_")
        return normalized

    for evaluation in evaluations:
        if isinstance(evaluation, dict):
            evaluation["candidate_id"] = normalize_candidate_id(evaluation.get("candidate_id"))
    payload["best_candidate_id"] = normalize_candidate_id(payload.get("best_candidate_id"))
    returned = [item.get("candidate_id") for item in evaluations if isinstance(item, dict)]
    if len(evaluations) != len(labels) or set(returned) != labels:
        raise ValueError(f"Judge returned candidate IDs {returned}; expected {sorted(labels)}")
    best = payload.get("best_candidate_id")
    if best not in labels:
        raise ValueError(f"Unknown best_candidate_id: {best}")
    return payload


def run_openai(
    judge: JudgeConfig,
    image_path: Path,
    prompt: str,
    labels: set[str],
    max_output_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = get_openai_client().responses.create(
        model=judge.model,
        instructions=JUDGE_INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                        "detail": judge.image_detail,
                    },
                ],
            }
        ],
        reasoning={"effort": judge.reasoning},
        text={
            "format": {
                "type": "json_schema",
                "name": "ocr_judgment",
                "strict": True,
                "schema": JUDGE_SCHEMA,
            }
        },
        max_output_tokens=max_output_tokens,
        store=False,
    )
    payload = normalize_judgment(json.loads(response.output_text), labels)
    raw = to_plain_object(response)
    raw_usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
    usage = {
        "input_tokens": int(raw_usage.get("input_tokens") or 0),
        "output_tokens": int(raw_usage.get("output_tokens") or 0),
        "reasoning_tokens": int(
            (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        ),
    }
    return payload, raw, usage


def run_gemini(
    judge: JudgeConfig,
    image_path: Path,
    prompt: str,
    labels: set[str],
    max_output_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    from google.genai import types

    resolution = types.MediaResolution.MEDIA_RESOLUTION_HIGH
    image_part = types.Part.from_bytes(
        data=image_path.read_bytes(),
        mime_type="image/png",
        media_resolution=resolution.value,
    )
    response = get_gemini_client().models.generate_content(
        model=judge.model,
        contents=[prompt, image_part],
        config=types.GenerateContentConfig(
            system_instruction=JUDGE_INSTRUCTIONS,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=JUDGE_SCHEMA,
            media_resolution=resolution,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        ),
    )
    payload = normalize_judgment(json.loads(response.text), labels)
    raw = to_plain_object(response)
    raw_usage = raw.get("usage_metadata", {}) if isinstance(raw, dict) else {}
    candidate_tokens = int(raw_usage.get("candidates_token_count") or 0)
    thought_tokens = int(raw_usage.get("thoughts_token_count") or 0)
    usage = {
        "input_tokens": int(raw_usage.get("prompt_token_count") or 0),
        "output_tokens": candidate_tokens + thought_tokens,
        "reasoning_tokens": thought_tokens,
    }
    return payload, raw, usage


def estimate_cost(judge: JudgeConfig, usage: dict[str, int]) -> float:
    return round(
        (
            usage["input_tokens"] * judge.input_usd_per_million
            + usage["output_tokens"] * judge.output_usd_per_million
        )
        / 1_000_000,
        8,
    )


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


def load_candidates(run_dir: Path) -> dict[str, dict[str, Any]]:
    raw_results = read_json(run_dir / "results.json")
    grouped: dict[str, dict[str, Any]] = {}
    for result in raw_results:
        if result.get("status") != "completed":
            continue
        sample = result["sample"]
        sample_id = str(sample["id"])
        entry = grouped.setdefault(sample_id, {"sample": sample, "candidates": {}})
        entry["candidates"][str(result["config"]["id"])] = result["ocr"]["text"]
    return grouped


def run_one(
    *,
    root: Path,
    run_dir: Path,
    judge: JudgeConfig,
    sample_id: str,
    sample: dict[str, Any],
    candidates: dict[str, str],
    max_output_tokens: int,
    max_retries: int,
    force: bool,
) -> dict[str, Any]:
    result_path = run_dir / "judging" / "results" / judge.id / f"{sanitize_slug(sample_id)}.json"
    if result_path.exists() and not force:
        existing = read_json(result_path)
        if isinstance(existing, dict) and existing.get("status") == "completed":
            return existing

    blinded, label_to_config = make_blind_candidates(sample_id, candidates)
    prompt = build_prompt(blinded)
    image_path = (root / sample["path"]).resolve()
    started = time.perf_counter()
    result: dict[str, Any] = {
        "judge": asdict(judge),
        "sample": sample,
        "candidate_mapping": label_to_config,
        "status": "error",
        "created_at": utc_now_iso(),
    }
    try:
        runner = run_openai if judge.provider == "openai" else run_gemini
        (call_result, attempts) = retry_call(
            f"{judge.id}/{sample_id}",
            max_retries,
            lambda: runner(judge, image_path, prompt, set(blinded), max_output_tokens),
        )
        judgment, raw_response, usage = call_result
        result.update(
            {
                "status": "completed",
                "attempts": attempts,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "judgment": judgment,
                "usage": usage,
                "estimated_cost_usd": estimate_cost(judge, usage),
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
    print(f"[{judge.id}/{sample_id}] {result['status']} ({result['duration_ms']} ms)")
    return result


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [result for result in results if result.get("status") == "completed"]
    scores: dict[str, list[dict[str, Any]]] = defaultdict(list)
    best_votes: Counter[str] = Counter()
    judge_costs: Counter[str] = Counter()
    errors: dict[str, Counter[str]] = defaultdict(Counter)

    for result in completed:
        judge_id = result["judge"]["id"]
        mapping = result["candidate_mapping"]
        judge_costs[judge_id] += float(result["estimated_cost_usd"])
        best_votes[mapping[result["judgment"]["best_candidate_id"]]] += 1
        for evaluation in result["judgment"]["evaluations"]:
            config_id = mapping[evaluation["candidate_id"]]
            text_score = int(evaluation["text_accuracy"])
            structure_score = int(evaluation["structure_fidelity"])
            scores[config_id].append(
                {
                    "judge_id": judge_id,
                    "sample_id": result["sample"]["id"],
                    "text_accuracy": text_score,
                    "structure_fidelity": structure_score,
                    "weighted_score": text_score * 0.85 + structure_score * 0.15,
                }
            )
            for error in evaluation["errors"]:
                errors[config_id][f"{error['severity']}_{error['type']}"] += 1

    config_rows = []
    for config_id, items in sorted(scores.items()):
        by_judge: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_judge[item["judge_id"]].append(item)
        config_rows.append(
            {
                "config_id": config_id,
                "evaluations": len(items),
                "average_text_accuracy": round(
                    sum(item["text_accuracy"] for item in items) / len(items), 3
                ),
                "minimum_text_accuracy": min(item["text_accuracy"] for item in items),
                "average_structure_fidelity": round(
                    sum(item["structure_fidelity"] for item in items) / len(items), 3
                ),
                "average_weighted_score": round(
                    sum(item["weighted_score"] for item in items) / len(items), 3
                ),
                "best_votes": best_votes[config_id],
                "errors": dict(sorted(errors[config_id].items())),
                "by_judge": {
                    judge_id: {
                        "average_text_accuracy": round(
                            sum(item["text_accuracy"] for item in judge_items) / len(judge_items),
                            3,
                        ),
                        "average_structure_fidelity": round(
                            sum(item["structure_fidelity"] for item in judge_items)
                            / len(judge_items),
                            3,
                        ),
                    }
                    for judge_id, judge_items in sorted(by_judge.items())
                },
            }
        )

    return {
        "generated_at": utc_now_iso(),
        "weighting": {"text_accuracy": 0.85, "structure_fidelity": 0.15},
        "attempted_judgments": len(results),
        "completed_judgments": len(completed),
        "failed_judgments": len(results) - len(completed),
        "judge_cost_usd": {
            **{key: round(value, 6) for key, value in sorted(judge_costs.items())},
            "total": round(sum(judge_costs.values()), 6),
        },
        "configs": config_rows,
    }


def parse_ids(raw: str, available: dict[str, Any], label: str) -> list[str]:
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [item for item in ids if item not in available]
    if unknown:
        raise ValueError(f"Unknown {label}(s): {', '.join(unknown)}")
    return ids


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Blindly judge OCR benchmark outputs")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--judges", default=",".join(JUDGES))
    parser.add_argument("--sample-ids")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    root = Path.cwd().resolve()
    run_dir = args.run_dir.resolve()
    try:
        judge_ids = parse_ids(args.judges, JUDGES, "judge")
        grouped = load_candidates(run_dir)
        sample_ids = (
            parse_ids(args.sample_ids, grouped, "sample") if args.sample_ids else sorted(grouped)
        )
    except Exception as exc:
        parser.error(str(exc))

    judges = [JUDGES[judge_id] for judge_id in judge_ids]
    missing_keys = []
    providers = {judge.provider for judge in judges}
    if "openai" in providers and not os.environ.get("OPENAI_API_KEY"):
        missing_keys.append("OPENAI_API_KEY")
    if "gemini" in providers and not os.environ.get("GEMINI_API_KEY"):
        missing_keys.append("GEMINI_API_KEY")
    if missing_keys and not args.dry_run:
        parser.error(f"Missing environment variables: {', '.join(missing_keys)}")

    call_count = len(judges) * len(sample_ids)
    print(
        f"Blind judging matrix: {len(sample_ids)} samples x {len(judges)} judges = {call_count} calls"
    )
    if args.dry_run:
        for judge in judges:
            print(
                f"- {judge.id}: {judge.model}, detail={judge.image_detail}, reasoning={judge.reasoning}"
            )
        return 0

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                run_one,
                root=root,
                run_dir=run_dir,
                judge=judge,
                sample_id=sample_id,
                sample=grouped[sample_id]["sample"],
                candidates=grouped[sample_id]["candidates"],
                max_output_tokens=args.max_output_tokens,
                max_retries=args.max_retries,
                force=args.force,
            )
            for judge in judges
            for sample_id in sample_ids
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            print(f"Progress: {index}/{len(futures)}")

    results.sort(key=lambda result: (result["judge"]["id"], result["sample"]["id"]))
    summary = build_summary(results)
    write_json(run_dir / "judging" / "results.json", results)
    write_json(run_dir / "judging" / "summary.json", summary)
    print(f"Wrote judging summary: {run_dir / 'judging' / 'summary.json'}")
    return 1 if summary["failed_judgments"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
