# Local-First Workstation Handoffs

GitHub remains optional. This mode moves portable code and durable research
state through a shared folder, NAS, or one external SSD. It does not need Git,
Git LFS, a GitHub account, or a new server. Common Crawl access and initial
dependency/model downloads still need internet unless available locally.

On this candidate branch, the receiver needs only Python 3.10+, while the
patched runtime requires 64-bit Python 3.11 or 3.12. Do not apply the candidate
runtime to production before the separate migration gate passes.

## Before Switching

Stop crawlers, enrichment, recovery jobs, and both human-review workbenches on
all PCs. Only one PC may write project state. If the old Git handoff mode owns
a lease, finish its verified checkpoint/release before changing modes; a local
Git owner token blocks the new export. Keep pending security branches and
their worktrees. This feature neither approves dependency migrations nor
changes their validation requirements.

Every participating PC must use this mode, the SAME store, and the newest
snapshot. Do not keep running an older Git-mode checkout. A copied store is a
separate authority: it cannot coordinate with the original. Do not use a
bidirectional cloud-sync folder as a distributed lock. A NAS must support
exclusive file creation and atomic rename. For USB, physically move the same
drive, stop the previous PC first, and keep it attached throughout a run.
Disconnected PCs cannot independently prove that another PC is stopped.

## First Export

From the current project, preview the transfer, then explicitly apply it:

```powershell
.\scripts\transfer.ps1 -Action export -Store E:\HometownXR-store
.\scripts\transfer.ps1 -Action export -Store E:\HometownXR-store -Apply
```

The store must be outside the checkout. This leaves original project data
unchanged and writes `data/handoff.local.json` to select folder mode locally.
No code is staged, committed, or pushed. No extraction or LLM is run. A fresh
SQLite backup and matching compressed database archive go into the snapshot.
File hashes, archive CRCs, database integrity, and known credential patterns
(including decoded gzip text and SQLite text) are checked before publication.
Accepted-output manifests are verified. Missing story fragments are hydrated
from verified packs, and updated fragments are repacked only inside staging;
source story text stays byte-for-byte unchanged. Stored LFS pointer files are
not mistaken for actual research data. Progress logs cover long database scans.
Source changes detected during export stop publication. Keep writers stopped.

## Receive On Another PC

The store includes a receiver tool. On the destination PC, it needs only
Python 3.10+, not GitHub access or a GPU environment. Read the current snapshot
identifier and use its receiver to preview and restore into a NEW directory:

```powershell
$Snapshot = (Get-Content E:\HometownXR-store\control.json -Raw | ConvertFrom-Json).snapshot_id
$Receiver = "E:\HometownXR-store\tools\$Snapshot\scripts\transfer.ps1"
& $Receiver -Action status -Store E:\HometownXR-store
& $Receiver -Action import -Store E:\HometownXR-store -Destination C:\Projects\hometown-resume
& $Receiver -Action import -Store E:\HometownXR-store -Destination C:\Projects\hometown-resume -Apply
cd C:\Projects\hometown-resume
.\scripts\setup.ps1 -Profile 4090 -Dev
.\scripts\workstation.ps1 -Action preflight -Profile 4090
```

Substitute the real profile (3080, 4090, or 5090) and this PC's store drive
letter. The destination parent must exist, but the destination itself must not.
An existing checkout is NEVER overwritten, even if it looks clean. Restore is
staged and validated before the completed directory is renamed into place.
Do not adopt a snapshot from an untrusted source: hashes detect corruption,
not a malicious person who can replace both the files and their manifest.

Setup recognizes folder mode and skips Git/Git LFS network operations. It
still uses the selected dependency lock and does not bypass security or GPU
validation. Environments and model caches are not transferred. Create a fresh
environment per restored directory; model/cache reuse can be configured
separately outside the portable state. Existing environments remain intact.

The snapshot records its required precision, without copying GPU worker/batch
tuning or machine identity. If this PC's precision differs (for example, its
defaults are FP32 but the source used FP16), preflight blocks the run. Set the
matching precision in this PC's local hardware override and validate it with
the existing model-validation workflow. Do not replace a baseline merely to
make a mismatch disappear. Precision migrations remain separately reviewed.

The snapshot contains source files, not `.git` history. Keep the original Git
checkout for version history. A separately reviewed `git bundle` can back up
local commits without GitHub, but is NOT a substitute for these data snapshots:
LFS history contains pointers, and Git history may contain historical secrets.
Do not distribute a raw history bundle without reviewing it separately.

## Continue And Send Back

The usual crawl and story commands automatically select folder ownership from
the local configuration. No GitHub fetch, push, or lease renewal is used.
The same stopped-work procedure applies before the next export:

```powershell
.\scripts\transfer.ps1 -Action status
.\scripts\transfer.ps1 -Action export
.\scripts\transfer.ps1 -Action export -Apply
```

