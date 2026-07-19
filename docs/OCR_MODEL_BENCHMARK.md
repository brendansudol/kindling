# Vision OCR model benchmark

Run date: 2026-07-18

## Recommendation

Use **Gemini 3.5 Flash with high media resolution, minimal thinking, the literal-copy
prompt, and one OCR pass** for the full transcripts.

It produced the best overall balance in this experiment: the highest blind-judge
weighted score, the best structure and emphasis preservation, and 9 of 20 best-model
votes. The projected standard API cost for all 2,367 captures currently listed in
`TODO.md` is **$14.45** with the longer literal prompt. That is inexpensive enough that
saving roughly $12 with Flash-Lite is not worth losing headings, italics, paragraph
boundaries, and workbook layout in the canonical transcripts.

Use Gemini 3.1 Flash-Lite instead when the output is primarily a plain-text search
corpus and formatting fidelity does not matter. Avoid OpenAI Nano as the sole
transcriber; it was cheap, but it made several real character/word errors. Terra's
higher cost did not buy higher quality here.

Do not keep the current mandatory two-pass OCR-plus-QA design. A second generative
pass doubles work and can reinforce a plausible semantic correction rather than catch
it. Prefer one strong pass, deterministic validation, and targeted review or
cross-provider adjudication only on suspicious pages.

### If quality matters more than cost

The best single-model choice remains Gemini 3.5 Flash. Paying more for Terra did not
improve the benchmark results, and the frontier judge models were not evaluated as
direct OCR candidates in the original matrix.

For a higher-assurance pipeline, change the verification strategy rather than merely
swapping in a more expensive primary model:

1. Generate the canonical draft with Gemini 3.5 Flash.
2. Generate an independent transcription with GPT-5.6 Sol at original image detail.
   Do not show it the Flash draft, which would anchor the verification pass.
3. Compare typography/Markdown-normalized content and adjudicate substantive
   disagreements against the image.
4. Manually review unresolved disagreements and a random sample.

Based on the measured page token volumes and published prices, Flash plus a full Sol
transcription would be roughly $60 for the current 2,367 captures before adjudication.
That is a reasonable optional archival-quality mode, but it is not justified for the
initial full-book run until Sol has also been benchmarked as an OCR candidate.

The chosen production policy is therefore the simpler original recommendation:
single-pass Gemini 3.5 Flash, followed by deterministic checks and sample review.

## Experiment design

The [sample manifest](../benchmarks/ocr_samples.json) contains ten interior captures:
five from `B00ZF6H8MC` and five from `B0847KS4ZN`. The pages include dense prose,
dialogue, headings, italics, bold text, block quotes, numbers, accented words, blank
workbook lines, and more complex workbook layouts.

Each candidate received the same image and structured-output OCR task. Calls were
single-pass so model quality and price could be compared directly. Every call stored
the normalized output, full provider response, token usage, latency, retry count, and
estimated cost under the ignored `books/ocr-benchmarks/` directory.

Five baseline configurations were tested, for 50 successful OCR calls:

| ID | Model | Image detail | Reasoning |
|---|---|---:|---:|
| `openai-nano-high-none` | GPT-5.4 Nano | high | none |
| `openai-luna-high-none` | GPT-5.6 Luna | high | none |
| `openai-terra-original-low` | GPT-5.6 Terra | original | low |
| `gemini-flash-lite-high-minimal` | Gemini 3.1 Flash-Lite | high | minimal |
| `gemini-flash-high-minimal` | Gemini 3.5 Flash | high | minimal |

Two additional Gemini runs added explicit instructions never to correct odd spelling,
grammar, acronyms, or page-edge punctuation. Those 20 calls tested prompt sensitivity.

Quality was evaluated three ways:

1. Manual inspection against the page image, especially every candidate disagreement.
2. Character agreement after normalizing typography, Markdown, and whitespace. This
   detects outliers but is not ground truth.
3. Blind grading by GPT-5.6 Sol and Gemini 3.1 Pro Preview. Candidate names were
   randomized per page. Each judge scored text accuracy and structure separately;
   the reported weighted score is 85% text and 15% structure.

The two model judges completed 20 comparisons. Their judgments are useful but not
authoritative: both made small visual mistakes, and Gemini Pro specifically misread
one `crop it` example that manual inspection resolved.

## Results

These are measured single-pass results. Full-corpus projections multiply the observed
mean per-page cost by the 2,367 current TODO captures. Batch prices are published-rate
projections; the repository does not yet submit batch jobs.

