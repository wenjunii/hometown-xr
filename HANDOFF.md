# Workstation Handoff Guide

Use this procedure to move the Hometown XR extractor between the RTX 3080 PC
and RTX 4090 PC. Never run the crawler on both PCs at the same time.

## What Is Synchronized

Git and Git LFS synchronize:

- All source code, tests, scripts, and documentation
- `data/progress.db` through Git LFS
- Committed `data/output/` JSONL shards
- Committed `data/exports/` Markdown files

The virtual environment and `data/models/` are machine-local and ignored.

## First-Time Setup

On each PC:

```powershell
git clone https://github.com/wenjunii/hometown-xr.git
cd hometown-xr
git lfs install
git lfs pull
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -Profile 3080
```

Use `-Profile 4090` on the 4090 PC.

## Send A Checkpoint

On the currently active PC:

1. Press `Ctrl+C` once.
2. Wait for active workers to return their claims and for the final summary.
3. Confirm the local lock is gone:

```powershell
Test-Path .\data\.crawler.lock
```

The result must be `False`.

Inspect and commit the checkpoint:

```powershell
git status --short
git add --all
git commit -m "checkpoint: hand off crawler state"
.\scripts\handoff.ps1 -Direction push
```

Do not copy a live `progress.db`, its WAL sidecars, or partially staged output.
The crawler's clean shutdown guarantees that interrupted source output was not
committed and those source rows are back in `pending`.

## Receive A Checkpoint

On the destination PC, with no local changes:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\handoff.ps1 -Direction pull
.\.venv\Scripts\python.exe main.py doctor --profile 3080
.\.venv\Scripts\python.exe main.py status
```

Use `--profile 4090` on the 4090 PC. If dependencies changed since the last
handoff, rerun `scripts/setup.ps1`.

Start the crawler:

```powershell
.\scripts\run.ps1 -Profile 3080 run --all
```

or:

```powershell
.\scripts\run.ps1 -Profile 4090 run --all
```

## After A Crash

A normal `Ctrl+C` releases claims immediately. After a hard power loss, the
last active rows remain leased for 10 minutes to protect a worker that may still
be alive.

Once you have confirmed no crawler process is running, recover immediately with:

```powershell
python main.py recover --minutes 0
```

Failed sources retry automatically after backoff. To retry every failed source
immediately, including attempts that reached the limit:

```powershell
python main.py retry --all
```

## Conflict Rule

If both PCs accidentally created commits, stop. Do not merge two versions of
`data/progress.db` or combine two sets of source shards by hand. Choose the
checkpoint from the PC that ran most recently, preserve the other branch for
inspection, and resume from the chosen serial checkpoint.
