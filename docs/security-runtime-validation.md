# Security Runtime Candidate

Status: **draft candidate, not approved for rollout**. Validation date: 2026-10-28.

This change consolidates the outstanding dependency updates and removes the
expired advisory allowances. It does not change the production environment,
model revision, concept anchors, filtering thresholds, source stories, or
tracked model baseline. No credentials or local environment files are included.

## Candidate

| Component | Version |
| --- | --- |
| Python | 3.11 or 3.12; locally tested with 3.11.9 |
| Torch | 2.13.0+cu130, all Windows GPU profiles |
| Sentence Transformers | 5.6.1 |
| Transformers | 5.10.4 |
| Pillow / filelock / fsspec | 13.0.0 / 4.2.1 / 2026.6.0 |
| urllib3 | 2.8.0 |

Transformers 5.10.0 was rejected because PyPI marks it yanked. The replacement
5.10.4 is a non-yanked patch release. Model loading requires the existing
pinned Safetensors snapshot, disables remote code, and uses eager attention.
The 3080 and 4090 move from CUDA 12.1 to CUDA 13.0; do not reuse a Python 3.10
virtual environment or assume a driver on another PC is compatible.

## Local Evidence

[Machine-readable validation summary](security-runtime-validation.json).

On the RTX 3080 Ti Laptop GPU with driver 617.42:

- FP16: all 400 scores, concepts, and threshold decisions exactly match the
  tracked baseline and a fresh capture from the original environment.
- FP32: all 400 concepts and threshold decisions match a fresh FP32 old-stack
  capture. Maximum score drift is 0.00000012 (limit 0.005).
- Real-source benchmark: five `CC-MAIN-2014-15` files completed with seven
  workers in 142.422 seconds, with zero failures or process-pool restarts.
  Both historical matches were reproduced; none were added or removed.
  The end-to-end validation helper repeated this in 123.906 seconds with the
  identical output digest. These timings are two measurements, not a speedup
  claim against the old runtime. Portable evidence is tracked in
  `data/checkpoints/profile-evidence/3080.json`.
- All four manifests pass the live advisory audit with no exceptions: 11 direct
  runtime pins, 56 pins in each GPU lock, and 10 direct test pins.
- `pip check` passes. Unit tests, Ruff, and PowerShell parsing are checked
  separately in CI on Windows/Linux and Python 3.11/3.12.

The local tuning file uses FP16, but an isolated worktree defaults to FP32.
Comparing those two modes initially changed one threshold decision, even with
the old libraries. Repeating in matching precision reproduced the old baseline
exactly. Future snapshots now record device, actual precision, and encoding
batch size. No baseline was overwritten and no comparison tolerance was relaxed.

The lock audit normalizes the Torch CUDA suffix for the upstream advisory
lookup, without installing GPU packages in CI. It rejects missing/skipped
coverage and malformed reports. A plain installed-environment `pip-audit` skips
Torch's CUDA-local version even when it prints "No known vulnerabilities found";
review skipped entries and use the manifest audit for Torch coverage.
The Windows locks do not pin Linux-only CUDA packages. Direct developer pins
are audited; their full resolved closure is not promised by the lock audit.

## Historical Audit

The isolated FP16 audit covered 26 unique historical source files, processing
1,152,555 records without failures. All seven historical matches were retained;
five additional matches appeared in two sources. These differences are not
treated as proof of equivalent historical output.

| Crawl | Sources | Historical matches | Candidate matches | Adoption eligible |
| --- | --- | --- | --- | --- |
| CC-MAIN-2013-20 | 5 | 1 | 1 | Yes |
| CC-MAIN-2013-48 | 5 | 1 | 1 | Yes |
| CC-MAIN-2014-10 | 5 | 2 | 3 | No: changed match set |
| CC-MAIN-2014-15 | 5 | 2 | 2 | Yes |
| CC-MAIN-2014-23 | 5 | 1 | 5 | No: changed match set |
| CC-MAIN-2026-12 | 1 | 0 | 0 | No: below five-source minimum |

