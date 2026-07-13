# Common Crawl Home/Belonging Extractor

A local, resumable Python application that extracts multilingual paragraphs about **home**, **hometown**, **belonging**, **roots**, **childhood**, **diaspora**, and **nostalgia** from Common Crawl web archive datasets spanning 2008–2026.

## 📊 Project Status (May 18, 2026)
- **Files Completed**: 176,012
- **Pages Processed**: ~7.89 Billion
- **Filtering Logic**: High-Precision Narrative Filter (Threshold 0.45, Narrative 8+, Strict Negative Scrubbing)
- **Output Quality**: Verified 100% clean of institutional noise and commercial metadata.


## How It Works

```
WET/ARC File → Split into Paragraphs → Keyword Pre-Filter → Semantic Scoring → Narrative Filter → Language Detection → JSONL Output
                                         (441 keywords,        (multilingual    (18+ languages)   (176 languages)      (by language)
                                          18 languages)         MiniLM model)
```

**Three-stage matching** keeps processing fast and highly accurate:

1. **Keyword Pre-Filter** — Scans each paragraph for any of 441 multilingual keywords covering home, belonging, roots, childhood, nostalgia, diaspora, and exile. Eliminates ~99% of irrelevant content instantly.
2. **Semantic Similarity Scoring** — Encodes remaining candidates with a multilingual sentence-transformer and compares them against 20 personal narrative concept anchors via cosine similarity. Filters out false positives like "home page" or "home button."
3. **Narrative Voice Filter** — (Refined) Checks passing paragraphs for first-person pronouns ("I", "my", "我", "yo") and storytelling indicators ("I remember", "when I grew up") in 18+ languages. **High-Precision Update (April 2026):** Now includes aggressive exclusion for institutional noise (genealogy databases like MyHeritage, travel directions like HostelWorld), site navigation (language pickers, month lists), and song lyrics to ensure only authentic personal stories are retained.

No LLM is used. The two ML models are local and high-performance:
- **GPU Accelerated**: If an NVIDIA GPU (RTX 3080/4090, etc.) is detected, semantic matches run on CUDA for massive throughput.
- **Optimized Multiprocessing**: Uses staggered worker initialization and adaptive batching; the RTX 3080 profile is configured for 7 parallel workers.

| Model | Size | Purpose |
|-------|------|---------|
| `paraphrase-multilingual-MiniLM-L12-v2` | ~500 MB | Sentence embeddings for semantic matching (50+ languages) |
| `lid.176.bin` (FastText) | ~126 MB | Language detection (176 languages) |

Both models are downloaded automatically on first run.

---

## 💾 Pre-extracted Data Available!

Because the full Common Crawl is massive, we have already run this extractor and synced the **ready-to-use output** directly to this repository! 

If you just want to read the extracted multilingual paragraphs, **you do not need to run any code or download models.** 
- Browse the processed JSONL datasets in `data/output/`
- Read the beautifully exported Markdown files in `data/exports/`

---

## 🚀 Multi-Hardware Support

This repository is optimized for dual-workstation high-performance extraction.

- **Root Version (RTX 3080)**: Optimized for 3080 GPUs (`MAX_WORKERS = 7`).
- **[RTX 4090 Version](./4090/)**: Optimized for 4090 GPUs (`MAX_WORKERS = 7`).

### 🔄 Cross-Workstation Checkpoint Handoff
GitHub carries a resumable checkpoint containing `data/progress.db`, extracted output, exports, code, and configuration. Stop the active crawler, push the stable checkpoint, then pull it (including Git LFS) on the receiving workstation before resuming.

> [!IMPORTANT]
> This is a **serial handoff**, not a live shared database. Only one workstation may run the crawler from the synchronized checkpoint at a time. SQLite/LFS database changes cannot be merged after two machines diverge, even if they process different crawl IDs.

Both RTX 3080 PCs use the root `main.py`; only the RTX 4090 uses `4090/main.py`.

