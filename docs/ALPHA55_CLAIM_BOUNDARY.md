# CLD2 alpha55 — Claim boundary

Alpha55 is a public showcase / examples pack.

## Safe claims

It is reasonable to say:

- CLD2 can be demonstrated on artifact-shaped workloads such as game assets, model/profile packs and CI artifacts.
- The demo checks `pack -> fetch -> audit-install -> payload digest` on local generated examples.
- CLD2 is most relevant when the receiver already has prior chunks/cache and the update reuses much of the previous artifact.
- Alpha55 helps users test whether their own artifacts are good-fit or bad-fit.

## Claims to avoid

Do not claim:

- CLD2 is a universal compressor.
- CLD2 always beats rsync/zsync/casync/OSTree/DVC/Docker/OCI.
- Alpha55 proves cloud, CDN, S3 or MinIO performance.
- Alpha55 is an external security audit.
- The synthetic examples prove a market-ready enterprise product.

## Recommended public wording

> CLD2 is an experimental cost-aware warm-update distribution planner. It is useful to test artifact delivery cases where a receiver already has prior cache/chunks and the next version reuses a meaningful portion of previous content. Alpha55 provides local public examples for game-like assets, model/profile packs and CI artifacts.
