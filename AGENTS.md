# AGENTS.md

## Before Every Code Change

- Inspect relevant files before editing.
- Check `git status --short` and preserve user changes.
- Follow existing project patterns, dependencies, and helper APIs.
- Explain the intended change before editing.
- For larger changes, provide a brief implementation plan.
- Keep edits scoped to the requested behavior.
- Avoid unrelated refactors, formatting churn, or metadata changes.

## Documentation

- Identify affected documentation before editing code.
- Update relevant docs (`README.md`, architecture docs, API docs, runbooks) when behavior, setup, architecture, APIs, commands, or workflows change.
- Update `CHANGELOG.md` for user-visible features, fixes, behavioral changes, and breaking changes.
- Do not modify unrelated documentation solely to satisfy documentation requirements.

## Verification

- Run the narrowest relevant tests, type checks, or linters after making changes.
- If verification cannot be performed, explain why and describe the remaining risk.

## Safety

- Preserve user work and local modifications.
- Use `apply_patch` for manual edits when appropriate.
- If requirements are ambiguous, ask for clarification instead of guessing.
- Document required migration, deployment, or configuration changes when applicable.

## Versioning

- Follow Semantic Versioning when the project has a public version.
- Decide whether a version bump is required based on the change type.
- Bump `MAJOR` for breaking API, config, CLI, database, behavior, or compatibility changes.
- Bump `MINOR` for backward-compatible features or meaningful enhancements.
- Bump `PATCH` for backward-compatible bug fixes, security fixes, or small user-visible corrections.
- Do not bump version for docs-only, tests-only, comments, formatting, internal refactors, or CI-only changes unless they affect released artifacts.
- Update all canonical version locations together, such as `package.json`, lock files, `pyproject.toml`, `Cargo.toml`, app metadata, or release files.
- Mention the version bump in `CHANGELOG.md`.
- If unsure whether to bump, explain the reasoning and ask before changing version files.