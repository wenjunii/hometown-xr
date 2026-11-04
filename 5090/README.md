# RTX 5090 Compatibility Launchers

The extractor has one shared implementation in the repository root. Files in
this directory provide visible compatibility commands while selecting the
`5090` hardware profile.

```powershell
python 5090/main.py status
python 5090/main.py run --all
python 5090/review.py --limit 20
```

The preferred equivalent is:

```powershell
.\scripts\run.ps1 -Profile 5090 run --all
```

Both forms use the root `data/` checkpoint and output directories. The handoff
restores the same compressed Git LFS database archive used by the 3080 and 4090
PCs. Never run more than one workstation at a time.

First create the Blackwell-compatible environment:

```powershell
.\scripts\setup.ps1 -Profile 5090 -Tune
```

The proposed 5090 setup uses `requirements-lock-5090.txt` with PyTorch
`2.13.0+cu130`, Python 3.11 or 3.12, and a CUDA 13.0-compatible NVIDIA driver.
The 3080 and 4090 lock now has the same package versions. Per-PC batch sizes
remain separate. This is a migration candidate: the 5090 needs its own model
comparison and real-source benchmark before rollout. See
[validation and rollout](../docs/security-runtime-validation.md).

Before resuming a checkpoint from another PC, receive and verify it with:

```powershell
.\scripts\handoff.ps1 -Direction pull -Profile 5090
```

The tracked `5090` profile starts with seven CPU parser workers feeding one
shared GPU model with candidate/inference/encoding batches of
`200`/`2400`/`512`. Adaptive batching halves the encoding batch after a CUDA
out-of-memory error. Benchmark results are written to the ignored
`data/hardware-profile.local.json`, so tuning on this PC cannot change either
older workstation.