In folder mode `checkpoint.ps1` exports and verifies a local snapshot instead
of committing/pushing. `handoff.ps1 -Direction push -Apply` also exports;
`-Direction pull -Destination C:\Projects\next-resume -Apply` restores into a
new directory. Local handoffs without `-Apply` are previews.
Existing Git-mode commands keep their previous behavior until explicitly
switched. No remote, Git history, LFS files, or GitHub settings are removed.

Ownership is retained while unexported work exists. Other PCs are blocked even
after the old lease timestamp passes. There is deliberately no force-steal in
folder mode: recover and export the owning checkout, or preserve the entire
store and all checkouts before an explicitly reviewed disaster recovery.
If the store goes offline, operations fail closed; they do not silently fall
back to GitHub or independent local work. An interrupted `.transfer-lock`
requires confirming that its host/PID has stopped before manual recovery.

## Included And Excluded

Included: source/configuration, scripts, tests, documentation, database,
accepted output, exact story fragments and packs, story reviews, evaluation
labels/replay/candidate evidence, checkpoints, run history, portable evidence
outbox, audit/recovery reports, and Markdown/structured/full-source exports.
Only supported source/research file formats are selected; inspect the export
plan and manifest for coverage. Limits are 100,000 files and 64 GiB of stored
payload. This is not an arbitrary whole-disk backup.

Excluded: `.git`, virtual environments, credentials and credential-like paths,
machine-local overrides/ownership tokens, models, inference caches, runtime
metrics, Parquet-derived datasets, `data/evidence-local`, logs, temporary files,
staging directories, and SQLite sidecars. Excluded research evidence should be
promoted into portable checkpoint/outbox files before handoff. Keep secrets
outside the project. Scanning is defense in depth, not a guarantee that every
possible secret format can be identified.

The rebuildable Common Crawl index cache at `data/full_sources/_index_cache/`
also stays local. This does not exclude recovered source records, the local
recovery-attempt ledger, or its portable copy under `data/full_exports/`.

Each export creates an immutable version; old snapshots are not auto-deleted.
Retain a second independent backup. Synchronization or an SSD alone is not a
backup against device failure. Existing GitHub LFS history is not purged by
switching modes and can continue consuming storage.

## Local Checks And Less CI

```powershell
.\scripts\transfer.ps1 -Action check
.\scripts\test.ps1
.\scripts\dependency-audit.ps1
.\scripts\model-migration.ps1 -Action validate
```

Local checks do not trigger Actions. Live vulnerability auditing still needs
the advisory service; failure/offline status is not evidence of cleanliness.
Without GitHub, run tests on code changes and repeat dependency auditing
regularly yourself; no automatic Dependabot replacement is scheduled here.
The unchanged human-label and 3080/4090/5090 migration gates remain required.
The full test suite also exercises legacy Git mode and therefore needs Git
installed; the receiver and folder-mode runtime themselves do not.

Optional GitHub CI now runs on pull requests, main pushes, and manual dispatch,
not both a feature-branch push and its PR. Superseded runs are canceled.
Credential scanning, dependency audits, Windows/Linux tests, and required job
names are preserved. No data is removed from Git tracking automatically: first
verify restores and backups on every PC, then review a code-only repository
migration separately. That keeps the existing Git handoff compatible meanwhile.

Optional GitHub publishing is code-only and separate from `checkpoint.ps1` in
folder mode. Follow [Optional Code Sync](README.md#optional-code-sync), stage
only explicit source/test/documentation paths, and inspect the outgoing commit.
The credential-check script can use installed Python 3.10+ in a code-only Git
checkout with no virtual environment; it does not install GPU dependencies,
stage files, export data, or contact GitHub. Git is still required for that scan.
This repository still contains historical tracked data; do not use `git add -A`
or include new database archives, story packs, or USB settings in a code PR.
After changing working code, refresh the local snapshot before resuming.

## Validation

Checked locally on Windows with Python 3.10, plus the transfer/ownership tests
on Python 3.11. Tests cover corrupt archives, traversal and link entries,
case-colliding paths, embedded credentials, SQLite WAL changes, stale snapshots,
exclusive ownership, precision mismatches, and unchanged existing destinations.
The receiver is also exercised with Python's site packages disabled.

A copied-data roundtrip used the complete 340,510-row progress checkpoint,
accepted output, and story packs: 6,710 portable files, a 40,467,034-byte archive,
successful restore and ownership claim/release, and unchanged source inventory.
That initial test left production state unchanged. A subsequent on-device USB
activation verified 7,256 files, including 3,273 source-story fragments, 422
recovered full-source records, and full-source exports. The source PC retained
seven workers and FP16; the bundle excluded the rebuildable index cache without
weakening credential checks. Activation settings and paths remain local.
These are transfer checks, not new 4090/5090 GPU evidence. Existing extracted
text, environments, review labels, and pending security branches were preserved.
The existing dependency advisories and migration blockers remain separate work.
