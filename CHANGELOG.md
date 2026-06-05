# Changelog

## 2.0.0a56.post2 - alpha56.2.1 packaging hotfix

- Fixes `pyproject.toml` package version so `python -m pip install -e .` uses a valid PEP 440 version.
- Sets runtime `__version__` to `2.0.0a56.post2`.
- Adds `__core_baseline__ = "2.0.0-alpha50.2"` to preserve the implementation-baseline distinction.
- Updates `dist-check` to validate the PEP 440 package version while still checking that README documents the core baseline.
- Does not change the core transfer model or add benchmark claims.

## 2.0.0-alpha56.2 - public-readability polish

- Adds a local console entry point: `cld2 = "cld2:main"`.
- Adds `scripts/verify_release.py` for one-command release-candidate checks.
- Rewrites README and quickstart around a five-minute external-review path.
- Updates CLI docs and public help strings to avoid confusing historical alpha labels.
- Adds `docs/REPRODUCIBILITY.md`.
- Does not change the core transfer model or add benchmark claims.

## alpha50.2 — MIT license finalization

- Selected MIT License.
- Replaced license placeholder.
- Updated repository metadata and docs.
- Regenerated manifest and release packages.


## 2.0.0-alpha50.2

GitHub repository candidate:

- extracted source tree to repository root;
- added public README, quickstart, claim boundary and architecture docs;
- added small local demo;
- added smoke-test scripts and GitHub Actions self-test;
- moved benchmark artifacts to release-asset guidance.

## Historical baseline

Implementation derived from alpha45.8 MinIO retry attach branch, after alpha45.9 consolidation.


## alpha56 — Integrated public candidate

- Integrated validated post-alpha50.2 docs/scripts/review assets through alpha55.
- Fixed README quickstart audit-install invocation to use `--install`.
- Added alpha56 status, claim boundary, updated benchmark summary and next-step docs.
- Included clean review artifacts for alpha46-alpha55 under `validated_reviews/`.
- Core implementation baseline remains `2.0.0-alpha50.2`.

## 2.0.0-alpha56.1 — GitHub publication polish

- Keeps the core implementation baseline at `2.0.0-alpha50.2`.
- Integrates alpha51–55 validated docs/assets from alpha56.
- Cleans legacy benchmark metadata in `corelangdistribution2/bench.py` from `legacy alpha32 marker` to `2.0.0-alpha50.2`.
- Adds a GitHub publication checklist and version-field note.
- Revalidates selftest, dist-check with selftest, and smoke test.
- Does not add new benchmark claims or production/cloud claims.
