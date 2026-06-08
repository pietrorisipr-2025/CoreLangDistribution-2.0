# CLD2 alpha56 claim boundary

## Safe claim

CLD2 explores cost-aware warm-update distribution using chunks, object reuse, manifests, audit install checks and optional provider workflows. It is strongest when there is substantial reuse between versions.

Alpha56.3 real benchmark boundary:

CLD2 shows real value on structured, updateable payloads with cross-version reuse. The strongest alpha56.2.1 real benchmark was the extracted AMD RDNA driver payload. CLD2 is not a generic compressor and does not automatically help on monolithic installers, single executables, or datasets without version locality.

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

The alpha56.3 real benchmark docs include positive, moderate and negative cases. They support a targeted structured-payload warm-update claim, not a universal updater/compressor claim.

## Provider mode framing

Artifact provider mode should be described as:

```text
fetch -> audit-install -> independent digest verification -> machine-readable JSON result
```

The consumer should still verify expected digests/hashes through an authenticated or pinned source.
