# CLD2 alpha54 — Object-store provider guide

## Purpose

Alpha54 connects the alpha52/alpha53 provider model to an object-store-oriented workflow.

It does **not** turn CLD2 into an S3 SDK and it does **not** claim cloud/CDN performance. It defines a practical handoff pattern:

```text
object-store / repo location
  -> provider request JSON
  -> CLD2 fetch/install/audit
  -> independent digest/tree verification
  -> machine-readable provider result
  -> trust policy validation
```

## What alpha54 validates

Alpha54 validates the packaging and workflow shape using a local object-store-like directory. This is deliberately safer and less fragile than requiring a live MinIO/S3 setup for the first pass.

Validated local pattern:

```text
small_demo release_v2.cldrepo
  copied/published into object_store/repositories/demo/release_v2.cldrepo
  referenced by alpha54_object_store_request.json
  fetched/verified through the alpha52 provider wrapper
```

## What alpha54 does not validate

Alpha54 does not validate:

- real S3 latency;
- CDN behavior;
- object-store authentication;
- signed URLs;
- multi-region replication;
- cloud billing;
- production security.

## Recommended integration boundary

CLD2 should remain a provider invoked through a narrow interface. Consumers should still verify expected hashes/digests after CLD2 fetch/audit-install.

Do not merge object-store backend logic into unrelated projects. Keep the boundary as:

```text
ArtifactProvider.resolve(request) -> verified install path + JSON result
```

## Why this matters

This bridges the public positioning from alpha50.2 and alpha51.x with a concrete integration story:

- alpha50.2: publishable MIT candidate;
- alpha51.x: real-artifact benchmark harness and fit/bad-fit classification;
- alpha52.1: provider fetch/verify demo;
- alpha53: provider trust policy validation;
- alpha54: object-store oriented provider handoff.

## Claim boundary

Safe claim:

> CLD2 can be used as a thin, auditable artifact-provider layer for large versioned artifacts, including object-store-style repositories, as long as consumers verify expected digests and treat storage as untrusted.

Claims to avoid:

```text
CLD2 is production-ready S3 delivery.
CLD2 replaces CDN/S3/object storage.
Alpha54 proves cloud performance.
Alpha54 is a security audit.
```