See **[HANDOFF.md](./HANDOFF.md)** for the step-by-step synchronization guide.

---


## Dataset Coverage

Supports **all 122+ Common Crawl datasets** from 2008 to present:

| Era | Years | Format | Files Available |
|-----|-------|--------|-----------------|
| Legacy | 2008–2012 | ARC (raw HTML → text extraction) | 3 crawls |
| Modern | 2013–present | WET (pre-extracted text) | 119+ crawls |

✨ **Auto-Discovery:** For modern crawls, the application automatically queries the Common Crawl Index API on startup. When Common Crawl publishes a new dataset, the application will pick it up automatically—no code updates required.

---

## Setup

### Requirements

- Python 3.10+
- **NVIDIA GPU (Optional but Recommended)**: Greatly accelerates semantic matching using CUDA.
- ~700 MB disk space for ML models (downloaded once)
- Internet connection (for streaming Common Crawl files)

### Installation

```powershell
git lfs install
git clone https://github.com/WenjunII/Hometown-XR.git
cd Hometown-XR
git lfs pull

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip

# For an NVIDIA GPU, install the tested CUDA 12.1 build first.
python -m pip install "torch==2.1.0+cu121" --index-url https://download.pytorch.org/whl/cu121
python -m pip install -r requirements.txt
```

For CPU-only use, skip the CUDA-specific command and install `requirements.txt`. The direct dependencies are pinned to the versions tested on both workstation profiles; update both requirements files together when changing the ML stack. The FastText and sentence-transformer models are downloaded automatically on first use and are intentionally not stored in Git.

Dependencies:
- `warcio` — WARC/ARC file parsing
- `requests` — HTTP streaming
- `sentence-transformers` — Multilingual semantic similarity
- `fasttext-wheel` — Language detection
- `torch` — ML backend
- `tqdm` — Progress bars

---

## Usage

### List All Available Crawls

```bash
python main.py list
```

Shows all 122 crawls organized by year, with format labels (ARC vs WET).

### Process a Single Crawl

```bash
# Process the latest crawl
python main.py run --crawl CC-MAIN-2026-12

# Process a specific older crawl
python main.py run --crawl CC-MAIN-2019-04

# Process a legacy crawl (2008-2012)
python main.py run --crawl CC-CRAWL-001
```

### Process ALL Crawls

```bash
python main.py run --all
```

Processes all 122+ crawls. Fully parallelized and resumable. Stop and restart at any time.

### Test with a Small Sample

```bash
# Process only 5 files from a crawl (for testing/tuning)
python main.py run --crawl CC-MAIN-2026-12 --limit 5
```

### Check Progress

```bash
python main.py status
```

Shows overall and per-crawl progress including files completed, matches found, and percentage done.

> [!NOTE]
> **Match Count Discrepancy:** The `Matches found` number reported by the status command reflects the historical count of matches found during the raw crawl. Because we retroactively applied an ultra-strict, high-precision narrative filter to the exported data, the actual number of high-quality records in the `data/exports/` Markdown files will be significantly lower than the raw database count.

### Adjust Semantic Threshold

```bash
# More strict (fewer but higher-quality matches)
python main.py run --crawl CC-MAIN-2026-12 --threshold 0.45

# More permissive (more matches, some may be loosely related)
python main.py run --crawl CC-MAIN-2026-12 --threshold 0.30
```

- **`0.45`** — Default (High Precision). Optimized to eliminate commercial/form noise.

### Wipe Data and Reset
If you want to delete all extracted results and start a crawl over from scratch:
```bash
python main.py reset
```
This safely deletes `progress.db` and the `data/output/` directory while preserving your ML models.

### Stop and Resume

Press `Ctrl+C` once, then wait for the main process and workers to exit. Before a GitHub handoff, run `python main.py status` and require `Processing: 0`.

An abrupt exit can leave files marked as `processing`. Entries older than one hour are recovered to `pending` when the progress tracker is next opened. Keep the crawler stopped during recovery, and do not push or resume on another workstation until the checkpoint has no active processing rows.

