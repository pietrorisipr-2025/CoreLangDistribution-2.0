# CLD2 alpha54 — Object-store provider request spec

Alpha54 request JSON is intentionally small and provider-oriented.

## Schema sketch

```json
{
  "schema": "CLD2/alpha54_object_store_provider_request",
  "code_baseline": "2.0.0-alpha50.2",
  "provider_mode": "local-object-store-like",
  "artifact_id": "demo/release_v2",
  "repo_path": "object_store/repositories/demo/release_v2.cldrepo",
  "install_path": "install/demo_release_v2",
  "cache_path": "cache/provider_cache",
  "expected_sha256": "optional, learned from first pass if absent",
  "require_audit_install": true,
  "require_digest_verification": true
}
```

## Fields

| Field | Required | Meaning |
|---|---:|---|
| `schema` | yes | Request type marker. |
| `code_baseline` | yes | CLD2 source baseline expected by this harness. |
| `provider_mode` | yes | `local-object-store-like` in alpha54. |
| `artifact_id` | yes | Human-readable artifact identity. |
| `repo_path` | yes | Path to the CLD2 repo artifact. |
| `install_path` | yes | Install destination. |
| `cache_path` | yes | Provider/cache destination. |
| `expected_sha256` | no | Expected installed tree digest; can be learned in first pass. |
| `require_audit_install` | yes | Result must pass `audit-install`. |
| `require_digest_verification` | yes | Result must pass digest verification. |

## Security model

The object-store location is not trusted. The consumer trusts:

1. an authenticated/pinned expected digest;
2. CLD2 audit-install result;
3. independent digest/tree verification;
4. optional higher-level policy validation from alpha53.