The immutable reports are archived under `data/checkpoints/audit-evidence/`.
This is sample-based evidence, not a full replay of all 176012 completed
sources. No production output or historical signatures were changed. The
changed source sets require review, and the one-source crawl cannot satisfy
the current minimum; neither gate was weakened.

An isolated control replay of the two changed sources used the original
Python 3.10 runtime, Torch 2.1.0+cu121, Sentence Transformers 2.7.0, Transformers
4.40.2, and legacy model loader with the current filter contract and FP16.
It reproduced the candidate's eight normalized matches exactly, including all
five additions. Thus the dependency update did not introduce these differences
in the tested sources. This does not resolve why the older historical output
differs or authorize rewriting it. See the
[old-runtime control comparison](security-runtime-control-3080.json).

## Finish Validation

Keep the candidate in a separate checkout with its own virtual environment.
Install Python 3.11 or 3.12 and a compatible NVIDIA driver, then run these
commands from that checkout, substituting the actual profile. While the PR
is a draft, use `codex/security-runtime-refresh`, not `main`:

```powershell
git lfs pull
.\scripts\setup.ps1 -Profile 4090 -Dev
.\scripts\validate-runtime.ps1 -Profile 4090
```

Use `-Profile 5090` on that PC, and run only one workstation at a time. The
validation helper checks the actual GPU, installed pins, and live advisories,
then captures/compares a model snapshot, benchmarks the same five sources with
seven workers, and exports and validates a credential-free evidence bundle.
It stops on a failed check or historical output mismatch. FP16 is explicit for validation
because the tracked baseline is FP16; local tuning files are not modified.

Do not copy credentials, caches, or the old virtual environment. A deliberate
precision change requires separate review, not a replacement baseline just to
make a check green. Lower-level `model-validation.ps1 -Action capture` and
`benchmark.ps1 -Real` also accept `-Precision fp16` or `-Precision fp32`.
Snapshots and workload evidence preserve the selected precision; workload
reports list the exact source files for cross-PC comparison.

The exported file is `data/evidence-outbox/profile-evidence-4090.json` (or
`profile-evidence-5090.json` in that folder). Bring that JSON back for validation and
import; do not sync the virtual environment. Compare its source list and
output digest with the 3080 evidence before any migration approval.

Run the same real-source benchmark on the 4090 and 5090 and compare it with
the fresh 3080 evidence. Historical metrics do not establish performance or
output identity for this new candidate. Finish the guided human-label and
blind-holdout campaign, and verify historical filter signatures using the isolated audit
workflow in the main README. Then check the existing final gate:

```powershell
.\scripts\model-migration.ps1 -Action validate
```

At local validation the gate is correctly blocked: no human labels or holdout
labels, 176012 unknown historical source signatures, and no 4090/5090 candidate
snapshots or matching real-source measurements. The draft PR must not be
merged/promoted until the required evidence is reviewed. No approval artifact
has been written.

The guided campaign has 100 queued judgments, including the blind holdout.
Start or resume it with `.\scripts\evaluation.ps1 -Action serve -OpenBrowser` in
the candidate environment. Human judgments are never synthesized by the
validation helper. The helper does not adopt audit signatures, replace
historical output, or approve the migration.

## Dependency PRs

This candidate includes or supersedes the updates proposed by #25, #26, #29,
#32, #35, #36, #37, #38, #39, #40, #41, #42, #43, and #44. The newer fixed
versions replace older proposals; migration-gated libraries remain manually
coordinated. Dependabot alerts will remain on the default branch until a
validated update is merged and GitHub rescans the manifests.

After approval, stop project processes, preserve the old environment under a
backup name, and use `setup.ps1` on each PC. Setup refuses Python 3.10, checks
the installed lock versions and `pip check`, and never deletes the old
environment. Keep source data and checkpoints intact.
