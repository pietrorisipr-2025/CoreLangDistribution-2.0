# CLD2 alpha53 — Provider trust model

## Core rule

A CLD2 provider should be treated as an **untrusted artifact delivery layer** unless the consumer verifies a pinned or authenticated digest.

Recommended sequence:

```text
1. Consumer obtains expected digest from a trusted source.
2. CLD2/provider fetches and installs artifact.
3. CLD2 audit-install checks installed files against the release metadata.
4. Provider computes actual digest/tree digest.
5. Consumer verifies actual digest == expected digest.
6. Consumer accepts artifact only if all checks pass.
```

## Security boundary

Alpha53 is not a full security proof and is not an external audit.

It documents and tests the intended local policy layer:

- reject digest mismatch;
- reject failed audit-install;
- reject missing expected digest when required;
- preserve machine-readable evidence in JSON.

## What CLD2 should not claim

Do not claim:

- storage backend is trusted;
- object store/CDN cannot be malicious;
- alpha provider mode is production-ready;
- digest verification replaces a full supply-chain security model;
- local demo equals cloud/CDN validation.

## What CLD2 can claim

Conservative claim:

> CLD2 can be used as a thin artifact provider when consumers verify expected hashes and require audit-install success before accepting delivered artifacts.
