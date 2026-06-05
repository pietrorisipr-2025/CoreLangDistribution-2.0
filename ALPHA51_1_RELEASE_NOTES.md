# CLD2 alpha51.1 release notes

Alpha51.1 is a benchmark harness/reporting correction for alpha51.

## Scope

- No intentional CLD2 core logic changes.
- No version bump of the main CLD2 engine required.
- This package is meant to be copied into the alpha50.2 repository candidate.

## Fixes versus alpha51

1. Lightweight review ZIP by default.
2. No default 1 GB full upload ZIP.
3. Aggregate report at the matrix root.
4. Conservative scenario classifier.
5. Best CLD2 run is selected by actual minimum `download_required_pack_bytes`.
6. Best conventional baseline is reported separately from raw transfer.
7. Synthetic scenarios are less artificially compressible:
   - `domain_pack_mixed_high_reuse`
   - `model_pack_localized_random`
   - `many_small_mixed_update`
   - `badfit_entropy_rewrite`
8. Claim boundary is explicit.

## Expected final upload

```text
alpha51_1_synthetic_matrix_REVIEW_UPLOAD.zip
```

## Public interpretation

Use alpha51.1 to decide whether a real artifact pair is a good fit for CLD2. Do not use it to claim that CLD2 universally beats every baseline.
