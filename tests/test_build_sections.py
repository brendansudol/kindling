from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_sections


class BuildSectionsTests(unittest.TestCase):
    def test_title_matching_tolerates_chapter_labels_and_articles(self) -> None:
        text = "CHAPTER 32\n\nWhat to Do When the Punchline Doesn't Land\n\nBody."
        match = build_sections.best_title_match(text, "32 What To Do When a Punchline Doesn’t Land")
        self.assertIsNotNone(match)
        self.assertEqual(match[0], 0)

    def make_book(
        self, root: Path, captures: list[dict[str, object]], sections: list[dict[str, object]]
    ) -> Path:
        book_dir = root / "books" / "TESTASIN"
        transcripts_dir = book_dir / "transcripts"
        canonical_dir = transcripts_dir / "canonical"
        analysis_dir = book_dir / "analysis"
        canonical_dir.mkdir(parents=True)
        analysis_dir.mkdir(parents=True)

        records = []
        book_lines = ["# Test Book", "", "- ASIN: `TESTASIN`", ""]
        for index, capture in enumerate(captures):
            capture_id = str(capture["capture_id"])
            text = str(capture.get("text", ""))
            page = capture.get("page")
            location = capture.get("location")
            if page is not None:
                label = f"Page {page} of 30"
            else:
                label = f"Location {location} of 300"
            canonical_path = canonical_dir / f"{capture_id}.json"
            build_sections.write_json(
                canonical_path,
                {"status": "completed", "final": {"text": text}},
            )
            records.append(
                {
                    "index": index,
                    "capture_id": capture_id,
                    "page": page,
                    "total": 30 if page is not None else None,
                    "location": location,
                    "total_location": 300 if location is not None else None,
                    "transcript_ref": f"canonical/{capture_id}.json",
                }
            )
            book_lines.extend([f"### {label}", "", text, ""])

        (transcripts_dir / "captures.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        (transcripts_dir / "book.md").write_text("\n".join(book_lines), encoding="utf-8")
        (analysis_dir / "chapter-map.md").write_text(
            "# Map\n\n```json\n" + json.dumps(sections, indent=2) + "\n```\n",
            encoding="utf-8",
        )
        return book_dir

    def test_shared_marker_sections_use_distinct_capture_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book_dir = self.make_book(
                root,
                captures=[
                    {
                        "capture_id": "loc-0010-v1",
                        "location": 10,
                        "text": "PART ONE\n\nPreparation",
                    },
                    {
                        "capture_id": "loc-0010-v2",
                        "location": 10,
                        "text": "# Chapter 1\n\n# Inspiration\n\nChapter body.",
                    },
                    {
                        "capture_id": "loc-0020-v1",
                        "location": 20,
                        "text": "# Chapter 2: Craft\n\nCraft body.",
                    },
                ],
                sections=[
                    {
                        "seq": 1,
                        "title": "Part One: Preparation",
                        "type": "Part divider",
                        "start_marker": "Location 10 of 300",
                        "end_marker": "Location 10 of 300",
                        "completeness": "complete",
                    },
                    {
                        "seq": 2,
                        "title": "Chapter 1: Inspiration",
                        "type": "Chapter",
                        "start_marker": "Location 10 of 300",
                        "end_marker": "Location 10 of 300",
                        "completeness": "complete",
                    },
                    {
                        "seq": 3,
                        "title": "Chapter 2: Craft",
                        "type": "Chapter",
                        "start_marker": "Location 20 of 300",
                        "end_marker": "Location 20 of 300",
                        "completeness": "complete",
                    },
                ],
            )

            manifest, files = build_sections.build_section_outputs(
                asin="TESTASIN",
                book_dir=book_dir,
                chapter_map_path=book_dir / "analysis" / "chapter-map.md",
            )

            self.assertEqual(manifest["counts"]["title_matched_sections"], 3)
            self.assertEqual(manifest["sections"][0]["start"]["capture_id"], "loc-0010-v1")
            self.assertEqual(manifest["sections"][1]["start"]["capture_id"], "loc-0010-v2")
            self.assertIn("PART ONE", files[manifest["sections"][0]["file"]])
            self.assertNotIn("Chapter body", files[manifest["sections"][0]["file"]])
            self.assertIn("Chapter body", files[manifest["sections"][1]["file"]])

    def test_same_capture_boundary_partitions_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book_dir = self.make_book(
                root,
                captures=[
                    {
                        "capture_id": "page-0001-v1",
                        "page": 1,
                        "text": "# Introduction\n\nIntro body.\n\n# Chapter One\n\nChapter body.",
                    }
                ],
                sections=[
                    {
                        "seq": 1,
                        "title": "Introduction",
                        "type": "Introduction",
                        "start_marker": "Page 1 of 30",
                        "end_marker": "Page 1 of 30",
                        "completeness": "complete",
                    },
                    {
                        "seq": 2,
                        "title": "Chapter One",
                        "type": "Chapter",
                        "start_marker": "Page 1 of 30",
                        "end_marker": "Page 1 of 30",
                        "completeness": "complete",
                    },
                ],
            )

            manifest, files = build_sections.build_section_outputs(
                asin="TESTASIN",
                book_dir=book_dir,
                chapter_map_path=book_dir / "analysis" / "chapter-map.md",
            )
            introduction = files[manifest["sections"][0]["file"]]
            chapter = files[manifest["sections"][1]["file"]]
            self.assertIn("Intro body", introduction)
            self.assertNotIn("Chapter body", introduction)
            self.assertIn("Chapter body", chapter)
            self.assertNotIn("Intro body", chapter)
            self.assertEqual(
                manifest["sections"][0]["end_exclusive"], manifest["sections"][1]["start"]
            )

    def test_unavailable_section_and_stale_source_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book_dir = self.make_book(
                root,
                captures=[
                    {
                        "capture_id": "page-0001-v1",
                        "page": 1,
                        "text": "# Title Page\n\nVisible title.",
                    }
                ],
                sections=[
                    {
                        "seq": 1,
                        "title": "Cover",
                        "type": "Front matter",
                        "start_marker": "Location 1 of 300 (TOC only)",
                        "end_marker": "Location 1 of 300 (TOC only)",
                        "completeness": "uncertain",
                    },
                    {
                        "seq": 2,
                        "title": "Title Page",
                        "type": "Front matter",
                        "start_marker": "Page 1 of 30",
                        "end_marker": "Page 1 of 30",
                        "completeness": "complete",
                    },
                ],
            )
            manifest, files = build_sections.build_section_outputs(
                asin="TESTASIN",
                book_dir=book_dir,
                chapter_map_path=book_dir / "analysis" / "chapter-map.md",
            )
            transcripts_dir = book_dir / "transcripts"
            for relative, content in files.items():
                build_sections.write_text(transcripts_dir / relative, content)
            manifest_path = transcripts_dir / "sections.json"
            build_sections.write_json(manifest_path, manifest)

            self.assertEqual(manifest["sections"][0]["status"], "unavailable")
            self.assertEqual(build_sections.validate_manifest(book_dir, manifest_path), [])

            (transcripts_dir / "book.md").write_text("changed", encoding="utf-8")
            errors = build_sections.validate_manifest(book_dir, manifest_path)
            self.assertTrue(any("Stale source hash" in error for error in errors))

    def test_reviewed_capture_override_resolves_untitled_shared_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book_dir = self.make_book(
                root,
                captures=[
                    {
                        "capture_id": "page-0004-v1",
                        "page": 4,
                        "text": "# Cheatsheet\n\nPromotional tail.",
                    },
                    {
                        "capture_id": "page-0004-v2",
                        "page": 4,
                        "text": '"What is comedy?"\n— Stan Laurel',
                    },
                ],
                sections=[
                    {
                        "seq": 1,
                        "title": "Cheatsheet",
                        "type": "Front matter",
                        "start_marker": "Page 4 of 30",
                        "end_marker": "Page 4 of 30",
                        "completeness": "complete",
                    },
                    {
                        "seq": 2,
                        "title": "Epigraph",
                        "type": "Front matter",
                        "start_marker": "Page 4 of 30",
                        "end_marker": "Page 4 of 30",
                        "completeness": "complete",
                    },
                ],
            )
            overrides_path = book_dir / "analysis" / "section-boundaries.json"
            build_sections.write_json(
                overrides_path,
                {
                    "schema_version": 1,
                    "sections": {"2": {"capture_id": "page-0004-v2"}},
                },
            )

            manifest, files = build_sections.build_section_outputs(
                asin="TESTASIN",
                book_dir=book_dir,
                chapter_map_path=book_dir / "analysis" / "chapter-map.md",
                overrides_path=overrides_path,
            )
            epigraph = manifest["sections"][1]
            self.assertEqual(epigraph["resolution"]["method"], "override_capture")
            self.assertEqual(epigraph["start"]["capture_id"], "page-0004-v2")
            self.assertNotIn("Promotional tail", files[epigraph["file"]])
            self.assertIn("Stan Laurel", files[epigraph["file"]])

    def test_marker_mismatch_override_requires_and_records_reason(self) -> None:
        captures = [
            build_sections.Capture(
                index=0,
                capture_id="loc-0100-v1",
                label="Location 100 of 300",
                marker_kind="Location",
                marker_value=100,
                transcript_ref="canonical/loc-0100-v1.json",
                text="Previous chapter conclusion.",
                status="completed",
            ),
            build_sections.Capture(
                index=1,
                capture_id="loc-0110-v1",
                label="Location 110 of 300",
                marker_kind="Location",
                marker_value=110,
                transcript_ref="canonical/loc-0110-v1.json",
                text="# New Chapter\n\nBody.",
                status="completed",
            ),
        ]
        entry = {
            "seq": 2,
            "title": "New Chapter",
            "start_marker": "Location 100 of 300",
        }
        with self.assertRaisesRegex(ValueError, "allow_marker_mismatch"):
            build_sections.resolve_start(
                entry,
                captures,
                build_sections.Anchor(0, 0),
                {"capture_id": "loc-0110-v1"},
            )

        resolution = build_sections.resolve_start(
            entry,
            captures,
            build_sections.Anchor(0, 0),
            {
                "capture_id": "loc-0110-v1",
                "allow_marker_mismatch": True,
                "reason": "The first visible heading is in the next capture.",
            },
        )
        self.assertEqual(resolution.anchor, build_sections.Anchor(1, 0))
        self.assertEqual(
            resolution.review_note, "The first visible heading is in the next capture."
        )


if __name__ == "__main__":
    unittest.main()
