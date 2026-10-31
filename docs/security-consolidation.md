# Consolidated Security Candidate

Status: draft, not approved for production. Rechecked on 2026-10-30.

PR #45 now combines the patched runtime with the local-first handoff and
code-only publishing work from #49. Production remains on its existing runtime
and verified USB checkpoint. No environments, raw stories, review judgments,
historical signatures, or production checkpoint rows were replaced.

## Current Alert Coverage

[Machine-readable coverage](security-advisory-coverage.json) compares all 325
open default-branch alert instances, representing 85 distinct advisories, with
the candidate's exact pins in all six affected manifests. No candidate version
falls in those reported vulnerable ranges; no alert instance is unverified.
This does not close any alert on `main`.

A fresh independent live audit also covers all pinned entries in the four
audited manifests: 17 direct pins, 56 pins in each Windows GPU lock, and 16
direct test pins. It reports no known vulnerabilities and uses no exceptions.
The installed-environment audit has 78 entries and no reported vulnerabilities;
it skips the local project and CUDA-local Torch version. The source project is
tested/scanned separately, and the strict manifest audit covers upstream Torch
2.13.0 explicitly. This is not a complete Linux CUDA or developer transitive lock.

Candidate versions include certifi 2026.10.23, filelock 4.2.1, fsspec 2026.6.0,
Pillow 13.0.0, urllib3 2.8.0, setuptools 84.2.1, Torch 2.13.0+cu130,
Sentence Transformers 5.6.1, and Transformers 5.10.4. The new Certifi proposals
#47/#48 use 2026.10.18 and are superseded by this newer coordinated pin.

## Integration Validation

The combined tree passes 278 local tests, Ruff, compile checks, PowerShell
parsing, `pip check`, and every installed 3080 lock pin. It keeps the full
Windows/Linux Python 3.11/3.12 matrix and audits all four manifests once per CI
run. Required profile and vulnerability checks remain blocking; no skip or
continue-on-error was added. Duplicate feature push/PR runs are avoided.

A fresh FP16 model capture on the actual 3080 matches all 400 baseline scores,
concepts, and threshold decisions exactly, with zero maximum score drift.
The isolated real-source benchmark through `scripts/validate-runtime.ps1`
completed five sources and 244,996 records with seven workers in 175.094
seconds, with zero failures or process-pool restarts. Both historical matches
were reproduced; none were added or removed. The output digest remains
`59fa2022fb1c97423259873417cc290306138ed836075d1bd009589f1d168a0c`.
Timings vary with the machine and cache state and are not a speedup claim.
These captures ran against the combined working tree before its integration
commit; the installed runtime versions were unchanged by the direct-pin additions.
No new 4090/5090 evidence is claimed.

## PR Consolidation

After the combined candidate's checks pass, #46, #47, #48, and #49 can be closed
as superseded, without deleting their branches or dismissing any security
alerts. #46's six explicit direct-install security pins are preserved in the
runtime, test, and package manifests, along with the patched build-tool floor
and dependency smoke tests; its proposed temporary allowances are not used here.
Its legacy
5090 setuptools conflict is resolved only by the coordinated candidate runtime,
not by an exception or an assertion of real 5090 validation.

## Remaining Approval Gates

- Complete the 100 human judgments, including the 20-example blind holdout.
  The current campaign has zero completed judgments; none were synthesized.
- Review/adopt only eligible historical-filter evidence. The existing gate
  still reports 176,012 completed source signatures as unknown. Earlier sampled
  output differences and insufficient coverage are not silently approved.
- Run `scripts/validate-runtime.ps1 -Profile 4090` on the actual 4090 and the
  same command with profile 5090 on that PC, in isolated candidate environments.
  Keep FP16 consistent with the baseline. No copied or emulated GPU results.
- Bring the sanitized evidence bundles back, compare source lists/output
  digests/library versions, and rerun `scripts/model-migration.ps1 -Action validate`.

Until these gates pass and the owner approves rollout, keep #45 in draft and
do not merge or deploy it. Default-branch Dependabot alerts remain open until
the validated fixes are merged and GitHub rescans. Green CI and version-range
coverage are not migration approval.
