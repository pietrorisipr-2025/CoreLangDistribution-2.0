# CLD2 alpha24 CLI notes

## Important commands

```bash
python3 cld2.py selftest
python3 cld2.py dist-check . --run-selftest
python3 cld2.py bench-real --help
python3 cld2.py bench-fastcdc-tune --help
python3 cld2.py bench-largefile-variants --help
```

## Large-file tuning

```bash
python3 cld2.py bench-fastcdc-tune \
  --old-dir OLD \
  --new-dir NEW \
  --out-dir REPORT \
  --profiles large-file-small,large-file-balanced,large-file-large \
  --codec raw \
  --file-level-tar-zstd-bytes 12748252690 \
  --full-tar-zstd-v2-bytes 12748230610
```

The external baseline options are for reusing already-measured heavy tar.zst baselines instead of regenerating them.

## Large-file default

`--profile large-file` now uses the alpha24 recommended balanced preset:

```text
128 KiB / 512 KiB / 2 MiB, stride 8
```

Use explicit profiles to explore the trade-off:

```text
large-file-small     64 KiB / 256 KiB / 1 MiB, stride 4
large-file-balanced  128 KiB / 512 KiB / 2 MiB, stride 8
large-file-large     256 KiB / 1 MiB / 4 MiB, stride 16
```

## alpha29 cost-aware scenarios

`bench-cost-aware-scenarios` runs the hybrid planner once and rescales the measured candidates for internal/public/massive download counts. It also reports break-even download thresholds and can create a report-only light ZIP.

`make-light-zip` creates a report-only ZIP from an existing benchmark directory while preserving relative paths.

`cleanup-heavy-artifacts` lists generated heavy artifacts in dry-run mode by default; pass `--apply` to delete them.


## alpha30 review ZIP

```powershell
python .\cld2.py make-review-zip `
  --src-dir REPORT_DIR `
  --zip-out REPORT_REVIEW.zip
```

For scenario planner runs, add:

```powershell
--make-review-zip
```

Use review ZIPs for AI/human handoff. They exclude generated repo internals, pack files and low-value metadata.
