# RTX 5090 Compatibility Launchers

**Security blocked:** do not run setup on this profile yet. The unchanged
Torch 2.12.1 requires setuptools below 82; the compatible 81.0.0 pin has two
known advisories and no approved exception. Setup stops before modifying the
environment and the dependency audit fails. The coordinated migration in
[PR #45](https://github.com/wenjunii/hometown-xr/pull/45) must resolve this first.

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

Use 64-bit Python 3.11 or 3.12. Preserve any existing Python 3.10 environment
before setup; the patched Pillow/filelock versions require at least 3.11.

```powershell
.\scripts\setup.ps1 -Profile 5090 -Tune
```

The 5090 setup uses the separate `requirements-lock-5090.txt` lock with stable
PyTorch 2.12.1 and its CUDA 13.0 runtime. This is required because the CUDA 12.1
PyTorch build retained for the 3080 and 4090 does not support the 5090's
Blackwell architecture.

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
