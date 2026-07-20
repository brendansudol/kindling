from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import export_book_analyses


class ExportBookAnalysesTests(unittest.TestCase):
    def make_book(
        self,
        root: Path,
        asin: str,
        *,
        map_title: str,
        metadata_title: str | None = None,
    ) -> Path:
        book_dir = root / "books" / asin
        analysis_dir = book_dir / "analysis"
        chapters_dir = analysis_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        (book_dir / "metadata.json").write_text(
            json.dumps({"asin": asin, "title": metadata_title}), encoding="utf-8"
        )
        (analysis_dir / "chapter-map.md").write_text(
            f"# Chapter Map — *{map_title}*\n", encoding="utf-8"
        )
        (analysis_dir / "book-synthesis.md").write_text(
            f"# *{map_title}* — Whole-Book Synthesis\n", encoding="utf-8"
        )
        (analysis_dir / "summary-audit.md").write_text(
            "# Audit\n\n## Final Verdict\n\nReady\n", encoding="utf-8"
        )
        (analysis_dir / "section-boundaries.json").write_text("{}\n", encoding="utf-8")
        (chapters_dir / "01-opening.md").write_text("# Opening\n", encoding="utf-8")
        return book_dir

    def write_allowlist(self, root: Path, *asins: str) -> Path:
        path = root / "config" / "book-analysis-allowlist.txt"
        path.parent.mkdir(parents=True)
        path.write_text("# allowed\n" + "\n".join(asins) + "\n", encoding="utf-8")
        return path

    def test_metadata_title_is_preferred_and_slugged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = self.make_book(
                root,
                "BOOK1",
                map_title="Map Title",
                metadata_title="You're Funny & Useful!",
            )
            self.assertEqual(export_book_analyses.resolve_title(book), "You're Funny & Useful!")
            self.assertEqual(
                export_book_analyses.slugify_title("You're Funny & Useful!"),
                "youre-funny-and-useful",
            )

    def test_chapter_map_title_fallback_builds_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_book(root, "BOOK1", map_title="The Map Title")
            allowlist = self.write_allowlist(root, "BOOK1")
            expected, books = export_book_analyses.build_export(root, allowlist)

            self.assertEqual(books[0].title, "The Map Title")
            self.assertIn(Path("the-map-title/book-synthesis.md"), expected)
            manifest = json.loads(expected[Path("manifest.json")])
            self.assertEqual(manifest["book_count"], 1)
            self.assertEqual(manifest["books"][0]["chapter_summary_count"], 1)
            self.assertEqual(manifest["books"][0]["audit_verdict"], "Ready")
            self.assertNotIn(Path("the-map-title/summary-audit.md"), expected)
            self.assertNotIn(Path("the-map-title/section-boundaries.json"), expected)

    def test_non_ready_audit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = self.make_book(root, "BOOK1", map_title="Test Book")
            (book / "analysis" / "summary-audit.md").write_text(
                "# Audit\n\n## Final Verdict\n\nRequires substantial revision\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not approved"):
                export_book_analyses.load_export_books(root, ["BOOK1"])

    def test_write_check_and_stale_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = self.make_book(root, "BOOK1", map_title="Test Book")
            allowlist = self.write_allowlist(root, "BOOK1")
            expected, _ = export_book_analyses.build_export(root, allowlist)
            output = root / "shared-book-analyses"

            export_book_analyses.write_export(output, expected, prune=False)
            self.assertEqual(export_book_analyses.check_export(output, expected), [])

            (book / "analysis" / "book-synthesis.md").write_text(
                "# Changed synthesis\n", encoding="utf-8"
            )
            changed_expected, _ = export_book_analyses.build_export(root, allowlist)
            errors = export_book_analyses.check_export(output, changed_expected)
            self.assertTrue(any("Stale or modified" in error for error in errors))

    def test_unexpected_file_requires_explicit_prune(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_book(root, "BOOK1", map_title="Test Book")
            allowlist = self.write_allowlist(root, "BOOK1")
            expected, _ = export_book_analyses.build_export(root, allowlist)
            output = root / "shared-book-analyses"
            output.mkdir()
            extra = output / "not-allow-listed.md"
            extra.write_text("extra", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "--prune"):
                export_book_analyses.write_export(output, expected, prune=False)
            self.assertTrue(extra.exists())

            export_book_analyses.write_export(output, expected, prune=True)
            self.assertFalse(extra.exists())
            self.assertEqual(export_book_analyses.check_export(output, expected), [])

    def test_slug_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_book(root, "BOOK1", map_title="Same Title")
            self.make_book(root, "BOOK2", map_title="Same Title")
            with self.assertRaisesRegex(ValueError, "slug collision"):
                export_book_analyses.load_export_books(root, ["BOOK1", "BOOK2"])


if __name__ == "__main__":
    unittest.main()
