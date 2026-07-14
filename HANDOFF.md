# Workstation Handoff Guide

Use this procedure to move Hometown XR between the RTX 3080 and RTX 4090 PCs.
Never run the crawler on both PCs at the same time.

## Shared And Local State

Git and Git LFS synchronize source code, tests, documentation,
`data/checkpoints/progress.db.gz`, committed JSONL output, manifests, the
bounded evaluation replay reservoir, run history, and Markdown exports.

The following remain local to each PC:

- `.venv/`
- `data/models/`
- `data/hardware-profile.local.json`
- `data/cache/`
- `data/metrics/`
- `data/parquet/`
- `data/audits/`
- `data/progress.db` (restored working copy)
- uncheckpointed live candidate evaluation samples

This division lets both PCs use one checkpoint while retaining their own GPU
tuning and reproducible derived data.

## First-Time Setup

```powershell
git clone https://github.com/wenjunii/hometown-xr.git
cd hometown-xr
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080 -Tune
```

Use `-Profile 4090` on the other PC. Optionally tune each machine once:

```powershell
.\scripts\benchmark.ps1 -Profile 3080
```

The setup command's `-Tune` switch runs a shorter benchmark instead. Both forms
write only the ignored local hardware override for the current PC.

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

The script first confirms that the current branch is `main` and that
`origin/main` is not ahead. After the integrity checkpoint and commit, it
checks Git LFS, pushes explicitly to `origin/main`, and confirms that the local
and remote commit IDs match.

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
and then runs `doctor`, `status`, and `verify-output`. `-SkipVerify` skips the
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

Check the shared sample balance and next human-review action with:

```powershell
.\scripts\evaluation.ps1
```

Then resume with:

```powershell
.\scripts\run.ps1 -Profile 3080 run --all
```

Use profile `4090` on the other PC.

`data/parquet/` is derived and remains local to each workstation. To rebuild it
from the received checkpoint while safely dry-running the current filters, use:

```powershell
.\scripts\handoff.ps1 -Direction pull -Profile 3080 -RefreshResults
```

The equivalent standalone command is `.\scripts\refresh-results.ps1`. Neither
form reprocesses completed Common Crawl sources or replaces accepted JSONL
output unless `refresh-results.ps1` is separately given `-ApplyRefilter`.

## After A Crash

A normal `Ctrl+C` releases source claims immediately. After power loss, wait
for the 10-minute lease expiry, or recover after confirming no crawler process
is alive:

```powershell
python main.py recover --minutes 0
```

Failed sources retry automatically. To reset every failed source immediately:

```powershell
python main.py failures
python main.py retry --all
```

The failure report separates transient Common Crawl pressure from worker,
inference, and output failures. HTTP retries honor `Retry-After`, add jitter,
and temporarily reduce parser concurrency after transient failures. A
terminated process pool is rebuilt automatically up to three times.

## Conflict Rule

If both PCs accidentally produced commits, stop. Do not merge two
database archives or combine source shards manually. Keep both branches for
inspection, choose the checkpoint from the PC that ran most recently, and
resume serially from that checkpoint.
