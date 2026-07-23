# Workstation Handoff Guide

Use this procedure to move Hometown XR among the RTX 3080, RTX 4090, and
RTX 5090 PCs. Never run the crawler on more than one PC at a time.

## Shared And Local State

Git and Git LFS synchronize source code, tests, documentation,
`data/checkpoints/progress.db.gz`, committed JSONL output, manifests,
`data/stories/` source-context fragments, the bounded evaluation replay
reservoir, run history, and Markdown/structured exports.
Together, those files are the complete durable project state required to resume
on another workstation.
The tracked evaluation state also includes the semantic model baseline; model
candidate and comparison reports stay local to the workstation that generated
them.

The following remain local to each PC:

- `.venv/`
- `data/models/`
- `data/hardware-profile.local.json`
- `data/cache/`
- `data/metrics/`
- `data/parquet/`
- `data/audits/`
- `data/evaluation/model-candidate*.json`
- `data/evaluation/model-comparison*.json`
- `data/dependency-audit.local.json`
- `data/progress.db` (restored working copy)
- uncheckpointed live candidate evaluation samples
- `.env*`, credential files, private keys, and service-account files

This division lets all PCs use one checkpoint while retaining their own GPU
tuning and reproducible derived data.

The checkpoint script stages the complete tracked project, then scans staged
filenames and file contents before it creates a commit. Files with findings are
unstaged, remain local, and stop the checkpoint. Keep secrets outside the
repository even when they are already covered by `.gitignore`.

Run a read-only scan before any handoff when local configuration changed:

```powershell
.\scripts\security-check.ps1
```

The default covers tracked and non-ignored worktree files. The scanner reports
only paths, line numbers, and rule names; it never prints detected values.

## First-Time Setup

```powershell
git clone https://github.com/wenjunii/hometown-xr.git
cd hometown-xr
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080 -Tune
```

Use `-Profile 4090` or `-Profile 5090` on the corresponding PC. The 5090 setup
selects the Blackwell-compatible CUDA 13.0 PyTorch lock automatically.
Optionally tune each machine once:

```powershell
.\scripts\benchmark.ps1 -Profile 3080
```

The setup command's `-Tune` switch runs a shorter benchmark instead. Both forms
write only the ignored local hardware override for the current PC.

Confirm the complete local state after setup:

```powershell
.\scripts\health.ps1 -Profile 3080 -Full -Strict
```

To measure parser concurrency against real data, compare identical completed
sources without changing the override:

```powershell
.\scripts\benchmark.ps1 -Profile 3080 -Real -Sources 5 -WorkerCount 1,4,7
```

Only add `-Apply` when all trials complete and report the same normalized output
digest. The tracked defaults remain seven workers on all PCs; any applied
override stays local to the machine that measured it.

## Send A Checkpoint

1. Press `Ctrl+C` once.
2. Wait for active parsers to return their source leases.
3. Confirm the lock is absent:

```powershell
Test-Path .\data\.crawler.lock
```

The result must be `False`.

Then verify output, compact metadata, commit every tracked project change, and
push in one command:

```powershell
.\scripts\checkpoint.ps1 -Message "checkpoint: hand off crawler state"
```

The script requires a named branch and confirms that its matching remote branch
is not ahead. After the integrity checkpoint and commit, it checks Git LFS,
pushes the current branch, and confirms that the local and remote commit IDs
match. Both workstations must check out the same branch. Because `main` is
protected, use a shared working branch until its pull request is approved and
merged; the script does not bypass repository protection.

Checkpointing verifies every output checksum, compacts manifests and SQLite,
merges replay/run history, and creates the deterministic compressed database
archive before Git stages anything.

The compatibility form is
`.\scripts\handoff.ps1 -Direction push -Message "checkpoint: hand off crawler state"`.
Use `-NoPush` with `checkpoint.ps1` to create the verified local commit without
sending it. `-ForceVacuum` forces a full SQLite vacuum; normal checkpoints
vacuum only after a schema migration or when enough free pages exist.

Do not copy a live SQLite database, WAL sidecar, staging directory, or Parquet
export. A clean shutdown leaves interrupted sources pending and keeps their old
committed output intact.

## Receive A Checkpoint

The destination worktree must be clean:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\handoff.ps1 -Direction pull -Profile 3080
```

The receive script checks every Git command, permits only a fast-forward pull,
refuses to overwrite a working database that differs from its current archive,
pulls Git LFS objects, atomically restores and validates `data/progress.db`,
and then runs `health --full --strict`, covering the runtime profile, Git state,
database digest, active leases, dependency locks, filter/evaluation readiness,
hardware metrics, model baseline, and output checks. `-SkipVerify` skips those
diagnostics but still restores the checkpoint, so a virtual environment is
required. Rerun `scripts\setup.ps1` whenever dependency lock files changed.

During the one-time upgrade from the formerly tracked raw database, any command
that opens a missing project DB also restores the validated archive. This keeps
the first pull from an older handoff script safe.

Inspect filter-signature coverage without changing checkpoint state:

```powershell
.\scripts\filter-state.ps1
```

After a recall-affecting filter change, plan an isolated audit before stamping
or resetting historical work:

```powershell
.\scripts\audit.ps1 -PerCrawl 2
```

Audit databases and output remain local. Their sampled decisions merge into
the shared evaluation replay at the next checkpoint.
Audit samples are tuning evidence because the selected sources are stratified;
normal crawl runs supply the probability-sampled benchmark rows used for
weighted recall estimates.
A quick two-source audit cannot authorize signature adoption. For that, run at
least five sources per crawl and provide its report explicitly:

```powershell
.\scripts\audit.ps1 -Action run -Profile 3080 -PerCrawl 5 -Apply
.\scripts\filter-state.ps1 -Action stamp-current `
  -AuditReport .\data\audits\AUDIT_ID\report.json -Apply
```

