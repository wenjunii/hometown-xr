# Workstation Handoff Guide

Use this procedure to move Hometown XR between the RTX 3080 and RTX 4090 PCs.
Never run the crawler on both PCs at the same time.

## Shared And Local State

Git and Git LFS synchronize source code, tests, documentation,
`data/progress.db`, committed JSONL output, manifests, and Markdown exports.

The following remain local to each PC:

- `.venv/`
- `data/models/`
- `data/hardware-profile.local.json`
- `data/cache/`
- `data/metrics/`
- `data/parquet/`
- live candidate evaluation samples

This division lets both PCs use one checkpoint while retaining their own GPU
tuning and reproducible derived data.

## First-Time Setup

```powershell
git clone https://github.com/wenjunii/hometown-xr.git
cd hometown-xr
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080
```

Use `-Profile 4090` on the other PC. Optionally tune each machine once:

```powershell
.\scripts\benchmark.ps1 -Profile 3080
```

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
.\scripts\handoff.ps1 -Direction pull
.\.venv\Scripts\python.exe main.py doctor --profile 3080
.\.venv\Scripts\python.exe main.py status
.\.venv\Scripts\python.exe main.py verify-output
.\.venv\Scripts\python.exe main.py cache stats
```

Rerun `scripts\setup.ps1` whenever dependency lock files changed. Resume with:

```powershell
.\scripts\run.ps1 -Profile 3080 run --all
```

Use profile `4090` on the other PC.

## After A Crash

A normal `Ctrl+C` releases source claims immediately. After power loss, wait
for the 10-minute lease expiry, or recover after confirming no crawler process
is alive:

```powershell
python main.py recover --minutes 0
```

Failed sources retry automatically. To reset every failed source immediately:

```powershell
python main.py retry --all
```

## Conflict Rule

If both PCs accidentally produced commits, stop. Do not merge two
`data/progress.db` files or combine source shards manually. Keep both branches
for inspection, choose the checkpoint from the PC that ran most recently, and
resume serially from that checkpoint.
