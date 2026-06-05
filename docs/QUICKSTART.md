# Quickstart

## Requirements

- Python 3.10+
- `pip`
- Optional: `zstandard` Python package for zstd codec support

## Install locally

```bash
python -m pip install -e .
cld2 --version
```

On Windows, use PowerShell. If `python` is not on PATH but the Python launcher is installed, replace `python` with `py -3`.

## Verify the checkout

Fast check:

```bash
python scripts/verify_release.py --fast
```

Full check:

```bash
python scripts/verify_release.py
```

The full check runs manifest verification, import checks, `dist-check`, embedded self-tests and the small smoke demo.

## Run the small demo

```bash
python scripts/smoke_test.py
```

This creates two tiny demo releases, packs them, diffs them, fetches the second release, audits the install, and cleans generated demo artifacts.

To keep the generated demo directories for inspection:

```bash
python scripts/smoke_test.py --keep-demo
```

## Use without installing

```bash
python cld2.py selftest
python cld2.py dist-check . --run-selftest
python scripts/smoke_test.py
```

Do not paste raw Windows paths as commands. Use `Set-Location` first when changing directories.
