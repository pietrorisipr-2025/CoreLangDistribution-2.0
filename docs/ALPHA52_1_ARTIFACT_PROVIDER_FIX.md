# CLD2 alpha52.1 — Artifact Provider Demo Fix

Alpha52.1 fixes a bug in the alpha52 wrapper/demo.

## Bug fixed

The alpha52 wrapper called:

```text
cld2.py audit-install <repo> <install>
```

but the public CLI expects:

```text
cld2.py audit-install <repo> --install <install>
```

Because of that, `fetch` succeeded but `audit-install` failed, so the verified result JSON was not created.

## Expected result after fix

The demo should produce:

```text
alpha52_1_provider_result_learn_digest.json
alpha52_1_provider_result_verified.json
```

with:

```json
"ok": true,
"digest_ok": true
```

## Claim boundary

This is still a thin wrapper/provider demo, not a new CLD2 core algorithm.