---

## Output

### Structure

```
data/output/
├── en/           ← English matches
│   ├── crawl-data_CC-MAIN-2026-12_...00000.warc.wet.jsonl.gz
│   └── crawl-data_CC-MAIN-2026-12_...00001.warc.wet.jsonl.gz
├── de/           ← German matches
├── ja/           ← Japanese matches
├── zh/           ← Chinese matches
├── fr/           ← French matches
├── es/           ← Spanish matches
├── ar/           ← Arabic matches
└── ...           ← One folder per detected language
```

### Record Format

Each line in a `.jsonl.gz` file is a JSON object:

```json
{
  "crawl_id": "CC-MAIN-2026-12",
  "url": "https://de.example.com/mein-leben",
  "warc_date": "2026-03-15T10:23:45Z",
  "language": "de",
  "language_confidence": 0.97,
  "paragraph": "Meine Heimat ist ein kleines Dorf in Bayern. Dort bin ich aufgewachsen und dort habe ich meine Kindheit verbracht. Die Erinnerungen an die Wiesen und Wälder sind mir bis heute geblieben.",
  "matched_keywords": ["Heimat", "Kindheit"],
  "semantic_score": 0.78,
  "concept_match": "I grew up in a village surrounded by fields and forests. The landscape of my hometown is etched into my memory."
}
```

| Field | Description |
|-------|-------------|
| `crawl_id` | Provenance: Which Common Crawl dataset it came from |
| `url` | Source web page URL |
| `warc_date` | When Common Crawl captured the page |
| `language` | Detected language (ISO 639-1 code) |
| `language_confidence` | FastText detection confidence (0–1) |
| `paragraph` | The extracted paragraph text |
| `matched_keywords` | Which keywords triggered the pre-filter |
| `semantic_score` | Cosine similarity to best concept anchor (0–1) |
| `concept_match` | The concept anchor sentence it matched |

### Reading Output (Markdown Export)

While files are stored as compressed JSONL to save space, you can easily export them to beautiful, readable Markdown files:

```bash
python export_md.py
```

This will convert all `.jsonl.gz` chunks into consolidated Markdown files in `data/exports/` (e.g., `matches_en.md`, `matches_de.md`), sorted by semantic score.

Alternatively, you can read the JSONL files directly in Python:

```python
import gzip
import json

with gzip.open("data/output/en/some_file.jsonl.gz", "rt", encoding="utf-8") as f:
    for line in f:
        record = json.loads(line)
        print(f"[{record['language']}] {record['semantic_score']:.2f} — {record['paragraph'][:100]}...")
```

### Review Helper

A built-in review script shows top matches in your terminal:

```bash
python review.py
```

---

## Project Structure

```
cc-home-extractor/
├── main.py               ← CLI entry point (run / status / list)
├── config.py              ← Tunable settings (thresholds, paths, model names)
├── crawl_catalog.py       ← Catalog of all 122 Common Crawl datasets
├── keywords.py            ← 441 multilingual keywords across 18 languages
├── concepts.py            ← 20 semantic anchor sentences
├── downloader.py          ← HTTP streaming for WET/ARC files
├── processor.py           ← WET text parser + ARC HTML-to-text extractor
├── matcher.py             ← Three-stage hybrid matcher (keyword + semantic + narrative)
├── language_detector.py   ← FastText wrapper (176 languages)
├── progress.py            ← SQLite-based resumable progress tracker
├── output.py              ← JSONL writer (gzip, organized by language)
├── export_md.py           ← Markdown exporter for extracted records
├── review.py              ← Helper script to inspect output quality
├── requirements.txt       ← Python dependencies
├── .gitignore             ← Excludes data/models/ from version control
└── data/                  ← Application data
    ├── progress.db        ← SQLite state database (synced to Git)
    ├── models/            ← Downloaded ML models (excluded from Git due to size)
    ├── exports/           ← Markdown exports (synced to Git)
    └── output/            ← Extracted results (synced to Git)
```

