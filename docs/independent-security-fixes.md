# Independent Security Fixes

Status: proposed for owner review on 2026-10-28, not a completed security
migration. The 5090 security gate remains blocked, intentionally and without
an additional exception. Do not bypass it to merge this proposal.

## Scope

| Package | Previous lock | Patched lock |
| --- | --- | --- |
| certifi | 2026.6.17 | 2026.10.23 |
| filelock | 3.29.0 | 4.2.1 |
| fsspec | 2026.4.0 | 2026.6.0 |
| Pillow | 12.3.0 | 13.0.0 |
| urllib3 | 2.7.0 | 2.8.0 |

These pins are shared by direct installs, both GPU locks, and tests.
Direct installs, tests, and the 3080/4090 lock also pin patched setuptools 84.2.1, previously
an unpinned runtime dependency of fasttext-wheel. Model libraries, weights,
keywords, thresholds, parser logic, and existing extracted results are unchanged.
Python 3.11 is required for the patched 3080/4090 environment. Setup preserves
an incompatible existing environment instead of deleting or upgrading it.

The GitHub alert snapshot contained 144 instances across filelock, fsspec,
Pillow, and urllib3, representing 48 distinct advisories.
The full audit additionally found the
[certifi CA-bundle advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2).
These are proposed fixes, not a claim that GitHub alerts have already closed.
Alerts are not dismissed; closure requires merged fixes and a GitHub rescan.

## Proposed Exception

The owner requested a short-lived model-stack exception for review.
The proposal in `.github/dependency-policy.json` runs from 2026-10-28 through
2026-11-11 UTC and has no automatic renewal. CI rejects expired policies, windows
longer than 14 days, different package versions, new IDs, and non-model exceptions.

It lists 50 exact audit IDs for sentence-transformers 2.7.0, Transformers
4.40.2, and Torch 2.1.0/2.12.1. Compared with the expired policy, five CVE IDs
are represented by their current PYSEC aliases, and two newly observed
model advisories are explicitly proposed for review:

- [Sentence Transformers, critical](https://github.com/advisories/GHSA-jhr6-gm9c-rqjv), PYSEC-2026-4164.
- [Transformers, high](https://github.com/advisories/GHSA-xrqw-3rrv-vx5w), PYSEC-2026-3929.

The retained model stack still has critical/high vulnerabilities. A passing
policy result is conditional risk acceptance, not a clean security audit;
the audit output reports `security_clean: false`. Load only the configured
pinned model revision, keep remote model code disabled, and never accept
arbitrary model files, checkpoints, tokenizers, or training configurations.
These controls reduce exposure but are not proof of safety.

## 5090 Blocker

Resolver checks for Windows Python 3.11 and 3.12 confirmed that retained
Torch 2.12.1 requires `setuptools<82`. Setuptools 81.0.0 is therefore now
explicit in the 5090 lock, exposing two previously unpinned advisories:

- [Windows path escape](https://github.com/advisories/GHSA-xmj3-9gf7-h52w), fixed in 84.1.0.
- [Source archive exclusion bypass](https://github.com/advisories/GHSA-h35f-9h28-mq5c), PYSEC-2026-3447, fixed in 83.0.0.

The owner explicitly declined a setuptools exception. The 5090 audit must
fail and setup stops before changing that environment. The existing Linux
CI job remains failing on this security gate; unit tests and CLI checks run
before it so their results are still visible. No required check is removed,
ignored, or marked continue-on-error.

Resolve the constraint through the separately validated
[model migration PR #45](https://github.com/wenjunii/hometown-xr/pull/45).
Do not install incompatible setuptools with `--no-deps`, ignore pip conflicts,
or merge around the security gate.

## 3080 Evidence

See [machine-readable evidence](independent-security-validation.json).

- Isolated Python 3.11.9 environment; the production checkout and its environment were untouched.
- 244 tests passed, including dependency round trips, exact version bounds, expiry, missing audit coverage, and the early 5090 setup stop.
- Lint, compile, PowerShell parsing, pip consistency, and all 43 installed GPU-lock pins passed.
- All 400 FP16 model samples matched the tracked baseline exactly: zero score drift, identical concepts and threshold decisions.
- Five CC-MAIN-2014-15 sources, seven configured workers, 244,996 records: five completed, zero failures, two matches retained, none added or removed.
- Real-source run: 159.359 seconds, cache disabled, output digest `59fa2022fb1c97423259873417cc290306138ed836075d1bd009589f1d168a0c`.
- Audited 17 direct pins, 43 pins in each GPU lock, and 16 test pins. The test manifest is clean; direct/3080 pins pass only under the proposed model exception; the 5090 lock fails on setuptools.
- All 68 installed packages were additionally audited; only the three unchanged model libraries had findings.

One local repeat hit Windows WinError 5 in unchanged story telemetry's atomic
file replacement. The isolated retry and full 244-test rerun passed; this
intermittent filesystem issue was not concealed by changing the unrelated code.

This is a bounded regression check, not full-corpus proof. No real 4090 or
5090 GPU validation is claimed. Human evaluation, full migration validation,
and removal of every exception remain required before the model upgrade.
No historical signatures, crawl progress, annotations, or production output
were adopted or rewritten by this work.
