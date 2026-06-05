# CLD2 alpha56.2.1 - GitHub publication checklist

## Repository candidate

- [x] MIT license present.
- [x] Package metadata reports PEP 440 version `2.0.0a56.post2`.
- [x] README keeps the core implementation baseline clear: `2.0.0-alpha50.2`.
- [x] Local install exposes the `cld2` console command.
- [x] README starts with a five-minute external-review path.
- [x] Quickstart uses `audit-install <repo> --install <dir>`.
- [x] Claim boundary explicitly avoids "CLD2 always wins" and "new compression algorithm" claims.
- [x] `scripts/verify_release.py` provides a single release-candidate verification command.
- [x] Heavy generated artifacts excluded from repo candidate.

## Validation to perform before public release

- [ ] `python -m pip install -e .`
- [ ] `cld2 --version`
- [ ] `python scripts/verify_release.py`
- [ ] README rendered in GitHub markdown preview.
- [ ] Release ZIP SHA-256 digest published beside the artifact.

## Claim boundary to keep

CLD2 is a cost-aware warm-update artifact distribution planner. It is not a generic compressor, not a CDN/cloud product, and not a universal replacement for rsync/zsync/casync/OSTree/DVC/Docker. Its strongest case is large versioned artifacts with real chunk/object reuse.
