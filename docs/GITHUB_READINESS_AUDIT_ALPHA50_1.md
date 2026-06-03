# GitHub readiness audit — alpha50.2

## Applied fixes

- Extracted source from nested ZIP to repository root.
- Replaced outdated source README with public alpha50.2 README.
- Added quickstart, architecture, claim boundary and license options.
- Added `.gitignore`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`.
- Added small reproducible demo and smoke test scripts.
- Added GitHub Actions self-test workflow.
- Kept benchmark matrix in `data/`.
- Added required legacy docs (`SPEC_CLD2.md`, `FORMAT_CLD2.md`, `CLI.md`, `THREAT_MODEL.md`, `BENCHMARK_PLAN.md`) so `dist-check` passes.
- Fixed smoke test audit-install invocation.

## Validation

Overall OK: `True`

| Check | OK | Return code |
|---|---:|---:|
| selftest | True | 0 |
| dist-check | True | 0 |
| smoke-test | True | 0 |

## Remaining blocker before public release

A final license must be selected. This repo candidate currently uses a conservative MIT License `LICENSE` stating MIT-licensed MIT-licensed release.