The database stores the audit ID and report hash, while the validated report is
copied into tracked `data/checkpoints/audit-evidence/` for the next PC. A
mismatched signature, incomplete source, changed normalized match set, or
ineligible crawl blocks the adoption and leaves historical state unchanged.

Check the shared sample balance and next human-review action with:

```powershell
.\scripts\evaluation.ps1
.\scripts\evaluation.ps1 -Action plan
.\scripts\evaluation.ps1 -Action serve -OpenBrowser
.\scripts\evaluation.ps1 -Action multilingual
```

Representative rows keep a stable tuning/holdout split across machines. The
local crawler also records low-rate pre-keyword shadow samples; checkpointing
merges them into the shared replay without copying machine-local candidate files.
The localhost workbench hides all model evidence for representative holdout
rows. The multilingual report identifies languages that still need samples and
human-labeled keyword misses.

When a dependency, CUDA, precision, or model change is proposed, capture a
candidate on that workstation and compare it with the tracked baseline:

```powershell
.\scripts\model-validation.ps1 -Action capture -Profile 4090
.\scripts\model-validation.ps1 -Action compare -Profile 4090
.\scripts\dependency-audit.ps1
```

Use the matching profile on each PC. Candidate and comparison files are ignored;
the baseline and dated dependency policy are shared. A model-stack migration is
not complete until comparisons pass on the 3080, 4090, and 5090 and the
human-labeled evaluation minimums are met.

Review recent shared runs or compare all hardware profiles with:

```powershell
python main.py metrics --history --limit 20
python main.py metrics --compare-profiles
```

Then resume with:

```powershell
.\scripts\run.ps1 -Profile 3080 run --all --strategy yield-aware --chunk-size 100
```

Use profile `4090` or `5090` on the corresponding PC. Yield-aware mode re-ranks
crawl chunks from smoothed historical matches per completed source while
preserving exploration and a full rotation through ready crawls.

`data/parquet/` is derived and remains local to each workstation. To rebuild it
from the received checkpoint while safely dry-running the current filters, use:

```powershell
.\scripts\handoff.ps1 -Direction pull -Profile 3080 -RefreshResults
```

The equivalent standalone command is `.\scripts\refresh-results.ps1`. Neither
form reprocesses completed Common Crawl sources or replaces accepted JSONL
output unless `refresh-results.ps1` is separately given `-ApplyRefilter`.
The derived schema-5 dataset adds `passages/` with adjacent-story reconstruction
and explainable place/time candidates; paragraph-level `stories/`, complete
`provenance/`, and the curated view remain intact.

Source-context story fragments are durable shared state, unlike Parquet. After
receiving a checkpoint, inspect and resume their exact-source backfill with:

```powershell
.\scripts\stories.ps1 -Action status -Limit 10
.\scripts\stories.ps1 -Action enrich -Limit 10 -Apply
.\scripts\stories.ps1 -Action export
```

Only one PC may run story enrichment at a time. It does not change canonical
matches or the crawl database, and each completed source fragment is resumable.
Commit the fragments and exports with the normal checkpoint command before
moving to another PC.

## After A Crash

A normal `Ctrl+C` releases source claims immediately. After power loss, wait
for the 10-minute lease expiry, or recover after confirming no crawler process
is alive:

```powershell
python main.py recover --minutes 0
```

Failed sources retry automatically. To inspect failures and reset a bounded
batch immediately:

```powershell
python main.py failures
.\scripts\retry.ps1 -All -Category http_503 -Limit 25
.\scripts\retry.ps1 -All -Category http_503 -Limit 25 -Apply
```

The first retry command is a dry run. `-Apply` resets only the deterministic,
bounded category batch; omit `-Category` only after reviewing the full report.
The failure report separates transient Common Crawl pressure from worker,
inference, and output failures. HTTP retries honor `Retry-After`, add jitter,
temporarily reduce parser concurrency, and open an escalating shared cooldown
after 429/503 pressure. A terminated process pool is rebuilt automatically up
to three times; healthy pools are periodically recycled after bounded work or a
high-RAM worker. Attempt-exhausted sources are reported as quarantined and are
left untouched until a bounded operator retry.

The current filter contract includes native semantic anchors for all 20 keyword
languages. Pulling this code does not rewrite historical output or reset the
checkpoint. Before adopting the new signature, run a bounded isolated audit;
only then choose evidence-backed stamping or a bounded selective recrawl.

## Conflict Rule

If multiple PCs accidentally produced commits, stop. Do not merge their
database archives or combine source shards manually. Keep every branch for
inspection, choose the checkpoint from the PC that ran most recently, and
resume serially from that checkpoint.