---

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `SEMANTIC_THRESHOLD` | `0.45` | Min cosine similarity to accept a match |
| `MIN_PARAGRAPH_LENGTH` | `150` | Skip paragraphs shorter than this (chars) |
| `MAX_PARAGRAPH_LENGTH` | `5000` | Skip paragraphs longer than this (chars) |
| `ENCODING_BATCH_SIZE` | `128` | Batch size for sentence-transformer |
| `DEFAULT_CRAWL_ID` | `CC-MAIN-2026-12` | Default crawl when `--crawl` is omitted |
| `LANG_DETECTION_THRESHOLD` | `0.5` | Min confidence for language detection |
| `MIN_NARRATIVE_INDICATORS` | `8` | Min unique narrative signals required |
| `_NEGATIVE_INDICATORS` | (list) | Blacklisted institutional/commercial words |

---

## Customization

### Adding Keywords

Edit `keywords.py` to add keywords for new languages or refine existing ones:

```python
KEYWORDS_BY_LANGUAGE = {
    "your_lang_code": [
        "keyword1", "keyword2", "keyword3",
        # ...
    ],
    # ...
}
```

### Adding Concept Anchors

Edit `concepts.py` to add new anchor sentences that describe what you're looking for:

```python
CONCEPT_ANCHORS = [
    "Your new concept description here",
    # ...
]
```

The sentence-transformer will automatically use these as comparison targets. Because the model is multilingual, English anchor sentences match content in any supported language.

### Current Concept Anchors

The application is pre-configured with 20 anchor sentences organized into 7 thematic categories. These are written in a **first-person narrative voice** to train the AI to prefer personal stories over dictionary definitions or marketing copy:

**Hometown & Place of Origin**
1. "I was born and raised in a small town. Every time I go back, I recognize the streets and houses from my childhood."
2. "I grew up in a village surrounded by fields and forests. The landscape of my hometown is etched into my memory."
3. "When I returned to the town where I spent my childhood, I felt overwhelming emotion and a deep sense of connection."

**Childhood & Growing Up**
4. "My earliest memories are of playing outside near our family home. Those carefree days shaped who I became."
5. "Growing up in my parents' house, I learned the values and traditions that would stay with me for the rest of my life."
6. "I remember my childhood vividly — the sounds, the smells, the rhythm of daily life in the neighborhood where I was raised."

**Belonging & Community**
7. "I finally found a community where I truly belong. For the first time in my life, I feel accepted and at home."
8. "Home for me is not just a building — it is the feeling of being among my own people, where I am understood and loved."
9. "After years of searching, I realized that belonging is not about a place but about the people who make me feel like myself."

**Roots & Heritage**
10. "When I visit the village where my grandparents grew up, I feel a deep connection to my family's history and traditions."
11. "My grandmother used to tell me stories about our ancestors. Those stories made me proud of where my family comes from."
12. "I decided to trace my family's roots back to the old country. Discovering my heritage gave me a new sense of identity."

**Nostalgia & Homecoming**
13. "After living abroad for many years, I ache with longing for my homeland and the simple life I once knew there."
14. "I miss my hometown terribly — the familiar faces, the food, the sound of my mother tongue spoken on every corner."
15. "When I finally came back to the place where I grew up after so many years away, tears streamed down my face."

**Diaspora & Displacement**
16. "As an immigrant, I carry two worlds inside me. My heart is split between the country I left and the one I now call home."
17. "Being part of the diaspora means I am caught between cultures, always longing for a home that may no longer exist as I remember."
18. "My family was forced to leave our homeland, and starting over in a new country was the hardest thing I have ever done."

**Concept of Home**
19. "Home for me is where I feel safe and truly myself. It is the place I return to in my mind when the world feels too big."
20. "I have moved many times in my life, but the meaning of home — that deep yearning for a place to call my own — never fades."

### Tuning the Threshold

