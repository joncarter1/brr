---
name: release
description: Automate version bump, changelog update, and GitHub release.
argument-hint: "[patch|minor|major] or [version-number]"
disable-model-invocation: true
---

# Release

Automate the full release workflow for brr.

## Arguments

`$ARGUMENTS` is one of:
- `patch`, `minor`, `major` — auto-increment from current version
- An explicit version like `0.5.0`

If no argument provided, ask the user what kind of release this is.

## Steps

Follow these steps in order. Stop and report if any step fails.

### 1. Pre-flight checks

- Run `git status` — abort if there are uncommitted changes.
- Run `git diff --cached` — abort if there are staged changes.

### 2. Run linters and tests

- If a test runner is configured (e.g. pytest, ruff), run it and abort on failure.
- Currently no linters or tests are configured — skip this step but log that it was skipped.

### 3. Determine new version

- Read current version from `pyproject.toml` (line 3: `version = "X.Y.Z"`).
- Parse `$ARGUMENTS`:
  - `patch`: increment Z
  - `minor`: increment Y, reset Z to 0
  - `major`: increment X, reset Y and Z to 0
  - Explicit version (e.g. `0.5.0`): use as-is

### 4. Summarize changes

- Run `git log $(git describe --tags --abbrev=0)..HEAD --oneline` to get commits since last tag.
- Use these to draft the changelog entry.

### 5. Update pyproject.toml

- Change `version = "OLD"` to `version = "NEW"` in `pyproject.toml`.

### 6. Update CHANGELOG.md

- Add a new section below the `# Changelog` header and above the previous version.
- Follow the existing format exactly:

```markdown
## X.Y.Z

### Fixed

- Description of bug fixes

### Added

- Description of new features

### Changed

- Description of breaking or notable changes
```

- Only include subsection headers that have entries.
- Summarize changes from the git log. Be concise — one line per change.

### 7. Review changelog with user

- Show the drafted changelog entry to the user using `AskUserQuestion`.
- If the user requests edits, apply them to `CHANGELOG.md` before proceeding.

### 8. Commit

- Stage only `pyproject.toml` and `CHANGELOG.md`.
- Commit with message: `bump version to X.Y.Z and update changelog`
- Include `Co-Authored-By` trailer.

### 9. Tag and push

- Create tag: `git tag vX.Y.Z`
- Push commit and tag: `git push && git push --tags`
- This triggers the `Publish to PyPI` workflow which publishes to PyPI and creates a GitHub Release automatically.

### 10. Verify

- Run `gh run list --workflow=publish.yml --limit 1` to confirm the workflow was triggered.
- Print a summary of what was released.
