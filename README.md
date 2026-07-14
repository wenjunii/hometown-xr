# Hometown XR Common Crawl Extractor

A resumable multilingual pipeline for finding first-person stories about home,
hometown, childhood, roots, migration, and belonging in Common Crawl data.

Canonical repository: [wenjunii/hometown-xr](https://github.com/wenjunii/hometown-xr)

## Models

The extractor uses two local machine-learning models:

- `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, pinned to
  revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`, scores text against the
  concepts in `concepts.py`.
- FastText `lid.176.bin` identifies language and records prediction confidence.

Neither model is a generative LLM. The crawler does not call OpenAI, Gemini,
Anthropic, or another hosted text-generation API.

A versioned local SQLite cache stores sentence embeddings, raw semantic scores,
and raw language predictions by normalized text hash. Model revision, precision,
and concept-anchor version are part of each cache namespace, so stale results
cannot cross a model change. `data/cache/` remains local to each workstation.

## Workstation Safety

The RTX 3080 and RTX 4090 PCs share one Git checkpoint. Run the crawler on
only one PC at a time.

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

Use `4090` in the first two commands on the other PC. The receive command
requires a clean worktree, performs a fast-forward-only Git pull plus Git LFS
pull, then runs `doctor`, `status`, and `verify-output`. The send command works
only from `main`, checks that `origin/main` is not ahead before checkpointing,
and confirms the pushed commit against the remote.

## Architecture

One run has seven lightweight CPU parser processes and one inference owner:

```text
WET/ARC sources
    -> bounded CPU download/parse/keyword workers
    -> bounded candidate queue
    -> hard-boilerplate prefilter
    -> versioned score/embedding cache
    -> one shared sentence-transformer on the GPU
    -> FastText language detection
    -> source-scoped staged output
    -> atomic shard + manifest commit
    -> SQLite checkpoint completion
```

The semantic model is loaded once, not once per worker. The process pool and
GPU service are reused across crawls. Queue backpressure keeps RAM bounded when
the CPU workers are faster than inference. Repeated text bypasses model encoding,
and hard navigation, policy, form, lyric, and repetitive-text negatives are
rejected before GPU work without changing which records can pass.

Each source has a SQLite lease. Interrupted sources return to `pending`, hard
crashes are recovered after the lease timeout, and failed sources retry with
exponential backoff. Output becomes visible only after an entire source is
successfully parsed and filtered.

## Quick Start

Requirements:

- Windows 10 or 11
- Python 3.10
- Git and Git LFS
- NVIDIA driver compatible with CUDA 12.1
- RTX 3080-class or RTX 4090 GPU

First-time setup on this RTX 3080 PC:

```powershell
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080
```

Use `-Profile 4090` on the other PC. Setup installs the exact versions in
`requirements-lock.txt`, including the CUDA 12.1 PyTorch wheel. Add `-Tune` to
run a quick local hardware benchmark or `-Dev` to install the test toolchain.

Verify the environment and checkpoint:

```powershell
.\.venv\Scripts\python.exe main.py doctor --profile 3080
.\.venv\Scripts\python.exe main.py status
```

Start or resume one crawl:

```powershell
.\scripts\run.ps1 -Profile 3080 run --crawl CC-MAIN-2026-12
```

Resume every available crawl:

```powershell
.\scripts\run.ps1 -Profile 3080 run --all
```

The old `4090\main.py` command remains a compatibility launcher. It invokes
the same root implementation and shared `data/` checkpoint.

## Hardware Profiles

These are the conservative settings tracked in Git:

| Profile | CPU workers | Candidate batch | Inference batch | Encoding batch | Precision |
| --- | ---: | ---: | ---: | ---: | --- |
| `3080` | 7 | 100 | 800 | 128 | FP32 |
| `4090` | 7 | 150 | 1600 | 256 | FP32 |

`candidate_batch_size` controls worker-to-parent messages.
`inference_batch_size` combines candidates from multiple sources before one
semantic call. `encoding_batch_size` is the sentence-transformer CUDA batch.

Benchmark this PC and write an ignored, machine-local override:

```powershell
.\scripts\benchmark.ps1 -Profile 3080
```

Use `-Quick` for a shorter pass. The benchmark measures FP32 and FP16, checks
maximum score drift and concept agreement on a fixed audit set, and selects
FP16 only when drift stays within the safety bound and throughput improves by
at least five percent. Results are written to
`data/hardware-profile.local.json`; that file is intentionally not synchronized
because the 3080 and 4090 should keep their own measured settings.

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

1. A multilingual keyword prefilter from `keywords.py`.
2. A behavior-preserving hard-boilerplate/narrative prefilter.
3. Cached or fresh sentence-transformer similarity against home concepts.
4. A multilingual first-person narrative filter.

CJK, Japanese, Korean, and Thai keywords use substring matching where word
boundaries are unreliable. FastText predictions below the confidence threshold
are stored under `unknown/` with the original confidence.

Every live candidate can contribute to a deterministic local evaluation sample.
Boundary cases are sampled more aggressively, while coverage sampling and
language stratification prevent the queue from collapsing around one score or
language. Human labels are never synthesized.

## Commands

PowerShell launchers provide the recommended workstation workflow and can
resolve the project root regardless of the caller's current directory:

| Script | Purpose |
| --- | --- |
| `.\scripts\setup.ps1 -Profile 3080` | Create/update the runtime and run diagnostics |
| `.\scripts\setup.ps1 -Profile 3080 -Tune -Dev` | Also tune this PC and install development tools |
| `.\scripts\run.ps1 -Profile 3080 run --all` | Start or resume using the selected hardware profile |
| `.\scripts\benchmark.ps1 -Profile 3080` | Audit FP16 safety and write this PC's local override |
| `.\scripts\handoff.ps1 -Direction pull -Profile 3080` | Fast-forward, pull LFS data, and verify the received checkpoint |
| `.\scripts\refresh-results.ps1` | Dry-run current filters and rebuild the local canonical dataset |
| `.\scripts\checkpoint.ps1 -Message "checkpoint: hand off crawler state"` | Verify, compact, commit, push, and confirm a checkpoint |
| `.\scripts\test.ps1` | Run tests, lint, and compilation checks |

The underlying Python CLI remains available directly:

| Command | Purpose |
| --- | --- |
| `python main.py run --crawl ID` | Start or resume one crawl |
| `python main.py run --all` | Process every known crawl |
| `python main.py run --limit 5` | Process at most five ready sources |
| `python main.py status` | Show checkpoint progress |
| `python main.py metrics` | Show latest rates, GPU time, and ETA |
| `python main.py doctor --profile 3080` | Check Python, PyTorch, CUDA, and profile |
| `python main.py benchmark --profile 3080` | Benchmark and tune this PC |
| `python main.py cache stats` | Inspect the local inference cache |
| `python main.py cache clear` | Rebuild the local inference cache from empty |
| `python main.py retry --all` | Retry all failed sources immediately |
| `python main.py recover --minutes 10` | Release expired source leases |
| `python main.py verify-output` | Verify committed shard checksums |
| `python main.py checkpoint` | Verify and compact state for handoff |
| `python main.py parquet --dedupe exact` | Build partitioned Parquet output |
| `python main.py evaluation sample` | Build a real-text annotation sample |
| `python main.py evaluation annotate` | Label samples interactively |
| `python main.py evaluation report` | Compute precision, recall, F1, and tuning |
| `python main.py reset` | Delete output, derivatives, and progress |

Use `recover --minutes 0` only after confirming no crawler is running.

## JSONL Output

Output is gzip-compressed JSON Lines grouped by detected language:

```text
data/
  progress.db
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
```

Schema version 2 records include deterministic provenance and content IDs:

```json
{
  "schema_version": 2,
  "record_id": "<sha256>",
  "content_fingerprint": "<sha256>",
  "crawl_id": "CC-MAIN-2026-12",
  "source_file": "crawl-data/.../example.warc.wet.gz",
  "url": "https://example.org/story",
  "warc_date": "2026-03-01T12:00:00Z",
  "language": "en",
  "language_confidence": 0.9821,
  "paragraph": "I remember the home where I grew up...",
  "matched_keywords": ["home", "grew up"],
  "semantic_score": 0.7312,
  "concept_match": "memories of childhood home",
  "narrative_score": 12
}
```

Each source with output has a manifest recording shard paths, row counts, byte
sizes, and SHA-256 checksums. During crawling, changed sources write loose
manifests. `main.py checkpoint` merges them into one deterministic compressed
catalog; tombstones safely remove superseded catalog entries. Zero-match
completion remains represented in SQLite rather than hundreds of thousands of
empty manifests.

Migrate existing output to schema version 2 and rebuild manifests with:

```powershell
python refilter_output.py
python main.py verify-output
```

The migration stages a complete replacement, atomically swaps it into place,
and updates SQLite counts in the same journaled operation.

## Parquet And Deduplication

Build a local analytical dataset:

```powershell
python main.py parquet --dedupe exact
python main.py parquet --dedupe near --near-distance 3
```

The export contains two Zstandard-compressed tables partitioned by `crawl_id`
and `language`:

- `stories/` contains one canonical text with quality and diversity fields.
- `provenance/` maps every original crawl capture to its canonical `story_id`.

Exact canonicalization uses normalized text independently of URL. Near
deduplication uses 64-bit SimHash with an SQLite-backed band index, so memory
does not grow with the corpus. Nothing is discarded from provenance. Canonical
rows include domain rank and `within_domain_cap`, explainable boilerplate
signals, structural template fingerprints, and concept-cluster IDs. The
manifest reports concentration and diversity diagnostics plus every file
checksum. Adjust the research-view limit with `--domain-story-cap`.

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
produces `1,356` canonical stories; `1,183` are within the default domain cap.

Reprocess completed Common Crawl sources only after a recall-affecting change,
such as broader keywords or concept anchors, a lower threshold, a new model
revision, or different paragraph extraction. Rejected candidates were not all
retained, so those changes cannot be applied retrospectively to accepted output
alone. Before a full recrawl, compare a representative sample of completed
sources in an isolated audit. Do not use `reset` merely to refresh results.

## Evaluation

Build an unlabeled sample from real committed records and sampled live rejects:

```powershell
python main.py evaluation sample --size 400
python main.py evaluation annotate
python main.py evaluation report
```

Sampling is deterministic, language-stratified, and prioritizes uncertain
examples near semantic or narrative thresholds. Existing labels are kept when
a sample is rebuilt. `annotate --language en --limit 25` supports focused work.
Reports use only human-labeled rows and include confidence intervals,
calibration bins, label-balance readiness, per-language support warnings,
false-positive/false-negative IDs, and a semantic/narrative threshold grid
search. Recommendations stay marked exploratory until at least 100 labels and
both classes are present.

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
orchestration, canonical/provenance deduplication, and Parquet export. GitHub
Actions runs lint, tests, and compilation on both Windows and Ubuntu without
downloading GPU models or Git LFS data.

## Project Structure

```text
main.py                 CLI and run orchestration
pipeline.py             bounded CPU queue and shared GPU inference owner
progress.py             SQLite leases, retries, and checkpoint migration
output.py               source transactions, stable IDs, and manifests
inference_cache.py      versioned embedding, score, and language cache
checkpoint.py           integrity verification and handoff compaction
processor.py            WET/ARC parsing and counters
matcher.py              keyword, semantic, and narrative filters
evaluation.py           real-text sampling, annotation, and reports
metrics.py              run rates, GPU timing, and ETA
benchmark.py            local hardware benchmark and autotuning
dedupe.py               disk-backed exact and SimHash duplicate index
parquet_export.py       staged partitioned analytical export
quality.py              boilerplate, template, domain, and diversity signals
refilter_output.py      transactional schema/filter migration
4090/                   compatibility launchers only
scripts/                setup, run, test, benchmark, and handoff commands
tests/                  unit, regression, and integration tests
```

## License

MIT. See [LICENSE](LICENSE). Common Crawl data remains subject to the
[Common Crawl Terms of Use](https://commoncrawl.org/terms-of-use).
