# Workstation Handoff Guide

This repository transfers a complete, resumable crawl checkpoint between RTX 3080 and RTX 4090 workstations through GitHub.

> [!IMPORTANT]
> GitHub is a **serial checkpoint transfer**, not a live shared database. Only one workstation may run the crawler from the synchronized checkpoint at a time. Do not start two PCs from the same commit and later push both: `data/progress.db` is a binary Git LFS object, and the compressed output shards are not mergeable. Using different crawl IDs does not make the shared database mergeable.

## What GitHub Synchronizes

- All source code and hardware profiles
- `data/progress.db` through Git LFS
- `data/output/` compressed JSONL results
- `data/exports/` readable Markdown exports
- Documentation and configuration

Python environments, bytecode caches, machine-local settings, and ML model caches are intentionally excluded. The FastText and sentence-transformer models download automatically on first use. Each database checkpoint creates another roughly 144 MB LFS object, so create checkpoints at handoffs rather than after every small batch.

## First-Time Setup on Either Workstation

Install Git, Git LFS, Python 3.10+, and a current NVIDIA driver. Then open PowerShell:

```powershell
git lfs install
git clone https://github.com/wenjunii/hometown-xr.git
cd hometown-xr
git lfs pull
git status --short --branch
git lfs ls-files
```

The status should be clean, and `git lfs ls-files` should list `data/progress.db`. From the repository root, create a machine-local environment and install the tested CUDA stack:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install "torch==2.1.0+cu121" --index-url https://download.pytorch.org/whl/cu121
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

Install and verify exactly one hardware profile from the repository root.

RTX 3080:

```powershell
python -m pip install -r requirements.txt
python main.py status
python main.py run --all
```

RTX 4090:

```powershell
python -m pip install -r 4090/requirements.txt
python 4090/main.py status
python 4090/main.py run --all
```

Both profiles use the same filters, shared checkpoint, and seven-worker configuration. The direct dependencies are pinned to the versions tested on both machines. Update both requirements files together when changing the ML stack. Never run both profile commands at the same time.

## Send a Checkpoint from the Active PC

1. Press `Ctrl+C` once and wait until the crawler and all Python worker processes have exited.
2. Run the active profile's status command: `python main.py status` on the RTX 3080 or `python 4090/main.py status` on the RTX 4090. The checkpoint must show `Processing: 0`. If it does not, keep the crawler stopped. Once the rows have been stale for more than one hour, opening the progress tracker recovers them to `pending`; verify status again before continuing.
3. Confirm there are no live SQLite sidecars named `progress.db-wal`, `progress.db-shm`, or `progress.db-journal`.
4. Fetch GitHub and verify that the active PC started from the current remote commit:

   ```powershell
   git fetch origin
   git rev-list --left-right --count HEAD...origin/main
   ```

   The command must print `0 0`. If it does not, stop and reconcile the machines before staging; never resolve a `progress.db` conflict by blindly choosing "ours" or "theirs."

5. Review, commit, and push the complete checkpoint:

   ```powershell
   git add -A
   git status --short
   git diff --cached --stat
   git commit -m "data: checkpoint crawl progress from WORKSTATION"
   git push origin main
   git status --short --branch
   git lfs status
   ```

The final Git status must be clean and aligned with `origin/main` before the receiving PC starts.

## Receive the Checkpoint on the Other PC

The receiving PC must not have a crawler running or unsaved local progress.

```powershell
git status --short
git fetch origin
git pull --ff-only origin main
git lfs pull
git status --short --branch
git lfs ls-files
```

Do not continue if the first status command shows local changes or if `git pull --ff-only` refuses to update. That indicates divergent work requiring deliberate reconciliation.

From the repository root, run exactly one profile's status command:

```powershell
# RTX 3080
python main.py status

# RTX 4090
python 4090/main.py status
```

Once the tree is clean and the checkpoint shows `Processing: 0`, resume exactly one profile:

```powershell
# RTX 3080
python main.py run --all

# RTX 4090
python 4090/main.py run --all
```

## Switching Back

Repeat the same sequence in the opposite direction: stop and push from the currently active PC, verify a clean remote checkpoint, then pull and resume on the other PC. Never have both crawlers active from copies of the same checkpoint.