| Configuration | Mean latency | Mean standard cost/page | 2,367 pages standard | 2,367 pages batch | Judge text | Judge structure | Weighted | Best votes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Gemini 3.5 Flash, baseline | 2.96 s | $0.005770 | $13.66 | $6.83 | 97.95 | 93.30 | **97.252** | **9/20** |
| OpenAI GPT-5.6 Luna | **2.30 s** | $0.003623 | $8.57 | $4.29 | 97.85 | 88.60 | 96.463 | 6/20 |
| OpenAI GPT-5.6 Terra | 3.27 s | $0.009327 | $22.08 | $11.04 | 97.40 | 88.05 | 95.997 | 3/20 |
| Gemini 3.1 Flash-Lite | 3.26 s | $0.000947 | $2.24 | $1.12 | 97.80 | 82.00 | 95.430 | 0/20 |
| OpenAI GPT-5.4 Nano | 3.74 s | **$0.000828** | **$1.96** | **$0.98** | 97.10 | 82.55 | 94.917 | 2/20 |

The literal prompt increased input tokens slightly:

| Configuration | Successful calls | Mean latency | Mean standard cost/page | 2,367 pages standard | 2,367 pages batch |
|---|---:|---:|---:|---:|---:|
| Gemini 3.5 Flash, literal | 10/10 | 15.06 s | $0.006104 | $14.45 | $7.22 |
| Gemini 3.1 Flash-Lite, literal | 10/10 | 3.11 s | $0.000983 | $2.33 | $1.16 |

Flash's literal run encountered a temporary high-demand period: four pages needed at
least one retry, while the baseline run needed none. The 15.06-second mean should not
be interpreted as an effect of the prompt from this small, non-simultaneous sample.
Flash-Lite completed all 20 of its baseline and literal calls without a retry.

Normalized content agreement across the five baseline models was:

| Configuration | Mean agreement with the other candidates |
|---|---:|
| Gemini 3.1 Flash-Lite | 99.789% |
| OpenAI GPT-5.6 Luna | 99.777% |
| Gemini 3.5 Flash | 99.771% |
| OpenAI GPT-5.6 Terra | 99.685% |
| OpenAI GPT-5.4 Nano | 99.366% |

Agreement is deliberately not treated as accuracy. On one page, four models agreed on
the plausible phrase `drop it`, but the image actually says `crop it`; Nano was the
only candidate to reproduce that word correctly.

## Qualitative findings

- Gemini 3.5 Flash consistently preserved Markdown structure best: headings, bold,
  italics, pull quotes, paragraph boundaries, and workbook sections.
- Gemini 3.1 Flash-Lite's visible words were almost identical to Flash's on this set,
  but it flattened interview questions, paragraph breaks, italics, and workbook layout.
- OpenAI Luna was a credible runner-up, but it made at least one semantic normalization
  (`disassociating` to `dissociating`) and preserved less structure than Flash.
- OpenAI Nano changed `finishing off` to `finishing of`, `Zeilen` to `Zeiten`,
  `transcripted` to `transcribed`, and the printed `LBGTQ` to `LGBTQ`. It also omitted
  visible workbook answer lines.
- OpenAI Terra omitted the visible `Comedy Bible Workbook > Setlists` header from one
  page. Its `original` image detail and higher price did not create a quality advantage.
- The explicit literal-copy addendum did not change Flash-Lite's normalized content on
  any of the ten pages and did not correct Flash's `crop`/`drop` mistake. Prompt wording
  alone is not a sufficient safeguard against plausible semantic correction.

## Proposed production policy

1. Run Gemini 3.5 Flash once per capture with high media resolution and minimal
   thinking. Use the literal-copy addendum even though it is not a complete safeguard.
2. Preserve usage, latency, retry count, raw response metadata, and source-image
   fingerprint in every canonical result so runs remain auditable and resumable.
3. Flag empty/short results, explicit uncertainties, extreme length changes between
   adjacent pages, clipped boundaries, and structurally complex workbook pages.
4. Audit a sample manually. For higher-assurance archival output, also run a cheap
   cross-provider OCR and escalate only substantive normalized disagreements to a
   stronger vision judge. Do not accept majority agreement as ground truth.
5. Keep model/provider and prompt version in the cache key. A source image, model,
   image-detail setting, or prompt change must invalidate the corresponding output.

For the current 2,367 pages, the recommended standard-mode OCR cost is small enough
that batch integration can wait. At much larger scale, route plain prose to Flash-Lite
and reserve Flash for pages with visible formatting or layout complexity.

