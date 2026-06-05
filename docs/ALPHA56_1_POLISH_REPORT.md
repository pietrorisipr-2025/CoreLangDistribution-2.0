# CLD2 alpha56.1 — publication polish report

## Scope

Alpha56.1 is a final public-candidate polish pass after alpha56. It does not change the core CLD2 transfer model.

## Changes

- README status changed to `alpha / public review candidate 56.1`.
- Core package baseline remains `2.0.0-alpha50.2`.
- Benchmark output metadata in `corelangdistribution2/bench.py` was normalized from legacy `legacy alpha32 marker` to `2.0.0-alpha50.2`.
- Added GitHub publication checklist.
- Added version-field note explaining internal `.cldrepo` schema versions.
- Added alpha56.1 test-results JSON.
- Regenerated manifest.

## Validation

See `TEST_RESULTS_ALPHA56_1_POLISH.json`.

## Claim boundary

No new benchmark claims are introduced. Alpha56.1 preserves the existing boundary: CLD2 is promising for high-reuse warm-update artifact delivery, and near-parity/bad-fit behavior must be declared for high-entropy or heavily rewritten artifacts.
