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
pulls Git LFS objects, and then runs `doctor`, `status`, and `verify-output`.
Use `-SkipVerify` only for maintenance when the local Python environment has
not been installed yet. Rerun `scripts\setup.ps1` whenever dependency lock
files changed. Resume with:

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
python main.py retry --all
```

## Conflict Rule

If both PCs accidentally produced commits, stop. Do not merge two
`data/progress.db` files or combine source shards manually. Keep both branches
for inspection, choose the checkpoint from the PC that ran most recently, and
resume serially from that checkpoint.
