# Reproducibility

This repository candidate is intended to be checked from a fresh clone in a few commands.

## Recommended path

```bash
python -m pip install -e .
python scripts/verify_release.py
```

The full verification script runs:

- `MANIFEST.sha256` verification;
- import checks for the main package modules;
- `cld2.py dist-check .`;
- alpha56.3 profile and benchmark-tooling acceptance smoke checks;
- `cld2.py dist-check . --run-selftest`;
- `scripts/smoke_test.py`.

## Fast path

```bash
python scripts/verify_release.py --fast
```

Use this when dependencies are present but you only want a quick source-tree sanity check. It skips the embedded self-test and smoke demo.

## Individual checks

```bash
python scripts/verify_manifest.py
cld2 dist-check .
cld2 selftest
cld2 profile-validate docs/profiles/amd-rdna-extracted-fixed-balanced.json
cld2 profile-validate docs/profiles/libreoffice-extracted-fixed-balanced.json
cld2 profile-validate docs/profiles/single-exe-small-chunks-cdc.json
python scripts/smoke_test.py
```

If `cld2` is not installed, replace `cld2` with `python cld2.py`.

## Expected dependency

The package depends on `cryptography>=41` for Ed25519 signing/trust-root checks. If `cld2 selftest` fails with a message about missing `cryptography`, install the project first:

```bash
python -m pip install -e .
```

## Artifact integrity

Release ZIPs should be published with `.sha256` files. Verify a downloaded ZIP by comparing its SHA-256 digest with the matching `.sha256` file before extracting or publishing it.

Final public ZIPs should use POSIX `/` internal paths so normal extraction works on Windows, macOS and Linux.
