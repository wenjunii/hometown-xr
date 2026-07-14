# RTX 4090 Compatibility Launchers

The extractor now has one shared implementation in the repository root. Files
in this directory only preserve the older commands while selecting the `4090`
hardware profile.

```powershell
python 4090/main.py status
python 4090/main.py run --all
python 4090/review.py --limit 20
```

The preferred equivalent is:

```powershell
.\scripts\run.ps1 -Profile 4090 run --all
```

Both forms use the root `data/` checkpoint and output directories. The handoff
restores the same compressed Git LFS database archive used by the 3080 PC.

Before resuming a checkpoint from the 3080 PC, receive and verify it with:

```powershell
.\scripts\handoff.ps1 -Direction pull -Profile 4090
```

Add `-RefreshResults` to rebuild this PC's ignored canonical Parquet dataset
after receiving a checkpoint. This dry-runs current filters and does not recrawl
completed Common Crawl sources.

The tracked `4090` profile uses seven CPU parser workers feeding one shared GPU
model with candidate/inference/encoding batches of `150`/`1600`/`256`. Run
`.\scripts\benchmark.ps1 -Profile 4090` on that PC to create its ignored local
autotuning override.
