# Working Preferences

## Automatic Cleanup After Tasks

- Routine cleanup of disposable files created or downloaded for a task is
  authorized after verification, without asking again. This includes one-off
  validation scripts, scratch files, completed installation archives, abandoned
  partial downloads, and redundant task copies that are no longer needed.
- Verify exact targets and dependencies before removal. Limit cleanup to known
  task artifacts; age or absence from the current workflow is not proof of disuse.
- When a workflow is retired, remove models downloaded exclusively for that
  workflow only after checking for shared dependencies. Preserve active/shared
  models, required tools and environments, source files, reference inputs, user
  outputs, backups, and pending validation evidence unless removal is authorized.
- Prefer the Recycle Bin when practical. Never empty it or permanently remove
  unrelated files as routine cleanup. Do not automatically purge shared pip or
  Hugging Face caches or uninstall system Python/CUDA packages.
- Report material cleanup, approximate size, and recoverability. Cleanup is part
  of task completion, not a background schedule.
- `scripts/cleanup.ps1` previews a small allowlist of regenerable project caches.
  Use `-Apply -Verified` only after successful task verification and after project
  writers have stopped. It is not authorization to delete arbitrary paths.
- Keep credentials and research data out of code-only GitHub updates. Preserve
  existing security checks and model-migration gates.
