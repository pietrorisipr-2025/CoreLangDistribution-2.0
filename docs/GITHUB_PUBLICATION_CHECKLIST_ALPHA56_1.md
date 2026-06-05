# CLD2 alpha56.1 — GitHub publication checklist

## Repository candidate

- [x] MIT license present.
- [x] Package metadata reports `2.0.0-alpha50.2`.
- [x] README status updated to alpha56.1 public-candidate integration/polish.
- [x] Quickstart uses `audit-install <repo> --install <dir>`.
- [x] Claim boundary explicitly avoids “CLD2 always wins” and “new compression algorithm” claims.
- [x] Validated reviews for alpha46–55 included under `validated_reviews/`.
- [x] Alpha51–55 docs/scripts integrated.
- [x] Heavy generated artifacts excluded from repo candidate.

## Validation performed

- [x] `python cld2.py selftest`
- [x] `python cld2.py dist-check . --run-selftest`
- [x] `python scripts/smoke_test.py`
- [x] Manifest regenerated.

## Before making the GitHub repository public

- [ ] Review README one final time in GitHub markdown preview.
- [ ] Decide whether to publish release assets as a GitHub Release attachment or keep them in a separate archive.
- [ ] Add optional donation/support link only as voluntary support, not a paywall.
- [ ] Avoid uploading old recovery packs or generated benchmark working directories.
- [ ] Use `CLD2_ALPHA56_1_GITHUB_REPO_CANDIDATE.zip` as the repo source.

## Claim boundary to keep

CLD2 is a cost-aware warm-update artifact distribution planner. It is not a generic compressor, not a CDN/cloud product, and not a universal replacement for rsync/zsync/casync/OSTree/DVC/Docker. Its strongest case is large versioned artifacts with real chunk/object reuse.
