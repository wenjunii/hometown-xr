# Hometown XR Common Crawl Extractor

A resumable multilingual pipeline for finding first-person stories about home,
hometown, childhood, roots, migration, and belonging in Common Crawl data.

The canonical repository is
[wenjunii/hometown-xr](https://github.com/wenjunii/hometown-xr).

## Models

The extractor uses two local machine-learning models:

- `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` for semantic
  similarity against the home and belonging concepts in `concepts.py`.
- FastText `lid.176.bin` for language identification.

Neither model is a generative LLM, and this extractor does not call a hosted
OpenAI, Gemini, Anthropic, or other text-generation API.

## Safety First

The 3080 and 4090 PCs share crawl state through GitHub, Git LFS, and the tracked
`data/` directory. Run the crawler on only one PC at a time.

Before switching machines:

1. Press `Ctrl+C` once and wait for the session summary.
2. Confirm `data/.crawler.lock` is gone.
3. Commit and push the checkpoint from the old PC.
4. Pull Git and Git LFS on the new PC before starting.

See [HANDOFF.md](HANDOFF.md) for the exact handoff commands.

## Quick Start

Requirements:

- Windows 10 or 11
- Python 3.10
- Git and Git LFS
- NVIDIA driver compatible with CUDA 12.1
- An RTX 3080-class or RTX 4090 GPU

First-time setup:

```powershell
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080
```

Use `-Profile 4090` on the 4090 PC. Setup installs the exact versions in
`requirements-lock.txt` and then runs the environment check.

Verify the machine and checkpoint:

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

The compatibility command below still works on the 4090 PC, but it launches
the same root implementation:

```powershell
.\.venv\Scripts\python.exe 4090\main.py run --all
```

## Hardware Profiles

Both profiles currently use the stable settings proven on the two machines:

| Profile | Workers | Stream batch | Encoding batch |
| --- | ---: | ---: | ---: |
| `3080` | 7 | 200 | 128 |
| `4090` | 7 | 200 | 128 |

The GPU is detected automatically, or it can be selected explicitly with
`--profile`. Any setting can be overridden for one run:

```powershell
python main.py run --profile 3080 --workers 7 --stream-batch-size 200 --encoding-batch-size 128
```

The definitions live in `config.py`; there is no longer a second 4090 code
tree to maintain.

## Processing Pipeline

Each WET or ARC source passes through three filters:

1. Multilingual keyword prefilter from `keywords.py`.
2. Sentence-transformer similarity against the concept anchors.
3. First-person narrative filtering across Latin and non-Latin scripts.

Short CJK, Japanese, Korean, and Thai keywords use substring matching because
those scripts do not reliably place spaces around words. Ordinary words such
as `train`, `bus`, and `station` are soft negative evidence, not automatic
rejections of otherwise strong personal stories.

FastText predictions below `LANG_DETECTION_THRESHOLD` are written under
`data/output/unknown/` while retaining their confidence score.

## Crash-Safe Resume

The progress database uses leased work claims:

- Only files assigned to live worker slots become `processing`.
- The parent refreshes active leases every 30 seconds.
- A clean shutdown returns interrupted files to `pending` immediately.
- A crashed lease becomes recoverable after 10 minutes.
- Failed files retry automatically with exponential backoff, up to four
  attempts.
- Parser and network-stream failures fail the source instead of silently
  marking truncated work complete.

Each worker writes into `data/output/.staging/`. It replaces the source's old
output only after the entire source has completed. Retrying a source therefore
replaces its results instead of appending duplicates.

The existing SQLite checkpoint is migrated in place on first use. Completed
rows and legacy output filenames remain valid.

## Commands

| Command | Purpose |
| --- | --- |
| `python main.py run --crawl ID` | Start or resume one crawl |
| `python main.py run --all` | Process all known crawls |
| `python main.py run --limit 5` | Process at most five ready files |
| `python main.py status` | Show overall and per-crawl progress |
| `python main.py list` | List available crawl datasets |
| `python main.py retry --all` | Reset every failed file for immediate retry |
| `python main.py retry --crawl ID` | Reset failed files in one crawl |
| `python main.py recover --minutes 10` | Release expired processing leases |
| `python main.py doctor --profile 3080` | Verify Python, PyTorch, CUDA, and GPU |
| `python main.py reset` | Delete all output and crawl progress |

Use `recover --minutes 0` only after confirming no crawler is running. The
local crawler lock prevents maintenance commands from racing an active run.

## Output

Output is gzip-compressed JSON Lines grouped by detected language:

```text
data/
  progress.db
  models/
    lid.176.bin
  output/
    en/
      <source-hash>_<source-name>.jsonl.gz
    zh/
    unknown/
  exports/
```

New records contain a collision-free source identity:

```json
{
  "crawl_id": "CC-MAIN-2026-12",
  "source_file": "crawl-data/.../example.warc.wet.gz",
  "url": "https://example.org/story",
  "warc_date": "2026-03-01T12:00:00Z",
  "language": "en",
  "language_confidence": 0.9821,
  "paragraph": "I remember the home where I grew up...",
  "matched_keywords": ["home", "grew up"],
  "semantic_score": 0.7312,
  "concept_match": "memories of childhood home"
}
```

Legacy shards without `source_file` are still readable. A successful refilter
adds that field while resolving the exact database source; ambiguous legacy
filenames stop with an error instead of updating the wrong row.

## Review And Export

Show the top matches without loading every result into memory:

```powershell
python review.py --limit 20
```

Export all records to Markdown. The exporter uses temporary SQLite storage for
disk-backed score sorting:

```powershell
python export_md.py
```

Preview the current filtering rules without changing output:

```powershell
python refilter_output.py --dry-run
```

Apply them transactionally:

```powershell
python refilter_output.py
```

Refiltering builds and validates a complete replacement directory before the
output/database swap. A journal finishes or rolls back an interrupted swap on
the next invocation.

## Filter Evaluation

The repository includes labeled multilingual positive and negative examples:

```powershell
python scripts\evaluate_filters.py
```

These cases cover personal transport memories, Chinese unsegmented hometown
phrases, Portuguese and Spanish stories, navigation, lyrics, privacy pages,
and shopping content.

## Development

Install the lightweight test tools and run all checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\scripts\test.ps1
```

The suite covers schema migration, retries, lease recovery, shutdown behavior,
accurate record counting, output rollback/idempotency, language confidence,
multilingual filters, and transactional refiltering. GitHub Actions runs tests,
linting, and compilation without downloading Git LFS data or GPU models.

## Configuration

Important defaults in `config.py`:

| Setting | Default |
| --- | ---: |
| `SEMANTIC_THRESHOLD` | `0.45` |
| `MIN_PARAGRAPH_LENGTH` | `150` |
| `MAX_PARAGRAPH_LENGTH` | `5000` |
| `MIN_NARRATIVE_INDICATORS` | `8` |
| `LANG_DETECTION_THRESHOLD` | `0.5` |
| `MAX_FILE_ATTEMPTS` | `4` |
| `LEASE_TIMEOUT_SECONDS` | `600` |

Modern WET crawls do not require AWS credentials. Listing the three legacy ARC
crawls requires AWS credentials with access to list the Common Crawl bucket.

## Project Structure

```text
main.py                 CLI and process orchestration
progress.py             SQLite leases, retries, and migrations
output.py               source-scoped atomic output transactions
processor.py            WET/ARC parsing and accurate counters
matcher.py              keyword, semantic, and narrative filters
language_detector.py    FastText confidence routing
refilter_output.py      transactional maintenance refilter
review.py               bounded-memory top-k review
export_md.py            disk-backed Markdown export
config.py               shared settings and hardware profiles
4090/                   compatibility launchers only
scripts/                setup, run, test, and handoff commands
tests/                  regression and multilingual evaluation suite
```

## Related Projects

- [Voice-to-Visual SDTD](https://github.com/wenjunii/voice-to-visual-sdtd)
- [SHARP-TD Bridge](https://github.com/wenjunii/sharp-td-bridge)
- [Home Podcast Generator](https://github.com/wenjunii/home-podcast-generator)

## License

MIT. See [LICENSE](LICENSE). Common Crawl data remains subject to the
[Common Crawl Terms of Use](https://commoncrawl.org/terms-of-use).
