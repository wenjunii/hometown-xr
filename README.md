# Hometown XR Common Crawl Extractor

A resumable multilingual pipeline for finding first-person stories about home,
hometown, childhood, roots, migration, and belonging in Common Crawl data.

Canonical repository: [wenjunii/hometown-xr](https://github.com/wenjunii/hometown-xr)

## Models

The extractor uses two local machine-learning models:

- `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, pinned to
  revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`, scores text against the
  English and native-language concepts in `concepts.py`.
- FastText `lid.176.bin` identifies language and records prediction confidence.

Neither model is a generative LLM. The crawler does not call OpenAI, Gemini,
Anthropic, or another hosted text-generation API.

Before either model sees text, `ftfy` repairs mojibake, decodes HTML entities,
and normalizes Unicode to NFC. Schema-5 output keeps `raw_paragraph` whenever
that cleanup changes the source and adds a bounded, role-labeled source-context
window, so matching improves without losing provenance.

A versioned local SQLite cache stores sentence embeddings, raw semantic scores,
and raw language predictions by normalized text hash. Model revision, precision,
and concept-anchor version are part of each cache namespace, so stale results
cannot cross a model change. `data/cache/` remains local to each workstation.

## Workstation Safety

The RTX 3080, RTX 4090, and RTX 5090 PCs share one Git checkpoint. Run the
crawler on only one PC at a time.

Git LFS stores `data/checkpoints/progress.db.gz`, a deterministic compressed
checkpoint. `data/progress.db` is restored locally by setup/handoff and is
never copied while live.

The Git repository contains all durable state needed to recreate or resume the
project on another PC: source, tests, documentation, the compressed database
checkpoint, committed output and manifests, source-context story fragments and
exports, bounded evaluation replay, annotations, and run history. Virtual
environments, downloaded models, caches, live SQLite files, metrics, derived
Parquet datasets, hardware overrides, and credential-like files remain local
because they are unsafe to copy or can be recreated deterministically.
`scripts\checkpoint.ps1` scans staged filenames and contents before every
commit. Credential-like files are unstaged and the checkpoint stops, even if a
local ignore rule is accidentally removed.

Run the same guard at any time:

```powershell
.\scripts\security-check.ps1
```

The default scans tracked and non-ignored worktree files without printing
secret values. Use `-Scope staged` to inspect exactly what is staged or
`-Scope tracked` to inspect repository files. GitHub Actions repeats the tracked
scan on every push and pull request. Keep credentials outside this repository;
the scanner is a final guard, not a password manager.

Before switching machines:

1. Press `Ctrl+C` once and wait for the final summary.
2. Confirm `data/.crawler.lock` is gone.
3. Run `scripts\checkpoint.ps1` on the old PC to verify, compact, commit, and push.
4. Run `scripts\handoff.ps1 -Direction pull -Profile <GPU>` on the new PC.
5. Resume only after the receive command passes its environment and output checks.

See [HANDOFF.md](HANDOFF.md) for the exact commands.

The normal 3080 receive/run/send cycle is:

```powershell
.\scripts\handoff.ps1 -Direction pull -Profile 3080
.\scripts\run.ps1 -Profile 3080 run --all
# After stopping the crawler cleanly:
.\scripts\checkpoint.ps1 -Message "checkpoint: hand off crawler state"
```

Use `4090` or `5090` in the first two commands on the corresponding PC. The
receive command
requires a clean worktree, performs a fast-forward-only Git pull plus Git LFS
pull, then runs the strict full project-health check. The send command works
from the current non-detached branch, checks that the matching remote branch is
not ahead before checkpointing, and confirms the pushed commit against that
remote branch. Both PCs must use the same branch. Because `main` is protected,
use a shared working branch until its pull request is approved and merged.

## Architecture

One run has seven lightweight CPU parser processes and one inference owner:

```text
WET/ARC sources
    -> bounded CPU download/parse workers
    -> versioned entity/encoding normalization
    -> eligible-paragraph funnel counters
    -> multilingual keyword prefilter
       -> deterministic pre-keyword shadow sample
    -> bounded candidate queue
    -> hard-boilerplate prefilter
    -> versioned score/embedding cache
    -> one shared sentence-transformer on the GPU
    -> FastText language detection
    -> precise seed plus bounded source-context expansion
    -> source-scoped staged output
    -> atomic shard + manifest commit
    -> filter-signed SQLite checkpoint completion
```

The semantic model is loaded once, not once per worker. The process pool and
GPU service are reused across crawls. Queue backpressure keeps RAM bounded when
the CPU workers are faster than inference. Repeated text bypasses model encoding,
and hard navigation, policy, form, lyric, and repetitive-text negatives are
rejected before GPU work without changing which records can pass.

Each source has a SQLite lease. Interrupted sources return to `pending`, hard
crashes are recovered after the lease timeout, and failed sources retry with
exponential backoff plus deterministic jitter. Repeated 429/503 pressure opens
a parent-level circuit cooldown shared by all parser submissions. The parser
pool is recycled after a bounded amount of work or excessive worker RAM, while
attempt-exhausted sources remain quarantined for operator review. Output becomes
visible only after an entire source is successfully parsed and filtered.

## Quick Start

Requirements:

- Windows 10 or 11
- Python 3.10, 3.11, or 3.12
- Git and Git LFS
- NVIDIA driver compatible with the profile's PyTorch CUDA runtime
- RTX 3080, RTX 4090, or RTX 5090 GPU

First-time setup on this RTX 3080 PC:

```powershell
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080
```

Use `-Profile 4090` or `-Profile 5090` on the corresponding PC. The 3080 and
4090 retain the exact CUDA 12.1 PyTorch environment in `requirements-lock.txt`.
The Blackwell-generation 5090 uses `requirements-lock-5090.txt`, which pins
stable PyTorch 2.12.1 with its CUDA 13.0 runtime. Add `-Tune` to run a quick
local hardware benchmark or `-Dev` to install the test toolchain.

PyTorch and Transformers are excluded from automatic Dependabot version bumps.
They are updated manually because the CUDA 12.1 and CUDA 13.0 locks must stay
separate, and the pinned sentence-transformer constrains compatible
Transformers releases. CI tests these profile relationships on every change.
The current model stack has upstream security advisories and is covered by the
dated migration policy in `.github/dependency-policy.json`; it is not treated as
silently clean. CI runs `pip-audit`, rejects unlisted vulnerable packages, and
fails when the exception expires. Complete the tracked model comparison, human
evaluation minimums, and all three workstation benchmarks before upgrading the
shared locks.

Verify the environment and checkpoint:

```powershell
.\scripts\health.ps1 -Profile 3080 -Full -Strict
```

Start or resume one crawl:

```powershell
.\scripts\run.ps1 -Profile 3080 run --crawl CC-MAIN-2026-12
```

Resume every available crawl:

```powershell
.\scripts\run.ps1 -Profile 3080 run --all
```

The `4090\main.py` and `5090\main.py` commands are compatibility launchers.
They invoke the same root implementation and shared `data/` checkpoint.

## Hardware Profiles

These are the conservative settings tracked in Git:

| Profile | CPU workers | Candidate batch | Inference batch | Encoding batch | Precision |
| --- | ---: | ---: | ---: | ---: | --- |
| `3080` | 7 | 100 | 800 | 128 | FP32 |
| `4090` | 7 | 150 | 1600 | 256 | FP32 |
| `5090` | 7 | 200 | 2400 | 512 | FP32 |

`candidate_batch_size` controls worker-to-parent messages.
`inference_batch_size` combines candidates from multiple sources before one
semantic call. `encoding_batch_size` is the sentence-transformer CUDA batch.

Audit FP16 behavior on a fixed synthetic workload and write an ignored,
machine-local override:

```powershell
.\scripts\benchmark.ps1 -Profile 3080
```

Use `-Quick` for a shorter pass. The benchmark measures FP32 and FP16, checks
maximum score drift and concept agreement on a fixed audit set, and selects
FP16 only when drift stays within the safety bound and throughput improves by
at least five percent. Results are written to
`data/hardware-profile.local.json`; that file is intentionally not synchronized
because all three workstations should keep their own measured settings.

Worker-count tuning uses the same completed Common Crawl sources for every
trial, with cache and evaluation sampling disabled and all output isolated:

```powershell
.\scripts\benchmark.ps1 -Profile 3080 -Real -Crawl CC-MAIN-2014-15 `
  -Sources 5 -WorkerCount 1,4,7
```

The command reports files/hour, MB/s, peak worker RAM, peak VRAM, failures,
pool restarts, and a normalized output digest. It changes no setting by default.
Add `-Apply` only after every source completes and every trial produces the
same normalized match set; then only this PC's ignored worker override changes.

One-run overrides are also available:

```powershell
python main.py run --profile 3080 --workers 7 `
  --candidate-batch-size 100 --inference-batch-size 800 --encoding-batch-size 128 `
  --precision fp32
```

CUDA OOMs automatically halve the active encoding batch and retry. After eight
stable batches, the configured size is restored. Use `--no-adaptive-batching`
or `--no-cache` only for diagnosis.

## Filtering

Each paragraph passes through:

1. Versioned HTML-entity, encoding, and Unicode normalization.
2. A multilingual keyword prefilter from `keywords.py`.
3. A behavior-preserving hard-boilerplate/narrative prefilter.
4. Cached or fresh sentence-transformer similarity against home concepts.
5. A multilingual first-person narrative filter.

CJK, Japanese, Korean, and Thai keywords use substring matching where word
boundaries are unreliable. FastText predictions below the confidence threshold
are stored under `unknown/` with the original confidence.
The narrative ruleset is regression-tested for every language represented in
the keyword catalog. Adjacent Cyrillic markers no longer hide one another, and
the per-form cap permits Vietnamese narratives to reach the configured threshold
without removing the anti-repetition bound.

Every live candidate can contribute to a deterministic local evaluation sample.
Low-rate coverage samples are marked `benchmark` and retain their inclusion
probabilities; decision-boundary samples are marked `tuning`. A deterministic
subset of source files also contributes a small reservoir of paragraphs rejected
before the keyword gate. Those shadow rows make missed-keyword recall visible
without sending the full corpus through the GPU. At checkpoint time, local
candidates merge into the bounded `data/evaluation/replay.jsonl.gz` reservoir
shared by all PCs. Replay compaction preserves representative rows and reserves
space for active-learning rows. Human labels are never synthesized.
An additional bounded two-percent probe retains up to 20 tuning examples per
non-English detected language, improving minority-language review coverage
without changing population-weighted benchmark odds.

Every run records a filter signature over text normalization, the model
revision, thresholds, paragraph bounds, narrative rules, keywords, and concept
anchors. New output, manifests, progress rows, and run history carry the
signature and run ID.

Run metrics now include the full paragraph funnel, accepted-versus-committed
counts, categorized failures, worker peak RAM, GPU peak VRAM, pool restarts and
planned recycles, source cooldowns, throughput, and ETA. `python main.py metrics`
prints a concise operational view; use `--full`, `--history`, or
`--compare-profiles` for deeper inspection.

## Commands

PowerShell launchers provide the recommended workstation workflow and can
resolve the project root regardless of the caller's current directory:

| Script | Purpose |
| --- | --- |
| `.\scripts\setup.ps1 -Profile 3080` | Create/update the runtime and run diagnostics |
| `.\scripts\setup.ps1 -Profile 3080 -Tune -Dev` | Also tune this PC and install development tools |
| `.\scripts\run.ps1 -Profile 3080 run --all` | Start or resume using the selected hardware profile |
| `.\scripts\health.ps1 -Profile 3080 -Full -Strict` | Check runtime, Git, checkpoint, dependencies, filters, evaluation, metrics, and output |
| `.\scripts\benchmark.ps1 -Profile 3080` | Audit FP16 safety and write this PC's local override |
| `.\scripts\benchmark.ps1 -Profile 3080 -Real -Sources 5 -WorkerCount 1,4,7` | Compare worker counts on identical isolated real sources |
| `.\scripts\handoff.ps1 -Direction pull -Profile 3080` | Fast-forward, pull LFS data, and verify the received checkpoint |
| `.\scripts\security-check.ps1` | Scan local Git candidates for credential paths and content |
| `.\scripts\filter-state.ps1` | Inspect current, stale, and unsigned completed work |
| `.\scripts\audit.ps1` | Plan a deterministic, isolated historical-source audit |
| `.\scripts\audit.ps1 -Action run -Profile 3080 -Apply` | Run the reviewed audit without changing historical state |
| `.\scripts\evaluation.ps1` | Show annotation balance and the next evaluation action |
| `.\scripts\evaluation.ps1 -Action plan` | Print balanced human-labeling queues without assigning labels |
| `.\scripts\evaluation.ps1 -Action serve -OpenBrowser` | Open the local browser annotation workbench |
| `.\scripts\evaluation.ps1 -Action multilingual` | Report language evidence, anchor gaps, and keyword misses |
| `.\scripts\evaluation.ps1 -Action annotate -Prediction rejected -Limit 25` | Review a focused batch interactively |
| `.\scripts\retry.ps1 -All -Category http_503 -Limit 25 -Apply` | Reset one bounded failure batch after a dry-run report |
| `.\scripts\stories.ps1 -Action plan -Limit 10` | Plan a bounded historical source-context backfill without downloading |
| `.\scripts\stories.ps1 -Action enrich -Limit 10 -Apply` | Reopen only selected matched source files and resume story expansion |
| `.\scripts\stories.ps1 -Action export` | Export story-length verbatim source passages |
| `.\scripts\stories.ps1 -Action export -IncludeShort` | Include short source context for diagnostics |
| `.\scripts\stories.ps1 -Action export -IncludeAnchorMismatches` | Include source passages that miss literal facets of a specific semantic reference |
| `.\scripts\refresh-results.ps1` | Dry-run current filters and rebuild the local canonical dataset |
| `.\scripts\model-validation.ps1 -Action capture -Profile 4090` | Capture an ignored model candidate on that GPU |
| `.\scripts\model-validation.ps1 -Action compare -Profile 4090` | Compare that candidate with the tracked baseline |
| `.\scripts\dependency-audit.ps1` | Validate profile pins and the dated vulnerability policy |
| `.\scripts\checkpoint.ps1 -Message "checkpoint: hand off crawler state"` | Verify, compact, commit, push, and confirm a checkpoint |
| `.\scripts\test.ps1` | Run tests, lint, and compilation checks |

The underlying Python CLI remains available directly:

| Command | Purpose |
| --- | --- |
| `python main.py run --crawl ID` | Start or resume one crawl |
| `python main.py run --all` | Process every known crawl |
| `python main.py run --all --strategy round-robin --chunk-size 100` | Rotate bounded chunks across old and new crawls |
| `python main.py run --all --strategy yield-aware --chunk-size 100` | Prioritize smoothed high-yield crawls while retaining exploration and coverage |
| `python main.py run --limit 5` | Process at most five ready sources globally |
| `python main.py status` | Show checkpoint progress |
| `python main.py health --profile 3080 --full --strict` | Fail on unsafe runtime, Git, database, dependency, or output state |
| `python main.py metrics` | Show concise latest rates, funnel, failures, resources, and ETA |
| `python main.py metrics --history --limit 20` | Show compact recent run history |
| `python main.py metrics --compare-profiles` | Compare aggregate workstation throughput/resources |
| `python main.py doctor --profile 3080` | Check Python, PyTorch, CUDA, and profile |
| `python main.py benchmark --profile 3080` | Benchmark and tune this PC |
| `python main.py benchmark --profile 3080 --real --sources 5 --worker-count 1 --worker-count 7` | Benchmark identical real sources without changing settings |
| `python main.py cache stats` | Inspect the local inference cache |
| `python main.py cache clear` | Rebuild the local inference cache from empty |
| `python main.py retry --all --category http_503 --limit 25` | Retry a deterministic bounded failure category |
| `python main.py failures` | Group failures into HTTP, connection, worker, inference, and output categories |
| `python main.py recover --minutes 10` | Release expired source leases |
| `python main.py verify-output` | Verify committed shard checksums |
| `python main.py checkpoint` | Verify and compact state for handoff |
| `python main.py filters status` | Compare completed work with the current filter signature |
| `python main.py filters stamp-current --audit-report PATH --yes` | Adopt only crawls proven equivalent by that audit report |
| `python main.py filters reset-stale --crawl ID --limit 100 --yes` | Queue a bounded stale-source recrawl |
| `python main.py database restore` | Restore the local SQLite DB from the shared archive |
| `python main.py database check` | Confirm local SQLite state matches the shared archive |
| `python main.py parquet --dedupe exact` | Build partitioned Parquet output |
| `python main.py stories status --limit 10` | Show completed and pending story-context source fragments |
| `python main.py stories enrich --limit 10 --yes` | Backfill a bounded, resumable batch from exact historical sources |
| `python main.py stories export` | Write story-length verbatim source passages |
| `python main.py stories export --include-short` | Include short context in diagnostic exports |
| `python main.py stories export --include-anchor-mismatches` | Include broad semantic matches that fail specific reference-fidelity checks |
| `python main.py audit plan --per-crawl 2` | Select matched and zero-match completed sources without changing state |
| `python main.py audit run --per-crawl 2 --profile 3080 --yes` | Run the selection in an isolated database/output tree |
| `python main.py evaluation status` | Show sample balance, labels, readiness, and the next action |
| `python main.py evaluation plan` | Build balanced human-labeling steps without synthesizing labels |
| `python main.py evaluation sample` | Build a real-text annotation sample |
| `python main.py evaluation annotate` | Label samples interactively |
| `python main.py evaluation annotate --split holdout --quick` | Label a balanced holdout queue with model categories accepted |
| `python main.py evaluation serve --open-browser` | Serve the protected local annotation workbench |
| `python main.py evaluation multilingual` | Write multilingual coverage and keyword-miss diagnostics |
| `python main.py evaluation undo --sample-id ID` | Restore the previous label and annotation metadata |
| `python main.py evaluation report` | Compute precision, recall, F1, and tuning |
| `python main.py evaluation replay` | Compact local decisions into the shared replay reservoir |
| `python main.py model-validation capture --profile 3080` | Capture the tracked semantic-output baseline |
| `python main.py model-validation compare --candidate PATH` | Enforce model-output regression limits |
| `python main.py reset` | Delete output, derivatives, and progress |

Use `recover --minutes 0` only after confirming no crawler is running.

## JSONL Output

Output is gzip-compressed JSON Lines grouped by detected language:

```text
data/
  progress.db                         # local restored working database
  checkpoints/
    progress.db.gz                    # synchronized Git LFS checkpoint
  models/
    lid.176.bin
  output/
    _manifest-catalog.jsonl.gz
    _manifests/
      <new-source-hash>.json
    en/
      <source-hash>_<source-name>.jsonl.gz
    zh/
    unknown/
  stories/
    _records/
      <source-hash>.jsonl.gz
  exports/
    stories.jsonl.gz
    stories_<language>.md
```

Schema version 5 records add bounded story context while retaining versioned
normalized text and the raw source paragraph whenever it changed. Schema-2,
schema-3, and schema-4 records remain supported and do not need rewriting:

```json
{
  "schema_version": 5,
  "record_id": "<sha256>",
  "content_fingerprint": "<sha256>",
  "crawl_id": "CC-MAIN-2026-12",
  "source_file": "crawl-data/.../example.warc.wet.gz",
  "run_id": "20260714T120000Z-ab12cd34",
  "filter_signature": "<sha256>",
  "url": "https://example.org/story",
  "warc_date": "2026-03-01T12:00:00Z",
  "language": "en",
  "language_confidence": 0.9821,
  "paragraph": "I remember the home where I grew up...",
  "raw_paragraph": "I remember the home where I grew up&amp;...",
  "document_id": "<sha256>",
  "paragraph_index": 4,
  "context_before": "The paragraph immediately before...",
  "context_after": "The paragraph immediately after...",
  "matched_keywords": ["home", "grew up"],
  "semantic_score": 0.7312,
  "concept_match": "memories of childhood home",
  "narrative_score": 12,
  "story": {
    "expansion_version": "seed-window-v4",
    "selection_policy": "precise_seed_with_unfiltered_document_context",
    "source_text_mode": "verbatim_extracted_paragraphs",
    "story_length_ready": true,
    "paragraph_count": 5,
    "sentence_count": 12,
    "paragraphs": [
      {"paragraph_index": 2, "role": "context_before", "text": "..."},
      {"paragraph_index": 4, "role": "seed", "text": "I remember..."},
      {"paragraph_index": 6, "role": "context_after", "text": "..."}
    ],
    "source_text_sha256": "<sha256>",
    "text": "..."
  }
}
```

Each source with output has a manifest recording shard paths, row counts, byte
sizes, and SHA-256 checksums. During crawling, changed sources write loose
manifests. `main.py checkpoint` merges them into one deterministic compressed
catalog; tombstones safely remove superseded catalog entries. Zero-match
completion remains represented in SQLite rather than hundreds of thousands of
empty manifests.

To apply a stricter filter to existing accepted output and rebuild manifests:

```powershell
python refilter_output.py
python main.py verify-output
```

The operation stages a complete replacement, atomically swaps it into place,
and updates SQLite counts in the same journaled operation.

## Source Story Expansion

The precise semantic, keyword, language, and narrative filters select the seed
paragraph that qualifies the source passage. Story expansion then includes up
to two preceding and three following paragraphs from the same source document,
bounded by headings, letter salutations, dangling letter introductions, eight
paragraphs, and 12,000 characters. Context paragraphs remain role-labeled in
structured data, but they are not represented as independently passing the
filters. Requiring every connective paragraph to repeat the seed keywords would
fragment ordinary prose rather than produce a coherent extract.

The configured semantic reference is a comparison example used by the embedding
model. It is not a summary of the matched page, and its people, events, or
details must not be assumed to occur in the source. For the specific
grandmother, storytelling, and ancestry reference, normal story exports require
all three literal facets somewhere in the extracted passage. This prevents a
broad heritage match from being presented as the referenced grandmother story.
`-IncludeAnchorMismatches` retains those broad matches for diagnostics.

Matching and deduplication use normalized text. Human-facing story text instead
preserves the Common Crawl WET/ARC paragraph content and its internal line
breaks. Each paragraph and the combined passage carry a SHA-256 source-text
hash; normalized comparison text is retained separately only when it differs.
This is verbatim text from Common Crawl's extracted-text record, not a
reconstruction of the original webpage HTML. The structured gzip preserves the
exact extracted text; Markdown rendering repairs character encoding and HTML
entities for readability and removes invisible trailing spaces. No generative
model writes, paraphrases, summarizes, or completes the story.

`story_length_ready` means the source window contains at least 350 normalized
characters and three sentence endings. Normal exports include only these
passages. `-IncludeShort` retains shorter context for diagnostics; it is not
part of the normal story product. The threshold is useful for review, not a
claim that a narrative is artistically or factually complete.

New schema-5 matches receive this context during crawling. Historical
schema-2/3/4 matches can be enriched without recrawling the full corpus:

```powershell
.\scripts\stories.ps1 -Action plan -Limit 10
.\scripts\stories.ps1 -Action enrich -Limit 10 -Apply
.\scripts\stories.ps1 -Action export
```

The backfill downloads only Common Crawl WET/ARC files already named by
accepted matches. Each source commits to its own fragment under
`data/stories/_records/`, so interruption is safe and the next invocation skips
current fragments. Expansion-version changes mark old fragments pending.
Canonical match shards and existing `matches_<language>.md` exports remain
unchanged. The final export deduplicates repeated crawl captures by normalized
story text while retaining every capture and source-text hash in provenance.

After a bounded trial, use `-All` to finish every matched source serially on
one workstation:

```powershell
.\scripts\stories.ps1 -Action enrich -All -Apply
.\scripts\stories.ps1 -Action export
.\scripts\checkpoint.ps1 -Message "feat: expand matched passages into source stories"
```

## Parquet And Deduplication

Build a local analytical dataset:

```powershell
python main.py parquet --dedupe exact
python main.py parquet --dedupe near --near-distance 3
```

Dataset schema 5 contains four Zstandard-compressed tables partitioned by `crawl_id`
and `language`:

- `stories/` contains one canonical text with quality and diversity fields.
- `provenance/` maps every original crawl capture to its canonical `story_id`.
- `curated/` is a conservative default view of personal prose within the domain cap.
- `passages/` assembles adjacent accepted paragraphs without crossing a document
  or language boundary.

Exact canonicalization uses normalized text independently of URL. Near
deduplication uses 64-bit SimHash with an SQLite-backed band index, so memory
does not grow with the corpus. Nothing is discarded from provenance. Canonical
rows include domain rank and `within_domain_cap`, explainable boilerplate
signals, structural template fingerprints, concept-cluster IDs, document
context, and content categories (`personal_prose`, `lyrics`, `poetry`,
`commercial`, `genealogy`, and `adult_content`). Category flags never delete
records; sensitive and non-story material remains in `stories/` and
`provenance/`. Passage rows retain all component record/story IDs, paragraph
indices, and context. Their candidate place mentions, years, decades, temporal
phrases, and migration routes use the explainable `regex-v1` method with an
evidence confidence; they are research hints, not geocoded facts. The manifest
reports concentration and diversity diagnostics plus every file checksum.
Adjust the research-view limit with `--domain-story-cap`.

`data/parquet/` is ignored because it is reproducible and can be large.

## Refreshing Historical Results

Runtime tuning does not require a historical recrawl. Worker counts, inference
batches, adaptive batching, caching, and an audited FP16 profile change
throughput rather than the matching policy. Refresh the useful research view
from every accepted capture with:

```powershell
.\scripts\refresh-results.ps1
```

By default this command safely simulates the current semantic and narrative
rules without replacing tracked JSONL output, then rebuilds `data/parquet/`
with near deduplication (distance `3`) and a `100`-story per-domain research
cap. The full canonical table and provenance remain available; the domain cap
is represented by `within_domain_cap` rather than deleting rows.

Use `-SkipParquet` for only the filter simulation. `-Dedupe exact` disables
near-duplicate grouping. `-DomainStoryCap 200` changes only the research-view
flag. `-SemanticThreshold` and `-NarrativeThreshold` can audit proposed values.
Actual replacement of accepted JSONL output requires the explicit
`-ApplyRefilter` switch, a clean checkpoint, and should follow human evaluation;
applied output is verified again before the canonical dataset is rebuilt.

The July 14, 2026 baseline keeps all `3,416` accepted captures under the
semantic threshold `0.45` and narrative threshold `8`. Near deduplication
produces `1,356` canonical stories; `1,183` are within the default domain cap
and `1,098` are in the default curated view. The retained full table classifies
`29` poetry, `22` lyrics, `22` genealogy, and `12` adult-content stories.
Those results remain preserved. The newer narrative ruleset creates a new filter
signature, so historical rows are reported as stale or unknown until audited;
they are not silently relabeled and are not automatically queued for recrawl.
The current anchor catalog now includes one native-language narrative anchor for
all 20 keyword languages. That is a recall-affecting signature change: evaluate
it with the isolated audit path below before adopting or selectively recrawling
historical checkpoint rows.

Reprocess completed Common Crawl sources only after a recall-affecting change,
such as broader keywords or semantic-reference anchors, a lower threshold, a
new model revision, or different paragraph extraction. Rejected candidates were
not all retained, so those changes cannot be applied retrospectively to
accepted output alone. Before a full recrawl, compare a representative sample
of completed sources in an isolated audit. Do not use `reset` merely to refresh
results.

Plan first, then explicitly run the bounded audit:

```powershell
.\scripts\audit.ps1 -PerCrawl 2
.\scripts\audit.ps1 -Action run -Profile 3080 -PerCrawl 2 -Apply
```

Selection is deterministic and stratified by crawl plus historical yield: when
available, each crawl contributes both a source that previously matched and a
zero-match source. The audit writes its temporary database and output under
ignored `data/audits/`, leaves `data/progress.db` and `data/output/` untouched,
records local performance metrics, and contributes sampled reject decisions to
the normal evaluation reservoir. The maximum is ten sources per crawl.
A two-source audit is useful for a quick regression check. Signature adoption
requires at least five completed, equivalent sources in each crawl being
adopted.
Because audit sources are deliberately stratified, their decision and shadow
samples are tuning evidence, not population-weighted benchmark rows. Normal
crawls provide the low-rate representative samples used for end-to-end metrics.

The audit launcher accepts the same deliberate runtime overrides as a crawl:
`-Workers`, `-CandidateBatchSize`, `-InferenceBatchSize`,
`-EncodingBatchSize`, `-Precision`, `-NoAdaptiveBatching`, and `-NoCache`.
Normally, leave them unset so each of `-Profile 3080`, `-Profile 4090`, and
`-Profile 5090` uses its tracked defaults.

Use filter signatures to make that audit selective:

```powershell
.\scripts\filter-state.ps1
.\scripts\filter-state.ps1 -Action reset-stale `
  -Crawl CC-MAIN-2014-15 -Limit 100 -IncludeUnknown -Apply
```

Historical rows created before signatures are reported as `unknown`. Add
`-IncludeUnknown` only for a deliberate bounded recrawl. The PowerShell helper
requires `-Crawl`, a positive `-Limit`, and `-Apply` before changing checkpoint
state. It never stamps every unknown row from a bare confirmation flag.

To adopt compatible historical work, run at least five sources per crawl, then
pass the resulting immutable report as evidence:

```powershell
.\scripts\audit.ps1 -Action run -Profile 3080 -PerCrawl 5 -Apply
.\scripts\filter-state.ps1 -Action stamp-current `
  -AuditReport .\data\audits\AUDIT_ID\report.json -Apply
```

The report must match the current filter signature, prove unchanged normalized
match sets, show every selected source completed, and list the crawl as eligible.
The database records the audit ID and report SHA-256 for each adoption, and the
validated report is copied to `data/checkpoints/audit-evidence/` for the next Git
handoff. Existing output and checkpoint rows remain untouched when evidence is
missing or inconsistent.

## Model And Dependency Validation

`data/evaluation/model-baseline.json` is the tracked 400-sample semantic model
baseline. It contains stable sample IDs, scores, selected semantic-reference
anchors, and threshold decisions, but no source paragraphs. After changing PyTorch,
Transformers, Sentence Transformers, CUDA, model files, or precision behavior,
capture an ignored candidate on each workstation and compare it:

```powershell
.\scripts\model-validation.ps1 -Action capture -Profile 3080
.\scripts\model-validation.ps1 -Action compare -Profile 3080
```

Repeat with `4090` and `5090` on those PCs. The comparison requires every sample
to be present, maximum score drift at most `0.005`, concept agreement at least
`0.99`, and identical threshold decisions. To deliberately replace the tracked
baseline after an approved migration, use:

```powershell
.\scripts\model-validation.ps1 -Action capture -Profile 3080 -AsBaseline
```

Review the diff and rerun all comparisons.

Run the dependency contract and advisory gate with:

```powershell
.\scripts\dependency-audit.ps1
```

The exception review date is a deadline for migration work, not a claim that the
listed advisories are harmless.

## Evaluation

Build an unlabeled sample from real committed records and sampled live rejects:

```powershell
.\scripts\evaluation.ps1
.\scripts\evaluation.ps1 -Action plan
.\scripts\evaluation.ps1 -Action sample -Size 400
.\scripts\evaluation.ps1 -Action annotate -Prediction rejected -Limit 25
.\scripts\evaluation.ps1 -Action annotate -Prediction accepted -Limit 75
.\scripts\evaluation.ps1 -Action annotate -Split holdout -Limit 25 -Quick
.\scripts\evaluation.ps1 -Action serve -OpenBrowser
.\scripts\evaluation.ps1 -Action multilingual
.\scripts\evaluation.ps1 -Action report
```

`.\scripts\evaluation.ps1` is always safe and explains whether more audit
samples or human labels are needed. Reports return a structured not-ready
result instead of a traceback when no labels exist. Sampling is deterministic
and language-stratified. Representative benchmark rows receive a stable 80/20
tuning/holdout split; uncertain rows are used only for tuning. Existing labels,
notes, annotator, timestamp, and label history are kept when a sample is rebuilt.
The plan action reserves balanced accepted/rejected tuning and holdout quotas,
reports missing queues, and never assigns a label; labels require human
judgment. Annotation queues rotate across prediction and language strata. `-Language`,
`-Prediction`, `-Split`, `-SampleId`, `-Relabel`, and `-Quick` support focused
work, while `-Action undo` restores the previous label.

The browser workbench binds to `127.0.0.1:8765` by default, uses the same atomic
annotation file and bounded history as the terminal workflow, and supports
positive, negative, skip, undo, content taxonomy, notes, and queue filters.
Representative holdout rows are blind: model decisions, scores, keywords,
concepts, and predicted categories are omitted from the browser API until
evaluation reporting. Use `-Port` when that local port is occupied.

The multilingual report compares keyword languages, native anchors, observed
candidate/annotation support, calibration readiness, shadow coverage, and
human-labeled positive keyword rejects. It never invents labels or activates
threshold changes.

Reports use only human labels and explicitly identify their metric scope:
descriptive active sample, weighted downstream filter, or weighted end-to-end
funnel. They include unweighted and sampling-weighted metrics, calibration bins,
per-language support warnings, content-category agreement, false-positive and
false-negative IDs, and a semantic/narrative threshold search performed without
holdout rows. Recommendations remain exploratory until at least 100 labels,
both classes, and at least 20 representative holdout labels with both classes
are present. Pre-keyword shadow labels are required before recall is described
as end-to-end, and those weighted recall rows must come from normal-crawl
probability sampling rather than a deliberately selected audit.

The original synthetic regression corpus remains available:

```powershell
python scripts\evaluate_filters.py
```

## Review And Export

```powershell
python review.py --limit 20
python export_md.py
```

Both commands stream data or use temporary SQLite storage instead of loading
the complete corpus into memory.

## Development

```powershell
.\scripts\setup.ps1 -Profile 3080 -Dev
.\scripts\test.ps1
```

The suite covers leases, retries, interruption, source transactions, stable
IDs, checksum rollback, compact manifest catalogs, inference caching,
multilingual filtering, real WET parsing, spawned Windows-compatible
orchestration, representative sampling weights, tuning/holdout isolation,
audit-gated signature adoption, real-workload recommendations,
canonical/provenance deduplication, and Parquet export. GitHub
Actions validates dependency-profile pins and the dated vulnerability policy,
then runs lint, tests, compilation, CLI smoke checks, and PowerShell parsing on
both Windows and Ubuntu without downloading GPU models or Git LFS data.

## Project Structure

```text
main.py                 CLI and run orchestration
annotation_workbench.py localhost labeling UI and protected JSON API
audit.py                isolated comparison and signature-adoption evidence
pipeline.py             bounded CPU queue and shared GPU inference owner
progress.py             SQLite leases, retries, and checkpoint migration
output.py               source transactions, stable IDs, and manifests
inference_cache.py      versioned embedding, score, and language cache
checkpoint.py           integrity verification and handoff compaction
database_checkpoint.py  compressed SQLite archive, restore, and sync checks
processor.py            WET/ARC parsing, funnel counters, and shadow candidates
story_context.py        deterministic seed-to-source-context expansion
story_enrichment.py     resumable historical backfill and story exports
text_normalization.py   versioned entity, encoding, and Unicode repair
matcher.py              keyword, semantic, and narrative filters
evaluation.py           weighted sampling, annotation history, tuning, and holdout
metrics.py              funnel, failures, resources, rates, history, and ETA
failure_analysis.py     stable operational failure categories
benchmark.py            synthetic precision and isolated real-source benchmarks
dependency_profiles.py  cross-profile lock and installed-package contract
dependency_audit.py     pip-audit policy enforcement with an expiry date
model_regression.py     semantic score/concept/threshold snapshots and comparison
project_health.py       consolidated workstation and handoff readiness
dedupe.py               disk-backed exact and SimHash duplicate index
parquet_export.py       staged partitioned analytical export
story_reconstruction.py adjacent passages and explainable place/time metadata
quality.py              boilerplate, template, domain, and diversity signals
scheduling.py           coverage-preserving yield-aware crawl ranking
signatures.py           filter contracts, run IDs, and Git provenance
refilter_output.py      transactional schema/filter migration
4090/                   RTX 4090 compatibility launchers
5090/                   RTX 5090 compatibility launchers
scripts/                setup, run, security, audit, evaluation, test, and handoff commands
tests/                  unit, regression, and integration tests
```

## License

MIT. See [LICENSE](LICENSE). Common Crawl data remains subject to the
[Common Crawl Terms of Use](https://commoncrawl.org/terms-of-use).