## Production sample follow-up

After selecting Gemini 3.5 Flash, the production transcriber was changed to use one
high-resolution, minimal-thinking pass. A 50-capture prefix of `B07NYBL322` was then
run through the new path.

The first sample exposed two useful edge cases:

- Superscript ordinal suffixes such as `1st` and `2nd` were occasionally returned as
  malformed Unicode glyphs. The transcriber now requests ASCII suffixes and applies a
  deterministic ASCII normalization as a safeguard.
- A diagram transcription repeated its setup line after converting the layout to a
  Markdown table. The literal prompt now explicitly requires each diagram label and
  passage exactly once, and a duplicate-line quality check catches recurrences.

The corrected `literal-v2` sample results were:

| Metric | Result |
|---|---:|
| Completed captures | 50/50 |
| Failed captures | 0 |
| Captures needing an API retry | 2 |
| Mean confidence | 0.993 |
| Mean latency | 2.45 s |
| Approximate p95 latency | 6.14 s |
| Final canonical-set cost | $0.243 |
| Flagged for review | 2 |
| Empty outputs | 0 |
| Duplicate-line flags after correction | 0 |
| Suspicious-Unicode flags after correction | 0 |

The two remaining review entries were inspected against their images. One is a real
printed typo (`map o which`) that the model correctly preserved and flagged instead of
silently fixing. The other is a complex diagram page with accurate visible text but a
lower-confidence Markdown layout reconstruction. The machine-readable review queue is
written to `books/<asin>/transcripts/review.json` on every run.

### Full-corpus operational follow-up

The full run exposed a provider behavior that the ten-page benchmark did not: Gemini
3.5 Flash returned an empty candidate with finish reason `RECITATION` on a small set of
otherwise ordinary prose pages. Repeating the same request, reducing concurrency, and
waiting for load to subside did not change those page-specific responses.

The production transcriber therefore keeps Gemini 3.5 Flash as the primary model and
uses the benchmark runner-up, GPT-5.6 Luna at high image detail with no reasoning, only
for that explicit finish reason. It does not fall back on ordinary transient errors,
which continue through the bounded Gemini retry policy. Fallback records retain the
Gemini refusal metadata and usage alongside the OpenAI result, and manifests count the
fallback pages separately. `--no-fallback` restores strict Gemini-only behavior.

One page containing an extended quotation was filtered by both cloud providers. The
final fallback is therefore local macOS Vision OCR through `ocrmac`; it has no API
cost, is used only after the two explicit provider filter signals, and is capped at a
0.95 confidence so it always enters the review queue. This route preserves corpus
coverage but not Markdown emphasis, so its provider and engine are recorded in the
canonical page result and counted separately in the manifest.

A broader manual spot check also found one unflagged character-level error:
`Neuro-Linguistic` was returned as `Neuro-Lingustic`. This is an important limitation
of the deterministic review queue: it catches operational and structural warning
signals, but it cannot prove word-for-word accuracy. Random image-to-text auditing is
still required when literal fidelity matters.

Because the sample was regenerated once after tightening the prompt, total exploratory
sample spend was about $0.49; the final 50 canonical outputs account for about $0.24.

## Reproducing the benchmark

Install dependencies and run the baseline matrix:

```bash
pip install -r requirements.txt
python scripts/benchmark_ocr.py \
  --configs openai-nano-high-none,openai-luna-high-none,openai-terra-original-low,gemini-flash-lite-high-minimal,gemini-flash-high-minimal \
  --output-dir books/ocr-benchmarks/model-comparison
```

Measure agreement and run the blinded quality evaluation:

```bash
python scripts/analyze_ocr_benchmark.py books/ocr-benchmarks/model-comparison
python scripts/judge_ocr_benchmark.py books/ocr-benchmarks/model-comparison
```

Run the literal-prompt comparison:

```bash
python scripts/benchmark_ocr.py \
  --configs gemini-flash-lite-high-minimal-literal,gemini-flash-high-minimal-literal \
  --output-dir books/ocr-benchmarks/literal-prompt
```

## Pricing and model references

Prices in the harness are snapshots of the published standard and batch rates as of
the run date. Recheck them before a large job:

- [OpenAI model selection](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI image inputs and detail levels](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [Gemini 3.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash)
- [Gemini 3.1 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)
- [Gemini image understanding and media resolution](https://ai.google.dev/gemini-api/docs/image-understanding)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
