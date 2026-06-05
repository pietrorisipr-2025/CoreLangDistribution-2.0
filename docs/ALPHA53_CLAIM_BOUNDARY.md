# CLD2 alpha53 — Claim boundary

Alpha53 is a provider-policy validation layer for local demos.

## Valid claims

- Alpha52.1 provider result can be validated by an external policy wrapper.
- Digest mismatch is rejected.
- Audit-install failure is rejected.
- The policy result is machine-readable JSON.
- This supports the design direction “CLD2 as a thin artifact provider”.

## Invalid claims

- Not production-ready.
- Not a security audit.
- Not cloud/CDN validation.
- Not a replacement for PKI, KMS, provenance, SBOM or supply-chain attestations.
- Not proof that every CLD2 artifact is safe.
