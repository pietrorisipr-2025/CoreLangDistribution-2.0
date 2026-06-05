# CLD2 alpha51.1 — Real Artifact Benchmark Fix

Created: 2026-06-04

This package is a **benchmark/reporting harness update** for CoreLangDistribution 2.0.
It does **not** change the CLD2 core engine.

Base repository expected:

```text
CoreLangDistribution 2.0 — 2.0.0-alpha50.2 MIT public review candidate
```

## Why alpha51.1 exists

The first alpha51 harness worked, but it produced an oversized upload ZIP and used synthetic cases that were too compressible in some places. This made the results useful for validating the pipeline, but too easy to misread as realistic public evidence.

Alpha51.1 fixes that by:

- creating a lightweight review ZIP by default;
- excluding generated `.cldrepo`, cache and payload directories from the default upload;
- adding a scenario summarizer and classification layer;
- adding more realistic mixed/random synthetic artifact profiles;
- comparing CLD2 against the best conventional baseline available in the result;
- classifying each scenario as `high-reuse-win`, `moderate-win`, `compressed-baseline-parity`, `near-parity` or `bad-fit`;
- keeping the claim boundary explicit.

## Files

```text
scripts/alpha51_1_make_synthetic_artifacts.py
scripts/alpha51_1_run_real_artifact_benchmark.ps1
scripts/alpha51_1_run_synthetic_matrix.ps1
scripts/alpha51_1_summarize_results.py
scripts/alpha51_1_make_review_zip.ps1
docs/ALPHA51_1_USAGE_WINDOWS.md
docs/ALPHA51_1_CLASSIFICATION.md
docs/ALPHA51_1_CLAIM_BOUNDARY.md
```

## Output expectation

The final upload should usually be:

```text
alpha51_1_synthetic_matrix_REVIEW_UPLOAD.zip
```

not a multi-GB full artifact ZIP.

## Interpretation

Alpha51.1 is designed to answer:

```text
Does CLD2 help on this actual artifact pair v1 → v2?
```

It is not designed to prove that CLD2 always beats existing tools.