- **`0.30`** — More permissive. Catches loosely related content. Good for exploration.
- **`0.35`** — Balanced precision/recall.
- **`0.45`** — Default (High Precision). Higher relevance, preferred for personal narratives.
- **`0.50+`** — Very strict. Only strong matches pass.

Recommended workflow: run with `--limit 10`, inspect output with `python review.py`, adjust threshold, repeat.

---

## Performance

- **Streaming Matcher Pipeline**: Processes paragraphs as they are read from the network, providing near-instant feedback and low memory overhead.
- **Parallel GPU Acceleration**: The RTX 3080 profile distributes work across 7 worker processes.
- **Three-Stage Filtering**: Combines fast keyword pre-filtering (Stage 1), deep semantic matching (Stage 2), and narrative voice detection with **hard exclusion for song lyrics (choruses), advertisements, and non-narrative copy** (Stage 3).

| Metric | Historical estimate (RTX 3080; hardware/network dependent) |
|--------|----------|
| **Matching Startup** | **Near-instant** (via Streaming + Batched DB) |
| **Throughput** | ~20,000–40,000 pages/minute |
| **Worker Profile** | 7 workers (`MAX_WORKERS = 7`) |
| **RAM Usage** | Stable (via Keyword Pre-filtering) |

> **Requirement:** This profile pins `numpy==1.26.4` for the tested `sentence-transformers` stack.

---

## FAQ

**Q: Does this need a GPU?**
No, it runs on CPU, but a **CUDA GPU is highly recommended**. It will speed up the semantic scoring stage by ~10–20x. The application will automatically detect and use CUDA if available.

**Q: Does this call any external API?**
No. Everything runs locally. The only network traffic is downloading WET/ARC files from Common Crawl's public servers.

**Q: How much disk space do I need?**
~700 MB for models (one-time download). Output is small — a few MB per 1,000 files processed. No raw data is stored locally.

**Q: Can I run this on multiple machines?**
Use multiple machines as a serial checkpoint handoff: stop and push from one, then pull and resume on the other. Do not run two clones concurrently from the same checkpoint; `progress.db` is a binary Git LFS object and cannot be merged. Assigning different crawl IDs does not make the shared database mergeable. True simultaneous multi-PC processing requires separate databases plus an explicit reconciliation or centralized coordination workflow.

**Q: What if a WET/ARC file fails to download?**
It's marked as "failed" in the database and skipped. Failed files can be retried by resetting their status in the SQLite database. If an entire crawl's index is unavailable or returns a 404, the application logs a warning and gracefully skips to the next crawl without crashing.

**Q: Do I need AWS credentials?**
No, you do not need AWS credentials for the 119+ modern WET format datasets. However, because Common Crawl disabled anonymous listing on their legacy S3 buckets, the 3 legacy ARC datasets (2008-2012) require authenticated requests to build their file lists. If you do not have AWS credentials configured, the application will simply log an error (`NoCredentialsError`) and gracefully skip these oldest 3 datasets, allowing you to seamlessly process the modern datasets without needing an AWS account.

---

## 🎨 Related Projects (Hometown-XR Ecosystem)

This extractor is the data foundation for a broader immersive installation. Check out the other components:

*   **[Voice-to-Visual SDTD](https://github.com/WenjunII/voice-to-visual-sdtd)**: Real-time bridge turning spoken language into generative visuals using OpenAI Whisper and StreamDiffusionTD.
*   **[SHARP-TD Bridge](https://github.com/WenjunII/sharp-td-bridge)**: Real-time 3D Gaussian Splatting pipeline that transforms 2D generated frames into 3D particles within TouchDesigner.
*   **[Home Podcast Generator](https://github.com/WenjunII/home-podcast-generator)**: AI-powered system that transforms extracted personal narratives into synthetic podcast conversations.

---

## License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE) file for details.

This project processes publicly available Common Crawl data. Common Crawl data is available under the [Common Crawl Terms of Use](https://commoncrawl.org/terms-of-use).
