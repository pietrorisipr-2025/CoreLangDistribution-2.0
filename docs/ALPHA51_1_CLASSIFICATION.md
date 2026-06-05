# Alpha51.1 result classification

Alpha51.1 classifies each artifact pair using the best CLD2 run and the best conventional baseline available in `bench_real_result.json`.

## Classes

### high-reuse-win

CLD2 is clearly better than both raw transfer and the best compressed conventional baseline.

Typical pattern:

```text
best_cld2_bytes << file_level_raw_download_bytes
best_cld2_bytes << best conventional compressed baseline
```

### moderate-win

CLD2 is meaningfully better than conventional baselines, but the gap is not orders of magnitude.

### compressed-baseline-parity

CLD2 strongly beats raw/file-level transfer but is close to the best compressed changed-file baseline, often `file_level_tar_zstd_bytes`.

This is still useful, but should not be oversold.

### near-parity

CLD2 is roughly equal to full/download or compressed baselines. Usually there is little useful reuse.

### bad-fit

The artifact changed in a way that gives CLD2 no useful advantage, or the artifact is too small/static/high-entropy to justify the machinery.

## Notes

- The classifier is intentionally conservative.
- It is a reporting aid, not a scientific proof.
- For public README claims, prefer human review of the generated report.
