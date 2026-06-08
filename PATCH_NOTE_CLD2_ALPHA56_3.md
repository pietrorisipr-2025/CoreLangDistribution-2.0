# CLD2 alpha56.3 - benchmark tooling, real benchmark docs, and JSON user profiles

alpha56.3 replaces alpha56.2.1 for public review.

This release fixes benchmark tooling/reporting, adds JSON user-defined profiles, and documents real benchmark results.

The core implementation baseline remains `2.0.0-alpha50.2`.

No new security audit.
No enterprise-ready claim.
No universal performance claim.

## Version

Python package/runtime version:

```text
2.0.0a56.post3
```

Core implementation baseline:

```text
2.0.0-alpha50.2
```

## What changed

- Fixed `make-review-zip` so `bench-real` report outputs are included.
- Fixed best-transfer-method selection/reporting.
- Added JSON user-defined profiles with `--profile-file`.
- Added `cld2 profile-validate` and `cld2 profile-show`.
- Added built-in example profiles in `docs/profiles/`.
- Added real benchmark documentation and benchmark claim boundary.
- Updated README, CLI docs, QUICKSTART, REPRODUCIBILITY, CHANGELOG and INDEX.

## Verification performed

Passed:

```bash
python -m pip install -e .
cld2 --version
python scripts/verify_release.py --fast
python scripts/verify_release.py
python scripts/smoke_test.py
python cld2.py selftest
python cld2.py dist-check . --run-selftest
python cld2.py profile-validate docs/profiles/amd-rdna-extracted-fixed-balanced.json
python cld2.py profile-validate docs/profiles/libreoffice-extracted-fixed-balanced.json
python cld2.py profile-validate docs/profiles/single-exe-small-chunks-cdc.json
python cld2.py pack examples/small_demo/release_v1 --out tmp_profile_test.cldrepo --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json --force
python cld2.py verify tmp_profile_test.cldrepo --deep
python cld2.py bench-real --old-dir tmp_old --new-dir tmp_new --out-dir tmp_profile_bench --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json --scenario-name profile_smoke
python cld2.py make-review-zip --src-dir tmp_results --zip-out tmp_review.zip --max-mb 10
```

Note: the first `pip install -e .` attempt inside the restricted sandbox could not reach PyPI for build dependencies. The same command was rerun with approved outside-sandbox access and passed.

## Artifact

```text
CLD2_ALPHA56_3_PUBLIC_REVIEW_CANDIDATE.zip
```

SHA-256:

```text
8f736d58043869a2dcc99b24b2482c33670c48b67580805979fc0ca9a103d75c
```

ZIP path verification:

```text
backslash entries: 0
slash entries: 142
```
