# CLD2 alpha5 threat model

## Covered in alpha5

- accidental pack corruption;
- chunk hash mismatch;
- unsafe extraction paths such as `../evil`;
- rollback to lower `release_seq` unless `--allow-downgrade` is explicit;
- tampered Ed25519 release signature;
- wrong public trust key;
- patch plan target mismatch;
- patch plan source/installed mismatch;
- patch plan chunk metadata tampering.

## Partially covered

- interrupted downloads: verified raw chunks remain in cache and can be reused;
- atomic install: staging plus backup/restore path exists, but more crash testing is still needed.

## Not production-complete yet

- trusted root / role metadata;
- key rotation;
- timestamp and expiry metadata;
- multi-signer policies;
- mirror compromise model;
- transparent release log;
- hardened installer against power loss at every filesystem boundary.

Alpha5 is a public-key trust slice, not a final software-update security framework.


---

## Alpha8 update — trusted root and expiry

Alpha8 adds a local trusted-root policy file. The root is distributed out-of-band and contains one or more trusted Ed25519 public keys, optional root expiry and a minimal policy with `require_signature`, `min_release_seq` and release-expiry enforcement. Repositories may include `expires_at` and `not_before`; signatures cover these fields.

Relevant commands:

```bash
cld2 pack INPUT --out REPO --expires-at 2035-01-01T00:00:00Z
cld2 keygen --out release_private.json --pub-out release_public.json
cld2 sign REPO --key release_private.json
cld2 root-init --out trusted_root.json --key release_public.json --expires-at 2035-01-01T00:00:00Z --min-release-seq 1
cld2 verify REPO --deep --trusted-root trusted_root.json
cld2 fetch REPO --install INSTALL --trusted-root trusted_root.json
```
