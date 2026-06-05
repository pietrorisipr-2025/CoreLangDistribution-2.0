# CLD2 alpha51.2.1 — version string polish

Created: 2026-06-04

## Purpose

This micro-fix does **not** rerun benchmarks and does **not** change CLD2 core logic.

It only fixes a cosmetic metadata/version artifact left by alpha51.2:

```text
malformed legacy alpha50.2-prefixed version string
```

should become:

```text
2.0.0-alpha50.2
```

## Scope

The patch targets:

- alpha51.1/alpha51.2 result files under the benchmark result directory;
- alpha51 scripts/docs copied into the repository;
- generated review ZIP metadata.

## Expected final state

- No references to `malformed legacy alpha50.2-prefixed version string`.
- No references to `legacy alpha32 marker` or `legacy alpha32 dotted marker` in alpha51 result/report files.
- Code baseline remains `2.0.0-alpha50.2`.
- Benchmark milestone becomes `alpha51.2.1`.
- Review ZIP regenerated as `alpha51_2_1_synthetic_matrix_REVIEW_UPLOAD.zip`.

