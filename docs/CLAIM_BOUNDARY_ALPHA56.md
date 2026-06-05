# CLD2 alpha56 claim boundary

## Safe claim

CLD2 explores cost-aware warm-update distribution using chunks, object reuse, manifests, audit install checks and optional provider workflows. It is strongest when there is substantial reuse between versions.

## Unsafe claims

Do not claim that CLD2:

- is a new compression algorithm;
- always beats rsync/zsync/casync/OSTree/DVC/Docker/OCI;
- has proven cloud/CDN/S3 performance from local demos;
- is enterprise-ready;
- has undergone external security audit;
- is a drop-in replacement for mature distribution systems.

## Showcase framing

The alpha55 examples are intentionally favorable public demos. They should be described as showcase examples rather than universal benchmarks.

## Provider mode framing

Artifact provider mode should be described as:

```text
fetch -> audit-install -> independent digest verification -> machine-readable JSON result
```

The consumer should still verify expected digests/hashes through an authenticated or pinned source.
