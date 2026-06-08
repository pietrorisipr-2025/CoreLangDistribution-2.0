# CLD2 JSON profiles

CLD2 alpha56.3 supports JSON profile files for reusable chunker and codec settings.

Validate a profile before using it:

```bash
cld2 profile-validate docs/profiles/amd-rdna-extracted-fixed-balanced.json
```

Use a profile for packing:

```bash
cld2 pack path/to/release --out release.cldrepo --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json --force
```

Use a profile for a real old/new benchmark:

```bash
cld2 bench-real --old-dir old --new-dir new --out-dir results --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json
```

Profile files are JSON only. Unknown top-level keys are rejected in alpha56.3 so repeated runs stay explicit and reproducible.
