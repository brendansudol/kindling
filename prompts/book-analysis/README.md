# Book analysis prompt workflow

These prompts turn an extracted book transcript into source-grounded chapter summaries
and a whole-book synthesis. Run them in numerical order. The staged workflow is
deliberate: it preserves chapter detail and makes the final synthesis less likely to
amplify an early summary error.

## Inputs

For a book at `books/<asin>/`, use:

- `toc.json` to establish chapter boundaries.
- `transcripts/book.md` as the primary source.
- `transcripts/review.json` to identify OCR uncertainty.
- `metadata.json` for title, author, and other book context when available.

Treat page and location headings as source locators, not content headings. Repeated
page markers can represent distinct screenshots of the same printed page and should
not automatically be discarded as duplicates.

## Workflow

1. Use `01-chapter-map.md` once to verify the book structure. Its JSON boundary block
   can drive scripted slicing of `transcripts/book.md` into per-chapter files.
2. Use `02-chapter-summary.md` separately for every section the map's summarization
   plan marks as substantive. For `book_context`, supply the map's structural overview
   plus the `Chapter in Brief` sections of earlier summaries; full previous summaries
   are usually too long and rarely necessary.
3. Use `03-book-synthesis.md` after all chapter summaries exist. Including the raw
   transcript improves verification but is optional when context is constrained.
4. Use `04-accuracy-audit.md` with a fresh context to check the synthesis. When the
   full transcript will not fit, supply excerpts keyed to the citations under review.
5. Apply the audit's corrections to the synthesis and re-check the corrected passages.
   A full re-audit is only warranted after substantial revision.

## Recommended output layout

```text
books/<asin>/analysis/
├── chapter-map.md
├── chapters/
│   ├── 01-<slug>.md
│   ├── 02-<slug>.md
│   └── ...
├── book-synthesis.md
└── summary-audit.md
```

Every substantive claim should be traceable to a visible page or location marker.
When the source is incomplete or ambiguous, the analysis should say so rather than
guessing.
