# Codex changelog - CLD2 alpha56.3

## Summary

Created one public alpha56.3 candidate from alpha56.2.1, following `CODEX_COMPLETE_PATCH_BRIEF_CLD2_ALPHA56_3.zip`.

## Code changes

- Updated package/runtime version to `2.0.0a56.post3`.
- Kept core implementation baseline at `2.0.0-alpha50.2`.
- Added `corelangdistribution2/profiles.py` for JSON profile validation and normalization.
- Added CLI commands `profile-validate` and `profile-show`.
- Added `--profile-file` support for `pack` and `bench-real`.
- Added profile metadata recording in `.cldrepo` release manifests.
- Fixed real benchmark best-method selection with byte-first tie-breaking.
- Fixed `make-review-zip` to include `bench-real` reports and fail on empty review ZIPs.

## Docs and tests

- Added `docs/profiles/` examples.
- Added real benchmark docs and CSV summary.
- Updated README, CLI docs, quickstart, reproducibility docs, claim boundary, changelog and index.
- Added `scripts/alpha56_3_acceptance_smoke.py`.
- Extended release verification imports/checks for the profiles module.

## Final artifact

```text
CLD2_ALPHA56_3_PUBLIC_REVIEW_CANDIDATE.zip
SHA-256: 8f736d58043869a2dcc99b24b2482c33670c48b67580805979fc0ca9a103d75c
```
