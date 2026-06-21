# AGENTS.md

## Before Every Code Change

* Inspect relevant files before editing.
* Check `git status --short` and preserve user changes.
* Follow existing project patterns, dependencies, and helper APIs.
* Explain the intended change before editing.
* For larger changes, provide a brief implementation plan.
* Keep edits scoped to the requested behavior.
* Avoid unrelated refactors, formatting churn, or metadata changes.

## Documentation

* Identify affected documentation before editing code.
* Update relevant docs (`README.md`, architecture docs, API docs, runbooks) when behavior, setup, architecture, APIs, commands, or workflows change.
* Update `CHANGELOG.md` for user-visible features, fixes, behavioral changes, breaking changes, and version bumps.
* Do not modify unrelated documentation solely to satisfy documentation requirements.

## CI/CD

* Treat passing CI as a release-blocking requirement.
* Before modifying workflows, build scripts, dependency files, Docker files, or release configuration, inspect existing GitHub Actions workflows.
* Validate that changes remain compatible with existing CI pipelines.
* Run the same checks locally that GitHub Actions is expected to run whenever possible.
* Review `.github/workflows/*` when changes may affect builds, tests, packaging, releases, containers, or deployments.
* If a change could affect CI, explicitly describe the expected impact on GitHub Actions.
* Do not modify GitHub Actions workflows unless required by the task or necessary to fix CI.
* When CI-related files are modified, explain why the change is required and what workflows are affected.
* After changes, verify that all required tests, linting, type checks, builds, and packaging steps still succeed.
* If CI cannot be validated locally, identify which GitHub Actions jobs are most likely to fail and explain the risk.
* Prefer fixing the root cause of CI failures rather than disabling checks, skipping tests, or weakening validation.
* Never remove or bypass CI checks solely to make a build pass.

## Verification

* Run the narrowest relevant tests, type checks, linters, and build commands after making changes.
* For dependency changes, verify installation and build steps.
* For API changes, verify affected tests and integration points.
* For frontend changes, verify production builds when applicable.
* For backend changes, verify startup and test execution when applicable.
* Report exactly which verification commands were executed and their results.
* If verification cannot be performed, explain why and describe the remaining risk.

## Safety

* Preserve user work and local modifications.
* Use `apply_patch` for manual edits when appropriate.
* If requirements are ambiguous, ask for clarification instead of guessing.
* Document required migration, deployment, configuration, or operational changes when applicable.
* Never overwrite user changes without explicit justification.

## Versioning

* Follow Semantic Versioning when the project has a public version.
* Decide whether a version bump is required based on the change type.
* Bump `MAJOR` for breaking API, config, CLI, database, behavior, or compatibility changes.
* Bump `MINOR` for backward-compatible features or meaningful enhancements.
* Bump `PATCH` for backward-compatible bug fixes, security fixes, or small user-visible corrections.
* Do not bump versions for docs-only, tests-only, comments, formatting, internal refactors, or CI-only changes unless released artifacts are affected.
* Update all canonical version locations together.
* Mention version bumps in `CHANGELOG.md`.
* If unsure whether a version bump is appropriate, explain the reasoning and ask before modifying version files.
