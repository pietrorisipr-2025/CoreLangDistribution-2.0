# CLD2 alpha53 — Provider policy JSON

Alpha53 uses a small JSON policy/result validation layer.

## Minimal policy

```json
{
  "schema": "CLD2/alpha53_provider_policy",
  "code_baseline": "2.0.0-alpha50.2",
  "benchmark_milestone": "alpha53",
  "require_ok": true,
  "require_digest_ok": true,
  "require_audit_install_ok": true,
  "expected_sha256": "<hex sha256 from trusted source>"
}
```

## Provider result fields expected

The validator is intentionally tolerant and accepts either top-level fields or nested evidence.

Preferred fields:

```json
{
  "ok": true,
  "digest_ok": true,
  "expected_sha256": "...",
  "actual_sha256": "...",
  "audit_install": {
    "ok": true
  }
}
```

## Validation decision

A result is accepted only if:

- `ok == true`, when `require_ok` is enabled;
- `digest_ok == true`, when `require_digest_ok` is enabled;
- `audit_install.ok == true`, when `require_audit_install_ok` is enabled;
- `expected_sha256` is present and equals `actual_sha256`;
- optional policy `expected_sha256` matches result `actual_sha256`.

Any failure produces a machine-readable rejection JSON.
