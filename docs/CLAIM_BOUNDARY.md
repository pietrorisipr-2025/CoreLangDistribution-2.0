# Claim boundary

## Safe claims

CLD2 is an experimental cost-aware update distribution planner. It can reduce warm-update transfer substantially when there is high chunk/object reuse.

The strongest validated local result so far is alpha45.8 fresh: CLD2 is clearly lower than zsync in `normal` and `small-files`, and roughly equal in `high-entropy` and `heavy-change`.

## Claims to avoid

Do **not** claim that:

- CLD2 always beats zsync/rsync/casync/OSTree/DVC/Docker/OCI;
- CLD2 is a new compression algorithm;
- local MinIO/WSL tests prove CDN/cloud production performance;
- CLD2 is enterprise-ready;
- CLD2 is a drop-in replacement for mature update systems.

## Suggested public wording

> CLD2 explores a cost-aware chunk/object distribution strategy for update-heavy workloads. It can significantly reduce warm-update transfer in high-reuse scenarios, while adversarial/high-entropy/heavy-change cases are reported transparently and often reduce the advantage.
