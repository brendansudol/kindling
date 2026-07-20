# Book analysis prompt workflow

These prompts turn an extracted book transcript into source-grounded chapter summaries
and a whole-book synthesis. Run them in numerical order. The staged workflow is
deliberate: it preserves chapter detail and makes the final synthesis less likely to
amplify an early summary error.

## Inputs

For a book at `books/<asin>/`, use:

- `toc.json` to establish chapter boundaries.
- `transcripts/book.md` as the canonical whole-book source.
- `transcripts/review.json` to identify OCR uncertainty.
- `transcripts/sections.json` and `transcripts/sections/*.md` as derived, capture-aware
  inputs after the chapter map has been verified.
- `metadata.json` for title, author, and other book context when available.

Treat page and location headings as source locators, not content headings. Repeated
page markers can represent distinct screenshots of the same printed page and should
not automatically be discarded as duplicates.

## Workflow

1. Use `01-chapter-map.md` once to verify the book structure. Its JSON boundary block
   drives `scripts/build_sections.py`. Save its machine-readable summarization plan as
   `analysis/summary-plan.json`.
2. Generate and validate the derived transcript views before summarizing:

   ```bash
   python scripts/build_sections.py --asin <asin> --fail-on-warnings
   python scripts/build_sections.py --asin <asin> --check
   ```

   Do not edit generated section files. Resolve ambiguous repeated-marker boundaries
   through reviewed `analysis/section-boundaries.json` overrides and regenerate.
3. Use `02-chapter-summary.md` separately for every output assigned by
   `summary-plan.json`. Supply the corresponding generated section file or files as
   `chapter_text`. For `book_context`, supply the map's structural overview plus the
   `Chapter in Brief` sections of earlier summaries; full previous summaries are
   usually too long and rarely necessary.
4. Use `03-book-synthesis.md` after all chapter summaries exist. Relevant generated
   sections improve verification without requiring the entire book in context;
   `book.md` remains authoritative for boundary disputes and cross-section claims.
5. Use `04-accuracy-audit.md` with a fresh context to check the synthesis. Supply the
   generated sections corresponding to the claims and citations under review, using
   `book.md` when broader context is necessary.
6. Apply the audit's corrections to the synthesis and re-check the corrected passages.
   A full re-audit is only warranted after substantial revision.

## Recommended output layout

```text
books/<asin>/analysis/
├── chapter-map.md
├── summary-plan.json
├── chapters/
│   ├── 01-<slug>.md
│   ├── 02-<slug>.md
│   └── ...
├── book-synthesis.md
└── summary-audit.md
```

The section manifest and files live beside the canonical transcript:

```text
books/<asin>/transcripts/
├── book.md
├── sections.json
└── sections/
    ├── 01-<slug>.md
    └── ...
```

Generated section headers, capture IDs, character offsets, and HTML comments are
provenance metadata, not book content. Published analysis should continue to cite the
visible page or location markers.

Every substantive claim should be traceable to a visible page or location marker.
When the source is incomplete or ambiguous, the analysis should say so rather than
guessing.
