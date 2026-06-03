# Quickstart

## Requirements

- Python 3.10+
- Optional: zstandard Python package for zstd codec support

## Run tests

```bash
python cld2.py selftest
python cld2.py dist-check . --run-selftest
```

## Run small demo

```bash
python scripts/smoke_test.py
```

This creates two tiny demo releases, packs them, diffs them, fetches the second release, and audits the install.

## Windows PowerShell

```powershell
python .\scripts\smoke_test.py
```

Do not paste raw Windows paths as commands. Use `Set-Location` first when changing directories.
