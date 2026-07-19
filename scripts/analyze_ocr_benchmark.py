"""Measure agreement among completed OCR benchmark outputs.

This is not a substitute for ground-truth transcription, but it is useful for
finding outlier models and pages that deserve manual or model-assisted review.

Example:
    python scripts/analyze_ocr_benchmark.py \
      books/ocr-benchmarks/2026-07-18-model-comparison
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_content(text: str) -> str:
    """Normalize typography and Markdown while keeping content differences."""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(
        str.maketrans(
            {
                "‘": "'",
                "’": "'",
                "‚": "'",
                "“": '"',
                "”": '"',
                "„": '"',
                "–": "-",
                "—": "-",
                "−": "-",
                "…": "...",
                "\u00a0": " ",
            }
        )
    )
    # Blank workbook lines vary arbitrarily in underscore count.
    normalized = re.sub(r"_{3,}", " <blank-line> ", normalized)
    # Strip common Markdown presentation characters for content agreement.
    normalized = normalized.replace("**", "").replace("*", "")
    normalized = re.sub(r"(?m)^\s*>\s?", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_format(text: str) -> str:
    """Normalize inconsequential spacing while preserving Markdown structure."""

    lines = []
    for raw_line in unicodedata.normalize("NFKC", text).splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        line = re.sub(r"_{3,}", "<blank-line>", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def load_completed_results(run_dir: Path) -> list[dict[str, Any]]:
    results_path = run_dir / "results.json"
    raw_results = read_json(results_path)
    if not isinstance(raw_results, list):
        raise ValueError(f"Expected a JSON list: {results_path}")
    return [
        item
        for item in raw_results
        if isinstance(item, dict)
        and item.get("status") == "completed"
        and isinstance(item.get("ocr", {}).get("text"), str)
    ]


def analyze(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for result in results:
        sample_id = str(result["sample"]["id"])
        config_id = str(result["config"]["id"])
        grouped[sample_id][config_id] = result["ocr"]["text"]

    config_ids = sorted({config_id for candidates in grouped.values() for config_id in candidates})
    pairwise_content: dict[tuple[str, str], list[float]] = defaultdict(list)
    pairwise_format: dict[tuple[str, str], list[float]] = defaultdict(list)
    config_content_scores: dict[str, list[float]] = defaultdict(list)
    config_format_scores: dict[str, list[float]] = defaultdict(list)
    medoid_wins: dict[str, int] = defaultdict(int)
    pages: list[dict[str, Any]] = []

    for sample_id, candidates in sorted(grouped.items()):
        ids = sorted(candidates)
        content = {key: normalize_content(value) for key, value in candidates.items()}
        formatted = {key: normalize_format(value) for key, value in candidates.items()}
        per_config_content: dict[str, list[float]] = defaultdict(list)
        per_config_format: dict[str, list[float]] = defaultdict(list)

        for index, left_id in enumerate(ids):
            for right_id in ids[index + 1 :]:
                key = (left_id, right_id)
                content_score = similarity(content[left_id], content[right_id])
                format_score = similarity(formatted[left_id], formatted[right_id])
                pairwise_content[key].append(content_score)
                pairwise_format[key].append(format_score)
                per_config_content[left_id].append(content_score)
                per_config_content[right_id].append(content_score)
                per_config_format[left_id].append(format_score)
                per_config_format[right_id].append(format_score)

        page_scores = {config_id: mean(per_config_content[config_id]) for config_id in ids}
        best_score = max(page_scores.values(), default=0.0)
        medoids = sorted(
            config_id for config_id, score in page_scores.items() if abs(score - best_score) < 1e-12
        )
        for config_id in medoids:
            medoid_wins[config_id] += 1
        for config_id in ids:
            config_content_scores[config_id].extend(per_config_content[config_id])
            config_format_scores[config_id].extend(per_config_format[config_id])
        pages.append(
            {
                "sample_id": sample_id,
                "content_agreement": {
                    key: round(value, 6) for key, value in sorted(page_scores.items())
                },
                "medoid_configs": medoids,
                "spread": round(
                    max(page_scores.values(), default=0.0) - min(page_scores.values(), default=0.0),
                    6,
                ),
            }
        )

    configs = []
    for config_id in config_ids:
        configs.append(
            {
                "config_id": config_id,
                "average_content_agreement": round(mean(config_content_scores[config_id]), 6),
                "average_format_agreement": round(mean(config_format_scores[config_id]), 6),
                "medoid_pages": medoid_wins[config_id],
            }
        )

    pairwise = []
    for left_index, left_id in enumerate(config_ids):
        for right_id in config_ids[left_index + 1 :]:
            key = (left_id, right_id)
            pairwise.append(
                {
                    "left": left_id,
                    "right": right_id,
                    "content_agreement": round(mean(pairwise_content[key]), 6),
                    "format_agreement": round(mean(pairwise_format[key]), 6),
                    "shared_pages": len(pairwise_content[key]),
                }
            )

    return {
        "method": {
            "content": "Unicode/typography/whitespace/Markdown-normalized character similarity",
            "format": "spacing-normalized character similarity retaining Markdown structure",
            "warning": "Agreement detects outliers; it does not establish ground truth.",
        },
        "completed_results": len(results),
        "sample_count": len(grouped),
        "configs": configs,
        "pairwise": pairwise,
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze OCR benchmark agreement")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path (default: RUN_DIR/agreement.json)",
    )
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    output_path = args.output.resolve() if args.output else run_dir / "agreement.json"
    report = analyze(load_completed_results(run_dir))
    write_json(output_path, report)

    print("config                               content  format  medoid-pages")
    for row in sorted(
        report["configs"],
        key=lambda item: item["average_content_agreement"],
        reverse=True,
    ):
        print(
            f"{row['config_id']:<36} "
            f"{row['average_content_agreement']:.4f}   "
            f"{row['average_format_agreement']:.4f}   "
            f"{row['medoid_pages']}"
        )
    print(f"Wrote agreement report: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
