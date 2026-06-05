# When to use CLD2 as an artifact provider

## Good fit

| Case | Why |
|---|---|
| Large domain/model/data pack with small localized updates | CLD2 can reuse chunks and reduce warm-update transfer. |
| Many versioned release artifacts with stable portions | Cache/object reuse can matter. |
| Game/CI/CD/model delivery with previous cache present | Warm update is CLD2's main use case. |
| Artifact must be auditable and rollback-aware | CLD2 has selftest-covered cache/audit/trust/rollback mechanisms. |

## Weak fit

| Case | Why |
|---|---|
| Tiny static artifact, e.g. 15 KB dictionary | Overhead and integration complexity are not worth it. |
| High-entropy rewrite | Little or no reuse, near full transfer. |
| Cold start only | CLD2's strongest benefit requires previous cache/release. |
| Existing compressed changed-file tar is already optimal | CLD2 may be near-parity, not a major win. |

## Decision rule

Before integrating CLD2 into another system, run a v1 → v2 artifact benchmark:

```text
if CLD2 warm bytes << full download and << best conventional baseline:
  integration may be justified
else:
  keep CLD2 optional or defer integration
```
