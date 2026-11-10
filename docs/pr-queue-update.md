# PR Queue Update

Review date: 2026-11-10. Status: isolated candidate, not approved for production.

## November 10 Queue Review

The three new Dependabot PRs fail before tests on `main` because its legacy
dependency security policy expired on 2026-08-31. Do not renew that exception,
weaken the build floor, or merge past failed checks to clear the queue.

| PR | Candidate treatment |
| --- | --- |
| #57 | Update boto3/botocore to 1.43.126 across direct requirements, package metadata, tests, and both identical GPU locks. s3transfer 0.19.3 is already present. |
| #58 | All proposed pins are already present: certifi 2026.10.23, charset-normalizer 3.5.2, idna 3.20, and urllib3 2.8.0. |
| #59 | Update Ruff to 0.17.4. pytest 9.2.0 is already present. Retain setuptools `>=84.2.1,<85`, not the weaker proposed `>=75` floor. The Python 3.10-only tomli conditional is inapplicable to this Python 3.11/3.12 candidate. |

PyPI release metadata confirms non-yanked artifacts and compatible Python
requirements for boto3/botocore 1.43.126 and Ruff 0.17.4. Existing offline AWS
pagination/transfer tests cover the updated SDK without network access or
real credentials. Closing #57-#59 as superseded requires a passing combined
Windows/Linux Python 3.11/3.12 CI matrix, live four-manifest audit, and CodeQL.
Retain the source branches and leave #45 draft.

Local verification passes all 294 tests with boto3/botocore 1.43.126 and
s3transfer 0.19.3 installed in a disposable CPU-test environment. Ruff 0.17.4,
credential scans, dependency-profile checks, Python syntax checks, PowerShell
parsing, and that test environment's `pip check` pass. Fresh live audits cover
17 direct runtime pins, 56 pins per GPU lock, and 16 test pins, with no known
vulnerabilities or exceptions. This is not an installed GPU-runtime check.

No model libraries, weights, concepts, filters, precision, tuning, security
exceptions, migration gates, or production data are changed. The separately
prepared USB and existing isolated 3080 environment remain pinned to
`53140a01b83850f063b855be16293764d85c6654`; they are not silently upgraded.
Their dated results remain evidence for that exact candidate, not a new
GPU validation of these dependency changes. Final rollout still needs
consistent actual-GPU evidence and the existing human/historical reviews.

## November 3 Review

### Consolidation

PR #45 incorporates the applicable changes from #51-#55 without relaxing its
security policy or deploying the model/runtime migration to `main`.

| PR | Candidate treatment |
| --- | --- |
| #51 | boto3/botocore 1.43.123 and s3transfer 0.19.3; coordinated across direct requirements, package metadata, tests, and both identical GPU locks. |
| #52 | certifi 2026.10.23, charset-normalizer 3.5.2, idna 3.20, and urllib3 2.8.0 were already present in the candidate. |
| #53 | pytest 9.2.0 and Ruff 0.17.3; retain the patched setuptools floor `>=84.2.1,<85`, not the proposed `>=75` floor. The Python 3.10-only tomli conditional is inapplicable to this Python 3.11/3.12 candidate. |
| #54 | tqdm 4.70.1 across package metadata, direct requirements, and both GPU locks. |
| #55 | Preserve the complete cleanup policy, README section, preview-first cache helper, and ten focused tests through a history-preserving merge. |

All six newly installed packages have non-yanked PyPI releases and compatible
Python requirements. An offline AWS pagination/transfer-configuration test
covers the updated SDK without network access or real credentials. The model
libraries, pinned weights, concepts, filters, and seven-worker FP16 tuning are
unchanged.

### Validation

A fresh complete audit covers 17 direct runtime pins, 56 pins in each GPU
lock, and 16 direct test pins. No known vulnerabilities or advisory exceptions
are reported. [Current alert coverage](security-advisory-coverage.json)
compares all 325 open main-branch alert instances across 85 advisories with
candidate versions; none remain within the reported vulnerable ranges and
none are unverified. This does not close those alerts on `main`.

All 56 installed 3080 pins match and `pip check` passes. The installed audit
also reports no known vulnerabilities across 78 entries; it skips the local
editable project and CUDA-local Torch version, with upstream Torch 2.13.0
covered by the strict manifest audit. This is not a full Linux CUDA or
developer transitive lock.

The full-suite rerun passes all 294 tests, including the new offline AWS test
and ten cleanup tests. Ruff, compile checks, PowerShell parsing, and staged
and worktree credential scans pass.

Fresh actual-3080 FP16 capture reproduces all 400 scores, concepts, and
threshold decisions exactly, with zero score drift. The isolated five-source
benchmark processes 244,996 records in 226.078 seconds with seven configured
workers, no failures or process-pool restarts, and both historical matches
retained with no additions or removals. Its output digest remains
`59fa2022fb1c97423259873417cc290306138ed836075d1bd009589f1d168a0c`.
Timings vary with machine activity and are not a speedup claim.

The fresh sanitized 3080 bundle is exported and dry-run imported locally,
not applied to tracked evidence or committed with raw runtime data. Captures
ran against the combined working tree before the integration commit and
retain the preceding HEAD; manifest input hashes are recorded separately.

One initial Windows receiver test encountered a transient `WinError 5` at the
final staging-directory rename; its immediate targeted and full-suite reruns
passed. The
transfer implementation was not changed or its failure suppressed. Pytest
9.2 also emits a nonblocking AnyIO plugin deprecation warning about Pytest 10;
the warning is not silenced.

### Remaining Gates

Keep #45 in draft. The candidate's human campaign still has 0/100 judgments
(including 0/20 holdout), historical signatures remain unadopted with 176,012
reported unknown, and actual 4090/5090 snapshots, identical real-source
benchmarks, and cross-PC candidate consistency are missing. No labels,
historical signatures, GPU results, or approval artifacts were synthesized.

Close #51-#55 as superseded only after the combined integration head passes
Windows/Linux Python 3.11/3.12 CI, the live dependency audit, and CodeQL. Retain
their branches. Do not dismiss alerts, bypass checks, merge #45 to `main`, or
apply candidate packages to production. The production environment, raw
stories, labels, checkpoint rows, baseline, and backup store stay unchanged.
