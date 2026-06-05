# CLD2 alpha53 — Provider trust model / policy validation plan

Alpha53 is a lightweight continuation after alpha52.1.

It does **not** change the CLD2 core engine.

## Purpose

Alpha52.1 validated the local provider flow:

```text
fetch → audit-install → digest verification → JSON result
```

Alpha53 adds a small trust/policy layer around that provider result:

```text
provider result JSON
+ expected digest / pinned digest
+ audit-install requirements
+ negative controls
= explicit trust decision
```

## Why this matters

CLD2 should be usable as an artifact delivery provider for other systems without asking those systems to blindly trust storage, transport or even the provider wrapper.

The safe integration rule is:

```text
Trust the expected hash / authenticated policy.
Do not trust the transport backend.
Do not install or consume an artifact until digest and audit checks pass.
```

## Alpha53 scope

Included:

- `scripts/alpha53_validate_provider_result.py`
- `scripts/alpha53_run_provider_trust_demo.ps1`
- docs explaining the trust model and claim boundary
- example provider policy JSON
- lightweight review ZIP command

Excluded:

- no new benchmark
- no cloud/CDN test
- no external security audit
- no production PKI/KMS
- no source logic change to `cld2.py`

## Expected outcome

Given the valid alpha52.1 provider result, alpha53 should produce:

```text
positive validation: PASS
tampered digest negative control: REJECTED
audit failure negative control: REJECTED
overall: PASS
```
