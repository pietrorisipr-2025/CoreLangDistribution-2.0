# CLD2 alpha51.2 — metadata/version cleanup

Created: 2026-06-04

## Goal

Alpha51.2 is a lightweight metadata cleanup for the alpha51.1 real-artifact benchmark harness.
It does **not** change CLD2 core logic and does **not** require rerunning the heavy benchmark matrix.

## Problem fixed

Some alpha51.1 JSON/CSV/report outputs still showed the legacy internal string:

```text
legacy alpha32 marker
```

This was confusing because the actual public repo candidate is:

```text
2.0.0-alpha50.2
```

Alpha51.2 separates the two concepts:

```text
CLD2 code baseline: 2.0.0-alpha50.2
Benchmark harness milestone: alpha51.2
```

## What the scripts do

- Patch alpha51.1 scripts/docs in the repo if they contain legacy `legacy alpha32 marker` references.
- Patch existing alpha51.1 result files in a copy/fixed result directory.
- Add `benchmark_harness_milestone = alpha51.2` style metadata where possible.
- Create a lightweight review ZIP, avoiding large `.cldrepo`, cache and synthetic artifact files.

## Claim boundary

Alpha51.2 is a metadata/reporting cleanup only.
It does not improve transfer numbers and must not be described as a performance improvement.
