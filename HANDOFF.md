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

## First-Time Setup on Another RTX 3080

Install Git, Git LFS, Python 3.10+, and a current NVIDIA driver. Then open PowerShell:

```powershell
git lfs install
git clone https://github.com/WenjunII/Hometown-XR.git
cd Hometown-XR
git lfs pull
git status --short --branch
git lfs ls-files
```

The status should be clean, and `git lfs ls-files` should list `data/progress.db`. Create a machine-local environment:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip

# Install the repository's currently documented CUDA 12.1 build first.
python -m pip install torch --index-url https://download.pytorch.org/whl/cu121
python -m pip install -r requirements.txt

python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
python main.py status
```

If CUDA 12.1 does not suit the receiving PC's driver, use the current Windows/Pip/CUDA command from the [official PyTorch installer](https://pytorch.org/get-started/locally/) and run it before `requirements.txt`.

Both RTX 3080 PCs run the root entry point:

```powershell
python main.py run --all
```

The `4090/main.py` entry point is only for the RTX 4090 profile.

## Send a Checkpoint from the Active PC

1. Press `Ctrl+C` once and wait until the crawler and all Python worker processes have exited.
2. Run `python main.py status`. The checkpoint must show `Processing: 0`. If it does not, keep the crawler stopped. Once the rows have been stale for more than one hour, opening the progress tracker recovers them to `pending`; verify status again before continuing.
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
   git commit -m "data: checkpoint crawl progress from 3080"
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
python main.py status
```

Do not continue if the first status command shows local changes or if `git pull --ff-only` refuses to update. That indicates divergent work requiring deliberate reconciliation. Once the tree is clean and the checkpoint shows `Processing: 0`, resume with `python main.py run --all`.

## Switching Back

Repeat the same sequence in the opposite direction: stop and push from the currently active PC, verify a clean remote checkpoint, then pull and resume on the other PC. Never have both crawlers active from copies of the same checkpoint.
