# CLD2 alpha56.3 benchmark tooling notes

Alpha56.3 fixes benchmark tooling found during the alpha56.2.1 real benchmark review.

## make-review-zip

`cld2 make-review-zip` now includes public `bench-real` outputs:

- `bench_real_result.json`
- `bench_real_summary.csv`
- `bench_real_technical_report.md`
- `CLD2_savings_business_report.md`
- `*_metadata.csv`
- `*_input_metadata.csv`
- `REVIEW_FILE_MANIFEST.csv`
- `*.sha256`
- `*.log`
- `*.txt`
- `*.html`

It continues to exclude generated or heavy internals:

- `*.cldrepo/`
- `release_v*.cldrepo/`
- `packs/`
- `cache/`
- `install/`
- `chunks.idx.json`
- `files.idx.json`
- `release.json`
- `signatures.json`
- binary pack/cache/temp artifacts
- Python cache and package metadata directories

If no review files would be included, the command returns `ok: false` and warns that the review ZIP contains no files.

## Best transfer method

Real benchmark reports now select the best CLD2 transfer method by this rule:

```text
minimum download_required_pack_bytes; ties broken by lower total pack time then method name
```

Reports include a `Best transfer method` section and the JSON result includes:

- `best_transfer_method`
- `best_transfer_bytes`
- `fixed_download_bytes`
- `best_saved_vs_fixed_bytes`
- `best_saved_ratio_vs_fixed`
- `selection_rule`

This avoids labeling a non-fixed method as optimized when it downloads more bytes than fixed.

## User-defined profiles

Alpha56.3 adds JSON user-defined profiles for reusable chunker and codec settings. See `docs/profiles/README.md`.
