# CLD2 alpha52 — Artifact Provider Mode

Created: 2026-06-04

Alpha52 is a **non-core** integration layer for CoreLangDistribution 2.0.
It does not change the validated CLD2 alpha50.2 engine.

Goal: make CLD2 usable as a thin artifact provider for other systems:

```text
expected hash / repo / cache / install path
        ↓
CLD2 fetch + audit-install
        ↓
independent installed-artifact verification
        ↓
JSON result for caller
```

## Why

The alpha51 real-artifact benchmark work showed that CLD2 is most useful when artifacts are:

- large;
- versioned;
- warm-updated from a previous release/cache;
- high-reuse between versions;
- worth auditing and verifying.

Alpha52 turns that positioning into an integration pattern.

## Contents

```text
docs/ALPHA52_ARTIFACT_PROVIDER_SPEC.md
  Interface, threat model, expected hash rule, JSON contract.

docs/ALPHA52_WHEN_TO_USE_AS_PROVIDER.md
  When CLD2 is worth using as provider and when it is not.

scripts/alpha52_artifact_fetch_verify.py
  Thin wrapper around cld2.py fetch + audit-install + independent verification.

scripts/alpha52_run_artifact_provider_demo.ps1
  PowerShell demo runner for Windows.

examples/alpha52_provider_request.example.json
  Example request contract.

tests/README_ALPHA52_TESTING.md
  Manual validation checklist.
```

## Claim boundary

Alpha52 does **not** make CLD2 production-ready.
It provides an experimental provider wrapper for public review.
The caller must still provide or pin an expected digest through a trusted/authenticated channel.
